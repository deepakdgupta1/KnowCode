"""Compatibility wrapper for the moved server entry point."""

from knowcode.api.main import create_app, start_server

__all__ = ["create_app", "start_server"]
