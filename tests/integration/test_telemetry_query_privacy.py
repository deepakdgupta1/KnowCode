"""End-to-end telemetry privacy over a real build and a real query (Step 20).

The unit suites stub retrieval at the search-engine boundary. This one runs the
real thing: a real repository, a real ``analyze()`` publication, and a real
``retrieve_context_for_query()`` — the production path an MCP client or the API
takes. It then inspects every file the run left behind.

The claim under test is the one a user cares about: after asking KnowCode a
question that contains a credential, the credential and the question are not on
their disk, the query was counted exactly once, and one command removes what
was written.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from knowcode import telemetry, telemetry_files
from knowcode.config import AppConfig
from knowcode.service import KnowCodeService

SECRET = "sk-ant-api03-Ab3dEfGh1jKlMn0pQrStUvWx"
QUERY = f"how does place_order validate the key {SECRET}?"


@pytest.fixture
def service(tmp_path: Path):  # type: ignore[no-untyped-def]
    src = tmp_path / "src"
    src.mkdir()
    (src / "orders.py").write_text(
        '"""Order handling."""\n\n\n'
        "def place_order(item: str) -> str:\n"
        '    """Place an order for an item and validate it."""\n'
        "    return item.strip()\n",
        encoding="utf-8",
    )
    built = KnowCodeService(store_path=tmp_path, app_config=AppConfig.default())
    stats = built.analyze(directory=src, output=tmp_path)
    assert stats["published"] is True, stats
    try:
        yield built
    finally:
        built.close()
        telemetry.shutdown_telemetry(timeout=5.0)


def _telemetry_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.jsonl*"))


def _payload(root: Path) -> str:
    return "".join(path.read_text(encoding="utf-8", errors="replace") for path in _telemetry_files(root))


def test_a_query_containing_a_credential_leaves_neither_on_disk(
    service: KnowCodeService, tmp_path: Path
) -> None:
    service.retrieve_context_for_query(QUERY)

    payload = _payload(tmp_path)
    assert payload, "the run wrote no telemetry at all"
    assert SECRET not in payload
    assert "place_order" not in payload
    assert "how does" not in payload


def test_one_query_is_counted_exactly_once(service: KnowCodeService, tmp_path: Path) -> None:
    before = telemetry.get_telemetry_summary(tmp_path)["total_queries"]

    service.retrieve_context_for_query(QUERY)

    after = telemetry.get_telemetry_summary(tmp_path)["total_queries"]
    assert after == before + 1


def test_every_telemetry_file_is_owner_only(service: KnowCodeService, tmp_path: Path) -> None:
    service.retrieve_context_for_query(QUERY)

    for path in _telemetry_files(tmp_path) + [telemetry_files.correlation_key_path(tmp_path)]:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600, path


def test_telemetry_lives_only_at_the_store_root(service: KnowCodeService, tmp_path: Path) -> None:
    service.retrieve_context_for_query(QUERY)

    assert _telemetry_files(tmp_path) == [telemetry_files.telemetry_path(tmp_path)]


def test_the_query_event_is_readable_aggregate_metadata(
    service: KnowCodeService, tmp_path: Path
) -> None:
    service.retrieve_context_for_query(QUERY)

    records = [
        json.loads(line)
        for line in telemetry_files.telemetry_path(tmp_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    queries = [record for record in records if record["event_type"] == "query"]
    assert len(queries) == 1
    assert queries[0]["query_chars"] == len(QUERY)
    assert queries[0]["sufficiency_score"] >= 0.0
    assert queries[0]["retrieval_mode"] in {"semantic", "lexical", "exact", "none"}


def test_deleting_telemetry_removes_everything_the_query_wrote(
    service: KnowCodeService, tmp_path: Path
) -> None:
    service.retrieve_context_for_query(QUERY)

    telemetry.delete_telemetry(tmp_path)

    assert _telemetry_files(tmp_path) == []
    assert telemetry.get_telemetry_summary(tmp_path)["total_queries"] == 0
