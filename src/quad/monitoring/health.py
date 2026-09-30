"""Health check HTTP server for Quad monitoring.

Simple aiohttp-based server providing health, readiness, liveness, and
metrics endpoints for operational observability.  Also serves as the
TradingView webhook receiver (POST endpoint).
"""

from __future__ import annotations

import os
import time as _time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog
from aiohttp import web

if TYPE_CHECKING:
    from quad.monitoring.metrics import MetricsCollector

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = structlog.get_logger(__name__)

#: HTTP methods for which a catch-all dispatch route is pre-registered on the
#: aiohttp router.  aiohttp freezes the router at application startup, so
#: this set is fixed; anything outside it cannot be mounted dynamically.
_DYNAMIC_METHODS: tuple[str, ...] = ("GET", "POST", "PUT", "PATCH", "DELETE")


# ============================================================================
# HealthServer
# ============================================================================


class HealthServer:
    """Simple health check HTTP server using aiohttp.

    Provides endpoints for liveness, readiness, health summary, and
    Prometheus-style metrics scraping.  Supports custom route registration
    for extensions such as the TradingView webhook receiver.

    Parameters
    ----------
    port:
        HTTP port to listen on (default 8080).
    components:
        Optional dict of named components for readiness checks.  Each
        value is either a bool (static) or a callable returning a bool
        (dynamic).
    metrics_collector:
        Optional ``MetricsCollector`` instance.  If provided, the
        ``/metrics`` endpoint returns collected metrics.
    """

    def __init__(
        self,
        port: int | None = None,
        components: dict[str, Any] | None = None,
        metrics_collector: MetricsCollector | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self._config = config or {}
        self._monitoring_config = self._config.get("monitoring", {})

        self._port = port or self._resolve_port()
        self._components: dict[str, Any] = dict(components or {})
        self._metrics: MetricsCollector | None = metrics_collector

        self._start_time: float = 0.0
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._app: web.Application | None = None
        # (method, path) -> handler for add_route()-registered endpoints.
        # Backing store for the catch-all dispatcher, because aiohttp freezes
        # its router at startup and refuses live registration.
        self._dynamic_routes: dict[tuple[str, str], Callable] = {}
        self._extra_routes: list[tuple[str, str, Callable]] = []
        self._mounted_routes: set[tuple[str, str]] = set()
        api_key = self._resolve_api_key()
        # Default to loopback when no auth key is configured; only expose
        # on all interfaces when the caller explicitly opts in via a key.
        bind_address = (
            self._monitoring_config.get("health_server", {}).get(
                "bind_address", "127.0.0.1"
            )
            if api_key
            else "127.0.0.1"
        )
        self._bind_address = bind_address
        self._log = logger.bind(port=self._port, bind_address=bind_address)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Custom route registration (for extensions like TradingView webhook)
    # ------------------------------------------------------------------

    def add_route(
        self,
        method: str,
        path: str,
        handler: Callable,
    ) -> None:
        """Register an additional HTTP route.

        Works **before and after** ``start()``.

        aiohttp freezes its ``UrlDispatcher`` when the application starts
        (``Application.pre_freeze()`` -> ``router.freeze()``) and exposes no
        way to unfreeze it, so a route added to a live router raises
        ``RuntimeError: Cannot register a resource into frozen router``.
        ``start()`` therefore pre-registers a catch-all dispatcher that
        consults ``_dynamic_routes``; this method only mutates that dict,
        so registration order in the orchestrator no longer matters.

        Re-registering the same ``(method, path)`` replaces the handler.

        Parameters
        ----------
        method:
            HTTP method, e.g. ``"GET"`` or ``"POST"``.
        path:
            URL path, e.g. ``"/webhook/tradingview"``.
        handler:
            Coroutine handler ``(request: web.Request) -> web.Response``.
        """
        key = (method.upper(), path)
        replacing = key in self._dynamic_routes
        self._dynamic_routes[key] = handler
        self._extra_routes = [(m, p, h) for (m, p), h in self._dynamic_routes.items()]
        self._mounted_routes.add(key)
        self._log.info(
            "route_registered" if not replacing else "route_replaced",
            method=key[0],
            path=key[1],
            live=self._runner is not None,
        )

    def has_route(self, method: str, path: str) -> bool:
        """Return ``True`` when *path* is registered for *method*.

        Used by startup self-tests to prove an extension route is actually
        reachable instead of assuming registration succeeded.
        """
        return (method.upper(), path) in self._dynamic_routes

    def set_metrics_collector(self, metrics_collector: MetricsCollector) -> None:
        """Attach a metrics collector after construction.

        The health server starts before the orchestrator builds its
        ``MetricsCollector``; without this hook ``/metrics`` would serve
        the uptime-only fallback forever.
        """
        self._metrics = metrics_collector
        self._log.info("metrics_collector_attached")

    async def _handle_dynamic(self, request: web.Request) -> web.Response:
        """Dispatch a request to a handler registered via :meth:`add_route`.

        Installed as a catch-all before the router is frozen.  The fixed
        health routes are registered first and aiohttp returns the first
        match, so they always win; anything reaching here is either a
        dynamic route or a genuinely unknown path.
        """
        key = (request.method.upper(), request.path)
        handler = self._dynamic_routes.get(key)
        if handler is None:
            return web.json_response(
                {"error": "Not Found", "path": request.path}, status=404
            )
        return await handler(request)

    async def start(self) -> None:
        """Start the aiohttp server."""
        if self._runner is not None:
            self._log.warning("health_server_already_running")
            return

        self._log.info("health_server_starting")
        self._start_time = _time.time()

        app = web.Application()
        # Fixed routes first — aiohttp resolves in registration order, so
        # these take precedence over the catch-all installed below.
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/", self._handle_health)
        app.router.add_get("/readiness", self._handle_readiness)
        app.router.add_get("/liveness", self._handle_liveness)
        app.router.add_get("/metrics", self._handle_metrics)
        # Short aliases for k8s-style probe names (documented in docs/api.md).
        app.router.add_get("/ready", self._handle_readiness)
        app.router.add_get("/live", self._handle_liveness)
        for key in (
            ("GET", "/health"),
            ("GET", "/"),
            ("GET", "/readiness"),
            ("GET", "/liveness"),
            ("GET", "/metrics"),
            ("GET", "/ready"),
            ("GET", "/live"),
        ):
            self._mounted_routes.add(key)

        # Catch-all for add_route()-registered handlers.  Registered last so
        # the fixed routes above always win.
        for method in _DYNAMIC_METHODS:
            app.router.add_route(method, "/{tail:.*}", self._handle_dynamic)

        # Any extra routes queued before start() are already in
        # _dynamic_routes and therefore already live via the catch-all.
        if self._extra_routes:
            self._log.debug(
                "preexisting_dynamic_routes",
                routes=[f"{m} {p}" for m, p, _ in self._extra_routes],
            )

        self._app = app
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(
            self._runner,
            self._bind_address,
            self._port,
        )
        await self._site.start()

        self._log.info(
            "health_server_started",
            port=self._port,
            bind_address=self._bind_address,
            auth_required=bool(self._resolve_api_key()),
            dynamic_routes=[f"{m} {p}" for m, p in self._dynamic_routes],
        )
        if not self._resolve_api_key():
            self._log.warning("health_no_api_key")

    async def stop(self) -> None:
        """Gracefully shut down the server."""
        if self._runner is None:
            self._log.warning("health_server_not_running")
            return

        self._log.info("health_server_stopping")
        if self._site is not None:
            await self._site.stop()
        await self._runner.cleanup()
        self._runner = None
        self._site = None
        self._app = None
        self._log.info("health_server_stopped")

    # ------------------------------------------------------------------
    # Component registration
    # ------------------------------------------------------------------

    def register_component(
        self, name: str, health_check: Callable[[], bool] | bool
    ) -> None:
        """Register a component for readiness checks.

        Parameters
        ----------
        name:
            Component name (e.g. ``"database"``, ``"websocket"``).
        health_check:
            Either a boolean (static) or a zero-arg callable returning bool.
        """
        self._components[name] = health_check
        self._log.debug("component_registered", name=name)

    # ------------------------------------------------------------------
    # Endpoint handlers
    # ------------------------------------------------------------------

    def _resolve_port(self) -> int:
        """Resolve the listen port: ``QUAD_HEALTH_PORT`` env, config, 9090."""
        try:
            env_port = int(os.environ.get("QUAD_HEALTH_PORT", "") or 0)
        except (TypeError, ValueError):
            env_port = 0
        try:
            cfg_port = int(
                self._monitoring_config.get("health_server", {}).get("port") or 0
            )
        except (TypeError, ValueError):
            cfg_port = 0
        return env_port or cfg_port or 9090

    def _resolve_api_key(self) -> str:
        """Return the health-server API key, env var first then config."""
        return os.environ.get("QUAD_HEALTH_API_KEY", "") or str(
            self._monitoring_config.get("health_server", {}).get("api_key", "")
        )

    def _check_api_key(self, request: web.Request) -> bool:
        """Check the ``X-API-Key`` header against the configured key.

        Returns ``True`` if no key is configured *and* the request is a
        direct loopback connection, or if the request includes a matching
        ``X-API-Key`` header.

        Reverse-proxy safety: behind a proxy ``request.remote`` is the
        proxy's own loopback address, so a naive loopback check would let
        *any* forwarded internet request through.  When forwarding headers
        are present the no-key bypass is therefore refused — an operator
        exposing the server through a proxy must configure
        ``QUAD_HEALTH_API_KEY``.
        """
        api_key = self._resolve_api_key()
        if not api_key:
            # Open access only on a direct loopback connection.  An absent
            # remote (unix socket, in-process test) cannot be remote either.
            remote = getattr(request, "remote", None) or ""
            if remote not in ("", "127.0.0.1", "::1"):
                return False
            if self._has_forwarding_headers(request):
                return False
            return True
        return request.headers.get("X-API-Key", "") == api_key

    @staticmethod
    def _has_forwarding_headers(request: web.Request) -> bool:
        """Return True when the request carries reverse-proxy headers.

        Defensive: a non-aiohttp request object (e.g. a test double) is
        treated as carrying no forwarding headers.
        """
        headers = getattr(request, "headers", None)
        if headers is None:
            return False
        get = getattr(headers, "get", None)
        if get is None:
            return False
        return any(
            get(h, "") != ""
            for h in (
                "X-Forwarded-For",
                "X-Real-IP",
                "X-Forwarded-Host",
                "Forwarded",
            )
        )

    async def _require_auth(self, request: web.Request) -> web.Response | None:
        """Return a 403 response if API key check fails, otherwise ``None``."""
        if not self._check_api_key(request):
            return web.json_response(
                {
                    "error": "Forbidden",
                    "message": "Invalid or missing X-API-Key header",
                },
                status=403,
            )
        return None

    async def _handle_health(self, request: web.Request) -> web.Response:
        """GET /health — Return overall bot health."""
        auth = await self._require_auth(request)
        if auth:
            return auth
        uptime = _time.time() - self._start_time

        # Degraded when any registered component (db, supervisor, …) is
        # unhealthy; each check is fail-closed.
        components: dict[str, bool] = {}
        degraded: list[str] = []
        for name, check in self._components.items():
            try:
                ok = check() if callable(check) else bool(check)
            except Exception:
                ok = False
            components[name] = bool(ok)
            if not ok:
                degraded.append(name)

        return web.json_response(
            {
                "status": "degraded" if degraded else "ok",
                "uptime": round(uptime, 2),
                "version": self._monitoring_config.get("health_server", {}).get(
                    "version", "0.1.0"
                ),
                "timestamp": int(_time.time() * 1000),
                "components": components,
                "degraded": degraded,
            }
        )

    async def _handle_readiness(self, request: web.Request) -> web.Response:
        """GET /readiness — Return component readiness status."""
        auth = await self._require_auth(request)
        if auth:
            return auth
        results: dict[str, bool] = {}
        all_ready = True

        for name, check in self._components.items():
            try:
                if callable(check):
                    ready = check()
                else:
                    ready = bool(check)
            except Exception:
                ready = False

            results[name] = ready
            if not ready:
                all_ready = False

        return web.json_response(
            {
                "ready": all_ready,
                "components": results,
            }
        )

    async def _handle_liveness(self, request: web.Request) -> web.Response:
        """GET /liveness — Return simple alive status."""
        auth = await self._require_auth(request)
        if auth:
            return auth
        return web.json_response({"alive": True})

    async def _handle_metrics(self, request: web.Request) -> web.Response:
        """GET /metrics — Return Prometheus-style metrics text."""
        auth = await self._require_auth(request)
        if auth:
            return auth
        if self._metrics is not None:
            text = self._metrics.get_metrics_text()
        else:
            # Default minimal metrics
            uptime = _time.time() - self._start_time
            text = (
                "# HELP quad_uptime_seconds Bot uptime in seconds\n"
                "# TYPE quad_uptime_seconds gauge\n"
                f"quad_uptime_seconds {uptime:.2f}\n"
            )

        return web.Response(text=text, content_type="text/plain; charset=utf-8")
