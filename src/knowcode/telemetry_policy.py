"""The versioned telemetry event schema and its field allowlist (ADR 5).

Telemetry answers operational questions — how often does retrieval answer
locally, how sufficient is the context, how long does a query take — and every
one of those is an aggregate. None of them needs the user's question, the
retrieved code, or an entity's file path, so none of those is representable
here: an event type is a fixed set of named fields with declared types, and a
field outside that set is dropped before the sink ever sees it.

Adding a field is therefore a deliberate edit to this module, reviewable on its
own. Adding an event type is the same. That is the point: the previous sink
accepted whatever dict a caller passed, so privacy depended on every call site
being careful forever.
"""

from __future__ import annotations

import time
from typing import Any, Final, Mapping, Sequence

#: Bumped when the meaning of an existing field changes. Readers use its
#: presence to tell a Step 20 record from legacy JSONL, which carried raw text.
TELEMETRY_SCHEMA_VERSION: Final = 1

QUERY_EVENT: Final = "query"
AGENT_DECISION_EVENT: Final = "agent_decision"
TOOL_CALL_EVENT: Final = "tool_call"
RERANKER_EVENT: Final = "reranker_latency"

#: Event types that count as one logical query. Everything else describes a
#: query that has already been counted, so a summary must not add it again.
COUNTED_EVENT_TYPES: Final = frozenset({QUERY_EVENT})

#: Fields every record carries; callers never supply them.
ENVELOPE_FIELDS: Final = frozenset(
    {"telemetry_schema_version", "event_type", "timestamp", "dropped_field_count"}
)

_STR: Final = "str"
_INT: Final = "int"
_FLOAT: Final = "float"
_BOOL: Final = "bool"

#: The allowlist. ``{event_type: {field: declared type}}``.
ALLOWED_FIELDS: Final[Mapping[str, Mapping[str, str]]] = {
    QUERY_EVENT: {
        # Keyed correlation, never the text itself.
        "query_id": _STR,
        "query_chars": _INT,
        "query_length_bucket": _STR,
        # Classification and routing outcome.
        "entry_point": _STR,
        "task_type": _STR,
        "task_confidence": _FLOAT,
        "retrieval_mode": _STR,
        "verbosity": _STR,
        "local_or_escalated": _STR,
        "sufficiency_score": _FLOAT,
        "sufficiency_bucket": _STR,
        "is_stale": _BOOL,
        # Shape of the answer, not its content.
        "total_tokens": _INT,
        "max_tokens": _INT,
        "truncated": _BOOL,
        "selected_entity_count": _INT,
        "evidence_count": _INT,
        "error_count": _INT,
        "retrievals": _INT,
        "duration_ms": _INT,
        "outcome": _STR,
        "user_marked_miss": _BOOL,
    },
    AGENT_DECISION_EVENT: {
        "query_id": _STR,
        "source": _STR,
        "task_type": _STR,
        "sufficiency_score": _FLOAT,
        "sufficiency_bucket": _STR,
        "threshold": _FLOAT,
        "sufficiency_threshold": _FLOAT,
        "routing_quality_floor": _FLOAT,
        "routing_policy_allowed": _BOOL,
        "force_llm": _BOOL,
        "llm_tokens_saved": _INT,
        "duration_ms": _INT,
        "outcome": _STR,
    },
    TOOL_CALL_EVENT: {
        "tool_name": _STR,
        # The consolidated MCP surface routes many capabilities through one
        # tool, so ``tool_name`` alone no longer identifies what was used.
        # Action names are a closed enum defined in ``knowcode.mcp.tools`` —
        # never user content — so recording one cannot leak a question or
        # pasted code. Additive and optional: records written before this
        # field existed stay valid, so no schema version bump is needed.
        "action": _STR,
        "argument_count": _INT,
        "query_id": _STR,
        "query_chars": _INT,
        "outcome": _STR,
        "duration_ms": _INT,
    },
    RERANKER_EVENT: {
        "method": _STR,
        "latency_seconds": _FLOAT,
        "num_chunks": _INT,
    },
}

#: Upper edge of each query-length bucket, in characters.
_LENGTH_EDGES: Final[Sequence[int]] = (0, 32, 128, 512, 2048)
#: Width of a sufficiency-score bucket.
_SCORE_STEP: Final = 0.2


def length_bucket(chars: int) -> str:
    """Bucket a character count, so length is a trend and not a fingerprint."""
    if chars <= 0:
        return "0"
    previous = 0
    for edge in _LENGTH_EDGES:
        if edge == 0:
            continue
        if chars <= edge:
            return f"{previous + 1}-{edge}"
        previous = edge
    return f"{previous + 1}+"


def score_bucket(score: float) -> str:
    """Bucket a 0..1 score into fifths."""
    clamped = min(max(float(score), 0.0), 1.0)
    index = min(int(clamped / _SCORE_STEP), int(1 / _SCORE_STEP) - 1)
    low = index * _SCORE_STEP
    return f"{low:.1f}-{low + _SCORE_STEP:.1f}"


def _matches(value: Any, declared: str) -> bool:
    """Whether ``value`` satisfies the declared type.

    ``bool`` is checked first everywhere because it is a subclass of ``int``:
    without that guard a ``True`` would silently satisfy an ``int`` field and a
    counter would report ``1`` for something that never happened.
    """
    if declared == _BOOL:
        return isinstance(value, bool)
    if declared == _INT:
        return isinstance(value, int) and not isinstance(value, bool)
    if declared == _FLOAT:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, str)


def sanitize_event(event: Mapping[str, Any]) -> dict[str, Any] | None:
    """Reduce a caller's event to the allowlist, or reject it.

    Returns ``None`` for an event type this module has not reviewed, which is
    the fail-closed direction: a new call site loses its metric until the
    schema is extended, rather than writing unreviewed data to a user's disk.
    """
    event_type = event.get("event_type")
    if not isinstance(event_type, str):
        return None
    allowed = ALLOWED_FIELDS.get(event_type)
    if allowed is None:
        return None

    record: dict[str, Any] = {
        "telemetry_schema_version": TELEMETRY_SCHEMA_VERSION,
        "event_type": event_type,
        "timestamp": int(event.get("timestamp", time.time())),
    }
    dropped = 0
    for name, value in event.items():
        if name in ENVELOPE_FIELDS:
            continue
        declared = allowed.get(name)
        if declared is None or not _matches(value, declared):
            dropped += 1
            continue
        record[name] = float(value) if declared == _FLOAT else value
    if dropped:
        record["dropped_field_count"] = dropped
    return record


def is_counted_query_event(record: Mapping[str, Any]) -> bool:
    """Whether a stored record counts as one logical query.

    Legacy records (written before this schema existed) are recognized by the
    absence of a version and the presence of a raw ``query`` field, so a
    pre-upgrade file keeps reporting the totals it always reported.
    """
    if record.get("telemetry_schema_version"):
        return record.get("event_type") in COUNTED_EVENT_TYPES
    return "query" in record
