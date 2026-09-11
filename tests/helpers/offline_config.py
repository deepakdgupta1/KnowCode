"""A configuration that declares an offline run, for tests without API keys.

``AppConfig.load(None)`` falls back to ``./aimodels.yaml``, so a test calling
``run_doctor`` from the repository root silently inherits this project's own
config, which names ``voyage-code-3``. The test process has no Voyage key, so
the build it just made used the dummy embedder, and BL-35 makes doctor fail
that combination: an index of deterministic hashes while a real embedder is
configured is exactly the state that check exists to catch.

Tests whose subject is generation coherence rather than embedding quality point
doctor at this config instead. Configuring no embedding model at all says what
an offline test run actually is, so the dummy is the intent, doctor's semantic
check judges the artifacts, and the verdict no longer depends on a file outside
the fixture.
"""

from __future__ import annotations

from pathlib import Path

_OFFLINE_CONFIG = """
natural_language_models:
  - name: gemini-test
    provider: google
    api_key_env: KC_OFFLINE_LLM_KEY
config:
  sufficiency_threshold: 0.8
"""


def write_offline_config(store_root: Path) -> Path:
    """Write an aimodels.yaml naming no embedding model, and return its path.

    Written to a sibling of the store root, never inside it. Doctor's freshness
    check walks the store root for sources newer than the index, so a config
    dropped in there reports the tree stale and fails the assertion it was
    added to satisfy.
    """
    directory = store_root.parent / f"{store_root.name}-offline-config"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "aimodels.yaml"
    path.write_text(_OFFLINE_CONFIG, encoding="utf-8")
    return path
