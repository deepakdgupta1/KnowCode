"""Tests for rename-resilient entity content hashing."""

from pathlib import Path

from knowcode.indexing.graph_builder import GraphBuilder


def _function_hash(builder: GraphBuilder, name: str) -> str:
    entity = next(
        entity
        for entity in builder.entities.values()
        if entity.kind.value == "function" and entity.name == name
    )
    value = entity.metadata.get("content_hash")
    assert isinstance(value, str)
    assert len(value) == 64
    return value


def test_graph_builder_populates_content_hash(tmp_path: Path) -> None:
    source = tmp_path / "sample.py"
    source.write_text(
        "def foo(x: int) -> int:\n"
        "    return x + 1\n",
        encoding="utf-8",
    )

    builder = GraphBuilder().build_from_directory(tmp_path)
    foo_hash = _function_hash(builder, "foo")

    assert foo_hash


def test_content_hash_is_stable_across_file_rename(tmp_path: Path) -> None:
    source = tmp_path / "old_name.py"
    source.write_text(
        "def foo(x: int) -> int:\n"
        "    return x + 1\n",
        encoding="utf-8",
    )

    original = GraphBuilder().build_from_directory(tmp_path)
    old_entity = next(
        entity
        for entity in original.entities.values()
        if entity.kind.value == "function" and entity.name == "foo"
    )
    old_hash = old_entity.metadata["content_hash"]

    source.rename(tmp_path / "new_name.py")
    renamed = GraphBuilder().build_from_directory(tmp_path)
    new_entity = next(
        entity
        for entity in renamed.entities.values()
        if entity.kind.value == "function" and entity.name == "foo"
    )
    new_hash = new_entity.metadata["content_hash"]

    assert old_entity.id != new_entity.id
    assert old_hash == new_hash
