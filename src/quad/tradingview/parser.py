"""TradingView webhook alert parser.

Parses JSON payloads sent by TradingView webhook alerts into a
structured ``TradingViewSignal`` dict.

Supports both:
1. JSON-formatted alert messages (``application/json`` content-type)
2. Message-encoded JSON within ``alert_message`` / ``message`` fields

TradingView alert messages contain placeholders like ``{{ticker}}``,
``{{strategy.order.action}}``, ``{{close}}``, etc. that are resolved
by TradingView before the webhook POST is sent.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import structlog

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = structlog.get_logger(__name__)


# ============================================================================
# Public API
# ============================================================================


def parse_alert(body: str | bytes, content_type: str = "") -> dict[str, Any]:
    """Parse a TradingView webhook payload into a structured dict.

    Parameters
    ----------
    body:
        Raw request body from the webhook POST.
    content_type:
        HTTP Content-Type header value.

    Returns
    -------
    dict
        Parsed alert with fields normalised to lowercase keys.
        At minimum contains a ``"raw"`` key with the original body.

    Raises
    ------
    ValueError
        If the body cannot be parsed as JSON.
    """
    raw = body.decode("utf-8") if isinstance(body, bytes) else body
    result: dict[str, Any] = {"raw": raw}

    # Attempt JSON parsing
    try:
        if raw.strip().startswith(("{", "[")):
            parsed = json.loads(raw)
        else:
            # Try extracting JSON from a plain-text message
            parsed = _extract_json_from_text(raw)
    except json.JSONDecodeError as exc:
        logger.warning("tv_alert_parse_failed", error=str(exc))
        # Return minimally structured data
        result["_format"] = "text"
        result["_parse_error"] = str(exc)
        return result

    if not isinstance(parsed, dict):
        # Array payloads are uncommon but handle gracefully
        result["_format"] = "array"
        result["data"] = parsed
        return result

    # Normalise all keys to lowercase for consistent access
    normalised: dict[str, Any] = {}
    for key, value in parsed.items():
        normalised[key.lower()] = value
        # Also keep original case at the original key
        normalised[key] = value

    # Detect and extract inner message (common relay format)
    inner = normalised.get("alert_message") or normalised.get("message") or ""
    if isinstance(inner, str) and inner.strip().startswith("{"):
        try:
            inner_parsed = json.loads(inner)
            if isinstance(inner_parsed, dict):
                for k, v in inner_parsed.items():
                    normalised[k.lower()] = v
        except (json.JSONDecodeError, TypeError):
            pass

    normalised["_format"] = "json"
    normalised["_content_type"] = content_type
    return normalised


# ============================================================================
# Internal helpers
# ============================================================================


def _extract_json_from_text(text: str) -> dict[str, Any]:
    """Try to extract a JSON object from a plain-text payload.

    Some TradingView setups send mixed text with embedded JSON.
    This attempts to locate a ``{...}`` block within the text.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidate = text[start : end + 1]
        return json.loads(candidate)
    raise json.JSONDecodeError("No JSON object found in text", text, 0)
