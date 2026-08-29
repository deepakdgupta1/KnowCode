"""The service emits one privacy-reviewed query event (Step 20).

Before this step ``retrieve_context_for_query`` logged ``{"query": <raw text>,
...}`` at the store root while the orchestrator nested inside it logged a
second record — carrying the raw query *again*, plus the selected entity ids —
into whichever path ``_assert_store_exists()`` returned. After Step 14 that is
``knowcode_index/generations/<id>/knowledge.db``, so every retrieval wrote a
log file *inside a published generation*: a directory ADR 4 declares immutable,
which retirement deletes and which no documented deletion path can find.

These cases pin the replacement: exactly one counted ``query`` event, at the
store root, containing no query text and no entity ids.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from knowcode import telemetry, telemetry_files
from knowcode.config import AppConfig
from knowcode.data_models import CodeChunk, TaskType
from knowcode.indexing import generations
from knowcode.retrieval.search_engine import ScoredChunk
from knowcode.service import KnowCodeService

QUERY = "how does place_order validate sk-ant-api03-Ab3dEfGh1jKlMn0pQrStUvWx?"


def _publish_stub_generation(index_root: Path) -> None:
    """Publish an empty but structurally valid full generation."""
    import sqlite3

    with generations.staged_generation(index_root) as staging:
        conn = sqlite3.connect(str(staging.path / "knowledge.db"))
        conn.execute("CREATE TABLE entities (entity_id TEXT PRIMARY KEY)")
        conn.commit()
        conn.close()
        conn = sqlite3.connect(str(staging.path / "chunks.db"))
        conn.execute("CREATE TABLE chunks (chunk_id TEXT PRIMARY KEY, embedding BLOB)")
        conn.commit()
        conn.close()
        (staging.path / "index_manifest.json").write_text("{}", encoding="utf-8")

        manifest = generations.build_manifest(
            staging.path,
            generation_id=staging.generation_id,
            kind=generations.KIND_FULL,
            entity_ids=[],
            relationship_count=0,
            chunk_ids=[],
            vector_count=0,
            embedding={},
            vector={"backend": "faiss", "dimension": 8},
        )
        generations.publish_generation(index_root, staging.path, manifest)
        staging.published = True


class StubSearchEngine:
    def __init__(self, scored: list[ScoredChunk]) -> None:
        self._scored = scored

    def search_scored(self, _query: str, **_kwargs: Any) -> list[ScoredChunk]:
        return self._scored


class StubService(KnowCodeService):
    """A real service with retrieval stubbed at the search-engine boundary."""

    def __init__(self, store_path: Path, scored: list[ScoredChunk]) -> None:
        super().__init__(store_path=store_path, app_config=AppConfig.default())
        self._engine = StubSearchEngine(scored)

    def get_search_engine(self, _index_path: Path | None = None) -> StubSearchEngine:  # type: ignore[override]
        return self._engine

    def get_exact_query_engine(
        self, _index_path: Path | None = None
    ) -> StubSearchEngine:  # type: ignore[override]
        return self._engine

    def get_context(  # type: ignore[override]
        self,
        target: str,
        max_tokens: int = 2000,
        task_type: TaskType | None = None,
        summarize: bool = False,
        is_stale: bool = False,
    ) -> dict[str, Any]:
        return {
            "entity_id": target,
            "context_text": f"CTX:{target}",
            "total_tokens": 10,
            "truncated": False,
            "included_entities": [target],
            "task_type": (task_type or TaskType.GENERAL).value,
            "sufficiency_score": 0.9,
        }

    def _validate_index_compatibility(self, _index_path: Path) -> None:
        return


@pytest.fixture
def service(tmp_path: Path) -> Any:
    (tmp_path / "knowcode_knowledge.json").write_text(
        json.dumps({"metadata": {}, "entities": {}, "relationships": []}),
        encoding="utf-8",
    )
    _publish_stub_generation(tmp_path / "knowcode_index")
    entity = "/private/var/repo/src/orders.py::place_order"
    chunk = CodeChunk(id="c1", entity_id=entity, content="one", tokens=["one"])
    built = StubService(
        tmp_path, [ScoredChunk(chunk=chunk, score=0.9, source="retrieved")]
    )
    try:
        yield built
    finally:
        built.close()
        telemetry.shutdown_telemetry(timeout=5.0)


def _records(store: Path) -> list[dict[str, Any]]:
    log = telemetry_files.telemetry_path(store)
    if not log.exists():
        return []
    return [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_one_retrieval_emits_exactly_one_counted_query_event(
    service: KnowCodeService, tmp_path: Path
) -> None:
    service.retrieve_context_for_query(QUERY)

    records = _records(tmp_path)
    assert [record["event_type"] for record in records] == ["query"]
    assert telemetry.get_telemetry_summary(tmp_path)["total_queries"] == 1


def test_the_query_event_carries_metadata_not_the_query(
    service: KnowCodeService, tmp_path: Path
) -> None:
    service.retrieve_context_for_query(QUERY, verbosity="standard")

    record = _records(tmp_path)[0]
    assert record["query_chars"] == len(QUERY)
    assert record["verbosity"] == "standard"
    assert record["retrieval_mode"] == "semantic"
    assert record["task_type"]
    assert record["sufficiency_score"] == 0.9
    assert record["local_or_escalated"] in {"local", "escalated"}
    assert record["is_stale"] is False
    assert record["selected_entity_count"] == 1
    assert record["entry_point"] == "service"


def test_no_query_text_or_entity_path_reaches_any_file(
    service: KnowCodeService, tmp_path: Path
) -> None:
    """Entity ids are absolute source paths; they are repository identity."""
    service.retrieve_context_for_query(QUERY)

    payload = "".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(tmp_path.rglob("*.jsonl*"))
    )
    assert "place_order" not in payload
    assert "sk-ant-api03" not in payload
    assert "src/orders.py" not in payload


def test_telemetry_stays_out_of_the_published_generation(
    service: KnowCodeService, tmp_path: Path
) -> None:
    service.retrieve_context_for_query(QUERY)

    assert list((tmp_path / "knowcode_index").rglob("*.jsonl")) == []
    assert telemetry_files.telemetry_path(tmp_path).exists()


def test_three_retrievals_count_as_three_queries(
    service: KnowCodeService, tmp_path: Path
) -> None:
    """Nesting collapses; three independent calls are three logical queries."""
    for _ in range(3):
        service.retrieve_context_for_query(QUERY)

    assert telemetry.get_telemetry_summary(tmp_path)["total_queries"] == 3
