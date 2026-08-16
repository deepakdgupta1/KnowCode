"""Shared readiness metadata and dependency checks."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from importlib.util import find_spec
from typing import Iterable, Sequence


@dataclass(frozen=True)
class FeatureSpec:
    """Optional feature metadata used by CLI guards and doctor checks."""

    key: str
    extra: str
    modules: tuple[str, ...]
    description: str

    @property
    def install_target(self) -> str:
        """Return the package extra target for this feature."""
        return f"knowcode[{self.extra}]"


@dataclass(frozen=True)
class MissingFeature:
    """One optional feature with missing importable modules."""

    feature: FeatureSpec
    modules: tuple[str, ...]


FEATURE_SPECS: dict[str, FeatureSpec] = {
    "search": FeatureSpec(
        key="search",
        extra="search",
        modules=("faiss", "lancedb"),
        description="semantic search and vector indexing",
    ),
    "llm": FeatureSpec(
        key="llm",
        extra="llm",
        modules=("openai", "google.genai", "google.api_core"),
        description="LLM answering and OpenAI/Gemini integrations",
    ),
    "server": FeatureSpec(
        key="server",
        extra="server",
        modules=("fastapi", "uvicorn", "slowapi"),
        description="FastAPI server",
    ),
    "watch": FeatureSpec(
        key="watch",
        extra="watch",
        modules=("watchdog",),
        description="background watch mode",
    ),
    "mcp": FeatureSpec(
        key="mcp",
        extra="mcp",
        modules=("mcp",),
        description="MCP server integration",
    ),
    "voyageai": FeatureSpec(
        key="voyageai",
        extra="voyageai",
        modules=("voyageai",),
        description="VoyageAI embeddings and reranking",
    ),
}

IDEAL_SETUP_EXTRAS = ("all", "mcp", "voyageai")
IDEAL_SETUP_TARGET = f"knowcode[{','.join(IDEAL_SETUP_EXTRAS)}]"
IDEAL_SETUP_FEATURES = (
    "semantic search",
    "LLM answering",
    "FastAPI server",
    "watch mode",
    "MCP server",
    "VoyageAI embeddings and reranking",
)
IDEAL_SETUP_FEATURE_KEYS = ("search", "llm", "server", "watch", "mcp", "voyageai")


def build_install_command(
    *,
    upgrade: bool = False,
    user_install: bool = False,
    python_executable: str | None = None,
) -> list[str]:
    """Build the pip command for installing the ideal KnowCode setup."""
    command = [python_executable or sys.executable, "-m", "pip", "install"]
    if upgrade:
        command.append("--upgrade")
    if user_install:
        command.append("--user")
    command.append(IDEAL_SETUP_TARGET)
    return command


def missing_modules(modules: Sequence[str]) -> tuple[str, ...]:
    """Return modules that are not importable in the current environment."""
    missing: list[str] = []
    for module in modules:
        try:
            if find_spec(module) is None:
                missing.append(module)
        except (ModuleNotFoundError, ImportError, ValueError):
            missing.append(module)
    return tuple(missing)


def missing_features(
    feature_keys: Iterable[str] | None = None,
) -> tuple[MissingFeature, ...]:
    """Return missing optional features, deduped by feature key."""
    keys = tuple(feature_keys) if feature_keys is not None else tuple(FEATURE_SPECS)
    results: list[MissingFeature] = []
    seen: set[str] = set()
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        feature = FEATURE_SPECS[key]
        missing = missing_modules(feature.modules)
        if missing:
            results.append(MissingFeature(feature=feature, modules=missing))
    return tuple(results)


def missing_feature_for_modules(
    extra: str,
    modules: Sequence[str] | None = None,
) -> MissingFeature | None:
    """Return a missing feature for a legacy extra/modules guard."""
    feature = FEATURE_SPECS.get(extra)
    checked_modules = tuple(modules or (feature.modules if feature else ()))
    missing = missing_modules(checked_modules)
    if not missing:
        return None
    if feature is None:
        feature = FeatureSpec(
            key=extra,
            extra=extra,
            modules=checked_modules,
            description=extra,
        )
    return MissingFeature(feature=feature, modules=missing)


def install_hint() -> str:
    """Return a concise ideal setup install hint."""
    return (
        f'Install the ideal setup with `pip install "{IDEAL_SETUP_TARGET}"` '
        "or run `knowcode install` in the target environment."
    )
