"""Tests for rename-resilient entity content hashing."""

from pathlib import Path

import pytest

from knowcode.indexing.graph_builder import GraphBuilder
from knowcode.utils.entity_identity import (
    EndpointKind,
    build_external_reference_id,
    build_internal_entity_id,
    build_unresolved_reference_id,
    classify_endpoint_id,
    normalize_file_identity,
)


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
        "def foo(x: int) -> int:\n    return x + 1\n",
        encoding="utf-8",
    )

    builder = GraphBuilder().build_from_directory(tmp_path)
    foo_hash = _function_hash(builder, "foo")

    assert foo_hash


def test_content_hash_is_stable_across_file_rename(tmp_path: Path) -> None:
    source = tmp_path / "old_name.py"
    source.write_text(
        "def foo(x: int) -> int:\n    return x + 1\n",
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


def test_canonical_identity_builders_classify_every_endpoint(tmp_path: Path) -> None:
    source = tmp_path / "quoted ' and λ.py"
    source.write_text("class Service: ...\n", encoding="utf-8")

    internal_id = build_internal_entity_id(source, "Service.run")
    external_id = build_external_reference_id("python", "vendor.Client")
    unresolved_id = build_unresolved_reference_id(
        "python",
        source,
        "Service.run",
        "missing.call",
    )

    assert internal_id == f"{normalize_file_identity(source)}::Service.run"
    assert classify_endpoint_id(internal_id) is EndpointKind.INTERNAL
    assert classify_endpoint_id(external_id) is EndpointKind.EXTERNAL
    assert classify_endpoint_id(unresolved_id) is EndpointKind.UNRESOLVED
    assert classify_endpoint_id("ref::missing.call") is EndpointKind.INVALID


def test_file_identity_resolves_symlink_aliases(tmp_path: Path) -> None:
    real_root = tmp_path / "real"
    real_root.mkdir()
    source = real_root / "module.py"
    source.write_text("value = 1\n", encoding="utf-8")
    alias_root = tmp_path / "alias"
    try:
        alias_root.symlink_to(real_root, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this platform")

    assert normalize_file_identity(alias_root / "module.py") == normalize_file_identity(
        source
    )


def test_identity_contract_rejects_empty_or_legacy_components(tmp_path: Path) -> None:
    source = tmp_path / "module.py"

    with pytest.raises(ValueError, match="qualified_name"):
        build_internal_entity_id(source, "  ")
    with pytest.raises(ValueError, match="namespace"):
        build_external_reference_id("", "Client")
    with pytest.raises(ValueError, match="scope"):
        build_unresolved_reference_id("python", source, "", "call")

    assert classify_endpoint_id("") is EndpointKind.INVALID
    assert classify_endpoint_id("relative.py::Thing") is EndpointKind.INVALID
    assert classify_endpoint_id("external::legacy") is EndpointKind.INVALID
    assert classify_endpoint_id("unresolved::legacy") is EndpointKind.INVALID
    assert classify_endpoint_id("type::Point") is EndpointKind.INVALID
    noncanonical = f"{normalize_file_identity(tmp_path)}/../module.py::Thing"
    assert classify_endpoint_id(noncanonical) is EndpointKind.INVALID
