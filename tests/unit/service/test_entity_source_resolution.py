"""Entity source resolves from a verified disk read under the default mode.

The service-level half of storage plan D3: `entity_source: disk` (the default)
persists no `source_code` copy, and `get_entity_details` re-reads the text from
the working tree, serving it only while it still hashes to the indexed digest.
`entity_source: stored` restores the persisted copy, drift and all.
"""

from pathlib import Path

from knowcode.config import AppConfig
from knowcode.service import KnowCodeService


def _source_tree(root: Path, body: str = "return 1") -> Path:
    src = root / "src"
    src.mkdir(exist_ok=True)
    (src / "m.py").write_text(f"def alpha():\n    {body}\n", encoding="utf-8")
    return src


def _service(root: Path, entity_source: str) -> KnowCodeService:
    config = AppConfig.default()
    config.entity_source = entity_source
    return KnowCodeService(store_path=root, app_config=config)


def _alpha_id(src: Path) -> str:
    # ``alpha`` hangs under the ``m`` module scope (BL-9).
    return f"{(src / 'm.py').resolve()}::m.alpha"


def test_disk_mode_resolves_entity_source_from_the_file(tmp_path: Path) -> None:
    src = _source_tree(tmp_path)
    service = _service(tmp_path, entity_source="disk")
    service.analyze(directory=src, output=tmp_path)

    details = service.get_entity_details(_alpha_id(src))

    assert details is not None
    assert "return 1" in (details["source_code"] or "")


def test_disk_mode_fails_closed_after_the_file_drifts(tmp_path: Path) -> None:
    src = _source_tree(tmp_path)
    service = _service(tmp_path, entity_source="disk")
    service.analyze(directory=src, output=tmp_path)

    (src / "m.py").write_text("def alpha():\n    return 42\n", encoding="utf-8")

    details = service.get_entity_details(_alpha_id(src))

    assert details is not None
    assert details["source_code"] is None


def test_stored_mode_serves_the_persisted_copy_even_after_drift(tmp_path: Path) -> None:
    src = _source_tree(tmp_path)
    service = _service(tmp_path, entity_source="stored")
    service.analyze(directory=src, output=tmp_path)

    (src / "m.py").write_text("def alpha():\n    return 42\n", encoding="utf-8")

    details = service.get_entity_details(_alpha_id(src))

    assert details is not None
    # Stale by design: that is what opting out of D3 buys.
    assert "return 1" in (details["source_code"] or "")


# ---------------------------------------------------------------------------
# BL-16: the same matrix through `get_context`, which carries the canonical MCP
# retrieval path. D3 wired its resolver into `get_entity_details` only, and the
# synthesizer reached for a live loader that the service built solely when the
# index was stale — a gate that made sense while a stored copy always existed.
# ---------------------------------------------------------------------------


def _context_text(service: KnowCodeService, entity_id: str, *, is_stale: bool) -> str:
    return service.get_context(entity_id, is_stale=is_stale)["context_text"]


def test_disk_mode_serves_context_source_on_a_fresh_index(tmp_path: Path) -> None:
    src = _source_tree(tmp_path)
    service = _service(tmp_path, entity_source="disk")
    service.analyze(directory=src, output=tmp_path)

    context = _context_text(service, _alpha_id(src), is_stale=False)

    assert "## Source Code" in context
    assert "return 1" in context


def test_disk_mode_context_fails_closed_after_the_file_drifts(tmp_path: Path) -> None:
    src = _source_tree(tmp_path)
    service = _service(tmp_path, entity_source="disk")
    service.analyze(directory=src, output=tmp_path)

    (src / "m.py").write_text("def alpha():\n    return 42\n", encoding="utf-8")

    context = _context_text(service, _alpha_id(src), is_stale=False)

    # Nothing, rather than either the drifted body or a stale one.
    assert "## Source Code" not in context
    assert "return 42" not in context
    assert "return 1" not in context


def test_disk_mode_context_serves_live_text_when_the_index_is_stale(
    tmp_path: Path,
) -> None:
    src = _source_tree(tmp_path)
    service = _service(tmp_path, entity_source="disk")
    service.analyze(directory=src, output=tmp_path)

    (src / "m.py").write_text("def alpha():\n    return 42\n", encoding="utf-8")

    context = _context_text(service, _alpha_id(src), is_stale=True)

    # A caller told the index is stale asked for the tree, drift included.
    assert "return 42" in context


def test_stored_mode_serves_context_source_on_a_fresh_index(tmp_path: Path) -> None:
    src = _source_tree(tmp_path)
    service = _service(tmp_path, entity_source="stored")
    service.analyze(directory=src, output=tmp_path)

    context = _context_text(service, _alpha_id(src), is_stale=False)

    assert "## Source Code" in context
    assert "return 1" in context
