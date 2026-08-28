"""Unit tests for MCP server root resolution and path containment.

The MCP server is registered once (usually at user scope) and then spawned
inside whatever repository the agent is working in. Claude Code does not
document the working directory it spawns a stdio server with, and its own
guidance is to resolve project-relative paths from ``CLAUDE_PROJECT_DIR``
rather than from the process CWD. These tests pin that precedence so the
"one global registration, every repository" setup cannot silently resolve
against the wrong tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from knowcode.mcp.roots import (
    PathOutsideRootError,
    ensure_within_root,
    resolve_server_root,
)


class TestResolveServerRoot:
    """Precedence: explicit --store, then CLAUDE_PROJECT_DIR, then CWD."""

    def test_explicit_path_wins_over_environment(self, tmp_path: Path) -> None:
        explicit = tmp_path / "explicit"
        explicit.mkdir()
        env_dir = tmp_path / "from_env"
        env_dir.mkdir()

        root = resolve_server_root(
            explicit=explicit, env={"CLAUDE_PROJECT_DIR": str(env_dir)}
        )

        assert root == explicit.resolve()

    def test_falls_back_to_claude_project_dir(self, tmp_path: Path) -> None:
        env_dir = tmp_path / "project"
        env_dir.mkdir()

        root = resolve_server_root(
            explicit=None, env={"CLAUDE_PROJECT_DIR": str(env_dir)}
        )

        assert root == env_dir.resolve()

    def test_falls_back_to_cwd_when_environment_is_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)

        root = resolve_server_root(explicit=None, env={})

        assert root == tmp_path.resolve()

    def test_blank_environment_value_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)

        root = resolve_server_root(explicit=None, env={"CLAUDE_PROJECT_DIR": "   "})

        assert root == tmp_path.resolve()

    def test_nonexistent_environment_directory_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stale env var must not make the server unusable."""
        monkeypatch.chdir(tmp_path)

        root = resolve_server_root(
            explicit=None, env={"CLAUDE_PROJECT_DIR": str(tmp_path / "gone")}
        )

        assert root == tmp_path.resolve()

    def test_result_is_always_absolute_and_normalized(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        monkeypatch.chdir(tmp_path)

        root = resolve_server_root(explicit=Path("a/../a/b"), env={})

        assert root.is_absolute()
        assert root == nested.resolve()

    def test_missing_explicit_path_is_reported(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            resolve_server_root(explicit=tmp_path / "nope", env={})


class TestEnsureWithinRoot:
    """A mutating action must never touch a tree outside the server root.

    Without this guard, an agent could pass ``path="/Users/me/other-repo"`` to
    a build action and cause KnowCode to parse — and, when an embedding
    provider is configured, upload chunk text from — a repository the user
    never pointed the server at.
    """

    def test_root_itself_is_allowed(self, tmp_path: Path) -> None:
        assert ensure_within_root(tmp_path, tmp_path) == tmp_path.resolve()

    def test_dot_resolves_to_root(self, tmp_path: Path) -> None:
        assert ensure_within_root(".", tmp_path) == tmp_path.resolve()

    def test_child_directory_is_allowed(self, tmp_path: Path) -> None:
        child = tmp_path / "src" / "pkg"
        child.mkdir(parents=True)

        assert ensure_within_root(child, tmp_path) == child.resolve()

    def test_relative_child_is_resolved_against_root_not_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Relative paths are repository-relative, never CWD-relative."""
        child = tmp_path / "src"
        child.mkdir()
        elsewhere = tmp_path.parent / "elsewhere"
        elsewhere.mkdir(exist_ok=True)
        monkeypatch.chdir(elsewhere)

        assert ensure_within_root("src", tmp_path) == child.resolve()

    def test_sibling_directory_is_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        sibling = tmp_path / "other-repo"
        sibling.mkdir()

        with pytest.raises(PathOutsideRootError):
            ensure_within_root(sibling, root)

    def test_parent_traversal_is_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()

        with pytest.raises(PathOutsideRootError):
            ensure_within_root("../", root)

    def test_absolute_escape_is_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()

        with pytest.raises(PathOutsideRootError):
            ensure_within_root("/etc", root)

    def test_symlink_escape_is_rejected(self, tmp_path: Path) -> None:
        """A symlink inside the root pointing out of it is still an escape."""
        root = tmp_path / "repo"
        root.mkdir()
        outside = tmp_path / "secrets"
        outside.mkdir()
        link = root / "link"
        link.symlink_to(outside, target_is_directory=True)

        with pytest.raises(PathOutsideRootError):
            ensure_within_root(link, root)

    def test_error_carries_actionable_code_and_hint(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()

        with pytest.raises(PathOutsideRootError) as excinfo:
            ensure_within_root("/etc", root)

        assert excinfo.value.code == "path_outside_root"
        assert excinfo.value.hint
        # The rejected path is named so the agent can correct its own call,
        # but the message must not imply the server will widen its scope.
        assert "/etc" in str(excinfo.value)

    def test_prefix_collision_is_not_treated_as_containment(
        self, tmp_path: Path
    ) -> None:
        """``/repo-backup`` shares a string prefix with ``/repo`` but is outside."""
        root = tmp_path / "repo"
        root.mkdir()
        decoy = tmp_path / "repo-backup"
        decoy.mkdir()

        with pytest.raises(PathOutsideRootError):
            ensure_within_root(decoy, root)

    def test_case_differing_path_inside_root_is_accepted_on_this_platform(
        self, tmp_path: Path
    ) -> None:
        """``resolve()`` does not canonicalize case; macOS is case-insensitive.

        Comparing raw paths rejected ``/repo/sub`` against root ``/Repo`` even
        though the OS treats them as one directory. On a case-sensitive
        filesystem they really are different paths, so rejection is correct
        there — this asserts whichever behaviour the platform actually has.
        """
        root = tmp_path / "Repo"
        (root / "sub").mkdir(parents=True)
        flipped = tmp_path / "repo" / "sub"

        if flipped.exists():  # case-insensitive filesystem
            assert ensure_within_root(flipped, root) is not None
        else:  # case-sensitive filesystem: genuinely a different path
            with pytest.raises(PathOutsideRootError):
                ensure_within_root(flipped, root)

    def test_case_differing_escape_is_still_rejected(self, tmp_path: Path) -> None:
        """Case-insensitive matching must not let a real escape through."""
        root = tmp_path / "Repo"
        root.mkdir()
        outside = tmp_path / "OTHER"
        outside.mkdir()

        with pytest.raises(PathOutsideRootError):
            ensure_within_root(outside, root)
        with pytest.raises(PathOutsideRootError):
            ensure_within_root(tmp_path / "other", root)
