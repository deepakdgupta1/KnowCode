"""Cross-language parser and graph integrity gates (Step 07).

These gates activate the Step 01 graph invariants across every supported
parser and validate behavior through :class:`GraphBuilder`, not only direct
parser output. Language-local tests can still agree with a broken global graph
contract; the parametrized cases here close that gap.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

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
