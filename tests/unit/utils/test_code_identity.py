"""Content identity of the running knowcode package (BL-32)."""

from __future__ import annotations

from pathlib import Path

from knowcode.utils.code_identity import builder_metadata, package_code_fingerprint


def test_fingerprint_is_deterministic(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("y = 2\n", encoding="utf-8")
    assert package_code_fingerprint(tmp_path) == package_code_fingerprint(tmp_path)


def test_fingerprint_moves_when_source_changes(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    before = package_code_fingerprint(tmp_path)
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    assert package_code_fingerprint(tmp_path) != before


def test_identical_trees_fingerprint_identically_wherever_installed(
    tmp_path: Path,
) -> None:
    """The fingerprint is content identity, not install-location identity.

    This is the property that lets a checkout venv and a uv tool environment
    running the same commit agree, and one that has moved on disagree.
    """
    for root_name in ("checkout", "tool-env"):
        root = tmp_path / root_name
        root.mkdir()
        (root / "pkg").mkdir()
        (root / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")
        (root / "pkg" / "b.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    first = package_code_fingerprint(tmp_path / "checkout" / "pkg")
    second = package_code_fingerprint(tmp_path / "tool-env" / "pkg")
    assert first == second


def test_fingerprint_covers_file_names_not_just_content(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    before = package_code_fingerprint(tmp_path)
    (tmp_path / "a.py").unlink()
    (tmp_path / "z.py").write_text("x = 1\n", encoding="utf-8")
    assert package_code_fingerprint(tmp_path) != before


def test_builder_metadata_names_the_fingerprint_and_file_count() -> None:
    metadata = builder_metadata()
    assert metadata["code_fingerprint"].startswith("sha256:")
    assert metadata["code_files"] > 0
    assert metadata["code_fingerprint"] == package_code_fingerprint()


def test_running_package_fingerprint_is_stable_across_calls() -> None:
    assert package_code_fingerprint() == package_code_fingerprint()
