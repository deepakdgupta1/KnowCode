import pytest

from knowcode.data_models import ParseResult, Entity, Relationship
from knowcode.parsers.python_parser import PythonParser
from knowcode.parsers.markdown_parser import MarkdownParser
from knowcode.parsers.yaml_parser import YamlParser
from knowcode.parsers.javascript_parser import JavaScriptParser
from knowcode.parsers.java_parser import JavaParser
from knowcode.parsers.rust_parser import RustParser
from knowcode.parsers.vue_parser import VueParser
from knowcode.parsers.typescript_parser import TypeScriptParser

PARSERS = [
    (PythonParser, "x = 1\n", ".py"),
    (MarkdownParser, "# Hello\n", ".md"),
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
