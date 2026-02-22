"""Tests for JavaScript parser."""

from pathlib import Path
from knowcode.data_models import EntityKind, RelationshipKind
from knowcode.parsers.javascript_parser import JavaScriptParser


def test_parse_simple_js(tmp_path: Path) -> None:
    """Test parsing a simple JavaScript file."""
    source = """
    import { something } from 'external-module';

    class MyClass {
        constructor() {}

        myMethod() {
            something();
        }
    }

    function globalFunc() {
        const x = new MyClass();
    }
    """
    
    file_path = tmp_path / "test.js"
    file_path.write_text(source, encoding="utf-8")
    
    parser = JavaScriptParser()
    result = parser.parse_file(file_path)
    
    assert not result.errors
    
    # Check entities
    entities = {e.qualified_name: e for e in result.entities}
    assert "MyClass" in entities
    assert entities["MyClass"].kind == EntityKind.CLASS
    
    # Check method (might be MyClass.myMethod or just myMethod dependent on implementation details)
    # Our implementation uses qualified names
    assert "MyClass.myMethod" in entities
    assert entities["MyClass.myMethod"].kind == EntityKind.METHOD
    
    assert "globalFunc" in entities
    assert entities["globalFunc"].kind == EntityKind.FUNCTION
    
    # Check relationships
    rels = result.relationships
    
    # Import
    imports = [r for r in rels if r.kind == RelationshipKind.IMPORTS]
    assert len(imports) == 1
    assert imports[0].target_id == "external::external-module"
    
    # Calls
    calls = [r for r in rels if r.kind == RelationshipKind.CALLS]
    # something() inside myMethod
    # new MyClass() inside globalFunc (constructor call)
    
    targets = {r.target_id for r in calls}
    assert "ref::something" in targets
    # assert "ref::MyClass" in targets # Constructor call logic might need verifying


def test_parse_lexical_function_assignments(tmp_path: Path) -> None:
    """Parse const/let function assignments as top-level functions."""
    source = """
    const bar = () => 1;
    let baz = function() {
        return bar();
    };
    """

    file_path = tmp_path / "lexical.js"
    file_path.write_text(source, encoding="utf-8")

    parser = JavaScriptParser()
    result = parser.parse_file(file_path)

    entities = {e.qualified_name: e for e in result.entities}
    assert "bar" in entities
    assert entities["bar"].kind == EntityKind.FUNCTION
    assert "baz" in entities
    assert entities["baz"].kind == EntityKind.FUNCTION

    calls = [r for r in result.relationships if r.kind == RelationshipKind.CALLS]
    assert any(r.source_id.endswith("::baz") and r.target_id == "ref::bar" for r in calls)


def test_parse_export_default_anonymous_function(tmp_path: Path) -> None:
    """Handle anonymous default export functions with a stable fallback name."""
    source = """
    export default function() {
        return 1;
    }
    """

    file_path = tmp_path / "default_export.js"
    file_path.write_text(source, encoding="utf-8")

    parser = JavaScriptParser()
    result = parser.parse_file(file_path)

    entities = {e.qualified_name: e for e in result.entities}
    assert "default_export" in entities
    assert entities["default_export"].kind == EntityKind.FUNCTION
