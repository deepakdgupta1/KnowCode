"""Step 22 release gate: the assembled pipeline over an adversarial repository.

Steps 02-18b each proved one layer over a trivial tree. This gate proves the
*assembled* system — scan → parse → graph → chunk → embed → publish → watch →
retrieve — over one repository that carries every C2 construct, a quoted/Unicode
path, and hostile code comments, for both vector backends.

Nothing here re-tests a primitive in isolation. Each case ties several layers
together on the hard input:

* the merged graph has no invalid or dangling endpoint and every reviewed C2
  defect is extracted correctly;
* a full build publishes one generation whose knowledge/chunk/vector/manifest
  counts and checksums agree;
* an apostrophe in a real path flows into the chunk id and the LanceDB predicate
  without reaching the filter grammar;
* a watch session's add/modify/duplicate/delete/move batch publishes a coherent
  generation a restart and ``knowcode doctor`` both accept;
* a failure before any publication boundary preserves the previous generation;
* concurrent readers never observe a mixed generation across a rebuild.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from knowcode.config import AppConfig
from knowcode.doctor import run_doctor
from knowcode.indexing import generations
from knowcode.indexing.background_indexer import BackgroundIndexer
from knowcode.indexing.generations import resolve_current_generation
from knowcode.indexing.graph_builder import GraphBuilder
from knowcode.indexing.indexer import Indexer
from knowcode.service import KnowCodeService
from knowcode.utils.entity_identity import EndpointKind, classify_endpoint_id

from tests.helpers.adversarial_repo import AdversarialRepo, build_adversarial_repo
from tests.helpers.graph_gate import assert_no_dangling_endpoints


@pytest.fixture(params=["faiss", "lancedb"])
def backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _config(backend: str) -> AppConfig:
    config = AppConfig.default()
    config.vector_backend = backend
    return config


def _service(repo: AdversarialRepo, backend: str) -> KnowCodeService:
    return KnowCodeService(store_path=repo.output, app_config=_config(backend))


def _publish(repo: AdversarialRepo, backend: str) -> str:
    builder = _service(repo, backend)
    try:
        stats = builder.analyze(directory=repo.source, output=repo.output)
    finally:
        builder.close()
    assert stats["published"] is True, stats
    return str(stats["generation_id"])


def _qualified_names(builder: GraphBuilder) -> set[str]:
    return {entity.qualified_name for entity in builder.entities.values()}


def _edge_reprs(builder: GraphBuilder) -> set[tuple[str, str, str]]:
    """A scan-order-independent view of edges as (source_qn, kind, target_repr).

    The target is the entity's qualified name when internal, else the trailing
    ``::``-separated symbol of the unresolved/external id.
    """
    reprs: set[tuple[str, str, str]] = set()
    for relationship in builder.relationships:
        source = builder.entities.get(relationship.source_id)
        if source is None:
            continue
        target_entity = builder.entities.get(relationship.target_id)
        if target_entity is not None:
            target_repr = target_entity.qualified_name
        else:
            target_repr = relationship.target_id.split("::")[-1]
        reprs.add((source.qualified_name, relationship.kind.value, target_repr))
    return reprs


def _join(threads: list[threading.Thread], timeout: float = 60.0) -> None:
    for thread in threads:
        thread.join(timeout=timeout)
        assert not thread.is_alive(), "a worker thread did not finish in time"


# ----------------------------------------------------------------------
# Graph integrity and exact C2 extraction (parser-independent of backend)
# ----------------------------------------------------------------------


def test_adversarial_merge_has_no_invalid_or_dangling_endpoints(tmp_path: Path) -> None:
    repo = build_adversarial_repo(tmp_path)
    builder = GraphBuilder().build_from_directory(repo.source)

    assert builder.entities, "the adversarial fixture produced no entities"
    assert not builder.errors, f"unexpected parse errors: {builder.errors}"
    assert_no_dangling_endpoints(builder)


def test_adversarial_graph_is_independent_of_scan_order(tmp_path: Path) -> None:
    repo = build_adversarial_repo(tmp_path)
    forward = GraphBuilder().build_from_directory(repo.source)
    files = list(forward.scanned_files)
    assert len(files) >= 5, "the adversarial fixture needs several files"

    reversed_build = GraphBuilder().build_from_files(list(reversed(files)))

    assert set(forward.entities) == set(reversed_build.entities)
    assert _edge_reprs(forward) == _edge_reprs(reversed_build)


def test_every_reviewed_c2_construct_extracts_exactly(tmp_path: Path) -> None:
    """Each C2 defect the review found silently dropped is now present."""
    repo = build_adversarial_repo(tmp_path)
    builder = GraphBuilder().build_from_directory(repo.source)
    qnames = _qualified_names(builder)
    edges = _edge_reprs(builder)

    # C2 TypeScript: every exported declaration form, not just the module.
    assert {
        "svc.User",
        "svc.UserId",
        "svc.Role",
        "svc.Session",
        "svc.load",
        "svc.make",
    } <= qnames

    # C2 JavaScript: `class Widget extends BaseComponent` emits one INHERITS edge
    # to an explicit unresolved (external) base, not nothing.
    assert ("app.Widget", "inherits", "BaseComponent") in edges

    # C2 Python nesting + scope confinement: place owns its own calls, and the
    # nested normalize()'s calls are never attributed to place.
    assert {
        "orders.OrderService",
        "orders.OrderService.place",
        "orders.OrderService.place.normalize",
    } <= qnames
    assert ("orders.OrderService.place", "calls", "orders.validate") in edges
    assert (
        "orders.OrderService.place",
        "calls",
        "orders.OrderService.place.normalize",
    ) in edges
    place_call_targets = {
        t for s, k, t in edges if s == "orders.OrderService.place" and k == "calls"
    }
    assert "strip" not in place_call_targets and "lower" not in place_call_targets, (
        "a nested function's calls leaked into the enclosing scope"
    )

    # C2 Rust impl identity: real internal `implements` edges, no dangling type::/trait::.
    assert ("core.Core", "implements", "core.Render") in edges
    assert ("core.Core.render", "implements", "core.Render") in edges

    # C2 Vue Composition API: template bindings resolve to internal entities.
    assert {"widget.Widget.onClick", "widget.Widget.label"} <= qnames
    assert ("widget.Widget", "references", "widget.Widget.label") in edges


def test_decorated_python_entity_location_includes_its_first_decorator(
    tmp_path: Path,
) -> None:
    """Invariant 5: a decorated declaration's location starts at its decorator."""
    repo = build_adversarial_repo(tmp_path)
    builder = GraphBuilder().build_from_directory(repo.source)

    service = next(
        entity
        for entity in builder.entities.values()
        if entity.qualified_name == "orders.OrderService"
    )
    assert service.metadata.get("decorators") == ["audit"]
    first_line = (service.source_code or "").splitlines()[0]
    assert first_line.strip() == "@audit", (
        f"location did not start at the decorator: {first_line!r}"
    )


# ----------------------------------------------------------------------
# Generation parity on the adversarial repository (both backends)
# ----------------------------------------------------------------------


def test_analyze_publishes_one_coherent_adversarial_generation(
    tmp_path: Path, backend: str
) -> None:
    repo = build_adversarial_repo(tmp_path)
    service = _service(repo, backend)

    stats = service.analyze(directory=repo.source, output=repo.output)

    assert stats["published"] is True, stats
    resolved = resolve_current_generation(repo.output / "knowcode_index")
    assert resolved is not None

    manifest = resolved.manifest
    assert manifest.counts["entities"] == len(
        generations.read_entity_ids(resolved.knowledge_db)
    )
    assert manifest.counts["chunks"] == len(
        generations.read_chunk_ids(resolved.chunks_db)
    )
    assert manifest.counts["chunks"] == manifest.counts["vectors"] > 0

    # Digests and every artifact checksum agree — one committed generation.
    assert (
        generations.validate_generation(
            resolved.path, expected_id=resolved.generation_id, verify_digests=True
        )
        == []
    )
    service.close()


def test_the_apostrophe_path_flows_safely_through_the_vector_store(
    tmp_path: Path, backend: str
) -> None:
    """A SQL-hostile path component reaches the vector store as data, not syntax.

    The exotic module lives under a directory whose name holds an apostrophe, a
    space, and non-ASCII letters. That apostrophe is preserved in the internal
    entity id and therefore in the chunk id; the LanceDB backend must neutralize
    it through the Step 12 digest predicate rather than let it reach the filter.
    """
    repo = build_adversarial_repo(tmp_path)
    service = _service(repo, backend)
    service.analyze(directory=repo.source, output=repo.output)

    resolved = resolve_current_generation(repo.output / "knowcode_index")
    chunk_ids = generations.read_chunk_ids(resolved.chunks_db)
    apostrophe_ids = [identifier for identifier in chunk_ids if "'" in identifier]
    assert apostrophe_ids, "precondition: the exotic path produced an apostrophe id"

    # The exotic module is retrievable, and its embedding round-trips by id.
    engine = service.get_search_engine()
    hits = engine.search("handle an order from the café module", limit=5)
    assert any("eta.py" in chunk.entity_id for chunk in hits), (
        "the module under the quoted/Unicode path was not retrievable"
    )

    indexer = service.get_indexer()
    for identifier in apostrophe_ids:
        assert indexer.vector_store.get_embedding(identifier) is not None, (
            f"the apostrophe-bearing id did not round-trip: {identifier!r}"
        )
    service.close()


# ----------------------------------------------------------------------
# Watch lifecycle: add, modify, duplicate, delete, move (both backends)
# ----------------------------------------------------------------------


def test_watch_lifecycle_add_modify_duplicate_delete_move(
    tmp_path: Path, backend: str
) -> None:
    """One watch session exercises every mutation and lands on a valid generation."""
    repo = build_adversarial_repo(tmp_path)
    service = _service(repo, backend)
    service.analyze(directory=repo.source, output=repo.output)
    base = resolve_current_generation(repo.output / "knowcode_index")
    assert base is not None

    added = repo.source / "app" / "shipping.py"
    moved_from = repo.source / "app" / "orders.py"
    moved_to = repo.source / "app" / "orders_renamed.py"
    deleted = repo.source / "web" / "svc.ts"

    worker = BackgroundIndexer(service.watch_writer())
    assert worker.start() is True
    try:
        # add
        added.write_text(
            "def ship(order: str) -> str:\n    return order\n", encoding="utf-8"
        )
        worker.queue_file(added)
        # modify, twice in a row: rapid duplicate events must coalesce to one commit
        (repo.source / "core" / "core.rs").write_text(
            "pub struct Core { value: i32 }\n"
            "impl Core {\n    pub fn triple(&self) -> i32 { self.value * 3 }\n}\n",
            encoding="utf-8",
        )
        worker.queue_file(repo.source / "core" / "core.rs")
        worker.queue_file(repo.source / "core" / "core.rs")
        # move
        moved_to.write_text(moved_from.read_text(encoding="utf-8"), encoding="utf-8")
        moved_from.unlink()
        worker.queue_move(moved_from, moved_to)
        # delete
        deleted.unlink()
        worker.queue_removal(deleted)

        report = worker.stop(timeout=90)
    finally:
        if worker.is_running:
            worker.stop(timeout=90)

    assert report.completed, report.incomplete_work
    current = service.current_generation()
    assert current is not None
    assert current.generation_id != base.generation_id, (
        "the watch session never published a generation"
    )
    service.close()

    # doctor accepts the resulting generation, and a restart sees the mutations.
    report_ = run_doctor(store_path=repo.output)
    for name in ("Index generation", "Semantic index"):
        check = next(item for item in report_.checks if item.name == name)
        assert check.status != "fail", check.message

    restarted = _service(repo, backend)
    try:
        with restarted.generation_lease() as bundle:
            files = bundle.indexer.chunk_repo.get_all_file_paths()
            chunks = bundle.indexer.chunk_repo.count()
            vectors = bundle.indexer.vector_store.count()
    finally:
        restarted.close()

    assert any("shipping.py" in path for path in files), "the added file is missing"
    assert any("orders_renamed.py" in path for path in files), (
        "the move destination is missing"
    )
    assert not any(path.endswith("app/orders.py") for path in files), (
        "the move source survived"
    )
    assert not any("svc.ts" in path for path in files), "the deleted file survived"
    assert chunks == vectors, "chunk/vector membership split after the watch session"


def test_reindex_leaves_no_stale_dense_ids_or_duplicate_fusion(
    tmp_path: Path, backend: str
) -> None:
    """After a re-index, hybrid results are unique and every dense id resolves."""
    repo = build_adversarial_repo(tmp_path)
    service = _service(repo, backend)
    service.analyze(directory=repo.source, output=repo.output)

    # Re-index a changed file through a fresh full build, then reload.
    (repo.source / "app" / "orders.py").write_text(
        '"""Order handling, revised."""\n\n\n'
        "def place_order(item: str) -> str:\n"
        "    return item.strip()\n",
        encoding="utf-8",
    )
    _publish(repo, backend)
    service.reload()

    engine = service.get_search_engine()
    hits = engine.search("place an order", limit=10)
    entity_ids = [chunk.entity_id for chunk in hits]
    assert entity_ids, "the reindexed generation returned nothing"
    assert len(entity_ids) == len(set(entity_ids)), (
        f"duplicate entity ids biased fusion: {entity_ids}"
    )
    resolved = resolve_current_generation(repo.output / "knowcode_index")
    assert resolved.manifest.counts["chunks"] == resolved.manifest.counts["vectors"]
    service.close()


# ----------------------------------------------------------------------
# Failure boundaries and concurrency on the adversarial repository
# ----------------------------------------------------------------------


def test_a_failed_rebuild_preserves_the_adversarial_generation(
    tmp_path: Path, backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A semantic-build failure on a rebuild publishes nothing and keeps the last."""
    repo = build_adversarial_repo(tmp_path)
    service = _service(repo, backend)
    service.analyze(directory=repo.source, output=repo.output)
    before = {
        chunk.entity_id
        for chunk in service.get_search_engine().search("place an order", limit=5)
    }
    assert before, "precondition: the adversarial generation is searchable"
    first = resolve_current_generation(repo.output / "knowcode_index")

    (repo.source / "app" / "orders.py").write_text(
        '"""Revised."""\n\n\ndef place_order(x: str) -> str:\n    return x\n',
        encoding="utf-8",
    )

    def explode(self: Indexer, *args: object, **kwargs: object) -> int:
        raise RuntimeError("embedding provider unavailable")

    monkeypatch.setattr(Indexer, "index_directory", explode)
    rebuilt = _service(repo, backend)
    stats = rebuilt.analyze(directory=repo.source, output=repo.output)
    rebuilt.close()
    monkeypatch.undo()

    assert stats["published"] is False
    current = resolve_current_generation(repo.output / "knowcode_index")
    assert current.generation_id == first.generation_id

    restarted = _service(repo, backend)
    after = {
        chunk.entity_id
        for chunk in restarted.get_search_engine().search("place an order", limit=5)
    }
    assert after == before, "the previous generation was disturbed by a failed rebuild"
    restarted.close()


def test_concurrent_search_never_mixes_generations_over_the_adversarial_repo(
    tmp_path: Path, backend: str
) -> None:
    """Eight readers loop while rebuilds publish; none sees half of two generations."""
    repo = build_adversarial_repo(tmp_path)
    reader_service = _service(repo, backend)
    reader_service.analyze(directory=repo.source, output=repo.output)

    stop = threading.Event()
    errors: list[BaseException] = []
    mixed: list[str] = []
    observed: list[frozenset[str]] = []
    lock = threading.Lock()

    def read() -> None:
        try:
            while not stop.is_set():
                with reader_service.generation_lease() as bundle:
                    first = frozenset(e["id"] for e in reader_service.search(""))
                    chunks = reader_service.get_indexer().chunk_repo.count()
                    second = frozenset(e["id"] for e in reader_service.search(""))
                    again = reader_service.get_indexer().chunk_repo.count()
                    if first != second or chunks != again:
                        with lock:
                            mixed.append(str(bundle.generation_id))
                with lock:
                    observed.append(first)
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            with lock:
                errors.append(exc)

    readers = [threading.Thread(target=read) for _ in range(8)]
    for thread in readers:
        thread.start()
    try:
        for index in range(3):
            (repo.source / "app" / f"more{index}.py").write_text(
                f"def more{index}():\n    return {index}\n", encoding="utf-8"
            )
            _publish(repo, backend)
            reader_service.reload()
    finally:
        stop.set()
        _join(readers)

    assert not errors, f"a reader touched a retired resource: {errors!r}"
    assert not mixed, f"an operation observed two generations: {mixed!r}"
    assert len(set(observed)) > 1, "the readers never actually spanned generations"
    # Every id in every observed snapshot is a canonical internal id.
    for snapshot in observed:
        for entity_id in snapshot:
            assert classify_endpoint_id(entity_id) is EndpointKind.INTERNAL
    reader_service.close()
