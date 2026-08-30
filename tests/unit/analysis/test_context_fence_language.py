"""Context bundles fence source in the language it is written in (BL-24).

All four signature and source-code fences were hardcoded to ```python, so Rust,
TypeScript, Java and Vue source reached the LLM -- and the user -- declared as
Python. Wrong highlighting for the reader, and a false claim to the model about
what it is being asked to reason over.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from knowcode.analysis.context_synthesizer import ContextSynthesizer
from knowcode.data_models import Entity, EntityKind, Location, TaskType


def _store(entity: Entity) -> MagicMock:
    store = MagicMock()
    store.get_entity.return_value = entity
    store.get_parent.return_value = None
    store.get_callers.return_value = []
    store.get_callees.return_value = []
    store.get_children.return_value = []
    return store


def _entity(file_name: str, **metadata: str) -> Entity:
    return Entity(
        id=f"{file_name}::alpha",
        kind=EntityKind.FUNCTION,
        name="alpha",
        qualified_name="alpha",
        location=Location(file_name, 1, 3),
        signature="fn alpha() -> i32",
        docstring="Returns a number.",
        source_code="fn alpha() -> i32 { 1 }",
        metadata=dict(metadata),
    )


@pytest.mark.parametrize(
    ("file_name", "metadata", "expected"),
    [
        ("m.rs", {}, "```rust"),
        ("m.ts", {}, "```typescript"),
        ("m.vue", {}, "```vue"),
        ("m.py", {}, "```python"),
        ("m.weird", {"language": "elixir"}, "```elixir"),
    ],
)
def test_both_synthesis_paths_fence_source_in_its_own_language(
    file_name: str, metadata: dict[str, str], expected: str
) -> None:
    """``synthesize`` and ``synthesize_with_task`` each carry their own fences."""
    entity = _entity(file_name, **metadata)
    synthesizer = ContextSynthesizer(_store(entity), max_tokens=2000)

    plain = synthesizer.synthesize(entity.id)
    tasked = synthesizer.synthesize_with_task(entity.id, TaskType.DEBUG)

    for bundle in (plain, tasked):
        assert bundle is not None
        assert expected in bundle.context_text
        if expected != "```python":
            assert "```python" not in bundle.context_text


def test_an_unfamiliar_suffix_is_passed_through_rather_than_guessed() -> None:
    """An unrecognised info string is ignored by renderers, so it costs nothing.

    Keeping it beats mapping it to "python" (which lies) or dropping it (which
    forgets what the file was). Renderers fall back to plain text.
    """
    entity = _entity("m.zzz")

    bundle = ContextSynthesizer(_store(entity), max_tokens=2000).synthesize_with_task(
        entity.id, TaskType.DEBUG
    )

    assert bundle is not None
    assert "```zzz" in bundle.context_text
    assert "```python" not in bundle.context_text


def test_a_file_with_no_suffix_gets_a_bare_fence_not_a_wrong_one() -> None:
    """```unknown would be a literal, wrong claim; a bare fence says nothing."""
    entity = _entity("Makefile")

    bundle = ContextSynthesizer(_store(entity), max_tokens=2000).synthesize_with_task(
        entity.id, TaskType.DEBUG
    )

    assert bundle is not None
    assert "```unknown" not in bundle.context_text
    assert "## Source Code" in bundle.context_text
    assert "```\nfn alpha" in bundle.context_text
