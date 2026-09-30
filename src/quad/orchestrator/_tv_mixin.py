"""TradingView webhook wiring for the orchestrator.

Extracted verbatim from ``orchestrator.py`` (the method's AST is unchanged --
only its enclosing class moved).  The mixin exists so ``QuadOrchestrator`` does
not have to carry a 300-line method whose only subject is the webhook
receiver; ``QuadOrchestrator`` keeps the method and its signature, so nothing
outside this package can tell the difference.
"""

from __future__ import annotations

from typing import Any

from quad.config.schema import TradingViewWebhookConfig
from quad.monitoring.correlation import (
    new_correlation_id,
    reset_correlation_id,
    set_correlation_id,
)


class TradingViewWebhookMixin:
    """Webhook-receiver wiring, split out of ``QuadOrchestrator``.

    The attributes below are the state this mixin *consumes*.  They are
    declared, not assigned, so the mixin documents the contract it expects from
    its host class without owning (or being able to clobber) the state --
    ``QuadOrchestrator.__init__`` remains the single place they are created.
    """

    _log: Any
    _config_dict: dict[str, Any]
    _exchange_adapter: Any
    _execution_engine: Any
    _health_server: Any
    _tv_seen: dict[str, float]
    _tv_webhook: Any
    _execute_ai_action: Any

    async def _init_tradingview_webhook(self) -> None:
        """Initialise the TradingView webhook receiver.

        Registers a ``POST /webhook/tradingview`` route on the health
        server.  Requires ``tradingview_webhook.enabled`` in config.
        """
        tv_cfg = TradingViewWebhookConfig.model_validate(
            self._config_dict.get("tradingview_webhook", {})
        )
        if not tv_cfg.enabled:
            self._log.info("tradingview_webhook_disabled")
            self._tv_webhook = None
            return

        if self._health_server is None:
            self._log.warning(
                "tradingview_webhook_no_health_server",
            )
            self._tv_webhook = None
            return

        from quad.tradingview.signals import convert_to_action

        secret = tv_cfg.secret
        port = tv_cfg.port

        if not secret:
            self._log.warning(
                "tradingview_webhook_empty_secret",
                msg=(
                    "TradingView webhook is enabled but no secret is configured. "
                    "Set a non-empty secret via tradingview_webhook.secret in config "
                    "or the QUAD_TRADINGVIEW_WEBHOOK_SECRET env var. "
                    "The webhook will reject all requests until a secret is set."
                ),
            )

        # Defence in depth: the schema already rejects an enabled webhook
        # without a >=16-char secret, so this should be unreachable.  It is
        # kept as a second fail-closed gate — there is deliberately NO
        # opt-in path, because an unauthenticated endpoint on the health
        # port is a remote order-execution hole.
        if not secret:
            self._log.error(
                "tradingview_webhook_disabled_no_secret",
                msg=(
                    "TradingView webhook is enabled but no usable secret is "
                    "configured.  The webhook is disabled (fail-closed).  Set "
                    "tradingview_webhook.secret (>=16 chars) or "
                    "QUAD_TRADINGVIEW_WEBHOOK_SECRET."
                ),
            )
            self._tv_webhook = None
            return

        # Build the aiohttp handler
        async def _tv_webhook_handler(request: Any) -> Any:
            """Handle incoming TradingView webhook POST requests."""

            # Each alert gets its own correlation id so its log lines can be
            # separated from the concurrent trading cycle's.
            tv_cid = new_correlation_id("tv")
            tv_token = set_correlation_id(tv_cid)
            try:
                return await _tv_webhook_handler_inner(request, tv_cid)
            finally:
                reset_correlation_id(tv_token)

        async def _tv_webhook_handler_inner(request: Any, correlation: str) -> Any:
            from aiohttp import web

            from quad.tradingview.parser import parse_alert

            log = self._log.bind(correlation_id=correlation)

            # Validate content type
            content_type = request.content_type or ""

            # Read body
            body = await request.read()
            raw_text = body.decode("utf-8", errors="replace")

            # Secret check — two methods supported:
            # 1. HMAC-SHA256 signature in X-Webhook-Signature header (preferred)
            # 2. Shared secret in JSON payload body (TradingView-compatible)
            if secret:
                import hashlib
                import hmac

                import json as _json

                # Method 1: HMAC-SHA256 header verification
                sig_header = request.headers.get("X-Webhook-Signature", "")
                if sig_header:
                    expected = hmac.new(
                        secret.encode("utf-8"),
                        body,
                        hashlib.sha256,
                    ).hexdigest()
                    if not hmac.compare_digest(sig_header, expected):
                        log.warning("tv_webhook_invalid_hmac_signature")
                        return web.Response(status=403, text="Forbidden")
                else:
                    # Method 2: Shared secret in JSON payload (TradingView fallback)
                    try:
                        payload = (
                            _json.loads(raw_text)
                            if raw_text.strip().startswith("{")
                            else {}
                        )
                        if payload.get("secret") != secret:
                            log.warning("tv_webhook_invalid_secret")
                            return web.Response(status=403, text="Forbidden")
                    except _json.JSONDecodeError:
                        log.warning("tv_webhook_invalid_json")
                        return web.Response(
                            status=400,
                            text="Invalid JSON payload",
                        )

            # Parse the alert
            parsed = parse_alert(body, content_type)
            # Pass the *configured* secret explicitly.  convert_to_action()
            # used to fall back to the QUAD_TV_WEBHOOK_SECRET env var, a
            # second name for the same secret that is never set, so its
            # in-alert credential check silently never ran.
            signal = convert_to_action(parsed, expected_secret=secret or None)

            if signal is None:
                log.warning(
                    "tv_webhook_unparseable",
                    body_preview=raw_text[:200],
                )
                return web.Response(
                    status=400,
                    text="Unparseable alert format",
                )

            log.info(
                "tv_webhook_received",
                symbol=signal.symbol,
                side=signal.side,
                quantity=str(signal.quantity),
                signal_type=signal.signal_type,
            )

            # --- TV dedupe (5-min window) + freshness (>60s reject) -------
            import hashlib as _hashlib
            import time as _time_mod

            now_ts = _time_mod.time()
            # Prune entries outside the window (single-task, atomic).
            _seen = getattr(self, "_tv_seen", None)
            if _seen is None:
                _seen = self._tv_seen = {}
            self._tv_seen = {k: ts for k, ts in _seen.items() if now_ts - ts < 300}
            dedupe_key = _hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:32]
            dedupe_key += f":{signal.symbol}:{signal.side}:{signal.quantity}"
            dedupe_key += f":{signal.signal_type}:{signal.strategy_name}"
            if dedupe_key in self._tv_seen:
                log.warning("tv_webhook_duplicate_rejected", symbol=signal.symbol)
                return web.json_response({"status": "duplicate"})
            self._tv_seen[dedupe_key] = now_ts

            alert_ts = None
            for _src in (parsed, dict(signal.metadata or {})):
                for _k in ("timestamp", "timenow", "time", "ts", "created"):
                    try:
                        _v = _src.get(_k)
                        if _v is not None:
                            alert_ts = float(_v)
                            if alert_ts > 1e12:  # ms -> s
                                alert_ts /= 1000.0
                            break
                    except (TypeError, ValueError):
                        continue
                if alert_ts is not None:
                    break
            if alert_ts is not None and now_ts - alert_ts > 60:
                log.warning(
                    "tv_webhook_stale_rejected",
                    symbol=signal.symbol,
                    age_s=round(now_ts - alert_ts, 1),
                )
                return web.Response(status=400, text="Stale alert")

            # Route through the serial-trade path so TV ENTERs obey the
            # same close-all-first invariant as AI decisions.
            if self._execution_engine is not None:
                try:
                    from quad.types.strategy import StrategyContext as _Sctx

                    live_positions: list = []
                    try:
                        if self._exchange_adapter is not None:
                            live_positions = (
                                await self._exchange_adapter.get_positions()
                            )
                    except Exception:
                        live_positions = []
                    tv_ctx = _Sctx(
                        account=None,
                        positions=live_positions,
                        orders=[],
                        config=self._config_dict,
                    )
                    _direction = (
                        "LONG"
                        if str(signal.side).strip().upper() == "BUY"
                        else "SHORT"
                        if str(signal.side).strip().upper() == "SELL"
                        else "NEUTRAL"
                    )
                    if signal.signal_type == "exit":
                        held = [
                            p
                            for p in live_positions
                            if (
                                getattr(p, "symbol", "")
                                or getattr(p, "contract_symbol", "")
                            )
                            == signal.symbol
                        ]
                        if not held:
                            # Loud: an EXIT for a symbol we do not hold.
                            log.error(
                                "tv_webhook_exit_missing_position",
                                symbol=signal.symbol,
                                strategy=signal.strategy_name,
                            )
                            return web.json_response({"status": "exit_no_position"})
                        # Derive the EXIT direction from the held position so
                        # the execution path can determine the close side
                        # (CLOSE alerts carry no direction of their own).
                        _held_side = (
                            str(
                                getattr(held[0], "side", "")
                                or getattr(held[0], "position_side", "")
                            )
                            .strip()
                            .upper()
                        )
                        if _held_side in ("LONG", "BUY"):
                            _direction = "LONG"
                        elif _held_side in ("SHORT", "SELL"):
                            _direction = "SHORT"
                    tv_decision = {
                        "action": "EXIT" if signal.signal_type == "exit" else "ENTER",
                        "direction": _direction,
                        "side": signal.side,
                        "contract": signal.symbol,
                        "quantity": str(signal.quantity),
                        "strategy": f"tradingview_{signal.strategy_name}",
                        "confidence": 1.0,
                        "reasoning": f"TradingView alert: {signal.strategy_name}",
                    }
                    # Serialized inside _execute_ai_action via _trade_lock.
                    ok = await self._execute_ai_action(tv_decision, tv_ctx)
                    if not ok:
                        log.warning(
                            "tv_webhook_execution_rejected",
                            symbol=signal.symbol,
                            signal_type=signal.signal_type,
                        )
                except Exception as exc:
                    log.exception("tv_webhook_execution_error", error=str(exc))

            return web.json_response({"status": "ok"})

        # Register the route on the health server
        self._health_server.add_route(
            "POST", "/webhook/tradingview", _tv_webhook_handler
        )

        # Self-test: prove the route is actually mounted.  Previously the
        # route was only queued (and never reachable) while still logging
        # "tradingview_webhook_initialized" — automation the operator
        # believed was armed returned 404 for every alert.
        if not self._health_server.has_route("POST", "/webhook/tradingview"):
            self._log.critical(
                "tradingview_webhook_route_missing",
                msg=(
                    "POST /webhook/tradingview is not mounted on the health "
                    "server.  TradingView alerts will 404.  Disabling the "
                    "webhook so status does not report a false 'armed' state."
                ),
            )
            self._tv_webhook = None
            return

        self._tv_webhook = {
            "enabled": True,
            "secret_configured": bool(secret),
            "authenticated": bool(secret),
            "port": port,
            "route": "POST /webhook/tradingview",
        }
        self._log.info(
            "tradingview_webhook_initialized",
            port=port,
            secret_configured=bool(secret),
            route="POST /webhook/tradingview",
        )
