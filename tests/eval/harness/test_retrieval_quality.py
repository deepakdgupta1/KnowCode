"""Pytest entry point for retrieval quality evaluation.

Loads ``tests/eval/golden/golden_v1.0.json``, runs KnowCode retrieval against
each query, scores the results, and asserts baseline thresholds.  Also writes
a committed baseline artifact when run with ``--save-baseline``.

Usage
-----
Run against the current HEAD::

    pytest tests/eval/harness/test_retrieval_quality.py -v

Save a new baseline (must match the golden SHA)::

    pytest tests/eval/harness/test_retrieval_quality.py --save-baseline

Skip if the golden dataset has not been built yet::

    pytest tests/eval/harness/test_retrieval_quality.py  # auto-skips

Schema references: docs/GOLDEN_DATASET_PIPELINE.md §10.1 (Step I), §11.

IMPORTANT — SHA guard (§12)
---------------------------
``golden_v1.0.meta.json`` records the git commit SHA at which the golden dataset
was built.  If the harness detects a SHA mismatch it will fail with a clear
message.  Pass ``--allow-drift`` to bypass (for local exploration only — never
in CI).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.eval.harness import calibration as cal
from tests.eval.harness import scorer

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HARNESS_DIR = Path(__file__).parent
_EVAL_DIR = _HARNESS_DIR.parent
_GOLDEN_DIR = _EVAL_DIR / "golden"
_GOLDEN_DATASET = _GOLDEN_DIR / "golden_v1.0.json"
_GOLDEN_META = _GOLDEN_DIR / "golden_v1.0.meta.json"
_BASELINE_PATH = _GOLDEN_DIR / "baseline_v1.0.json"



# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def golden_records() -> list[dict[str, Any]]:
    """Load and return all records from the golden dataset.

    The entire test session is skipped if the dataset file does not exist yet.
    """
    if not _GOLDEN_DATASET.exists():
        pytest.skip(
            f"Golden dataset not found at {_GOLDEN_DATASET}. "
            "Run the pipeline (docs/GOLDEN_DATASET_PIPELINE.md §10.1) first."
        )
    with _GOLDEN_DATASET.open() as fh:
        records: list[dict[str, Any]] = json.load(fh)
    assert records, "golden_v1.0.json is empty"
    return records


@pytest.fixture(scope="session")
def knowcode_service():  # type: ignore[return]
    """Return an initialised KnowCodeService pointed at the repo root.

    The test session is skipped if the knowledge store artefact is absent
    (i.e. ``knowcode analyze`` has not been run).
    """
    try:
        from knowcode.service import KnowCodeService
    except ImportError:
        pytest.skip("knowcode package not importable — check your virtualenv.")

    repo_root = Path(__file__).parents[3]  # …/KnowCode/
    service = KnowCodeService(store_path=repo_root)

    # Probe: a missing store raises MissingKnowledgeStoreError on first access.
    try:
        from knowcode.errors import MissingKnowledgeStoreError
        _ = service.store  # noqa: F841  — triggers the lazy load
    except Exception as exc:
        pytest.skip(f"KnowCode knowledge store not ready: {exc}")

    return service


@pytest.fixture(scope="session")
def sha_guard(request: pytest.FixtureRequest) -> None:
    """Fail loudly if the current HEAD differs from the golden SHA (§12)."""
    if request.config.getoption("--allow-drift"):
        return

    if not _GOLDEN_META.exists():
        return  # meta not yet written — skip guard

    with _GOLDEN_META.open() as fh:
        meta: dict[str, Any] = json.load(fh)

    recorded_sha: str | None = meta.get("source_sha")
    if not recorded_sha:
        return  # meta exists but SHA not populated yet

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        current_sha = result.stdout.strip()
    except subprocess.CalledProcessError:
        return  # not in a git repo — skip guard

    if current_sha != recorded_sha:
        pytest.fail(
            f"HEAD SHA ({current_sha[:12]}) does not match the SHA recorded in "
            f"golden_v1.0.meta.json ({recorded_sha[:12]}). "
            "Line ranges in the golden dataset may be stale. "
            "Re-run the pipeline or pass --allow-drift to bypass."
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_retrieval(service: Any, query_text: str) -> dict[str, Any]:
    """Call KnowCode retrieval and return the result dict."""
    return service.retrieve_context_for_query(
        query=query_text,
        limit_entities=10,   # generous upper bound for recall@10
        max_tokens=8000,
        # Short-term eval fix: scorer needs selected_entities, which are only
        # exposed by the diagnostic response today.
        verbosity="diagnostic",
    )


def _collect_scores(
    golden_records: list[dict[str, Any]],
    service: Any,
) -> list[dict[str, Any]]:
    """Run retrieval + scoring for every golden record and return scores."""
    scores: list[dict[str, Any]] = []
    for record in golden_records:
        retrieval = _run_retrieval(service, record["query_text"])
        score = scorer.score_record(golden=record, retrieval_result=retrieval)
        scores.append(score)
    return scores


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.eval
class TestRetrievalQuality:
    """Retrieval metric tests against the golden dataset.

    All tests in this class share a single retrieval pass over the dataset
    (via the ``all_scores`` session-scoped fixture defined inside the class).
    """

    @pytest.fixture(scope="class")
    def all_scores(
        self,
        golden_records: list[dict[str, Any]],
        knowcode_service: Any,
        sha_guard: None,
        request: pytest.FixtureRequest,
    ) -> list[dict[str, Any]]:
        """Run the full retrieval pass once and cache the scores."""
        scores = _collect_scores(golden_records, knowcode_service)

        if request.config.getoption("--save-baseline"):
            summary = scorer.aggregate(scores)
            baseline = {"scores": scores, "summary": summary}
            _BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _BASELINE_PATH.open("w") as fh:
                json.dump(baseline, fh, indent=2)
            print(f"\nBaseline written to {_BASELINE_PATH}")

        return scores

    @pytest.fixture(scope="class")
    def summary(self, all_scores: list[dict[str, Any]]) -> dict[str, Any]:
        return scorer.aggregate(all_scores)

    # -- Structural retrieval metrics ----------------------------------------

    def test_mean_mrr_above_floor(self, summary: dict[str, Any]) -> None:
        """Mean MRR across all queries must be >= 0.4.

        A well-functioning semantic retrieval system should rank a relevant
        entity first or second on average.  MRR < 0.4 indicates the index or
        query classifier is fundamentally broken.
        """
        assert summary["mean_mrr"] >= 0.4, (
            f"mean_mrr={summary['mean_mrr']:.3f} is below floor of 0.40"
        )

    def test_mean_recall_at_10_above_floor(self, summary: dict[str, Any]) -> None:
        """Mean recall@10 must be >= 0.5.

        With 10 retrieved entities and ground-truth sets of 1–4 entities, a
        recall@10 of 0.5 is a weak lower bound.  Healthy retrieval should
        exceed this substantially.
        """
        assert summary["mean_recall_at_10"] >= 0.5, (
            f"mean_recall_at_10={summary['mean_recall_at_10']:.3f} is below floor of 0.50"
        )

    def test_mean_file_coverage_at_5_above_floor(self, summary: dict[str, Any]) -> None:
        """Mean file_coverage@5 must be >= 0.5."""
        assert summary["mean_file_coverage_at_5"] >= 0.5, (
            f"mean_file_coverage_at_5={summary['mean_file_coverage_at_5']:.3f} "
            "is below floor of 0.50"
        )

    def test_easy_queries_precision_at_1(self, all_scores: list[dict[str, Any]]) -> None:
        """Every EASY query must have precision@1 == 1.0.

        Easy queries have single-entity ground truth.  Failing to rank the
        correct entity at rank 1 for easy queries indicates a critical
        retrieval regression (§7.2, §8).
        """
        easy_scores = [s for s in all_scores if s["difficulty"] == "easy"]
        if not easy_scores:
            pytest.skip("No easy queries in golden dataset")
        failing = [s["query_id"] for s in easy_scores if s["precision_at_1"] < 1.0]
        assert not failing, (
            f"Easy queries with precision@1 < 1.0: {failing}\n"
            "This is a critical regression — easy queries must always surface "
            "the correct entity at rank 1."
        )

    # -- Sufficiency score / routing -----------------------------------------

    def test_sufficiency_calibration_curve_published(
        self,
        all_scores: list[dict[str, Any]],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Build and print the calibration curve; the test always passes.

        The calibration curve is a finding, not a pass/fail gate.  Even a
        badly-calibrated score is useful information (§11).  The test exists
        to ensure the curve is computed and visible in CI output.
        """
        curve = cal.build_calibration_curve(all_scores)
        if curve["judged_count"] == 0:
            pytest.skip(
                "No records have a populated 'correct' field — "
                "wire up the LLM narrative judge (scorer.score_narrative) first."
            )
        diagnosis = cal.diagnose_calibration(curve)
        with capsys.disabled():
            print("\n--- Sufficiency Calibration Curve ---")
            for b in curve["bins"]:
                bar_width = int((b["fraction_correct"] or 0) * 20)
                bar = "#" * bar_width
                print(
                    f"  [{b['bin_lower']:.2f}, {b['bin_upper']:.2f})  "
                    f"n={b['count']:3d}  "
                    f"pred={b['mean_predicted'] or 0:.3f}  "
                    f"acc={b['fraction_correct'] or 0:.3f}  {bar}"
                )
            print(f"  ECE={curve['ece']:.4f}  diagnosis={diagnosis}")
            print("-------------------------------------")

    def test_answer_correctness_at_0_8_published(
        self,
        all_scores: list[dict[str, Any]],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Compute and print answer_correctness@0.8; the test always passes.

        Like the calibration curve, this is a *measurement* not a gate.
        The gate in ``test_routing_claim`` uses a configurable floor that
        should be raised once the baseline is established.
        """
        result = cal.answer_correctness_at_threshold(all_scores, threshold=0.8)
        if result["judged_count"] == 0:
            pytest.skip(
                "No records have a populated 'correct' field — "
                "wire up the LLM narrative judge (scorer.score_narrative) first."
            )
        with capsys.disabled():
            print("\n--- answer_correctness@0.8 ---")
            print(f"  routed_count  = {result['routed_count']}")
            print(f"  judged_count  = {result['judged_count']}")
            print(f"  correct_count = {result['correct_count']}")
            ac = result["answer_correctness"]
            print(f"  answer_correctness = {ac:.3f}" if ac is not None else "  answer_correctness = N/A (no judged records)")
            print("-------------------------------")

    # -- Per-task-type checks ------------------------------------------------

    def test_locate_queries_mrr(self, all_scores: list[dict[str, Any]]) -> None:
        """LOCATE queries should have mean MRR >= 0.6.

        LOCATE is the task type where KnowCode's local-first mode is strongest
        (§7.1 rationale).  A MRR below 0.6 on LOCATE is a product-critical signal.
        """
        locate = [s for s in all_scores if s["task_type"] == "locate"]
        if not locate:
            pytest.skip("No LOCATE queries in golden dataset")
        mean_mrr = sum(s["mrr"] for s in locate) / len(locate)
        assert mean_mrr >= 0.6, (
            f"LOCATE mean_mrr={mean_mrr:.3f} is below floor of 0.60"
        )

    def test_no_task_type_has_zero_recall(self, summary: dict[str, Any]) -> None:
        """No task type should have mean recall@10 == 0.0.

        Zero recall on any task type indicates a complete retrieval failure
        (likely an index or classifier bug), not a quality regression.
        """
        by_task = summary.get("by_task_type", {})
        zero_recall = [
            task for task, s in by_task.items()
            if s.get("mean_recall_at_10", 1.0) == 0.0 and s.get("n", 0) > 0
        ]
        assert not zero_recall, (
            f"Task types with recall@10 == 0: {zero_recall}"
        )
