"""Tests for JavaScript parser."""

from pathlib import Path

import pytest

from knowcode.data_models import EntityKind, RelationshipKind
from knowcode.parsers.javascript_parser import JavaScriptParser
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


def test_parse_inheritance_fixture_exactly() -> None:
    """Resolve local bases and classify member/complex bases explicitly."""
    contract = load_parser_fixture_contract(
        FIXTURE_ROOT / "javascript" / "inheritance.js"
    )

    result = JavaScriptParser().parse_file(contract.source_path)

    assert not result.errors
    assert_exact_parse_result(result, contract)


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
    assert imports[0].target_id == build_external_reference_id("npm", "external-module")

    # Calls
    calls = [r for r in rels if r.kind == RelationshipKind.CALLS]
    # something() inside myMethod
    # new MyClass() inside globalFunc (constructor call)
    targets = {r.target_id for r in calls}
    assert (
        build_unresolved_reference_id(
            "javascript", file_path, "MyClass.myMethod", "something"
        )
        in targets
    )
    assert build_internal_entity_id(file_path, "MyClass") in targets


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
    assert any(
        r.source_id == build_internal_entity_id(file_path, "baz")
        and r.target_id == build_internal_entity_id(file_path, "bar")
        for r in calls
    )


def test_parse_export_default_anonymous_function(tmp_path: Path) -> None:
    """Handle anonymous default export functions with a stable fallback name."""
    source = """
    export default function() {
        return 1;
    }
    """

    file_path = tmp_path / "anonymous_default.js"
    file_path.write_text(source, encoding="utf-8")

    parser = JavaScriptParser()
    result = parser.parse_file(file_path)

    entities = {e.qualified_name: e for e in result.entities}
    assert set(entities) == {"anonymous_default", "default_export"}
    assert entities["default_export"].kind == EntityKind.FUNCTION
    assert entities["default_export"].location.line_start == 2
    module_id = build_internal_entity_id(file_path, "anonymous_default")
    assert len(result.relationships) == 1
    assert result.relationships[0].source_id == module_id
    assert result.relationships[0].target_id == build_internal_entity_id(
        file_path, "default_export"
    )
    assert result.relationships[0].kind is RelationshipKind.CONTAINS


@pytest.mark.parametrize(
    ("source", "declaration_name"),
    [
        ("export class NamedClass {}", "NamedClass"),
        ("export default class DefaultClass {}", "DefaultClass"),
        ("export default class {}", "default_export"),
    ],
)
def test_parse_named_and_default_exported_classes(
    tmp_path: Path,
    source: str,
    declaration_name: str,
) -> None:
    """Extract both named and default-exported class declarations."""
    file_path = tmp_path / "exported_class.js"
    file_path.write_text(f"{source}\n", encoding="utf-8")

    result = JavaScriptParser().parse_file(file_path)

    assert not result.errors
    assert [(entity.qualified_name, entity.kind) for entity in result.entities] == [
        (file_path.stem, EntityKind.MODULE),
        (declaration_name, EntityKind.CLASS),
    ]
    assert result.entities[1].location.line_start == 1
    assert result.entities[1].location.line_end == 1
    assert len(result.relationships) == 1
    assert result.relationships[0].source_id == build_internal_entity_id(
        file_path, file_path.stem
    )
    assert result.relationships[0].target_id == build_internal_entity_id(
        file_path, declaration_name
    )


def test_parse_exported_function_forms_and_exact_calls(tmp_path: Path) -> None:
    """Extract named, arrow, and anonymous function-valued exports once."""
    source = """export function create() {}
export const factory = () => create()
export const resolver = function() { return factory() }
"""
    file_path = tmp_path / "exports.js"
    file_path.write_text(source, encoding="utf-8")

    result = JavaScriptParser().parse_file(file_path)

    assert not result.errors
    assert [
        (
            entity.qualified_name,
            entity.kind,
            entity.location.line_start,
            entity.location.line_end,
        )
        for entity in result.entities
    ] == [
        ("exports", EntityKind.MODULE, 1, 3),
        ("create", EntityKind.FUNCTION, 1, 1),
        ("factory", EntityKind.FUNCTION, 2, 2),
        ("resolver", EntityKind.FUNCTION, 3, 3),
    ]
    module_id = build_internal_entity_id(file_path, "exports")
    create_id = build_internal_entity_id(file_path, "create")
    factory_id = build_internal_entity_id(file_path, "factory")
    resolver_id = build_internal_entity_id(file_path, "resolver")
    assert {
        (relationship.source_id, relationship.target_id, relationship.kind)
        for relationship in result.relationships
    } == {
        (module_id, create_id, RelationshipKind.CONTAINS),
        (module_id, factory_id, RelationshipKind.CONTAINS),
        (module_id, resolver_id, RelationshipKind.CONTAINS),
        (factory_id, create_id, RelationshipKind.CALLS),
        (resolver_id, factory_id, RelationshipKind.CALLS),
    }


def test_nonlocal_call_uses_scoped_unresolved_reference(tmp_path: Path) -> None:
    """Keep a bare call unresolved when no local declaration proves its target."""
    file_path = tmp_path / "calls.js"
    file_path.write_text("function run() { remote() }\n", encoding="utf-8")

    result = JavaScriptParser().parse_file(file_path)

    run_id = build_internal_entity_id(file_path, "run")
    assert any(
        relationship.source_id == run_id
        and relationship.target_id
        == build_unresolved_reference_id("javascript", file_path, "run", "remote")
        and relationship.kind is RelationshipKind.CALLS
        for relationship in result.relationships
    )


def test_commonjs_require_is_an_external_import(tmp_path: Path) -> None:
    """Classify a literal CommonJS dependency without exposing filter syntax."""
    file_path = tmp_path / "commonjs.js"
    file_path.write_text(
        'function load() { return require("@scope/pkg") }\n',
        encoding="utf-8",
    )

    result = JavaScriptParser().parse_file(file_path)

    load_id = build_internal_entity_id(file_path, "load")
    assert any(
        relationship.source_id == load_id
        and relationship.target_id == build_external_reference_id("npm", "@scope/pkg")
        and relationship.kind is RelationshipKind.IMPORTS
        for relationship in result.relationships
    )
