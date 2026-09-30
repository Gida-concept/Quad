"""POST /v1/auth/telegram — Telegram Login Widget -> JWT."""

from __future__ import annotations

import os
import structlog
import uuid

from fastapi import APIRouter, Depends

from .deps import ApiState, current_tenant, envelope_error, get_state
from .schemas import AuthResponse, TelegramLoginBody
from .security import AuthError, issue_token, verify_telegram_login

router = APIRouter(prefix="/v1/auth", tags=["auth"])
logger = structlog.get_logger(__name__)


@router.post("/telegram", response_model=AuthResponse)
async def telegram_login(body: TelegramLoginBody, state: ApiState = Depends(get_state)):
    if not state.bot_token:
        raise envelope_error(
            "server_misconfigured", "telegram login not configured", 503
        )
    try:
        # exclude_unset: Telegram signs only the fields it actually sent.
        data = verify_telegram_login(
            body.model_dump(exclude_unset=True), state.bot_token
        )
    except AuthError as exc:
        raise envelope_error("invalid_login", str(exc), 401) from exc

    tg_id = int(data["id"])
    tenant = await state.tenants.get_by_telegram_user(tg_id)
    if tenant is None:
        # Invite-code gate: when QUAD_INVITE_CODES is set, require a valid code
        # for new tenant creation. Unset = unrestricted (dev mode).
        invite_codes_raw = os.environ.get("QUAD_INVITE_CODES", "").strip()
        if invite_codes_raw:
            allowed = {
                c.strip().upper() for c in invite_codes_raw.split(",") if c.strip()
            }
            provided = (getattr(body, "invite_code", None) or "").strip().upper()
            if provided not in allowed:
                raise envelope_error(
                    "forbidden",
                    "valid invite code required for new accounts",
                    403,
                )
        tenant = await state.tenants.create_tenant(
            tenant_uuid=uuid.uuid4().hex,
            telegram_user_id=tg_id,
            username=str(data.get("username", "")),
            display_name=str(data.get("first_name", "")),
        )
        await state.configs.get_or_default(tenant.tenant_uuid)  # linear defaults
    try:
        token_version = getattr(tenant, "token_version", 1)
        token = issue_token(tenant.tenant_uuid, tg_id, token_version=token_version)
    except AuthError as exc:
        raise envelope_error("server_misconfigured", str(exc), 503) from exc
    return AuthResponse(
        access_token=token, tenant_uuid=tenant.tenant_uuid, telegram_user_id=tg_id
    )


@router.post("/revoke")
async def revoke_tokens(
    state: ApiState = Depends(get_state),
    tenant_info: dict = Depends(current_tenant),
) -> dict:
    """Invalidate all outstanding tokens for this tenant.

    Increments the tenant's ``token_version`` counter.  Any JWT issued
    with an older ``ver`` claim is rejected on the next authenticated
    request.
    """
    tenant = tenant_info["tenant"]
    new_version = getattr(tenant, "token_version", 1) + 1
    await state.tenants.update_token_version(tenant.tenant_uuid, new_version)
    return {"ok": True, "message": "all tokens revoked"}
