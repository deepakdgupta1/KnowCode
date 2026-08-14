"""Merged-graph endpoint invariants for the release gate.

These are the Step 01 graph invariants expressed as an assertion over a built
:class:`GraphBuilder`. The Step 07 cross-language gate proves them over a small
fixture; the Step 22 gate reuses the same rules over the adversarial repository
so one helper defines "a well-formed merged graph" for both.
"""

from __future__ import annotations

from knowcode.data_models import RelationshipKind
from knowcode.indexing.graph_builder import GraphBuilder
from knowcode.utils.entity_identity import EndpointKind, classify_endpoint_id


def assert_no_dangling_endpoints(builder: GraphBuilder) -> None:
    """Require the Step 01 endpoint and containment invariants on a merged graph.

    Every entity id is canonical and internal; every edge source is an existing
    internal entity; no edge target is invalid; every internal target resolves to
    a real entity; and every ``contains`` target is internal.
    """
    entity_ids = set(builder.entities)

    for entity_id in entity_ids:
        assert classify_endpoint_id(entity_id) is EndpointKind.INTERNAL, (
            f"non-internal entity id: {entity_id!r}"
        )

    for relationship in builder.relationships:
        source_kind = classify_endpoint_id(relationship.source_id)
        target_kind = classify_endpoint_id(relationship.target_id)

        assert source_kind is EndpointKind.INTERNAL, (
            f"edge source must be internal: {relationship.source_id!r}"
        )
        assert relationship.source_id in entity_ids, (
            f"missing internal source entity: {relationship.source_id!r}"
        )
        assert target_kind is not EndpointKind.INVALID, (
            f"invalid edge target: {relationship.target_id!r}"
        )
        if target_kind is EndpointKind.INTERNAL:
            assert relationship.target_id in entity_ids, (
                f"dangling internal target: {relationship.target_id!r}"
            )
        if relationship.kind is RelationshipKind.CONTAINS:
            assert target_kind is EndpointKind.INTERNAL, (
                f"contains target must be internal: {relationship.target_id!r}"
            )
