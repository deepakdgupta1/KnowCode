"""Tests for Java parser."""

from pathlib import Path
from knowcode.data_models import EntityKind, RelationshipKind
from knowcode.parsers.java_parser import JavaParser


def test_parse_simple_java(tmp_path: Path) -> None:
    """Test parsing a simple Java file."""
    source = """
    package com.example;
    
    import java.util.List;

    public class MyClass extends BaseClass {
        public MyClass() {}

        public void myMethod() {
            helper();
        }
        
        private void helper() {}
    }
    """
    
    file_path = tmp_path / "MyClass.java"
    file_path.write_text(source, encoding="utf-8")
    
    parser = JavaParser()
    result = parser.parse_file(file_path)
    
    assert not result.errors
    
    # Check entities
    entities = {e.qualified_name: e for e in result.entities}
    assert "MyClass" in entities
    assert entities["MyClass"].kind == EntityKind.CLASS
    
    assert "MyClass.myMethod" in entities
    assert entities["MyClass.myMethod"].kind == EntityKind.METHOD
    
    # Check relationships
    rels = result.relationships
    
    # Import
    imports = [r for r in rels if r.kind == RelationshipKind.IMPORTS]
    assert len(imports) == 1
    assert imports[0].target_id == "external::java.util.List"
    
    # Inheritance
    inherits = [r for r in rels if r.kind == RelationshipKind.INHERITS]
    assert len(inherits) == 1
    assert inherits[0].target_id == "ref::BaseClass"
    
    # Calls
    calls = [r for r in rels if r.kind == RelationshipKind.CALLS]
    targets = {r.target_id for r in calls}
    assert "ref::helper" in targets
