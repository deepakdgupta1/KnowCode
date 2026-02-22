"""Tests for TypeScript parser."""

from pathlib import Path
from knowcode.data_models import EntityKind, RelationshipKind
from knowcode.parsers.typescript_parser import TypeScriptParser


def test_parse_typescript_features(tmp_path: Path) -> None:
    """Test parsing a TypeScript file with TS-specific features."""
    source = """
    import { something } from 'external-module';

    interface MyInterface {
        myMethod(a: number): string;
    }
    
    type MyType = string | number;
    
    enum MyEnum {
        A = 1,
        B = 2
    }

    class MyClass implements MyInterface {
        constructor() {}

        myMethod(a: number): string {
            something();
            return "hello";
        }
    }

    const arrowFunc = (b: MyType) => {
        const x = new MyClass();
    };
    """
    
    file_path = tmp_path / "test.ts"
    file_path.write_text(source, encoding="utf-8")
    
    parser = TypeScriptParser()
    result = parser.parse_file(file_path)
    
    assert not result.errors
    
    # Check entities
    entities = {e.qualified_name: e for e in result.entities}
    
    # Ensure TS specific entities are mapped as CLASS
    assert "MyInterface" in entities
    assert entities["MyInterface"].kind == EntityKind.CLASS
    
    assert "MyType" in entities
    assert entities["MyType"].kind == EntityKind.CLASS
    
    assert "MyEnum" in entities
    assert entities["MyEnum"].kind == EntityKind.CLASS
    
    # Normal JS parts
    assert "MyClass" in entities
    assert entities["MyClass"].kind == EntityKind.CLASS
    
    assert "MyInterface.myMethod" in entities
    assert entities["MyInterface.myMethod"].kind == EntityKind.METHOD
    
    assert "MyClass.myMethod" in entities
    assert entities["MyClass.myMethod"].kind == EntityKind.METHOD
    
    assert "arrowFunc" in entities
    assert entities["arrowFunc"].kind == EntityKind.FUNCTION
    
    # Check relationships
    rels = result.relationships
    
    # Import
    imports = [r for r in rels if r.kind == RelationshipKind.IMPORTS]
    assert len(imports) == 1
    assert imports[0].target_id == "external::external-module"
    
    # Calls
    calls = [r for r in rels if r.kind == RelationshipKind.CALLS]
    targets = {r.target_id for r in calls}
    assert "ref::something" in targets
