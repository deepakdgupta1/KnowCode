"""Helpers for optional dependency gating."""

from __future__ import annotations

from importlib.util import find_spec
from typing import Sequence


def require_extra(extra: str, command: str, modules: Sequence[str]) -> None:
    """Validate that optional modules are available for a command.

    Args:
        extra: Optional dependency extra name (for install hint).
        command: CLI command string shown in the install hint.
        modules: Python module names to verify.

    Raises:
        ImportError: If any required module is missing.
    """
    missing: list[str] = []
    for module in modules:
        try:
            if find_spec(module) is None:
                missing.append(module)
        except ModuleNotFoundError:
            missing.append(module)

    if missing:
        raise ImportError(f"Install knowcode[{extra}] to use '{command}'.")
