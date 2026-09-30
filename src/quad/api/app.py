"""FastAPI application factory for quad-api v1."""

from __future__ import annotations

import os
import time

import structlog
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import Response

from quad.persistence import create_database
from quad.persistence.database import DatabaseManager

from . import (
    routes_auth,
    routes_config,
    routes_exchange,
    routes_telegram,
    routes_trading,
    routes_worker,
)
from .deps import ApiState, RateLimiter, rate_limit

log = structlog.get_logger(__name__)


def _error_response(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"ok": False, "error": {"code": code, "message": message}},
    )


def _warn_if_multi_worker() -> None:
    """Warn that the in-memory rate limiter is per-process.

    ``RateLimiter`` keeps its counters in a dict, so running uvicorn with
    more than one worker (or a replicated deployment) silently multiplies the
    effective limit by the worker count instead of enforcing it.  A shared
    store would be needed to fix that properly; for now make the hazard
    visible rather than let it fail silently.
    """
    workers = os.environ.get("WEB_CONCURRENCY", "").strip()
    try:
        count = int(workers) if workers else 1
    except ValueError:
        count = 1
    if count > 1:
        log.warning(
            "rate_limiter_is_per_process",
            workers=count,
            msg=(
                f"Running with WEB_CONCURRENCY={count}: the in-memory rate "
                f"limiter is per-process, so the effective limit is {count}x "
                "the configured value. Run a single worker or move the "
                "limiter to a shared store."
            ),
        )


def create_app(
    db: DatabaseManager | None = None,
    bot_token: str = "",
    server_symbols: list[str] | None = None,
) -> FastAPI:
    db = db or create_database(os.environ.get("DATABASE_URL", "data/quad.db"))
    # In production, require DATABASE_URL to be set
    quad_env = os.environ.get("QUAD_ENV", "development").strip().lower()
    if quad_env == "production" and "DATABASE_URL" not in os.environ:
        raise RuntimeError(
            "DATABASE_URL must be set in production mode (QUAD_ENV=production). "
            "SQLite fallback is only allowed in development."
        )
    bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import asyncio

        _warn_if_multi_worker()
        stop_sweep = asyncio.Event()

        async def _sweep_loop():
            while not stop_sweep.is_set():
                try:
                    if app.state.supervisor is not None:
                        await app.state.supervisor.sweep()
                except Exception:
                    log.exception("supervisor_sweep_failed")
                try:
                    await asyncio.wait_for(stop_sweep.wait(), timeout=60)
                except asyncio.TimeoutError:
                    pass

        task = asyncio.create_task(_sweep_loop())
        async with db:
            yield
        stop_sweep.set()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    app = FastAPI(
        title="quad-api",
        version="1.0.0",
        lifespan=lifespan,
        dependencies=[Depends(rate_limit)],
    )

    # CORS — lock down in production; relaxed for dev
    allowed_origins = os.environ.get("QUAD_CORS_ORIGINS", "").split(",")
    allowed_origins = [o.strip() for o in allowed_origins if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins or ["http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: StarletteRequest, call_next):
            response: Response = await call_next(request)
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
            return response

    app.add_middleware(_SecurityHeadersMiddleware)

    app.state.api = ApiState(db, bot_token)
    # Server-owned trade universe: users never pick symbols. Single source
    # of truth shared by the API (informational GET) and workers (ai pairs).
    app.state.api.server_symbols = list(
        server_symbols or ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]
    )
    app.state.limiter = RateLimiter(
        max_requests=int(os.environ.get("QUAD_API_RATE_LIMIT", "60")),
        window_seconds=60,
    )
    # Worker supervisor (Hetzner): when enabled, exchange-connect starts a
    # per-tenant worker process and disconnect stops it.  Disabled by
    # default so dev/test runs stay single-process.
    app.state.supervisor = None
    if os.environ.get("QUAD_SUPERVISOR_ENABLED", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        from quad.supervisor import Supervisor

        app.state.supervisor = Supervisor(db)
    app.state.started_at = time.time()

    for router in (
        routes_auth.router,
        routes_exchange.router,
        routes_config.router,
        routes_trading.router,
        routes_telegram.router,
        routes_worker.router,
    ):
        app.include_router(router)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        # Never echo raw input/context back: loc/msg/type only.
        details = [
            {
                "loc": list(e.get("loc", [])),
                "msg": str(e.get("msg", "")),
                "type": str(e.get("type", "")),
            }
            for e in exc.errors()[:3]
        ]
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "error": {
                    "code": "invalid_request",
                    "message": "request validation failed",
                    "details": details,
                },
            },
        )

    @app.exception_handler(HTTPException)
    async def http_handler(request: Request, exc: HTTPException):
        # envelope_error() stashes the envelope in detail — render it flat.
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        msg = exc.detail if isinstance(exc.detail, str) else "request failed"
        return _error_response("http_error", str(msg), exc.status_code)

    @app.get("/live")
    async def live():
        """Liveness probe: is the process itself running?

        Deliberately checks *no* dependency.  A liveness probe exists to answer
        "should this process be restarted?", and a transient database blip is
        not a reason to restart the API -- it would amplify a small problem
        into an outage.  Point ``livenessProbe`` here and ``readinessProbe`` at
        ``/health``, which does check the database.
        """
        return {
            "ok": True,
            "data": {
                "status": "ok",
                "uptime": round(time.time() - app.state.started_at, 2),
                "version": "1.0.0",
            },
        }

    @app.get("/health")
    async def health():
        """Readiness/health: is the API able to actually serve requests?

        Verifies the database, because an API that cannot reach its store can
        serve nothing useful -- returning an unconditional ``ok`` meant a
        load balancer or orchestrator kept routing traffic to an API whose
        every real endpoint would fail.  Each check is fail-closed: an
        exception counts as unhealthy, never as healthy.
        """
        components: dict[str, bool] = {}
        try:
            components["database"] = bool(await db.is_healthy())
        except Exception:
            log.exception("health_check_failed")
            components["database"] = False

        degraded = sorted(name for name, ok in components.items() if not ok)
        payload = {
            "status": "degraded" if degraded else "ok",
            "uptime": round(time.time() - app.state.started_at, 2),
            "version": "1.0.0",
            "timestamp": int(time.time() * 1000),
            "components": components,
            "degraded": degraded,
        }
        if degraded:
            return JSONResponse(
                status_code=503,
                content={
                    "ok": False,
                    "error": {
                        "code": "unhealthy",
                        "message": f"unhealthy components: {', '.join(degraded)}",
                    },
                    "data": payload,
                },
            )
        return {"ok": True, "data": payload}

    return app


def get_app() -> FastAPI:
    """Uvicorn entrypoint: ``uvicorn quad.api.app:get_app --factory``."""
    return create_app()
