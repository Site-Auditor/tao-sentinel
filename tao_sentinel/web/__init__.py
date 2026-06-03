"""Web dashboard for tao-sentinel.

Exposes :func:`tao_sentinel.web.app.create_app`, a factory that builds the
FastAPI application serving the read-only watchtower dashboard.
"""

from __future__ import annotations

from .app import create_app

__all__ = ["create_app"]
