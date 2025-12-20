"""Unit tests for the file scanner."""

from pathlib import Path

from knowcode.indexing.scanner import Scanner


def test_scanner_respects_gitignore_and_extensions(tmp_path: Path) -> None:
    """Scanner should respect ignore rules and extensions."""
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("print('ignore')", encoding="utf-8")
    (tmp_path / "skip.py").write_text("print('skip')", encoding="utf-8")
    (tmp_path / "keep.py").write_text("print('keep')", encoding="utf-8")
    (tmp_path / "note.txt").write_text("text", encoding="utf-8")

    scanner = Scanner(tmp_path, additional_ignores=["skip.py"])
    files = scanner.scan_all()
    paths = {f.relative_path for f in files}

    assert "keep.py" in paths
    assert "ignored.py" not in paths
    assert "skip.py" not in paths
    assert "note.txt" not in paths
