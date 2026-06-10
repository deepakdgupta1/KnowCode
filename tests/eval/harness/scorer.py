"""Structural and narrative scoring for retrieval quality evaluation.

Operates on a single (golden_record, retrieval_result) pair and returns a flat
dict of named metric values.  All metrics are pure functions — no I/O, no LLM
calls.  Narrative scoring (must_mention_facts / must_not_mention_facts) requires
an external LLM judge and is left as a stub (see ``score_narrative``).

Schema references: docs/GOLDEN_DATASET_PIPELINE.md §4, §6, §11.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Primitive metrics
# ---------------------------------------------------------------------------


def precision_at_k(retrieved: list[str], expected: set[str], k: int) -> float:
    """Fraction of the top-k retrieved entity IDs that are in *expected*.

    Args:
        retrieved: Ordered list of entity IDs as returned by the system
                   (index 0 = rank 1).
        expected:  Ground-truth entity IDs from the golden record.
        k:         Cut-off rank.

    Returns:
        Float in [0, 1].  Returns 0.0 when k == 0.
    """
    if k <= 0:
        return 0.0
    top_k = retrieved[:k]
    if not top_k:
        return 0.0
    hits = sum(1 for e in top_k if e in expected)
    return hits / k


def recall_at_k(retrieved: list[str], expected: set[str], k: int) -> float:
    """Fraction of *expected* entity IDs found in the top-k retrieved.

    Args:
        retrieved: Ordered list of entity IDs.
        expected:  Ground-truth entity IDs.
        k:         Cut-off rank.

    Returns:
        Float in [0, 1].  Returns 0.0 when *expected* is empty.
    """
    if not expected:
        return 0.0
    top_k = set(retrieved[:k])
    hits = sum(1 for e in expected if e in top_k)
    return hits / len(expected)


def reciprocal_rank(retrieved: list[str], expected: set[str]) -> float:
    """Reciprocal of the rank of the first relevant entity ID.

    Returns 0.0 if no relevant result appears in *retrieved*.
    """
    for rank, entity_id in enumerate(retrieved, start=1):
        if entity_id in expected:
            return 1.0 / rank
    return 0.0


def file_coverage_at_k(
    retrieved_entities: list[str],
    expected_files: list[str],
    k: int,
) -> float:
    """Fraction of *expected_files* covered by the entity IDs in the top-k.

    An entity ID covers a file if the file path is a prefix of the entity ID
    (KnowCode convention: ``src/knowcode/llm/agent.py::Agent.smart_answer``).

    Args:
        retrieved_entities: Ordered entity IDs from the retrieval result.
        expected_files:     File paths from the golden record.
        k:                  Cut-off rank.

    Returns:
        Float in [0, 1].  Returns 0.0 when *expected_files* is empty.
    """
    if not expected_files:
        return 0.0
    top_k = retrieved_entities[:k]
    covered = {
        f for f in expected_files if any(entity.startswith(f) for entity in top_k)
    }
    return len(covered) / len(expected_files)


def entity_jaccard(set_a: set[str], set_b: set[str]) -> float:
    """Jaccard similarity between two sets of entity IDs.

    Used by the §6 material-difference definition:
    ``jaccard(oracle.expected_entities, adversary_supported_entities) >= 0.8``.

    Returns 0.0 when both sets are empty.
    """
    if not set_a and not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union


# ---------------------------------------------------------------------------
# Narrative scoring stub
# ---------------------------------------------------------------------------


def score_narrative(
    query_text: str,
    must_mention_facts: list[str],
    must_not_mention_facts: list[str],
    candidate_answer: str,
) -> dict[str, Any]:
    """Check must_mention / must_not_mention facts against a candidate answer.

    Currently a **stub** — returns ``None`` for all fact verdicts.  A complete
    implementation requires an LLM judge (§6, §9.3) and is intentionally out of
    scope for the structural harness.

    Args:
        query_text:             The original query string.
        must_mention_facts:     Facts the answer must contain.
        must_not_mention_facts: Facts the answer must NOT assert.
        candidate_answer:       The answer text to evaluate.

    Returns:
        Dict with keys:
        - ``must_mention_verdicts``: list of ``{fact, verdict}`` dicts
          (``"supported"`` | ``"unsupported"`` | None)
        - ``must_not_mention_verdicts``: list of ``{fact, verdict}`` dicts
        - ``narrative_score``: None until implemented
    """
    # TODO: implement via LLM judge (§9.3 Adversarial Reviewer prompt template)
    return {
        "must_mention_verdicts": [
            {"fact": f, "verdict": None} for f in must_mention_facts
        ],
        "must_not_mention_verdicts": [
            {"fact": f, "verdict": None} for f in must_not_mention_facts
        ],
        "narrative_score": None,
    }


# ---------------------------------------------------------------------------
# Per-record scorer
# ---------------------------------------------------------------------------


def normalize_entity_id(entity_id: str) -> str:
    """Normalize entity ID by making the file path component relative to project root."""
    from pathlib import Path

    if "::" not in entity_id:
        return entity_id
    path_part, symbol_part = entity_id.split("::", 1)

    path = Path(path_part)
    if not path.is_absolute():
        return entity_id

    try:
        rel_path = path.relative_to(Path.cwd().resolve())
        return f"{rel_path}::{symbol_part}"
    except ValueError:
        pass

    parts = path.parts
    for idx, part in enumerate(parts):
        if part in ("src", "tests"):
            rel_path = Path(*parts[idx:])
            return f"{rel_path}::{symbol_part}"

    return entity_id


def _would_route_local(
    retrieval_result: dict[str, Any],
    sufficiency: float,
) -> bool:
    """Derive the local-routing decision from retrieval metadata."""
    explicit = retrieval_result.get("would_route_local")
    if isinstance(explicit, bool):
        return explicit

    threshold = retrieval_result.get("sufficiency_threshold", 0.8)
    if not isinstance(threshold, (int, float)):
        threshold = 0.8

    return bool(retrieval_result.get("context_text")) and sufficiency >= float(
        threshold
    )


def score_record(
    golden: dict[str, Any],
    retrieval_result: dict[str, Any],
) -> dict[str, Any]:
    """Compute all structural metrics for one (golden, retrieval_result) pair.

    Args:
        golden:           One record from ``golden_v1.0.json``.
        retrieval_result: Return value of
                          ``KnowCodeService.retrieve_context_for_query``.

    Returns:
        Flat dict with keys:
        ``query_id``, ``task_type``, ``difficulty``,
        ``precision_at_1``, ``precision_at_5``,
        ``recall_at_10``, ``mrr``,
        ``file_coverage_at_1``, ``file_coverage_at_5``,
        ``sufficiency_score``, ``routed_local``,
        ``narrative`` (sub-dict from ``score_narrative``).
    """
    query_id: str = golden["query_id"]
    task_type: str = golden.get("task_type", "unknown")
    difficulty: str = golden.get("difficulty", "unknown")

    # Handle expected_entities which may be a list of dicts with 'entity_id' or a list of string IDs
    raw_expected = golden.get("expected_entities", [])
    expected_entity_ids: set[str]
    if raw_expected and isinstance(raw_expected[0], dict):
        expected_entity_ids = {
            normalize_entity_id(e["entity_id"]) for e in raw_expected
        }
    else:
        expected_entity_ids = {normalize_entity_id(e) for e in raw_expected}
    expected_files: list[str] = golden.get("expected_files", [])

    # Build an ordered list of entity IDs from the retrieval result.
    # ``selected_entities`` is ordered by retrieval rank.
    retrieved_entities: list[str] = [
        normalize_entity_id(e["entity_id"])
        for e in retrieval_result.get("selected_entities", [])
        if "entity_id" in e
    ]

    sufficiency: float = float(retrieval_result.get("sufficiency_score", 0.0))
    routed_local = _would_route_local(retrieval_result, sufficiency)

    precision_1 = precision_at_k(retrieved_entities, expected_entity_ids, k=1)
    precision_5 = precision_at_k(retrieved_entities, expected_entity_ids, k=5)
    recall_10 = recall_at_k(retrieved_entities, expected_entity_ids, k=10)
    mrr = reciprocal_rank(retrieved_entities, expected_entity_ids)
    file_coverage_1 = file_coverage_at_k(retrieved_entities, expected_files, k=1)
    file_coverage_5 = file_coverage_at_k(retrieved_entities, expected_files, k=5)

    result = {
        "query_id": query_id,
        "task_type": task_type,
        "difficulty": difficulty,
        "precision_at_1": precision_1,
        "precision_at_5": precision_5,
        "recall_at_10": recall_10,
        "mrr": mrr,
        "file_coverage_at_1": file_coverage_1,
        "file_coverage_at_5": file_coverage_5,
        "sufficiency_score": sufficiency,
        "routed_local": routed_local,
        "narrative": score_narrative(
            query_text=golden.get("query_text", ""),
            must_mention_facts=golden.get("must_mention_facts", []),
            must_not_mention_facts=golden.get("must_not_mention_facts", []),
            candidate_answer=retrieval_result.get("context_text", ""),
        ),
    }

    judged_correct = golden.get("correct")
    if isinstance(judged_correct, bool):
        structural_match = recall_10 == 1.0 and file_coverage_5 == 1.0
        result["correct"] = bool(judged_correct and structural_match)

    return result


# ---------------------------------------------------------------------------
# Aggregate metrics
# ---------------------------------------------------------------------------


def aggregate(scores: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute mean of all numeric metrics across a list of per-record scores.

    Args:
        scores: Output of ``score_record`` for each query in the dataset.

    Returns:
        Dict with ``mean_*`` prefixed keys for every numeric metric, plus
        ``n`` (record count) and ``by_task_type`` / ``by_difficulty``
        breakdowns (each a dict of the same shape).
    """
    if not scores:
        return {"n": 0}

    numeric_keys = [
        "precision_at_1",
        "precision_at_5",
        "recall_at_10",
        "mrr",
        "file_coverage_at_1",
        "file_coverage_at_5",
        "sufficiency_score",
    ]

    def _mean(records: list[dict[str, Any]], key: str) -> float:
        values = [r[key] for r in records if isinstance(r.get(key), (int, float))]
        return sum(values) / len(values) if values else 0.0

    def _summarise(records: list[dict[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {"n": len(records)}
        for k in numeric_keys:
            result[f"mean_{k}"] = _mean(records, k)
        local_count = sum(1 for r in records if r.get("routed_local"))
        result["local_routing_rate"] = local_count / len(records) if records else 0.0
        return result

    summary = _summarise(scores)

    # Break down by task_type
    task_groups: dict[str, list[dict[str, Any]]] = {}
    for r in scores:
        task_groups.setdefault(r.get("task_type", "unknown"), []).append(r)
    summary["by_task_type"] = {k: _summarise(v) for k, v in task_groups.items()}

    # Break down by difficulty
    diff_groups: dict[str, list[dict[str, Any]]] = {}
    for r in scores:
        diff_groups.setdefault(r.get("difficulty", "unknown"), []).append(r)
    summary["by_difficulty"] = {k: _summarise(v) for k, v in diff_groups.items()}

    return summary
