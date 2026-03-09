"""Tests for strict config loading in server contexts."""

from __future__ import annotations

from typing import Any

from knowcode.api import main as api_main


def test_create_app_initializes_service_with_strict_config(monkeypatch) -> None:  # type: ignore
    """FastAPI app should construct KnowCodeService with strict config validation."""
    captured: dict[str, Any] = {}

    class DummyService:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(api_main, "KnowCodeService", DummyService)
    app = api_main.create_app(store_path=".")

    assert app is not None
    assert captured.get("strict_config") is True
