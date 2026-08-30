"""Tests for TypeScript parser."""

from pathlib import Path

from knowcode.data_models import EntityKind, RelationshipKind
from knowcode.parsers.typescript_parser import TypeScriptParser
from knowcode.utils.entity_identity import (
    build_external_reference_id,
    build_internal_entity_id,
    build_unresolved_reference_id,
)
from tests.helpers.parser_assertions import (
    assert_exact_parse_result,
    load_parser_fixture_contract,
)


FIXTURE_ROOT = Path(__file__).parents[2] / "fixtures" / "parser_contracts"


def test_parse_exported_declarations_fixture_exactly() -> None:
    """Extract every committed TypeScript export with exact edges and lines."""
    contract = load_parser_fixture_contract(
        FIXTURE_ROOT / "typescript" / "exported_declarations.ts"
    )

    result = TypeScriptParser().parse_file(contract.source_path)

    assert not result.errors
    assert_exact_parse_result(result, contract)


def test_parse_default_exported_typescript_class(tmp_path: Path) -> None:
    """Unwrap a default exported class in the TypeScript grammar."""
    file_path = tmp_path / "default_class.ts"
    file_path.write_text("export default class Service {}\n", encoding="utf-8")

    result = TypeScriptParser().parse_file(file_path)

    assert [(entity.qualified_name, entity.kind) for entity in result.entities] == [
        ("default_class", EntityKind.MODULE),
        ("default_class.Service", EntityKind.CLASS),
    ]


def test_parse_typescript_inheritance_forms(tmp_path: Path) -> None:
    """Resolve local class/interface bases and classify qualified bases."""
    source = """interface Parent {}
interface Child extends Parent, Framework.Shape {}
class Base {}
class Derived extends Base {}
"""
    file_path = tmp_path / "inheritance.ts"
    file_path.write_text(source, encoding="utf-8")

    result = TypeScriptParser().parse_file(file_path)

    relationships = {
        (relationship.source_id, relationship.target_id, relationship.kind)
        for relationship in result.relationships
        if relationship.kind is RelationshipKind.INHERITS
    }
    assert relationships == {
        (
            build_internal_entity_id(file_path, "inheritance.Child"),
            build_internal_entity_id(file_path, "inheritance.Parent"),
            RelationshipKind.INHERITS,
        ),
        (
            build_internal_entity_id(file_path, "inheritance.Child"),
            build_unresolved_reference_id(
                "typescript", file_path, "inheritance.Child", "Framework.Shape"
            ),
            RelationshipKind.INHERITS,
        ),
        (
            build_internal_entity_id(file_path, "inheritance.Derived"),
            build_internal_entity_id(file_path, "inheritance.Base"),
            RelationshipKind.INHERITS,
        ),
    }


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
    assert "test.MyInterface" in entities
    assert entities["test.MyInterface"].kind == EntityKind.CLASS

    assert "test.MyType" in entities
    assert entities["test.MyType"].kind == EntityKind.CLASS

    assert "test.MyEnum" in entities
    assert entities["test.MyEnum"].kind == EntityKind.CLASS

    # Normal JS parts
    assert "test.MyClass" in entities
    assert entities["test.MyClass"].kind == EntityKind.CLASS

    assert "test.MyInterface.myMethod" in entities
    assert entities["test.MyInterface.myMethod"].kind == EntityKind.METHOD

    assert "test.MyClass.myMethod" in entities
    assert entities["test.MyClass.myMethod"].kind == EntityKind.METHOD

    assert "test.arrowFunc" in entities
    assert entities["test.arrowFunc"].kind == EntityKind.FUNCTION

    # Check relationships
    rels = result.relationships

    # Import
    imports = [r for r in rels if r.kind == RelationshipKind.IMPORTS]
    assert len(imports) == 1
    assert imports[0].target_id == build_external_reference_id("npm", "external-module")

    # Calls
    calls = [r for r in rels if r.kind == RelationshipKind.CALLS]
    targets = {r.target_id for r in calls}
    assert (
        build_unresolved_reference_id(
            "typescript", file_path, "test.MyClass.myMethod", "something"
        )
        in targets
    )
    assert build_internal_entity_id(file_path, "test.MyClass") in targets
