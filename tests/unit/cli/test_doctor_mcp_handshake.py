"""Unit tests for the doctor's MCP stdio handshake check."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest


doctor_module = importlib.import_module("knowcode.doctor")


_UNDECODABLE_STDERR_SERVER = r"""
import json
import sys

import anyio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

sys.stderr.buffer.write(b"before \x81\x8d\x8f\x90\x9d after\n")
sys.stderr.buffer.flush()
server = Server("undecodable-stderr")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [Tool(name=sys.argv[1], inputSchema={"type": "object"})]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"context_text": "stub"}))]


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        options = server.create_initialization_options()
        await server.run(read_stream, write_stream, options)


anyio.run(main)
"""


def test_doctor_undecodable_mcp_stderr_cannot_fail_a_passed_handshake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server writes stderr in its locale encoding, cp1252 on Windows.

    Those bytes land unchanged in the file the check reads back. 0x81, 0x8D,
    0x8F, 0x90 and 0x9D are undefined in cp1252 and invalid as UTF-8, so a
    strict decode raises on every CI platform, and `_run_mcp_check` reports a
    handshake that passed as a failure. The pass message shows them as U+FFFD
    instead, one per byte.
    """
    mcp_stdio = pytest.importorskip("mcp.client.stdio")
    server = tmp_path / "server.py"
    server.write_text(_UNDECODABLE_STDERR_SERVER, encoding="utf-8")
    real_stdio_client = mcp_stdio.stdio_client

    def _spawn_stand_in(params: Any, errlog: Any) -> Any:
        args = [str(server), doctor_module.PRIMARY_TOOL_NAME]
        return real_stdio_client(params.model_copy(update={"args": args}), errlog)

    monkeypatch.setattr(mcp_stdio, "stdio_client", _spawn_stand_in)

    check = doctor_module._run_mcp_check(
        store_path=tmp_path, config_path=None, timeout_seconds=30.0
    )

    assert check.status == "pass", check.message
    replaced = "\N{REPLACEMENT CHARACTER}" * 5
    assert f"before {replaced} after" in check.message
