"""Unit tests for graph builder reference resolution."""

from pathlib import Path

import pytest

from knowcode.indexing.graph_builder import GraphBuilder
from knowcode.indexing.scanner import FileInfo
from knowcode.data_models import Entity, EntityKind, Location, ParseResult, Relationship, RelationshipKind
from knowcode.utils.entity_identity import EndpointKind, classify_endpoint_id
from tests.helpers.parser_assertions import (
    assert_exact_parse_result,
    load_parser_fixture_contract,
)


FIXTURE_ROOT = Path(__file__).parents[2] / "fixtures" / "parser_contracts"


def _fixture_sources() -> list[Path]:
    return sorted(
        path
        for path in FIXTURE_ROOT.rglob("*")
        if path.is_file() and not path.name.endswith(".expected.json")
    )


@pytest.mark.parametrize("source", _fixture_sources(), ids=lambda p: p.name)
def test_graph_builder_matches_fixture_contract(source: Path) -> None:
    """Every committed fixture survives a GraphBuilder merge unchanged.

    Language-local tests call a parser directly; this gate builds the graph
    through ``GraphBuilder.build_from_files`` (which runs annotation,
    content-hashing, and ``ref::`` reference resolution) and requires the merged
    result to still match the exact fixture contract: identical entities,
    relationships, locations, and endpoint classification.
    """
    contract = load_parser_fixture_contract(source)
    file_info = FileInfo(
        path=source,
        relative_path=str(source),
        extension=source.suffix,
        size_bytes=source.stat().st_size,
    )
    builder = GraphBuilder().build_from_files([file_info])
    merged = ParseResult(
        file_path=str(source),
        entities=list(builder.entities.values()),
        relationships=list(builder.relationships),
    )

    assert_exact_parse_result(merged, contract)


def test_reference_resolution() -> None:
    """ref:: targets should resolve to known entities when possible."""
    builder = GraphBuilder()
    entity = Entity(
        id="file.py::Foo",
        kind=EntityKind.CLASS,
        name="Foo",
        qualified_name="Foo",
        location=Location("file.py", 1, 5),
    )
    builder.entities = {entity.id: entity}
    builder.relationships = [
        Relationship(
            source_id="file.py::Caller",
            target_id="ref::Foo",
            kind=RelationshipKind.REFERENCES,
        )
    ]

    builder._resolve_references()

    assert builder.relationships[0].target_id == entity.id


RUST_IMPL_SOURCE = """\
use std::fmt;

trait Draw {
}

pub struct Point {
    x: f64,
    y: f64,
}

impl Point {
    pub fn new(x: f64, y: f64) -> Self {
        Point { x, y }
    }
}

impl Draw for Point {
    fn draw(&self) {}
}

impl fmt::Display for Point {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        write!(f, "point")
    }
}

impl Draw for Vec<u8> {
    fn draw(&self) {}
}

mod helpers {
    pub struct Helper;

    impl Helper {
        pub fn assist() {}
    }
}

fn main() {
    let point = Point::new(1.0, 2.0);
}
"""


def test_rust_impl_graph_has_no_invalid_or_dangling_endpoints(tmp_path: Path) -> None:
    """A merged Rust graph keeps every endpoint classified and every edge anchored."""
    (tmp_path / "shapes.rs").write_text(RUST_IMPL_SOURCE, encoding="utf-8")

    builder = GraphBuilder().build_from_directory(tmp_path)

    assert builder.entities, "expected the Rust file to produce entities"
    for entity_id in builder.entities:
        assert classify_endpoint_id(entity_id) is EndpointKind.INTERNAL, entity_id

    for rel in builder.relationships:
        assert classify_endpoint_id(rel.source_id) is EndpointKind.INTERNAL, rel
        assert rel.source_id in builder.entities, rel
        target_kind = classify_endpoint_id(rel.target_id)
        assert target_kind is not EndpointKind.INVALID, rel
        if target_kind is EndpointKind.INTERNAL:
            assert rel.target_id in builder.entities, rel
