"""Unit tests for knowledge store helpers and persistence."""

import json
from pathlib import Path

import pytest

from knowcode.data_models import (
    Entity,
    EntityKind,
    Location,
    Relationship,
    RelationshipKind,
)
from knowcode.storage.knowledge_store import KnowledgeStore
from knowcode.utils import atomic_write
from knowcode.utils.atomic_write import TEMP_SUFFIX


def _make_entity(entity_id: str, kind: EntityKind, name: str) -> Entity:
    return Entity(
        id=entity_id,
        kind=kind,
        name=name,
        qualified_name=name,
        location=Location("file.py", 1, 1),
    )


def test_kind_filters_and_relationship_helpers() -> None:
    """KnowledgeStore should filter by kind and expose relationships."""
    store = KnowledgeStore()
    foo = _make_entity("file.py::foo", EntityKind.FUNCTION, "foo")
    bar = _make_entity("file.py::bar", EntityKind.FUNCTION, "bar")
    store.entities = {foo.id: foo, bar.id: bar}

    rel = Relationship(source_id=foo.id, target_id=bar.id, kind=RelationshipKind.CALLS)
    store.relationships = [rel]

    assert store.get_entities_by_kind("function") == [foo, bar]
    assert store.get_outgoing_relationships(foo.id) == [rel]
    assert store.get_incoming_relationships(bar.id) == [rel]


def test_persistence_round_trip(tmp_path) -> None:  # type: ignore
    """Save/load should preserve entities, relationships, and metadata."""
    store = KnowledgeStore()
    store.metadata = {"stats": {"total": 1}}
    foo = _make_entity("file.py::foo", EntityKind.FUNCTION, "foo")
    foo.metadata = {"is_async": False, "complexity": 5, "tags": ["core", "entry"]}
    store.entities = {foo.id: foo}
    store.relationships = [
        Relationship(
            source_id=foo.id,
            target_id="external::dep",
            kind=RelationshipKind.IMPORTS,
            metadata={"kind": "test", "weight": 0.75},
        )
    ]

    save_path = tmp_path / "knowledge.json"
    store.save(save_path)
    payload = json.loads(save_path.read_text(encoding="utf-8"))

    loaded = KnowledgeStore.load(save_path)
    assert payload["schema_version"] == KnowledgeStore.SCHEMA_VERSION
    assert loaded.metadata["stats"]["total"] == 1
    assert foo.id in loaded.entities
    assert loaded.entities[foo.id].metadata["is_async"] is False
    assert loaded.entities[foo.id].metadata["complexity"] == 5
    assert loaded.entities[foo.id].metadata["tags"] == ["core", "entry"]
    assert loaded.relationships[0].metadata["kind"] == "test"
    assert loaded.relationships[0].metadata["weight"] == 0.75


def test_load_migrates_legacy_store_without_schema_version(tmp_path: Path) -> None:
    """Legacy stores without schema_version should load via migration shim."""
    legacy = {
        "version": "1.0",
        "metadata": {"stats": {"total": 0}},
        "entities": {},
        "relationships": [],
    }
    path = tmp_path / "legacy_knowledge.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = KnowledgeStore.load(path)
    assert loaded.metadata["stats"]["total"] == 0


def test_load_migrates_legacy_schema_version_one(tmp_path: Path) -> None:
    """Explicit schema_version=1 payloads should migrate forward."""
    legacy = {
        "schema_version": 1,
        "metadata": {"stats": {"total": 2}},
        "entities": {},
        "relationships": [],
    }
    path = tmp_path / "legacy_schema_one.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = KnowledgeStore.load(path)
    assert loaded.metadata["stats"]["total"] == 2


def test_load_rejects_unsupported_schema_version(tmp_path: Path) -> None:
    """Unknown schema versions should raise a clear compatibility error."""
    invalid = {
        "schema_version": 999,
        "metadata": {},
        "entities": {},
        "relationships": [],
    }
    path = tmp_path / "bad_knowledge.json"
    path.write_text(json.dumps(invalid), encoding="utf-8")

    with pytest.raises(ValueError, match="schema version"):
        KnowledgeStore.load(path)


def test_load_backfills_entity_content_hash(tmp_path: Path) -> None:
    """Loading legacy entities should backfill missing content_hash metadata."""
    payload = {
        "schema_version": KnowledgeStore.SCHEMA_VERSION,
        "metadata": {},
        "entities": {
            "file.py::foo": {
                "id": "file.py::foo",
                "kind": "function",
                "name": "foo",
                "qualified_name": "foo",
                "location": {"file_path": "file.py", "line_start": 1, "line_end": 2},
                "source_code": "def foo():\n    return 1\n",
                "metadata": {},
            }
        },
        "relationships": [],
    }
    path = tmp_path / "content_hash_backfill.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = KnowledgeStore.load(path)
    entity = loaded.entities["file.py::foo"]

    content_hash = entity.metadata.get("content_hash")
    assert isinstance(content_hash, str)
    assert len(content_hash) == 64


# --- Step 13: crash-safe replacement --------------------------------------


def _saved_store(path: Path) -> KnowledgeStore:
    """Persist a small store and return it."""
    store = KnowledgeStore()
    entity = _make_entity("file.py::foo", EntityKind.FUNCTION, "foo")
    store.entities = {entity.id: entity}
    store.save(path)
    return store


def test_save_failure_preserves_the_previous_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed replacement must leave the last good graph loadable.

    Before Step 13 ``save`` truncated the target with ``open(path, "w")``, so a
    failure destroyed the previous store and left an unparsable file.
    """
    path = tmp_path / "knowcode_knowledge.json"
    _saved_store(path)

    replacement = KnowledgeStore()
    other = _make_entity("file.py::bar", EntityKind.FUNCTION, "bar")
    replacement.entities = {other.id: other}

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr(atomic_write, "_replace", boom)
    with pytest.raises(OSError):
        replacement.save(path)

    reloaded = KnowledgeStore.load(path)
    assert set(reloaded.entities) == {"file.py::foo"}


def test_save_leaves_no_temporary_files(tmp_path: Path) -> None:
    """A published store cleans up its staging file."""
    path = tmp_path / "knowcode_knowledge.json"
    _saved_store(path)

    assert [p.name for p in tmp_path.iterdir()] == ["knowcode_knowledge.json"]


def test_load_reports_a_truncated_store_with_rebuild_guidance(tmp_path: Path) -> None:
    """A pre-Step-13 truncated artifact must fail closed, not raise raw JSON errors."""
    path = tmp_path / "knowcode_knowledge.json"
    _saved_store(path)
    text = path.read_text(encoding="utf-8")
    path.write_text(text[: len(text) // 2], encoding="utf-8")

    with pytest.raises(ValueError, match="knowcode build"):
        KnowledgeStore.load(path)


def test_load_removes_an_orphaned_temporary_file(tmp_path: Path) -> None:
    """A crash between staging and replace leaves an orphan; startup clears it."""
    path = tmp_path / "knowcode_knowledge.json"
    _saved_store(path)
    orphan = tmp_path / f".knowcode_knowledge.json.pid999999.abcd{TEMP_SUFFIX}"
    orphan.write_text("{partial", encoding="utf-8")

    KnowledgeStore.load(path)

    assert not orphan.exists()


def test_save_failure_during_serialization_preserves_the_previous_store(
    tmp_path: Path,
) -> None:
    """The reviewed defect, reproduced without fault injection.

    ``open(path, "w")`` truncates the live artifact before ``json.dump`` has
    encoded anything, so a payload that fails to serialize part-way through
    leaves a truncated file and destroys the previous graph.
    """
    path = tmp_path / "knowcode_knowledge.json"
    _saved_store(path)

    broken = KnowledgeStore()
    entity = _make_entity("file.py::bar", EntityKind.FUNCTION, "bar")
    entity.metadata["unserializable"] = {1, 2, 3}
    broken.entities = {entity.id: entity}

    with pytest.raises(TypeError):
        broken.save(path)

    reloaded = KnowledgeStore.load(path)
    assert set(reloaded.entities) == {"file.py::foo"}
