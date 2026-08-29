import pytest

from knowcode.data_models import ParseResult, Entity, Relationship
from knowcode.parsers.python_parser import PythonParser
from knowcode.parsers.markdown_parser import MarkdownParser
from knowcode.parsers.rst_parser import RstParser
from knowcode.parsers.yaml_parser import YamlParser
from knowcode.parsers.javascript_parser import JavaScriptParser
from knowcode.parsers.java_parser import JavaParser
from knowcode.parsers.rust_parser import RustParser
from knowcode.parsers.vue_parser import VueParser
from knowcode.parsers.typescript_parser import TypeScriptParser

PARSERS = [
    (PythonParser, "x = 1\n", ".py"),
    (MarkdownParser, "# Hello\n", ".md"),
    (RstParser, "Title\n=====\n\nBody.\n", ".rst"),
    (YamlParser, "key: value\n", ".yaml"),
    (JavaScriptParser, "let x = 1;\n", ".js"),
    (JavaParser, "class Main {}\n", ".java"),
    (RustParser, "fn main() {}\n", ".rs"),
    (VueParser, "<template><div></div></template>\n", ".vue"),
    (TypeScriptParser, "let x: number = 1;\n", ".ts"),
]


@pytest.mark.parametrize("parser_class, code, ext", PARSERS)
def test_contract_missing_file(tmp_path, parser_class, code, ext):
    parser = parser_class()
    missing_file = tmp_path / f"missing_file{ext}"
    result = parser.parse_file(missing_file)

    assert isinstance(result, ParseResult)
    assert result.file_path == str(missing_file)
    assert len(result.errors) > 0
    assert isinstance(result.entities, list)
    assert isinstance(result.relationships, list)


@pytest.mark.parametrize("parser_class, code, ext", PARSERS)
def test_contract_empty_file(tmp_path, parser_class, code, ext):
    parser = parser_class()
    empty_file = tmp_path / f"empty{ext}"
    empty_file.write_text("")

    result = parser.parse_file(empty_file)
    assert isinstance(result, ParseResult)
    assert result.file_path == str(empty_file)
    assert isinstance(result.entities, list)
    assert isinstance(result.relationships, list)
    assert isinstance(result.errors, list)

    for entity in result.entities:
        assert isinstance(entity, Entity)
    for rel in result.relationships:
        assert isinstance(rel, Relationship)


@pytest.mark.parametrize("parser_class, code, ext", PARSERS)
def test_contract_valid_file(tmp_path, parser_class, code, ext):
    parser = parser_class()
    valid_file = tmp_path / f"valid{ext}"
    valid_file.write_text(code)

    result = parser.parse_file(valid_file)
    assert isinstance(result, ParseResult)
    assert result.file_path == str(valid_file)
    assert isinstance(result.entities, list)
    assert isinstance(result.relationships, list)
    assert isinstance(result.errors, list)

    for entity in result.entities:
        assert isinstance(entity, Entity)
        assert isinstance(entity.id, str)
        assert entity.id != ""
        assert isinstance(entity.qualified_name, str)
    for rel in result.relationships:
        assert isinstance(rel, Relationship)
        assert isinstance(rel.source_id, str)
        assert isinstance(rel.target_id, str)


# Fixtures shaped to collide under a naive identity scheme: a heading whose slug
# equals the filename, and two sibling symbols sharing a name (BL-1). None of
# these names the file itself; that separate collision is pinned below.
COLLISION_PRONE = [
    (
        PythonParser,
        "def alpha():\n    pass\n\n\nclass Alpha:\n    def alpha(self):\n        pass\n",
        ".py",
    ),
    (MarkdownParser, "# Alpha\n\n## Notes\n\na\n\n# Beta\n\n## Notes\n\nb\n", ".md"),
    (
        RstParser,
        "Alpha\n=====\n\nNotes\n-----\n\na\n\nBeta\n====\n\nNotes\n-----\n\nb\n",
        ".rst",
    ),
    (YamlParser, "a:\n  key: 1\nb:\n  key: 2\n", ".yaml"),
    (JavaScriptParser, "function alpha() {}\nclass Alpha { alpha() {} }\n", ".js"),
    (JavaParser, "class Alpha { void alpha() {} void beta() {} }\n", ".java"),
    (
        RustParser,
        "fn alpha() {}\nstruct Alpha;\nimpl Alpha { fn alpha(&self) {} }\n",
        ".rs",
    ),
    (
        VueParser,
        "<template><div></div></template>\n<script>export default { name: 'alpha' }</script>\n",
        ".vue",
    ),
    (
        TypeScriptParser,
        "function alpha(): void {}\nclass Alpha { alpha(): void {} }\n",
        ".ts",
    ),
]


def _colliding_ids(entities) -> list[str]:
    ids = [entity.id for entity in entities]
    return sorted({entity_id for entity_id in ids if ids.count(entity_id) > 1})


@pytest.mark.parametrize("parser_class, code, ext", COLLISION_PRONE)
def test_contract_entity_ids_are_unique_within_a_file(
    tmp_path, parser_class, code, ext
):
    """Two entities sharing an ID make the chunker emit duplicate chunk IDs,
    which `validate_prepared_chunks` rejects, which drops the whole file from
    the index with only a warning. Uniqueness is a parser contract (BL-1)."""
    source = tmp_path / f"doc{ext}"
    source.write_text(code)

    entities = parser_class().parse_file(source).entities

    assert _colliding_ids(entities) == [], (
        f"{parser_class.__name__} emitted colliding IDs"
    )


# A top-level symbol named after its own file collides with the file's MODULE
# entity, whose qualified name is the stem. Every language is affected and the
# graph, not the chunk table, is what breaks: the entities silently overwrite
# each other and every edge to that ID becomes ambiguous. BL-9.
NAMED_AFTER_FILE = [
    (PythonParser, "def doc():\n    pass\n", ".py"),
    (JavaScriptParser, "function doc() {}\n", ".js"),
    (TypeScriptParser, "function doc(): void {}\n", ".ts"),
]


@pytest.mark.xfail(
    strict=True, reason="BL-9: MODULE takes the file stem as its qualified name"
)
@pytest.mark.parametrize("parser_class, code, ext", NAMED_AFTER_FILE)
def test_contract_a_symbol_named_after_its_file_does_not_shadow_the_module(
    tmp_path, parser_class, code, ext
):
    source = tmp_path / f"doc{ext}"
    source.write_text(code)

    entities = parser_class().parse_file(source).entities

    assert _colliding_ids(entities) == []
