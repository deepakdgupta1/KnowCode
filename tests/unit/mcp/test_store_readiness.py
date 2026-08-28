"""Unit tests for MCP store-readiness detection (negative cases).

A current build publishes the knowledge graph as ``knowledge.db`` *inside* a
generation directory and writes the flat ``knowcode_knowledge.json`` only when
``export_json=True``. The MCP server used to test readiness by looking for
that JSON alone, so it reported ``missing_knowledge_store`` for every
repository built by a current KnowCode — including ones with a fully
published, searchable generation.

The positive case for the generation layout is an integration test
(``tests/integration/test_mcp_generation_readiness.py``): it needs a real
published generation, and hand-rolling a manifest here would duplicate
production validation rules and rot against them.
"""

from __future__ import annotations

import json
from pathlib import Path

from knowcode.mcp.server import KnowCodeMCPServer


class TestStoreReadiness:
    def test_legacy_json_layout_counts_as_ready(self, tmp_path: Path) -> None:
        (tmp_path / "knowcode_knowledge.json").write_text("{}")

        assert KnowCodeMCPServer(tmp_path)._store_is_ready() is True

    def test_empty_repository_is_not_ready(self, tmp_path: Path) -> None:
        assert KnowCodeMCPServer(tmp_path)._store_is_ready() is False

    def test_unreadable_pointer_is_not_ready_rather_than_fatal(
        self, tmp_path: Path
    ) -> None:
        """A corrupt pointer must degrade to "build me", not crash the server."""
        index_root = tmp_path / "knowcode_index"
        index_root.mkdir()
        (index_root / "current.json").write_text("{ this is not json")

        assert KnowCodeMCPServer(tmp_path)._store_is_ready() is False

    def test_index_directory_with_no_pointer_is_not_ready(self, tmp_path: Path) -> None:
        (tmp_path / "knowcode_index").mkdir()

        assert KnowCodeMCPServer(tmp_path)._store_is_ready() is False

    def test_pointer_to_a_missing_generation_is_not_ready(self, tmp_path: Path) -> None:
        index_root = tmp_path / "knowcode_index"
        index_root.mkdir()
        (index_root / "current.json").write_text(
            json.dumps(
                {
                    "generation_id": "20260101T000000000000Z-deadbeef",
                    "kind": "full",
                    "published_at": 1767225600,
                    "schema_version": 1,
                }
            )
        )

        assert KnowCodeMCPServer(tmp_path)._store_is_ready() is False


class TestPrerequisiteHints:
    """A missing store must tell an MCP agent what *it* can do about it."""

    def test_hint_names_a_tool_call_not_a_shell_command(self, tmp_path: Path) -> None:
        server = KnowCodeMCPServer(tmp_path)

        result = json.loads(
            server.handle_tool_call(
                "knowcode_retrieve", {"action": "query", "query": "anything"}
            )
        )

        assert result["code"] == "missing_knowledge_store"
        assert "knowcode_lifecycle" in result["hint"]
        assert "job_status" in result["hint"]


class TestDoctorHandsTheServerARoot:
    """``doctor --mcp`` must spawn the server on a root, not a store file.

    ``_resolve_store_file`` returns a *nonexistent* ``knowcode_knowledge.json``
    whenever the graph lives inside a generation — i.e. for every repository
    built by a current KnowCode — so passing it made the handshake spawn the
    server with a path that does not exist.
    """

    def test_resolve_store_file_is_not_a_usable_server_root(
        self, tmp_path: Path
    ) -> None:
        from knowcode.doctor import _resolve_store_file, _resolve_store_root

        # A repository with no flat JSON, which is the normal modern layout.
        assert not (tmp_path / "knowcode_knowledge.json").exists()

        store_file = _resolve_store_file(tmp_path)
        store_root = _resolve_store_root(tmp_path)

        assert not store_file.exists()
        assert store_root == tmp_path
        assert store_root.is_dir()

    def test_handshake_is_wired_to_the_root(self) -> None:
        """Pins the argument, since the failure mode was a silent bad path."""
        import inspect

        from knowcode import doctor

        source = inspect.getsource(doctor.run_doctor)
        assert "store_path=context.store_root" in source
        assert "store_path=context.store_file" not in source
