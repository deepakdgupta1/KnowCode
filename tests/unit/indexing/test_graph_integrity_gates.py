"""Cross-language parser and graph integrity gates (Step 07).

These gates activate the Step 01 graph invariants across every supported
parser and validate behavior through :class:`GraphBuilder`, not only direct
parser output. Language-local tests can still agree with a broken global graph
contract; the parametrized cases here close that gap.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from knowcode.data_models import RelationshipKind
from knowcode.indexing.graph_builder import GraphBuilder
from knowcode.indexing.scanner import FileInfo
from knowcode.parsers.java_parser import JavaParser
from knowcode.parsers.javascript_parser import JavaScriptParser
from knowcode.parsers.python_parser import PythonParser
from knowcode.parsers.rust_parser import RustParser
from knowcode.parsers.typescript_parser import TypeScriptParser
from knowcode.parsers.vue_parser import VueParser
from knowcode.utils.entity_identity import EndpointKind, classify_endpoint_id


# ---------------------------------------------------------------------------
# Unique entity ID invariant (target invariant 2 + "never silently dropped")
# ---------------------------------------------------------------------------

# (parser factory, extension, source with a repeated declaration). Rust is the
# control case: it already deduplicates and reports the collision (Step 06).
DUPLICATE_DECLARATIONS = [
    pytest.param(
        PythonParser,
        ".py",
        "class Service:\n    pass\nclass Service:\n    pass\n",
        id="python",
    ),
    pytest.param(
        JavaScriptParser,
        ".js",
        "class Service {}\nclass Service {}\n",
        id="javascript",
    ),
    pytest.param(
        TypeScriptParser,
        ".ts",
        "class Service {}\nclass Service {}\n",
        id="typescript",
    ),
    pytest.param(
        JavaParser,
        ".java",
        "class Service {}\nclass Service {}\n",
        id="java",
    ),
    pytest.param(
        RustParser,
        ".rs",
        "struct Service {}\nstruct Service {}\n",
        id="rust",
    ),
    pytest.param(
        VueParser,
        ".vue",
        "<script>\nexport default {\n"
        "  data() { return { count: 0 } },\n"
        "  computed: { count() { return 1 } },\n"
        "}\n</script>\n",
        id="vue",
    ),
]


@pytest.mark.parametrize("parser_class, extension, source", DUPLICATE_DECLARATIONS)
def test_duplicate_declarations_never_produce_duplicate_entity_ids(
    tmp_path: Path,
    parser_class: object,
    extension: str,
    source: str,
) -> None:
    """A repeated declaration yields unique IDs and a visible collision report.

    Two declarations in one file cannot share one canonical ID. The parser must
    keep one entity and report the dropped duplicate through ``ParseResult.errors``
    rather than silently collapsing it later in ``GraphBuilder``.
    """
    parser = parser_class()  # type: ignore[operator]
    source_path = tmp_path / f"sample{extension}"
    source_path.write_text(source, encoding="utf-8")

    result = parser.parse_file(source_path)

    entity_ids = [entity.id for entity in result.entities]
    assert len(entity_ids) == len(set(entity_ids)), (
        f"duplicate entity IDs emitted by {parser_class.__name__}: {entity_ids}"
    )
    assert any(
        "duplicate" in message.lower() for message in result.errors
    ), f"{parser_class.__name__} must report the dropped duplicate; errors={result.errors}"


# ---------------------------------------------------------------------------
# Merged-graph integrity across languages and scan order
# ---------------------------------------------------------------------------

MIXED_FIXTURE_ROOT = Path(__file__).parents[2] / "fixtures" / "mixed_language"


def _snapshot(
    builder: GraphBuilder,
) -> tuple[frozenset[str], tuple[tuple[str, str, str], ...]]:
    """A scan-order-independent fingerprint of a merged graph.

    Entity IDs are a set (identity only) and relationships are a sorted multiset
    of ``(source, target, kind)`` so duplicate edges are still counted.
    """
    entities = frozenset(builder.entities)
    relationships = tuple(
        sorted((r.source_id, r.target_id, r.kind.value) for r in builder.relationships)
    )
    return entities, relationships


def assert_merged_graph_invariants(builder: GraphBuilder) -> None:
    """Require the Step 01 endpoint and containment invariants on a merged graph."""
    entity_ids = set(builder.entities)

    # Every entity ID is canonical and internal.
    for entity_id in entity_ids:
        assert classify_endpoint_id(entity_id) is EndpointKind.INTERNAL, (
            f"non-internal entity id: {entity_id!r}"
        )

    for relationship in builder.relationships:
        source_kind = classify_endpoint_id(relationship.source_id)
        target_kind = classify_endpoint_id(relationship.target_id)

        assert source_kind is EndpointKind.INTERNAL, (
            f"edge source must be internal: {relationship.source_id!r}"
        )
        assert relationship.source_id in entity_ids, (
            f"missing internal source entity: {relationship.source_id!r}"
        )
        assert target_kind is not EndpointKind.INVALID, (
            f"invalid edge target: {relationship.target_id!r}"
        )
        if target_kind is EndpointKind.INTERNAL:
            assert relationship.target_id in entity_ids, (
                f"dangling internal target: {relationship.target_id!r}"
            )
        if relationship.kind is RelationshipKind.CONTAINS:
            assert target_kind is EndpointKind.INTERNAL, (
                f"contains target must be internal: {relationship.target_id!r}"
            )


def _mixed_file_infos() -> list[FileInfo]:
    return [
        FileInfo(
            path=path,
            relative_path=str(path.relative_to(MIXED_FIXTURE_ROOT)),
            extension=path.suffix,
            size_bytes=path.stat().st_size,
        )
        for path in sorted(MIXED_FIXTURE_ROOT.iterdir())
        if path.is_file()
    ]


def test_mixed_language_merge_has_no_invalid_or_dangling_endpoints() -> None:
    """A real mixed-language repository merges into one well-formed graph.

    Asserts the Step 01 invariants hold through ``build_from_directory`` across
    JavaScript, TypeScript, Python, Vue, and Rust in one graph: every entity is
    internal, every edge source exists, every internal target exists, no edge is
    invalid, and every ``contains`` target is internal.
    """
    builder = GraphBuilder().build_from_directory(MIXED_FIXTURE_ROOT)

    assert builder.entities, "the mixed-language fixture produced no entities"
    assert not builder.errors, f"unexpected parse errors: {builder.errors}"
    assert_merged_graph_invariants(builder)


def test_mixed_language_graph_is_independent_of_scan_order() -> None:
    """The merged graph is stable when files are scanned in reversed order.

    Target invariant 7 requires parser output to be independent of filesystem
    scan order. Building the same files forward and reversed must yield one
    identical entity set and relationship multiset, so reference resolution
    cannot quietly pick a scan-order-dependent endpoint.
    """
    files = _mixed_file_infos()
    assert len(files) >= 2, "mixed-language fixture needs at least two files"

    forward = GraphBuilder().build_from_files(list(files))
    reversed_build = GraphBuilder().build_from_files(list(reversed(files)))

    assert _snapshot(forward) == _snapshot(reversed_build), (
        "merged graph differs when files are scanned in reversed order"
    )
