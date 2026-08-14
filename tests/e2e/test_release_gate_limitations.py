"""Step 22 release gate: pinned, documented limitations.

The release gate is honest about what the assembled system does *not* yet do.
Four items were flagged during Steps 12-19 for Step 22 to resolve. None is a
newly introduced defect and none is silently patched here; each is pinned by a
test asserting the *current* behavior so it cannot change unnoticed, and each is
documented in ``docs/architecture/hardening-contracts.md`` and the release
checklist. Fixing any of them is product work (a new scoped step), not a
weakened assertion in this gate.

The four:

1. **Directory-level watch events are ignored.** A subtree rename that arrives
   only as a directory event is not expanded into its files; the next build
   corrects it. Per-file events cover the common editor and VCS cases.
2. **An empty LanceDB index cannot be saved as a loadable artifact.** The
   backend residual is contained at the caller: ``flush()`` never writes an
   index nobody built.
3. **A watched edit refreshes retrieval but not the knowledge graph.** Chunk and
   vector membership follow a watched edit; ``knowledge.db`` does not, so graph
   queries go stale until a rebuild. ``knowcode doctor`` warns, and retrieval —
   the primary entry point — stays fresh.
4. **The prompt boundary adds bounded instruction/envelope overhead.** It is a
   small, declared, proportional cost, accepted for the security property it
   buys and because the local-first router keeps most queries off the provider.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

from knowcode.config import AppConfig
from knowcode.data_models import TaskType
from knowcode.doctor import run_doctor
from knowcode.indexing.background_indexer import BackgroundIndexer
from knowcode.indexing.monitor import IndexingHandler
from knowcode.llm.prompt_contract import (
    UNTRUSTED_DATA_POLICY,
    build_system_instruction,
    build_untrusted_payload,
)
from knowcode.service import KnowCodeService
from knowcode.storage.vector_backends import create_vector_store

from tests.helpers.adversarial_repo import build_adversarial_repo


# ----------------------------------------------------------------------
# 1. Directory-level watch events are ignored by design (Step 16 discovery)
# ----------------------------------------------------------------------


class _RecordingWorker:
    """Records the queue calls the handler makes without doing any indexing."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def queue_file(self, path: Path) -> None:
        self.calls.append(("file", str(path)))

    def queue_removal(self, path: Path) -> None:
        self.calls.append(("removal", str(path)))

    def queue_move(self, old_path: Path, new_path: Path) -> None:
        self.calls.append(("move", str(old_path), str(new_path)))


def test_directory_level_watch_events_are_ignored_by_design(tmp_path: Path) -> None:
    """A directory move/delete queues nothing; the subtree is not re-keyed.

    This is the documented limitation: on a platform that reports only the
    directory event for a subtree rename, the files beneath keep their identity
    until the next build. A per-file event (the common case) is the positive
    control below.
    """
    (tmp_path / "sub").mkdir()
    worker = _RecordingWorker()
    handler = IndexingHandler(worker, tmp_path)  # type: ignore[arg-type]

    handler.on_deleted(types.SimpleNamespace(is_directory=True, src_path=str(tmp_path / "sub")))
    handler.on_moved(
        types.SimpleNamespace(
            is_directory=True,
            src_path=str(tmp_path / "sub"),
            dest_path=str(tmp_path / "renamed"),
        )
    )

    assert worker.calls == [], "a directory-level event was expanded into work"


def test_per_file_watch_events_are_queued(tmp_path: Path) -> None:
    """Positive control: an indexable file event is queued (the common case)."""
    source = tmp_path / "m.py"
    source.write_text("def f():\n    return 1\n", encoding="utf-8")
    worker = _RecordingWorker()
    handler = IndexingHandler(worker, tmp_path)  # type: ignore[arg-type]

    handler.on_created(types.SimpleNamespace(is_directory=False, src_path=str(source)))

    assert worker.calls == [("file", str(source))]


# ----------------------------------------------------------------------
# 2. An empty LanceDB index is never written as a broken artifact
#    (Step 12 backend residual, contained by the Step 17 caller guard)
# ----------------------------------------------------------------------


def test_flush_never_writes_an_index_nobody_built(tmp_path: Path) -> None:
    """The caller guard keeps the empty-LanceDB backend residual unreachable.

    ``LanceDBVectorStore.save()`` on a brand-new empty store still writes a
    metadata envelope that fails closed on the next load (the residual backend
    defect). ``KnowCodeService._has_index_state`` is what keeps ``flush()`` from
    ever calling it: an empty store with no prior artifact persists nothing.
    """
    store = create_vector_store("lancedb", dimension=8, index_dir=tmp_path)
    try:
        assert store.count() == 0
        indexer = types.SimpleNamespace(vector_store=store)

        # Empty store, no prior artifact → nothing is written.
        assert KnowCodeService._has_index_state(indexer, tmp_path) is False

        # Empty store *beside* a prior artifact → still written, because that
        # records a full deletion rather than an index nobody built.
        (tmp_path / "vectors.json").write_text("{}", encoding="utf-8")
        assert KnowCodeService._has_index_state(indexer, tmp_path) is True
    finally:
        store.close()


# ----------------------------------------------------------------------
# 3. A watched edit refreshes retrieval but not the graph (Step 18b discovery)
# ----------------------------------------------------------------------


def test_a_watched_edit_refreshes_retrieval_but_the_graph_needs_a_rebuild(
    tmp_path: Path,
) -> None:
    """Chunks/vectors follow a watched edit; ``knowledge.db`` does not.

    This is the accepted watch-mode limitation for this release. The retrieval
    path (the MCP/CLI entry point) is fresh immediately; the graph query path is
    stale until a rebuild; ``knowcode doctor`` warns via ``store_stale_source_changed``;
    and a full ``analyze()`` refreshes the graph. Fixing it needs incremental
    ``GraphBuilder`` work and is scoped as future product work, not patched here.
    """
    repo = build_adversarial_repo(tmp_path)
    config = AppConfig.default()
    service = KnowCodeService(store_path=repo.output, app_config=config)
    service.analyze(directory=repo.source, output=repo.output)

    added = repo.source / "app" / "gamma.py"
    added.write_text(
        "def gamma_handler(order):\n"
        '    """Gamma handler with a unique token zqxwce."""\n'
        "    return order\n",
        encoding="utf-8",
    )
    worker = BackgroundIndexer(service.watch_writer())
    assert worker.start() is True
    try:
        worker.queue_file(added)
        report = worker.stop(timeout=60)
    finally:
        if worker.is_running:
            worker.stop(timeout=60)
    assert report.completed

    # Retrieval (chunks/vectors) is fresh: the new symbol is findable.
    chunk_hits = service.get_search_engine().search(
        "gamma handler unique token zqxwce", limit=5
    )
    assert any("gamma.py" in chunk.entity_id for chunk in chunk_hits), (
        "retrieval did not pick up the watched edit"
    )
    # The graph (knowledge.db) is stale: the symbol is not yet a graph entity.
    assert service.search("gamma_handler") == [], (
        "the graph unexpectedly followed the watched edit"
    )
    service.close()

    # doctor reports the staleness rather than hiding it, and the generation is
    # otherwise valid.
    report_ = run_doctor(store_path=repo.output)
    freshness = next(c for c in report_.checks if c.name == "Freshness")
    assert freshness.status == "warn"
    assert "store_stale_source_changed" in freshness.message
    for name in ("Index generation", "Knowledge store", "Semantic index"):
        check = next(c for c in report_.checks if c.name == name)
        assert check.status == "pass", check.message

    # A full rebuild is the documented remedy: the graph catches up.
    rebuilt = KnowCodeService(store_path=repo.output, app_config=config)
    rebuilt.analyze(directory=repo.source, output=repo.output)
    try:
        assert rebuilt.search("gamma_handler"), "a rebuild did not refresh the graph"
    finally:
        rebuilt.close()


# ----------------------------------------------------------------------
# 4. The prompt boundary overhead is bounded and declared (Step 19 discovery)
# ----------------------------------------------------------------------


def test_prompt_boundary_overhead_is_bounded_and_declared() -> None:
    """The instruction policy and envelope overhead are small, fixed, and stated.

    Step 19 traded a few hundred instruction tokens and a per-field envelope for
    the instruction/data separation. The cost is accepted and bounded here rather
    than guessed at; retrieval-quality is measured offline by ``tests/eval``, and
    the local-first router keeps most queries off any provider entirely.
    """
    # The fixed policy is a few hundred words, not an unbounded preamble.
    assert len(UNTRUSTED_DATA_POLICY) < 2_000
    assert len(build_system_instruction(TaskType.GENERAL)) < 4_000

    # The envelope overhead is a small constant, and content cost is proportional
    # — the payload is not multiplied, wrapped repeatedly, or re-encoded.
    empty = build_untrusted_payload(question="", context="")
    overhead = len(empty)
    assert overhead < 400

    body = "x" * 5_000
    payload = build_untrusted_payload(question="q", context=body)
    assert len(payload) <= overhead + len("q") + len(body) + 64

    # The declared per-field length is what lets a reader bound the data channel.
    envelope = json.loads(payload)
    assert envelope["repository_context"]["chars"] == len(body)
    assert envelope["question"]["chars"] == 1
