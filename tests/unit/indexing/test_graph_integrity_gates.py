"""Cross-language parser and graph integrity gates (Step 07).

These gates activate the Step 01 graph invariants across every supported
parser and validate behavior through :class:`GraphBuilder`, not only direct
parser output. Language-local tests can still agree with a broken global graph
contract; the parametrized cases here close that gap.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from knowcode.parsers.java_parser import JavaParser
from knowcode.parsers.javascript_parser import JavaScriptParser
from knowcode.parsers.python_parser import PythonParser
from knowcode.parsers.rust_parser import RustParser
from knowcode.parsers.typescript_parser import TypeScriptParser
from knowcode.parsers.vue_parser import VueParser


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
# Mixed-language repository merge & scan-order determinism
# ---------------------------------------------------------------------------

MIXED_FIXTURE_ROOT = Path(__file__).parents[2] / "fixtures" / "mixed_language"


def test_mixed_language_graph_merge_and_order_determinism() -> None:
    """GraphBuilder produces identical entities and relationships regardless
    of forward or reversed scan order, and leaves no invalid endpoints."""
    from knowcode.indexing.graph_builder import GraphBuilder
    from knowcode.indexing.scanner import FileInfo
    from knowcode.utils.entity_identity import EndpointKind, classify_endpoint_id

    files = sorted(MIXED_FIXTURE_ROOT.glob("*"))
    file_infos = [
        FileInfo(
            path=p,
            relative_path=str(p.relative_to(MIXED_FIXTURE_ROOT)),
            extension=p.suffix,
            size_bytes=p.stat().st_size,
        )
        for p in files
        if p.is_file()
    ]

    forward_builder = GraphBuilder().build_from_files(file_infos)
    reversed_builder = GraphBuilder().build_from_files(list(reversed(file_infos)))

    assert set(forward_builder.entities.keys()) == set(reversed_builder.entities.keys())
    for entity_id, entity in forward_builder.entities.items():
        rev_entity = reversed_builder.entities[entity_id]
        assert entity.name == rev_entity.name
        assert entity.kind == rev_entity.kind
        assert entity.qualified_name == rev_entity.qualified_name
        assert entity.metadata.get("content_hash") == rev_entity.metadata.get("content_hash")

    forward_rels = {
        (r.source_id, r.target_id, r.kind) for r in forward_builder.relationships
    }
    reversed_rels = {
        (r.source_id, r.target_id, r.kind) for r in reversed_builder.relationships
    }
    assert forward_rels == reversed_rels

    for rel in forward_builder.relationships:
        assert classify_endpoint_id(rel.source_id) != EndpointKind.INVALID
        assert classify_endpoint_id(rel.target_id) != EndpointKind.INVALID
