"""Helpers for optional dependency gating."""

from __future__ import annotations

from typing import Sequence

from knowcode.readiness import missing_feature_for_modules


def require_extra(extra: str, command: str, modules: Sequence[str]) -> None:
    """Validate that optional modules are available for a command.

    Args:
        extra: Optional dependency extra name (for install hint).
        command: CLI command string shown in the install hint.
        modules: Python module names to verify.

    Raises:
        ImportError: If any required module is missing.
    """
    missing = missing_feature_for_modules(extra, modules)
    if missing is not None:
        raise ImportError(f"Install knowcode[{extra}] to use '{command}'.")
