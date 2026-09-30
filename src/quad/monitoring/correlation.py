"""Request-scoped correlation IDs for structlog.

A single trading cycle fans out over several pairs, the TradingView webhook
can fire concurrently with that cycle, and the Telegram job queue runs
independently — all writing into one log stream.  Without a correlation id,
interleaved lines from concurrent pair scans cannot be separated after the
fact.

The id lives in a single :class:`~contextvars.ContextVar`, so concurrent
tasks each see their own value and a scope exit restores exactly what was
bound before.  :func:`structlog_context_processor` injects it into every
event emitted inside the scope.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any, Iterator, MutableMapping

#: The one place a correlation id is stored.  A ContextVar (not a global) is
#: required: asyncio tasks run in separate contexts, so a task that binds an
#: id cannot leak it into a concurrently-running one.
_correlation_id: ContextVar[str | None] = ContextVar(
    "quad_correlation_id", default=None
)


def new_correlation_id(prefix: str = "") -> str:
    """Return a fresh correlation id, optionally prefixed for readability.

    ``new_correlation_id("cycle")`` -> ``"cycle-3f2a1b9c4d5e6f70"``.
    """
    token = uuid.uuid4().hex[:16]
    return f"{prefix}-{token}" if prefix else token


def get_correlation_id() -> str | None:
    """Return the correlation id bound to the current context, or ``None``."""
    return _correlation_id.get()


def set_correlation_id(value: str | None) -> Token:
    """Bind *value* as the correlation id for the current context.

    Returns the token needed by :func:`reset_correlation_id`.
    """
    return _correlation_id.set(value)


def reset_correlation_id(token: Token) -> None:
    """Restore the correlation id captured by :func:`set_correlation_id`."""
    try:
        _correlation_id.reset(token)
    except ValueError:
        # Token was created in a different context; fall back to clearing so
        # a leaked id can never outlive its scope.
        _correlation_id.set(None)


@contextmanager
def correlation_scope(prefix: str = "") -> Iterator[str]:
    """Bind a fresh correlation id for the duration of the block.

    Usage::

        with correlation_scope("cycle") as cid:
            log.info("cycle_start", cycle=cid)
    """
    cid = new_correlation_id(prefix)
    token = set_correlation_id(cid)
    try:
        yield cid
    finally:
        reset_correlation_id(token)


def structlog_context_processor(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor that injects ``correlation_id`` into events.

    An id already bound on the event (e.g. via ``logger.bind(...)``) wins, so
    an explicitly-scoped component can override the ambient one.

    The ``event_dict`` parameter is a ``MutableMapping`` (not a ``dict``)
    because that is structlog's ``Processor`` protocol: a ``dict`` parameter is
    contravariant, so it would not satisfy the protocol and the processor would
    be rejected when added to the processor chain.
    """
    if "correlation_id" not in event_dict:
        cid = _correlation_id.get()
        if cid:
            event_dict["correlation_id"] = cid
    return event_dict
