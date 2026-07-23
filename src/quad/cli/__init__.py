"""Quad CLI — secondary interface for debugging, manual commands,
and maintenance operations.

Built on Typer with async command support.
"""

from __future__ import annotations

from .app import app

__all__ = [
    "app",
]
