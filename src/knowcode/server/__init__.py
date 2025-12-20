"""Compatibility package for the moved API server modules."""

from knowcode.api.api import router
from knowcode.api.main import create_app, start_server

__all__ = ["router", "create_app", "start_server"]
