"""Telegram pairing: issue a single-use code the user hands to the bot."""

from __future__ import annotations

import secrets as _secrets
import time
from typing import Any

from fastapi import APIRouter, Depends

from quad.persistence.models import PairingCodeModel

from .deps import ApiState, current_tenant, get_state
from .schemas import PairingCodeResponse

router = APIRouter(prefix="/v1/telegram", tags=["telegram"])

CODE_TTL_MS = 15 * 60 * 1000


@router.post("/pairing-code", response_model=PairingCodeResponse)
async def issue_pairing_code(
    ctx: dict[str, Any] = Depends(current_tenant),
    state: ApiState = Depends(get_state),
):
    """Issue a code for ``/start <code>`` in the Telegram bot (Phase 3 binds it)."""
    now = int(time.time() * 1000)
    code = _secrets.token_hex(6).upper()  # 12 hex chars, 48-bit entropy
    await state.pairing.create(
        PairingCodeModel(
            id=0,
            tenant_uuid=ctx["tenant"].tenant_uuid,
            code=code,
            purpose="link",
            expires_at=now + CODE_TTL_MS,
            created_at=now,
        )
    )
    return PairingCodeResponse(code=code, expires_at=now + CODE_TTL_MS)
