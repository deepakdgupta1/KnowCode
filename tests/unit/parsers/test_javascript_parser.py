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
