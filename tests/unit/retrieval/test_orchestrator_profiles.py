"""The query path's projection honors the response profiles (P3-2).

``verbosity='minimal'`` is the summary-first profile: the synthesizer is told
to summarize, and the response says what it omitted. When the resolved task
type is one that needs the code — debugging, review — the profile includes
source at minimal, and the response must stop claiming it omitted some.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from knowcode.data_models import CodeChunk, TaskType
from knowcode.retrieval.orchestrator import RetrievalOrchestrator
from knowcode.retrieval.search_engine import ScoredChunk


class _StubEngine:
    def search_scored(
        self, query: str, limit: int = 10, expand_deps: bool = True
    ) -> list[ScoredChunk]:
        chunk = CodeChunk(
            id="/repo/alpha.py::alpha::0",
            entity_id="/repo/alpha.py::alpha",
            content="def alpha(): pass",
        )
        return [ScoredChunk(chunk=chunk, score=0.9, source="retrieved")]


class _ProfileRecordingService:
    """Records the ``summarize`` flag the orchestrator hands to get_context."""

    def __init__(self, store_path: Path) -> None:
        self.store_path = store_path
        self.summarize_flags: list[bool] = []

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
        self.summarize_flags.append(summarize)
        return {
            "entity_id": entity_id,
            "task_type": "general",
            "context_text": "CONTEXT",
            "total_tokens": 10,
            "truncated": False,
            "sufficiency_score": 0.5,
        }


def _retrieve(tmp_path: Path, *, task_type: TaskType, verbosity: str) -> dict[str, Any]:
    service = _ProfileRecordingService(tmp_path)
    orchestrator = RetrievalOrchestrator(service)
    result = orchestrator.retrieve_context_for_query(
        query="alpha",
        max_tokens=1500,
        task_type=task_type,
        limit_entities=1,
        verbosity=verbosity,
    )
    result["_flags"] = service.summarize_flags
    return result


def test_minimal_summarizes_for_an_exploratory_task(tmp_path: Path) -> None:
    result = _retrieve(tmp_path, task_type=TaskType.EXPLAIN, verbosity="minimal")

    assert result["_flags"] == [True]
    assert result["reduction_summary"]["omitted_raw_source_count"] == 1


def test_minimal_includes_source_for_debug(tmp_path: Path) -> None:
    """A debugging query needs the code even at the default verbosity."""
    result = _retrieve(tmp_path, task_type=TaskType.DEBUG, verbosity="minimal")

    assert result["_flags"] == [False]
    # The projection must not claim an omission that did not happen.
    assert "reduction_summary" not in result
    assert result["source_included"] is True


def test_minimal_includes_source_for_review(tmp_path: Path) -> None:
    result = _retrieve(tmp_path, task_type=TaskType.REVIEW, verbosity="minimal")

    assert result["_flags"] == [False]
    assert "reduction_summary" not in result


def test_standard_never_summarizes(tmp_path: Path) -> None:
    result = _retrieve(tmp_path, task_type=TaskType.GENERAL, verbosity="standard")

    assert result["_flags"] == [False]
    assert "reduction_summary" not in result


#: Fields the MCP contract's minimal projection promises to withhold. An agent
#: that reads one of these under ``verbosity='minimal'`` is relying on a field
#: the contract does not send.
INTERNAL_FIELDS = frozenset(
    {
        "query",
        "task_type",
        "task_confidence",
        "retrieval_mode",
        "max_tokens",
        "truncated",
        "evidence",
        "selected_entities",
    }
)


def _projection(tmp_path: Path, task_type: TaskType) -> frozenset[str]:
    result = _retrieve(tmp_path, task_type=task_type, verbosity="minimal")
    return frozenset(result) - {"_flags"}


def test_minimal_projection_is_exactly_the_contracted_fields(tmp_path: Path) -> None:
    """The release checklist's "minimal projection" line, pinned.

    The projection is built as an allowlist, so nothing leaks by accident
    today. Rewriting it as a denylist over the full response would invert that
    guarantee silently, which is what this exact-set assertion catches.
    """
    assert _projection(tmp_path, TaskType.EXPLAIN) == {
        "context_text",
        "sufficiency_score",
        "total_tokens",
        "reduction_summary",
    }
    assert _projection(tmp_path, TaskType.DEBUG) == {
        "context_text",
        "sufficiency_score",
        "total_tokens",
        "source_included",
    }


def test_minimal_never_carries_internal_metadata(tmp_path: Path) -> None:
    """Stated as the contract states it, so a new internal field is caught
    even if someone widens the allowlist above without thinking."""
    for task_type in (TaskType.EXPLAIN, TaskType.DEBUG, TaskType.REVIEW):
        assert not (_projection(tmp_path, task_type) & INTERNAL_FIELDS), task_type
