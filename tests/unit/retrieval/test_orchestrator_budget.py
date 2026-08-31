"""The retrieval budget is a cap the orchestrator cannot exceed (BL-29)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from knowcode.data_models import CodeChunk, TaskType
from knowcode.retrieval.orchestrator import RetrievalOrchestrator
from knowcode.retrieval.search_engine import ScoredChunk


class _StubEngine:
    """Three ranked hits, one per entity the budget will be split across."""

    def search_scored(
        self,
        query: str,
        limit: int = 10,
        expand_deps: bool = True,
    ) -> list[ScoredChunk]:
        return [
            ScoredChunk(
                CodeChunk(
                    f"/repo/{name}.py::{name}::0", f"/repo/{name}.py::{name}", "pass"
                ),
                0.9,
                "retrieved",
            )
            for name in ("a", "b", "c")
        ]


class _BudgetRecordingService:
    """Records the per-bundle budgets the orchestrator hands to get_context.

    Each bundle reports exactly the allowance it was given, which is the
    worst case the budget has to hold against: a synthesizer that fills
    whatever it is asked for.
    """

    def __init__(self, store_path: Path) -> None:
        self.store_path = store_path
        self.budgets: list[int] = []

    def _assert_store_exists(self) -> Path:
        return self.store_path

    def _assert_index_exists(self) -> Path:
        return self.store_path

    def _validate_index_compatibility(self, index_path: Path) -> None:
        return None

    def get_search_engine(self, index_path: Optional[Path] = None) -> _StubEngine:
        return _StubEngine()

    def search(self, pattern: str) -> list[dict[str, Any]]:
        return []

    def _extract_query_keywords(self, query: str) -> list[str]:
        return []

    def get_context(
        self,
        entity_id: str,
        max_tokens: int = 2000,
        task_type: Optional[TaskType] = None,
        summarize: bool = False,
        is_stale: bool = False,
    ) -> dict[str, Any]:
        self.budgets.append(max_tokens)
        return {
            "entity_id": entity_id,
            "task_type": "general",
            "context_text": "x" * max_tokens,
            "total_tokens": max_tokens,
            "truncated": False,
            "sufficiency_score": 0.5,
        }


def test_a_small_budget_is_split_not_overshot(tmp_path: Path) -> None:
    """300 tokens over 3 entities is 100 each, never three 200-token bundles.

    The old 200-token floor bought this request 600 tokens and reported
    ``total_tokens=600`` against ``max_tokens=300``.
    """
    service = _BudgetRecordingService(tmp_path)

    result = RetrievalOrchestrator(service).retrieve_context_for_query(
        "query", max_tokens=300, limit_entities=3
    )

    assert service.budgets == [100, 100, 100]
    assert result["total_tokens"] == 300


def test_an_explicit_per_entity_budget_is_capped_by_the_total(
    tmp_path: Path,
) -> None:
    """A caller-supplied per-entity allowance cannot outbid the total."""
    service = _BudgetRecordingService(tmp_path)

    result = RetrievalOrchestrator(service).retrieve_context_for_query(
        "query",
        max_tokens=300,
        limit_entities=3,
        per_entity_max_tokens=500,
        verbosity="standard",
    )

    assert service.budgets == [300]
    assert result["total_tokens"] == 300
    assert result["truncated"] is True


def test_a_generous_budget_keeps_the_even_split(tmp_path: Path) -> None:
    """Nothing about the fix thins ordinary budgets: 3000 over 3 stays 1000."""
    service = _BudgetRecordingService(tmp_path)

    result = RetrievalOrchestrator(service).retrieve_context_for_query(
        "query", max_tokens=3000, limit_entities=3, verbosity="standard"
    )

    assert service.budgets == [1000, 1000, 1000]
    assert result["total_tokens"] == 3000
    assert result["truncated"] is False
