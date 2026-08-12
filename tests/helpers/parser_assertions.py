"""Exact parser fixture loading and semantic graph assertions."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from knowcode.data_models import (
    EntityKind,
    ParseResult,
    RelationshipKind,
)
from knowcode.utils.entity_identity import (
    EndpointKind,
    build_external_reference_id,
    build_internal_entity_id,
    build_unresolved_reference_id,
    classify_endpoint_id,
    normalize_file_identity,
)


@dataclass(frozen=True)
class ExpectedEntity:
    """Exact entity facts committed next to a parser source fixture."""

    id: str
    kind: EntityKind
    name: str
    qualified_name: str
    line_start: int
    line_end: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExpectedRelationship:
    """Exact edge facts committed next to a parser source fixture."""

    source_id: str
    target_id: str
    kind: RelationshipKind
    target_classification: EndpointKind
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParserFixtureContract:
    """Validated expected graph for one committed source fixture."""

    source_path: Path
    language: str
    entities: tuple[ExpectedEntity, ...]
    relationships: tuple[ExpectedRelationship, ...]


def _require_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AssertionError(f"{label} must be a JSON object")
    return value


def _require_string(mapping: dict[str, Any], key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise AssertionError(f"{label}.{key} must be a non-empty string")
    return value


def _require_line(mapping: dict[str, Any], key: str, label: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise AssertionError(f"{label}.{key} must be an integer")
    return value


def _metadata(mapping: dict[str, Any], label: str) -> dict[str, Any]:
    value = mapping.get("metadata", {})
    return _require_mapping(value, f"{label}.metadata")


def load_parser_fixture_contract(source_path: Path) -> ParserFixtureContract:
    """Load and validate the ``*.expected.json`` next to ``source_path``."""
    source_path = source_path.resolve(strict=True)
    expected_path = source_path.with_name(f"{source_path.name}.expected.json")
    payload = _require_mapping(
        json.loads(expected_path.read_text(encoding="utf-8")),
        str(expected_path),
    )
    if payload.get("schema_version") != 1:
        raise AssertionError(f"{expected_path} has an unsupported schema_version")

    language = _require_string(payload, "language", str(expected_path))
    raw_entities = payload.get("entities")
    raw_relationships = payload.get("relationships")
    if not isinstance(raw_entities, list) or not isinstance(raw_relationships, list):
        raise AssertionError(f"{expected_path} must contain entity and relationship lists")

    line_count = len(source_path.read_text(encoding="utf-8").splitlines())
    entities: list[ExpectedEntity] = []
    qualified_names: set[str] = set()
    for index, raw_entity in enumerate(raw_entities):
        label = f"{expected_path}:entities[{index}]"
        entity = _require_mapping(raw_entity, label)
        qualified_name = _require_string(entity, "qualified_name", label)
        line_start = _require_line(entity, "line_start", label)
        line_end = _require_line(entity, "line_end", label)
        if not 1 <= line_start <= line_end <= line_count:
            raise AssertionError(f"{label} has a location outside the source fixture")
        if qualified_name in qualified_names:
            raise AssertionError(f"{label} duplicates qualified_name {qualified_name!r}")
        qualified_names.add(qualified_name)
        try:
            kind = EntityKind(_require_string(entity, "kind", label))
        except ValueError as exc:
            raise AssertionError(f"{label} has an unsupported entity kind") from exc
        entities.append(
            ExpectedEntity(
                id=build_internal_entity_id(source_path, qualified_name),
                kind=kind,
                name=qualified_name.rsplit(".", 1)[-1],
                qualified_name=qualified_name,
                line_start=line_start,
                line_end=line_end,
                metadata=_metadata(entity, label),
            )
        )

    relationships: list[ExpectedRelationship] = []
    for index, raw_relationship in enumerate(raw_relationships):
        label = f"{expected_path}:relationships[{index}]"
        relationship = _require_mapping(raw_relationship, label)
        source_name = _require_string(relationship, "source", label)
        if source_name not in qualified_names:
            raise AssertionError(f"{label} has an unknown internal source")
        source_id = build_internal_entity_id(source_path, source_name)
        target = _require_mapping(relationship.get("target"), f"{label}.target")
        try:
            classification = EndpointKind(
                _require_string(target, "classification", f"{label}.target")
            )
        except ValueError as exc:
            raise AssertionError(f"{label} has an unsupported target classification") from exc

        if classification is EndpointKind.INTERNAL:
            target_name = _require_string(target, "qualified_name", f"{label}.target")
            if target_name not in qualified_names:
                raise AssertionError(f"{label} has an unknown internal target")
            target_id = build_internal_entity_id(source_path, target_name)
        elif classification is EndpointKind.EXTERNAL:
            target_id = build_external_reference_id(
                _require_string(target, "namespace", f"{label}.target"),
                _require_string(target, "symbol", f"{label}.target"),
            )
        elif classification is EndpointKind.UNRESOLVED:
            target_id = build_unresolved_reference_id(
                language,
                source_path,
                _require_string(target, "scope", f"{label}.target"),
                _require_string(target, "symbol", f"{label}.target"),
            )
        else:
            raise AssertionError(f"{label} cannot declare an invalid target")

        if classify_endpoint_id(target_id) is not classification:
            raise AssertionError(f"{label} target does not match its classification")
        try:
            kind = RelationshipKind(_require_string(relationship, "kind", label))
        except ValueError as exc:
            raise AssertionError(f"{label} has an unsupported relationship kind") from exc
        if kind is RelationshipKind.CONTAINS and classification is not EndpointKind.INTERNAL:
            raise AssertionError(f"{label} contains edge must target an internal entity")
        relationships.append(
            ExpectedRelationship(
                source_id=source_id,
                target_id=target_id,
                kind=kind,
                target_classification=classification,
                metadata=_metadata(relationship, label),
            )
        )

    edge_keys = [
        (relationship.source_id, relationship.target_id, relationship.kind)
        for relationship in relationships
    ]
    if len(edge_keys) != len(set(edge_keys)):
        raise AssertionError(f"{expected_path} contains duplicate relationships")

    return ParserFixtureContract(
        source_path=source_path,
        language=language,
        entities=tuple(entities),
        relationships=tuple(relationships),
    )


def _contains_metadata(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    return all(actual.get(key) == value for key, value in expected.items())


def assert_exact_entities(result: ParseResult, contract: ParserFixtureContract) -> None:
    """Assert exact IDs, kinds, names, qualified names, and one-based locations."""
    expected = Counter(
        (
            entity.id,
            entity.kind,
            entity.name,
            entity.qualified_name,
            normalize_file_identity(contract.source_path),
            entity.line_start,
            entity.line_end,
        )
        for entity in contract.entities
    )
    actual = Counter(
        (
            entity.id,
            entity.kind,
            entity.name,
            entity.qualified_name,
            normalize_file_identity(entity.location.file_path),
            entity.location.line_start,
            entity.location.line_end,
        )
        for entity in result.entities
    )
    assert actual == expected, f"expected exact entities {expected}, got {actual}"

    actual_by_id = {entity.id: entity for entity in result.entities}
    for expected_entity in contract.entities:
        assert _contains_metadata(
            actual_by_id[expected_entity.id].metadata,
            expected_entity.metadata,
        ), f"metadata mismatch for {expected_entity.id}"


def assert_exact_relationships(
    result: ParseResult,
    contract: ParserFixtureContract,
) -> None:
    """Assert exact edge source, target, kind, multiplicity, and required metadata."""
    expected = Counter(
        (relationship.source_id, relationship.target_id, relationship.kind)
        for relationship in contract.relationships
    )
    actual = Counter(
        (relationship.source_id, relationship.target_id, relationship.kind)
        for relationship in result.relationships
    )
    assert actual == expected, f"expected exact relationships {expected}, got {actual}"

    for expected_relationship in contract.relationships:
        matching = [
            relationship
            for relationship in result.relationships
            if (
                relationship.source_id,
                relationship.target_id,
                relationship.kind,
            )
            == (
                expected_relationship.source_id,
                expected_relationship.target_id,
                expected_relationship.kind,
            )
        ]
        assert any(
            _contains_metadata(relationship.metadata, expected_relationship.metadata)
            for relationship in matching
        ), f"metadata mismatch for edge {expected_relationship}"


def assert_relationship_endpoints_classified(result: ParseResult) -> None:
    """Require explicit endpoint classes and real entities for internal edges."""
    entity_ids = {entity.id for entity in result.entities}
    for entity_id in entity_ids:
        assert classify_endpoint_id(entity_id) is EndpointKind.INTERNAL, (
            f"invalid endpoint for entity {entity_id!r}"
        )

    for relationship in result.relationships:
        source_kind = classify_endpoint_id(relationship.source_id)
        target_kind = classify_endpoint_id(relationship.target_id)
        assert source_kind is not EndpointKind.INVALID, (
            f"invalid endpoint {relationship.source_id!r}"
        )
        assert target_kind is not EndpointKind.INVALID, (
            f"invalid endpoint {relationship.target_id!r}"
        )
        assert source_kind is EndpointKind.INTERNAL, (
            f"parser edge source must be internal: {relationship.source_id!r}"
        )
        assert relationship.source_id in entity_ids, (
            f"missing internal source entity {relationship.source_id!r}"
        )
        if target_kind is EndpointKind.INTERNAL:
            assert relationship.target_id in entity_ids, (
                f"missing internal target entity {relationship.target_id!r}"
            )
        if relationship.kind is RelationshipKind.CONTAINS:
            assert target_kind is EndpointKind.INTERNAL, (
                f"contains target must be internal: {relationship.target_id!r}"
            )


def assert_exact_parse_result(
    result: ParseResult,
    contract: ParserFixtureContract,
) -> None:
    """Apply all exact fixture and endpoint invariants to a parser result."""
    assert normalize_file_identity(result.file_path) == normalize_file_identity(
        contract.source_path
    )
    assert_exact_entities(result, contract)
    assert_exact_relationships(result, contract)
    assert_relationship_endpoints_classified(result)
