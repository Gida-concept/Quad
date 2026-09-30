"""quad-api — public REST API (FastAPI, /v1) for the multi-tenant Bybit service.

Telegram-login auth, per-tenant Bybit credentials (Fernet-encrypted),
per-tenant config, and tenant-scoped trading reads.
"""

from .app import create_app

__all__ = ["create_app"]
