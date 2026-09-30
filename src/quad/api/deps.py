"""Shared FastAPI dependencies: DB handle, authenticated tenant, rate limits."""

from __future__ import annotations

import hashlib
import time
from collections import defaultdict, deque
from typing import Any

import structlog
from fastapi import Depends, Header, HTTPException, Request

from quad.persistence import DatabaseManager
from quad.persistence.repositories import (
    ConfigChangeRepository,
    ExchangeCredentialRepository,
    OrderRepository,
    PairingCodeRepository,
    PositionRepository,
    TelegramBindingRepository,
    TenantConfigRepository,
    TenantRepository,
    TradeRepository,
)

from .security import AuthError, parse_token

logger = structlog.get_logger(__name__)


class ApiState:
    """Process-wide handles attached to ``app.state``."""

    def __init__(self, db: DatabaseManager, bot_token: str = "") -> None:
        self.db = db
        self.bot_token = bot_token
        self.tenants = TenantRepository(db)
        self.credentials = ExchangeCredentialRepository(db)
        self.configs = TenantConfigRepository(db)
        self.bindings = TelegramBindingRepository(db)
        self.pairing = PairingCodeRepository(db)
        self.positions = PositionRepository(db)
        self.orders = OrderRepository(db)
        self.trades = TradeRepository(db)
        self.config_audit = ConfigChangeRepository(db)


def get_state(request: Request) -> ApiState:
    return request.app.state.api


def envelope_error(code: str, message: str, status: int) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"ok": False, "error": {"code": code, "message": message}},
    )


async def log_audit(
    state: "ApiState",
    tenant_uuid: str,
    key: str,
    old_value: str,
    new_value: str,
    source: str,
) -> None:
    """Append an audit row (best-effort; never breaks the calling request)."""
    try:
        import time as _time

        from quad.persistence.models import ConfigChangeModel

        repo = state.config_audit
        await repo.create(
            ConfigChangeModel(
                id=0,
                timestamp=int(_time.time() * 1000),
                key=key,
                old_value=str(old_value)[:500],
                new_value=str(new_value)[:500],
                source=source,
                tenant_id=tenant_uuid,
            )
        )
    except Exception:
        logger.exception("audit_write_failed", tenant=tenant_uuid, key=key)


async def current_tenant(
    authorization: str = Header(default=""),
    state: ApiState = Depends(get_state),
) -> dict[str, Any]:
    """Resolve the JWT bearer token to a tenant row dict."""
    if not authorization.lower().startswith("bearer "):
        raise envelope_error("unauthorized", "missing bearer token", 401)
    try:
        claims = parse_token(authorization[7:].strip())
    except AuthError as exc:
        raise envelope_error("unauthorized", str(exc), 401) from exc
    tenant = await state.tenants.get_by_uuid(str(claims.get("sub", "")))
    if tenant is None or tenant.status not in ("active", "halted"):
        raise envelope_error("unauthorized", "unknown or inactive tenant", 401)
    # Token version check — invalidate tokens after rotation
    token_ver = claims.get("ver", 1)
    tenant_ver = getattr(tenant, "token_version", 1)
    if token_ver < tenant_ver:
        raise envelope_error(
            "unauthorized", "token revoked; please re-authenticate", 401
        )
    # Halted tenants stay authenticated (status/audit/resume must work);
    # trading actions (connect, worker ensure) refuse them explicitly.
    return {"tenant": tenant, "claims": claims}


# ---------------------------------------------------------------------------
# Sliding-window rate limiter (in-memory; Hetzner single-box is one process;
# revisit with Redis when horizontally scaled)
# ---------------------------------------------------------------------------


class RateLimiter:
    def __init__(self, max_requests: int = 60, window_seconds: int = 60) -> None:
        self.max_requests = max_requests
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._check_count = 0

    def check(self, key: str, max_requests: int | None = None) -> None:
        now = time.monotonic()
        limit = max_requests or self.max_requests
        hits = self._hits[key]
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        if len(hits) >= limit:
            raise envelope_error("rate_limited", "too many requests; slow down", 429)
        hits.append(now)
        self._check_count += 1
        if self._check_count % 1000 == 0:
            self._cleanup(now)

    def _cleanup(self, now: float) -> None:
        """Remove entries whose deques are fully expired."""
        empty = [
            k for k, v in self._hits.items() if not v or v[-1] <= now - self.window
        ]
        for k in empty:
            del self._hits[k]


def rate_limit(request: Request) -> None:
    limiter: RateLimiter = request.app.state.limiter
    auth = request.headers.get("authorization", "")
    key = (
        hashlib.sha256(auth.encode()).hexdigest()[:16]
        if auth
        else (request.client.host if request.client else "?")
    )
    # Stricter limit for auth endpoints. 30/min (not 10): Telegram payloads
    # cannot be brute-forced without the bot token, and NAT-shared client
    # IPs would otherwise block legitimate onboarding bursts.
    if request.url.path.startswith("/v1/auth/"):
        limiter.check(f"{request.url.path}:{key}", max_requests=30)
    else:
        limiter.check(f"{request.url.path}:{key}")
