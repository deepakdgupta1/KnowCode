"""Structural checks for the committed golden retrieval dataset."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


_EVAL_DIR = Path(__file__).parents[1]
_PIPELINE_PLAN = _EVAL_DIR / "pipeline" / "phase1_plan.json"
_GOLDEN_DATASET = _EVAL_DIR / "golden" / "golden_v1.0.json"
_GOLDEN_META = _EVAL_DIR / "golden" / "golden_v1.0.meta.json"


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def test_golden_dataset_matches_phase1_strata() -> None:
    plan = _load_json(_PIPELINE_PLAN)
    records = _load_json(_GOLDEN_DATASET)
    meta = _load_json(_GOLDEN_META)

    expected_counts = {
        (row["task_type"], row["difficulty"]): row["target"]
        for row in plan["strata"]
        if row["target"] > 0
    }
    actual_counts = Counter(
        (record["task_type"], record["difficulty"]) for record in records
    )

    assert len(records) == plan["target_records"]
    assert actual_counts == expected_counts
    assert meta["query_count"] == len(records)
    assert meta["strata_counts"] == {
        f"{task_type}:{difficulty}": count
        for (task_type, difficulty), count in sorted(actual_counts.items())
    }


def test_golden_records_have_required_fields_and_unique_ids() -> None:
    records = _load_json(_GOLDEN_DATASET)
    seen_ids: set[str] = set()

    for record in records:
        query_id = record.get("query_id")
        assert isinstance(query_id, str) and query_id
        assert query_id not in seen_ids
        seen_ids.add(query_id)

        assert isinstance(record.get("query_text"), str) and record["query_text"]
        assert record.get("task_type") in {
            "locate",
            "explain",
            "debug",
            "extend",
            "review",
            "general",
        }
        assert record.get("difficulty") in {"easy", "medium", "hard"}
        assert record.get("expected_entities")
        assert record.get("expected_files")
        assert isinstance(record.get("must_mention_facts"), list)
        assert isinstance(record.get("must_not_mention_facts"), list)
