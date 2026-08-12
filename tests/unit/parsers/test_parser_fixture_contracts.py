"""Tests for committed parser fixtures and exact graph assertions."""

from __future__ import annotations

from pathlib import Path

import pytest

from knowcode.data_models import Entity, Location, ParseResult, Relationship
from tests.helpers.parser_assertions import (
    assert_exact_parse_result,
    assert_relationship_endpoints_classified,
    load_parser_fixture_contract,
)


FIXTURE_ROOT = Path(__file__).parents[2] / "fixtures" / "parser_contracts"


def _fixture_sources() -> list[Path]:
    return sorted(
        path
        for path in FIXTURE_ROOT.rglob("*")
        if path.is_file() and not path.name.endswith(".expected.json")
    )


def test_all_parser_fixture_contracts_are_well_formed() -> None:
    contracts = [load_parser_fixture_contract(path) for path in _fixture_sources()]

    assert {contract.language for contract in contracts} == {
        "javascript",
        "python",
        "rust",
        "typescript",
        "vue",
    }
    assert len(contracts) == 9


def test_exact_parse_assertion_accepts_only_the_committed_graph() -> None:
    contract = load_parser_fixture_contract(
        FIXTURE_ROOT / "javascript" / "inheritance.js"
    )
    entities = [
        Entity(
            id=expected.id,
            kind=expected.kind,
            name=expected.qualified_name.rsplit(".", 1)[-1],
            qualified_name=expected.qualified_name,
            location=Location(
                file_path=str(contract.source_path),
                line_start=expected.line_start,
                line_end=expected.line_end,
            ),
            metadata=dict(expected.metadata),
        )
        for expected in contract.entities
    ]
    relationships = [
        Relationship(
            source_id=expected.source_id,
            target_id=expected.target_id,
            kind=expected.kind,
            metadata=dict(expected.metadata),
        )
        for expected in contract.relationships
    ]
    result = ParseResult(
        file_path=str(contract.source_path),
        entities=entities,
        relationships=relationships,
    )

    assert_exact_parse_result(result, contract)

    result.entities.append(entities[-1])
    with pytest.raises(AssertionError, match="exact entities"):
        assert_exact_parse_result(result, contract)


def test_endpoint_assertion_rejects_legacy_reference_namespaces() -> None:
    contract = load_parser_fixture_contract(
        FIXTURE_ROOT / "javascript" / "inheritance.js"
    )
    parent = contract.entities[1]
    child = contract.entities[2]
    result = ParseResult(
        file_path=str(contract.source_path),
        entities=[
            Entity(
                id=expected.id,
                kind=expected.kind,
                name=expected.qualified_name,
                qualified_name=expected.qualified_name,
                location=Location(
                    str(contract.source_path),
                    expected.line_start,
                    expected.line_end,
                ),
            )
            for expected in (parent, child)
        ],
        relationships=[
            Relationship(
                source_id=child.id,
                target_id="ref::Parent",
                kind=contract.relationships[3].kind,
            )
        ],
    )

    with pytest.raises(AssertionError, match="invalid endpoint"):
        assert_relationship_endpoints_classified(result)
