"""Health check HTTP server for Quad monitoring.

Simple aiohttp-based server providing health, readiness, liveness, and
metrics endpoints for operational observability.  Also serves as the
TradingView webhook receiver (POST endpoint).
"""

from __future__ import annotations

import asyncio
import time as _time
from typing import TYPE_CHECKING, Any, Callable

import structlog
from aiohttp import web

if TYPE_CHECKING:
    from quad.monitoring.metrics import MetricsCollector

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = structlog.get_logger(__name__)


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

        self._port = (
            port
            or self._monitoring_config.get("health_server", {}).get("port")
            or 9090
        )
        self._components: dict[str, Any] = dict(components or {})
        self._metrics: MetricsCollector | None = metrics_collector

        self._start_time: float = 0.0
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._extra_routes: list[tuple[str, str, Callable]] = []
        self._log = logger.bind(port=self._port)

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

        Routes registered before ``start()`` are added to the aiohttp
        application automatically.  Routes registered after ``start()``
        are appended immediately.

        Parameters
        ----------
        method:
            HTTP method, e.g. ``"GET"`` or ``"POST"``.
        path:
            URL path, e.g. ``"/webhook/tradingview"``.
        handler:
            Coroutine handler ``(request: web.Request) -> web.Response``.
        """
        self._extra_routes.append((method, path, handler))
        self._log.debug("route_registered", method=method, path=path)

    async def start(self) -> None:
        """Start the aiohttp server."""
        if self._runner is not None:
            self._log.warning("health_server_already_running")
            return

        self._log.info("health_server_starting")
        self._start_time = _time.time()

        app = web.Application()
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/", self._handle_health)
        app.router.add_get("/readiness", self._handle_readiness)
        app.router.add_get("/liveness", self._handle_liveness)
        app.router.add_get("/metrics", self._handle_metrics)

        # Register any extra routes (e.g. TradingView webhook)
        for method, path, handler in self._extra_routes:
            app.router.add_route(method, path, handler)
            self._log.debug("extra_route_mounted", method=method, path=path)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(
            self._runner,
            self._monitoring_config.get("health_server", {}).get("bind_address", "127.0.0.1"),
            self._port,
        )
        await self._site.start()

        self._log.info("health_server_started", port=self._port)

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
        self._log.info("health_server_stopped")

    # ------------------------------------------------------------------
    # Component registration
    # ------------------------------------------------------------------

    def register_component(self, name: str, health_check: Callable[[], bool] | bool) -> None:
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

    def _check_api_key(self, request: web.Request) -> bool:
        """Check the ``X-API-Key`` header against the configured key.

        Returns ``True`` if no key is configured (no auth required), or
        if the request includes a matching ``X-API-Key`` header.
        """
        api_key = self._monitoring_config.get("health_server", {}).get("api_key", "")
        if not api_key:
            return True  # No auth configured
        return request.headers.get("X-API-Key", "") == api_key

    async def _require_auth(self, request: web.Request) -> web.Response | None:
        """Return a 403 response if API key check fails, otherwise ``None``."""
        if not self._check_api_key(request):
            return web.json_response(
                {"error": "Forbidden", "message": "Invalid or missing X-API-Key header"},
                status=403,
            )
        return None

    async def _handle_health(self, request: web.Request) -> web.Response:
        """GET /health — Return overall bot health."""
        auth = await self._require_auth(request)
        if auth:
            return auth
        uptime = _time.time() - self._start_time

        return web.json_response(
            {
                "status": "ok",
                "uptime": round(uptime, 2),
                "version": self._monitoring_config.get("health_server", {}).get("version", "0.1.0"),
                "timestamp": int(_time.time() * 1000),
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
