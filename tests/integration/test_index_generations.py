"""End-to-end generation publication over a real repository (Step 14).

The unit suites prove the primitive and the service wiring in isolation. These
exercise the assembled pipeline — scan, parse, graph, chunk, embed, publish —
on a small multi-file tree, for both vector backends, across restarts and
reloads.
"""

from __future__ import annotations

import shutil
import threading
from pathlib import Path

import pytest

from knowcode.config import AppConfig
from knowcode.doctor import run_doctor
from knowcode.indexing import generations
from knowcode.indexing.generations import resolve_current_generation
from knowcode.service import KnowCodeService


@pytest.fixture(params=["faiss", "lancedb"])
def backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _config(backend: str) -> AppConfig:
    config = AppConfig.default()
    config.vector_backend = backend
    return config


def _repository(root: Path) -> Path:
    """A small multi-file, multi-language tree."""
    src = root / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (src / "pkg" / "orders.py").write_text(
        '"""Order handling."""\n\n\n'
        "class OrderService:\n"
        '    """Place and cancel orders."""\n\n'
        "    def place(self, item: str) -> str:\n"
        '        """Place an order for an item."""\n'
        "        return validate(item)\n\n\n"
        "def validate(item: str) -> str:\n"
        '    """Validate an item name."""\n'
        "    return item.strip()\n",
        encoding="utf-8",
    )
    (src / "pkg" / "billing.py").write_text(
        "def charge(amount: int) -> int:\n"
        '    """Charge an amount."""\n'
        "    return amount\n",
        encoding="utf-8",
    )
    (src / "app.js").write_text(
        "export function boot() {\n  return 42;\n}\n", encoding="utf-8"
    )
    return src


def _service(root: Path, backend: str) -> KnowCodeService:
    return KnowCodeService(store_path=root, app_config=_config(backend))


def _entity_ids(service: KnowCodeService) -> set[str]:
    return {entity["id"] for entity in service.search("")}


def test_analyze_publishes_one_coherent_generation(
    tmp_path: Path, backend: str
) -> None:
    src = _repository(tmp_path)
    service = _service(tmp_path, backend)

    stats = service.analyze(directory=src, output=tmp_path)

    assert stats["published"] is True
    resolved = resolve_current_generation(tmp_path / "knowcode_index")
    assert resolved is not None

    manifest = resolved.manifest
    # Manifest counts describe the artifacts actually published.
    assert manifest.counts["entities"] == len(
        generations.read_entity_ids(resolved.knowledge_db)
    )
    assert manifest.counts["chunks"] == len(
        generations.read_chunk_ids(resolved.chunks_db)
    )
    assert manifest.counts["chunks"] == manifest.counts["vectors"]
    assert manifest.counts["chunks"] > 0

    # And the whole set validates, digests and checksums included.
    assert (
        generations.validate_generation(
            resolved.path, expected_id=resolved.generation_id, verify_digests=True
        )
        == []
    )


def test_chunks_derive_from_the_same_parse_as_the_graph(tmp_path: Path) -> None:
    """An ignored file must be absent from both the graph and the chunk store.

    Before Step 14 the indexer re-scanned without the caller's ignore patterns,
    so a generation could hold chunks for files its knowledge graph never saw.
    """
    src = _repository(tmp_path)
    service = _service(tmp_path, "faiss")

    service.analyze(directory=src, output=tmp_path, ignore=["**/billing.py"])

    resolved = resolve_current_generation(tmp_path / "knowcode_index")
    entity_ids = generations.read_entity_ids(resolved.knowledge_db)
    chunk_ids = generations.read_chunk_ids(resolved.chunks_db)

    assert not any("billing.py" in identifier for identifier in entity_ids)
    assert not any("billing.py" in identifier for identifier in chunk_ids)
    assert any("orders.py" in identifier for identifier in chunk_ids)


def test_restart_reads_the_published_generation(tmp_path: Path, backend: str) -> None:
    src = _repository(tmp_path)
    builder = _service(tmp_path, backend)
    builder.analyze(directory=src, output=tmp_path)
    published = _entity_ids(builder)

    restarted = _service(tmp_path, backend)

    assert _entity_ids(restarted) == published
    assert restarted.get_search_engine().search("place an order", limit=3)


def test_reload_advances_to_the_next_generation(tmp_path: Path) -> None:
    src = _repository(tmp_path)
    reader = _service(tmp_path, "faiss")
    reader.analyze(directory=src, output=tmp_path)
    first = resolve_current_generation(tmp_path / "knowcode_index")

    (src / "pkg" / "shipping.py").write_text(
        "def ship(order: str) -> str:\n    return order\n", encoding="utf-8"
    )
    _service(tmp_path, "faiss").analyze(directory=src, output=tmp_path)

    # Before reload the long-lived reader still serves its own generation.
    assert not any("shipping.py" in identifier for identifier in _entity_ids(reader))

    reader.reload()

    second = resolve_current_generation(tmp_path / "knowcode_index")
    assert second.generation_id != first.generation_id
    assert any("shipping.py" in identifier for identifier in _entity_ids(reader))


def test_a_reader_keeps_working_while_a_rebuild_publishes(tmp_path: Path) -> None:
    """A concurrent full rebuild never disturbs an in-flight reader.

    Bounded and barrier-synchronized: the reader is proven to be mid-read when
    publication happens, rather than relying on a sleep.
    """
    src = _repository(tmp_path)
    _service(tmp_path, "faiss").analyze(directory=src, output=tmp_path)

    reader = _service(tmp_path, "faiss")
    expected = _entity_ids(reader)

    reading = threading.Barrier(2, timeout=30)
    failures: list[BaseException] = []
    observed: list[set[str]] = []

    def read_repeatedly() -> None:
        try:
            reading.wait()
            for _ in range(20):
                observed.append(_entity_ids(reader))
        except BaseException as exc:  # noqa: BLE001 - reported to the assertion
            failures.append(exc)

    def rebuild() -> None:
        try:
            reading.wait()
            (src / "pkg" / "returns.py").write_text(
                "def refund(order: str) -> str:\n    return order\n", encoding="utf-8"
            )
            _service(tmp_path, "faiss").analyze(directory=src, output=tmp_path)
        except BaseException as exc:  # noqa: BLE001 - reported to the assertion
            failures.append(exc)

    threads = [
        threading.Thread(target=read_repeatedly),
        threading.Thread(target=rebuild),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()

    assert failures == []
    assert observed and all(snapshot == expected for snapshot in observed)


def test_ensure_index_publishes_then_reuses_one_generation(
    tmp_path: Path, backend: str
) -> None:
    src = _repository(tmp_path)

    _service(tmp_path, backend).ensure_index(directory=src)
    first = resolve_current_generation(tmp_path / "knowcode_index")
    assert first is not None

    _service(tmp_path, backend).ensure_index(directory=src)
    second = resolve_current_generation(tmp_path / "knowcode_index")

    assert second.generation_id == first.generation_id


def test_a_corrupt_newest_generation_falls_back_and_stays_searchable(
    tmp_path: Path,
) -> None:
    src = _repository(tmp_path)
    _service(tmp_path, "faiss").analyze(directory=src, output=tmp_path)
    first = resolve_current_generation(tmp_path / "knowcode_index")

    (src / "pkg" / "billing.py").write_text(
        "def charge(amount: int) -> int:\n    return amount * 2\n", encoding="utf-8"
    )
    _service(tmp_path, "faiss").analyze(directory=src, output=tmp_path)
    second = resolve_current_generation(tmp_path / "knowcode_index")
    assert second.generation_id != first.generation_id

    # Simulate a half-destroyed newest generation.
    (second.path / generations.MANIFEST_FILENAME).write_text("{ truncated", "utf-8")

    recovered = _service(tmp_path, "faiss")
    assert recovered.current_generation().generation_id == first.generation_id
    assert recovered.get_search_engine().search("charge an amount", limit=3)


def test_retention_bounds_disk_use_but_keeps_a_recovery_generation(
    tmp_path: Path,
) -> None:
    src = _repository(tmp_path)
    for index in range(4):
        (src / "pkg" / "billing.py").write_text(
            f"def charge(amount: int) -> int:\n    return amount + {index}\n",
            encoding="utf-8",
        )
        _service(tmp_path, "faiss").analyze(directory=src, output=tmp_path)

    retained = generations.list_generations(tmp_path / "knowcode_index")
    current = resolve_current_generation(tmp_path / "knowcode_index")

    assert len(retained) == generations.DEFAULT_RETAINED_GENERATIONS
    assert current.generation_id in retained
    # The other retained generation is a complete, usable fallback.
    fallback = next(name for name in retained if name != current.generation_id)
    assert (
        generations.validate_generation(
            generations.generations_dir(tmp_path / "knowcode_index") / fallback,
            expected_id=fallback,
            verify_digests=True,
        )
        == []
    )


def test_doctor_passes_on_a_freshly_published_generation(tmp_path: Path) -> None:
    src = _repository(tmp_path)
    _service(tmp_path, "faiss").analyze(directory=src, output=tmp_path)

    report = run_doctor(store_path=tmp_path)

    generation_check = next(
        check for check in report.checks if check.name == "Index generation"
    )
    assert generation_check.status == "pass", generation_check.message


def test_an_abandoned_staging_generation_is_ignored_and_cleaned(
    tmp_path: Path,
) -> None:
    src = _repository(tmp_path)
    _service(tmp_path, "faiss").analyze(directory=src, output=tmp_path)
    index_root = tmp_path / "knowcode_index"
    current = resolve_current_generation(index_root)

    abandoned = (
        index_root / f"{generations.STAGING_PREFIX}20200101T000000000000Z-abcdef12.pid1"
    )
    shutil.copytree(current.path, abandoned)

    # A restart resolves past it...
    assert (
        _service(tmp_path, "faiss").current_generation().generation_id
        == current.generation_id
    )
    # ...and the next build removes it.
    _service(tmp_path, "faiss").analyze(directory=src, output=tmp_path)
    assert not abandoned.exists()
