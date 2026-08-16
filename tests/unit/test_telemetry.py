"""The telemetry sink contract (Step 20).

Telemetry used to be an append-only plaintext file that took whatever dict a
caller handed it: raw queries, raw MCP tool arguments, and any nested field a
future caller invented. It was created with the process umask, never rotated,
never retained, and had no deletion path. One logical query produced several
records that all counted as separate queries.

These cases pin the replacement contract from ADR 5:

* only allowlisted event types and fields reach disk,
* the file is `0600`, bounded by rotation, and swept by retention,
* one logical query produces exactly one counted `query` event,
* telemetry is written under the store root and nowhere else,
* and a user can delete all of it in one documented operation.

The hostile-input matrix lives in ``test_telemetry_privacy.py``; this module
owns the mechanical sink behavior.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from knowcode import telemetry
from knowcode import telemetry_files, telemetry_policy

TIMEOUT = 5.0


@pytest.fixture(autouse=True)
def _restore_executor() -> Any:
    """The writer pool is process state; leave it usable for the next test."""
    yield
    telemetry.shutdown_telemetry(timeout=TIMEOUT)


def _records(store: Path) -> list[dict[str, Any]]:
    log = telemetry_files.telemetry_path(store)
    if not log.exists():
        return []
    return [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _query_event(store: Path, **overrides: Any) -> None:
    """Emit one counted query event through the public scope API."""
    fields: dict[str, Any] = {
        "task_type": "explain",
        "retrieval_mode": "semantic",
        "sufficiency_score": 0.9,
        "local_or_escalated": "local",
    }
    fields.update(overrides)
    with telemetry.query_scope(store, query="who calls place_order?") as scope:
        scope.record_retrieval(**fields)


# ----------------------------------------------------------------------
# Versioned schema and field allowlist
# ----------------------------------------------------------------------


def test_every_record_carries_the_schema_version_and_a_timestamp(
    tmp_path: Path,
) -> None:
    """A reader must be able to tell the new schema from legacy JSONL."""
    _query_event(tmp_path)

    record = _records(tmp_path)[0]
    assert (
        record["telemetry_schema_version"] == telemetry_policy.TELEMETRY_SCHEMA_VERSION
    )
    assert record["event_type"] == telemetry_policy.QUERY_EVENT
    assert isinstance(record["timestamp"], int)


def test_a_field_outside_the_allowlist_never_reaches_disk(tmp_path: Path) -> None:
    """The allowlist is the policy; callers cannot extend the schema in place."""
    telemetry.log_event(
        tmp_path,
        {
            "event_type": "agent_decision",
            "source": "local",
            "prompt_body": "the entire system prompt",
            "future_field_test": 12345,
        },
    )

    record = _records(tmp_path)[0]
    assert record["source"] == "local"
    assert "prompt_body" not in record
    assert "future_field_test" not in record
    assert record["dropped_field_count"] == 2


def test_an_unknown_event_type_is_rejected_rather_than_written(tmp_path: Path) -> None:
    """Fail closed: a caller policy has not reviewed writes nothing."""
    before = telemetry.dropped_event_count()

    telemetry.log_event(tmp_path, {"event_type": "prompt_dump", "body": "secret"})

    assert _records(tmp_path) == []
    assert telemetry.dropped_event_count() == before + 1


def test_a_raw_query_field_is_dropped_even_when_a_caller_passes_one(
    tmp_path: Path,
) -> None:
    """Defense in depth: the sink refuses raw text no matter who sends it."""
    telemetry.log_event(
        tmp_path,
        {"event_type": "query", "query": "how do I rotate the signing key?"},
    )

    payload = telemetry_files.telemetry_path(tmp_path).read_text(encoding="utf-8")
    assert "rotate the signing key" not in payload
    assert "query" not in _records(tmp_path)[0]


def test_query_events_carry_derived_metadata_not_text(tmp_path: Path) -> None:
    """Classification, length, and routing outcome replace the query string."""
    with telemetry.query_scope(tmp_path, query="x" * 40) as scope:
        scope.record_retrieval(
            task_type="explain", local_or_escalated="local", sufficiency_score=0.9
        )

    record = _records(tmp_path)[0]
    assert record["query_chars"] == 40
    assert record["query_length_bucket"] == telemetry_policy.length_bucket(40)
    assert record["sufficiency_bucket"] == telemetry_policy.score_bucket(0.9)
    assert record["task_type"] == "explain"
    assert "x" * 40 not in json.dumps(record)


def test_the_correlation_id_is_keyed_stable_and_not_the_query(tmp_path: Path) -> None:
    """Correlation must survive restarts without being reversible text."""
    _query_event(tmp_path)
    _query_event(tmp_path)

    first, second = _records(tmp_path)
    assert first["query_id"] == second["query_id"]
    assert "place_order" not in first["query_id"]

    key = telemetry_files.correlation_key_path(tmp_path)
    assert key.exists()
    assert stat.S_IMODE(os.stat(key).st_mode) == 0o600


def test_a_different_store_derives_a_different_correlation_id(tmp_path: Path) -> None:
    """The key is per store, so ids cannot be joined across repositories."""
    first_store = tmp_path / "one"
    second_store = tmp_path / "two"
    first_store.mkdir()
    second_store.mkdir()

    _query_event(first_store)
    _query_event(second_store)

    assert _records(first_store)[0]["query_id"] != _records(second_store)[0]["query_id"]


# ----------------------------------------------------------------------
# One logical query, one counted event
# ----------------------------------------------------------------------


def test_nested_scopes_emit_exactly_one_counted_query_event(tmp_path: Path) -> None:
    """An agent that retries retrieval three times is still one query."""
    with telemetry.query_scope(tmp_path, query="explain place_order") as outer:
        for _ in range(3):
            with telemetry.query_scope(tmp_path, query="explain place_order") as inner:
                assert inner is outer
                inner.record_retrieval(retrieval_mode="semantic", sufficiency_score=0.5)
        outer.annotate(local_or_escalated="local", sufficiency_score=0.9)

    records = _records(tmp_path)
    assert [r["event_type"] for r in records] == ["query"]
    assert records[0]["retrievals"] == 3
    assert records[0]["sufficiency_score"] == 0.9


def test_a_separate_event_type_is_not_counted_as_a_query(tmp_path: Path) -> None:
    """`agent_decision` describes an already-counted query."""
    _query_event(tmp_path)
    telemetry.log_event(
        tmp_path,
        {"event_type": "agent_decision", "source": "llm", "task_type": "explain"},
    )

    summary = telemetry.get_telemetry_summary(tmp_path)
    assert summary["total_queries"] == 1
    assert summary["events_by_type"] == {"query": 1, "agent_decision": 1}


def test_the_scope_records_its_own_duration_and_entry_point(tmp_path: Path) -> None:
    """Duration and entry point are the aggregate replacements for the text."""
    with telemetry.query_scope(tmp_path, query="q", entry_point="mcp") as scope:
        scope.annotate(local_or_escalated="local")

    record = _records(tmp_path)[0]
    assert record["entry_point"] == "mcp"
    assert isinstance(record["duration_ms"], int)
    assert record["duration_ms"] >= 0
    assert record["outcome"] == "ok"


def test_a_failing_scope_still_emits_one_event_marked_failed(tmp_path: Path) -> None:
    """A raising query is the interesting one; it must not vanish."""
    with pytest.raises(ValueError):
        with telemetry.query_scope(tmp_path, query="boom"):
            raise ValueError("retrieval exploded")

    record = _records(tmp_path)[0]
    assert record["outcome"] == "error"
    assert "exploded" not in json.dumps(record)


# ----------------------------------------------------------------------
# Where telemetry is allowed to live
# ----------------------------------------------------------------------


def test_telemetry_never_lands_inside_a_published_generation(tmp_path: Path) -> None:
    """A generation directory is immutable; telemetry belongs to the store root.

    The orchestrator passed the resolved ``knowledge.db`` path, which after
    Step 14 lives inside ``knowcode_index/generations/<id>/``. Every retrieval
    therefore wrote a log file into a published generation, where a retirement
    sweep deleted it and no documented deletion path could find it.
    """
    generation = tmp_path / "knowcode_index" / "generations" / "20260814T000000Z-abcd"
    generation.mkdir(parents=True)

    telemetry.log_event(generation / "knowledge.db", {"event_type": "reranker_latency"})

    assert list(generation.glob("*.jsonl")) == []
    assert telemetry_files.telemetry_path(tmp_path).exists()


def test_an_unresolvable_store_path_drops_the_event(tmp_path: Path) -> None:
    """Telemetry is never written to the process working directory.

    The reranker passed ``"."`` because it holds no store handle, so telemetry
    landed wherever the process happened to be started from — outside any
    store, and outside anything ``knowcode telemetry clear`` can find.
    """
    cwd_log = Path.cwd() / telemetry_files.TELEMETRY_FILENAME
    before_bytes = cwd_log.read_bytes() if cwd_log.exists() else None
    before = telemetry.dropped_event_count()

    telemetry.log_event(
        None, {"event_type": "reranker_latency", "method": "signal_based"}
    )

    assert telemetry.dropped_event_count() == before + 1
    assert (cwd_log.read_bytes() if cwd_log.exists() else None) == before_bytes


def test_an_event_without_a_store_path_uses_the_active_scope(tmp_path: Path) -> None:
    """The reranker has no store path of its own but runs inside a query."""
    with telemetry.query_scope(tmp_path, query="q"):
        telemetry.log_event(
            None,
            {
                "event_type": "reranker_latency",
                "method": "signal_based",
                "num_chunks": 3,
            },
        )

    assert [r["event_type"] for r in _records(tmp_path)] == [
        "reranker_latency",
        "query",
    ]


# ----------------------------------------------------------------------
# Permissions, rotation, retention, deletion
# ----------------------------------------------------------------------


def test_the_log_file_is_created_with_owner_only_permissions(tmp_path: Path) -> None:
    """A world-readable telemetry file is a local disclosure.

    Asserted against the literal mode, not against
    ``telemetry_files.FILE_MODE``: a test that reads the constant it is meant
    to pin passes whatever the constant is later changed to.
    """
    _query_event(tmp_path)

    mode = stat.S_IMODE(os.stat(telemetry_files.telemetry_path(tmp_path)).st_mode)
    assert mode == 0o600
    assert telemetry_files.FILE_MODE == 0o600


def test_an_existing_loose_log_file_is_tightened(tmp_path: Path) -> None:
    """Upgrading must fix files the previous writer created at 0644."""
    log = telemetry_files.telemetry_path(tmp_path)
    log.write_text("", encoding="utf-8")
    os.chmod(log, 0o644)

    _query_event(tmp_path)

    assert stat.S_IMODE(os.stat(log).st_mode) == 0o600


def test_the_log_rotates_at_the_size_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unbounded growth is the retention defect; rotation is the bound."""
    monkeypatch.setattr(telemetry_files, "MAX_FILE_BYTES", 400)

    for _ in range(20):
        _query_event(tmp_path)

    log = telemetry_files.telemetry_path(tmp_path)
    assert log.stat().st_size <= 400
    assert telemetry_files.rotation_path(log, 1).exists()


def test_rotation_keeps_a_bounded_number_of_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Total on-disk telemetry is bounded, not merely chunked."""
    monkeypatch.setattr(telemetry_files, "MAX_FILE_BYTES", 200)
    monkeypatch.setattr(telemetry_files, "MAX_ROTATIONS", 2)

    for _ in range(40):
        _query_event(tmp_path)

    rotations = sorted(tmp_path.glob(f"{telemetry_files.TELEMETRY_FILENAME}.*"))
    assert len(rotations) == 2


def test_retention_deletes_rotations_older_than_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bounded retention window is part of the privacy contract."""
    monkeypatch.setattr(telemetry_files, "MAX_FILE_BYTES", 200)
    log = telemetry_files.telemetry_path(tmp_path)
    stale = telemetry_files.rotation_path(log, 1)
    stale.write_text('{"event_type": "query"}\n', encoding="utf-8")
    expired = time.time() - (telemetry_files.RETENTION_DAYS + 1) * 86400
    os.utime(stale, (expired, expired))

    _query_event(tmp_path)

    assert not stale.exists()


def test_the_summary_reads_rotated_files_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rotation must not silently reset the trend the summary reports.

    A query record is ~390 bytes, so this bound holds three per file: eight
    events span the active file and two rotations, all within the retention
    cap, and the summary must see every one of them.
    """
    monkeypatch.setattr(telemetry_files, "MAX_FILE_BYTES", 1200)

    for _ in range(8):
        _query_event(tmp_path)

    assert len(_records(tmp_path)) < 8, "the bound did not force a rotation"
    assert telemetry.get_telemetry_summary(tmp_path)["total_queries"] == 8


def test_delete_telemetry_removes_every_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One documented operation, no leftovers, including the correlation key."""
    monkeypatch.setenv("KNOWCODE_TELEMETRY_RAW", "1")
    monkeypatch.setattr(telemetry_files, "MAX_FILE_BYTES", 200)
    for _ in range(10):
        _query_event(tmp_path)

    removed = telemetry.delete_telemetry(tmp_path)

    assert removed["removed"] >= 3
    assert list(tmp_path.glob("*.jsonl*")) == []
    assert not telemetry_files.correlation_key_path(tmp_path).exists()
    assert telemetry.get_telemetry_summary(tmp_path)["total_queries"] == 0


def test_delete_telemetry_on_a_clean_store_is_a_no_op(tmp_path: Path) -> None:
    """Deletion is idempotent and never raises for a store with no telemetry."""
    assert telemetry.delete_telemetry(tmp_path)["removed"] == 0


# ----------------------------------------------------------------------
# Opt-in raw capture
# ----------------------------------------------------------------------


def test_raw_capture_is_off_by_default(tmp_path: Path) -> None:
    """Redaction is not the default; omission is."""
    _query_event(tmp_path)

    assert not telemetry_files.raw_telemetry_path(tmp_path).exists()
    assert telemetry.raw_capture_enabled() is False


def test_raw_capture_opt_in_writes_a_separate_warned_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Opt-in is explicit, warned, and never mixed into the default file."""
    monkeypatch.setenv("KNOWCODE_TELEMETRY_RAW", "1")
    telemetry.reset_raw_capture_warning()

    with caplog.at_level("WARNING"):
        _query_event(tmp_path)

    raw = telemetry_files.raw_telemetry_path(tmp_path)
    assert (
        json.loads(raw.read_text(encoding="utf-8").strip())["query"]
        == "who calls place_order?"
    )
    assert stat.S_IMODE(os.stat(raw).st_mode) == 0o600
    assert "place_order" not in telemetry_files.telemetry_path(tmp_path).read_text(
        encoding="utf-8"
    )
    assert any("raw" in message.lower() for message in caplog.messages)


def test_raw_capture_still_redacts_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opting into raw text is not opting into storing credentials."""
    monkeypatch.setenv("KNOWCODE_TELEMETRY_RAW", "1")

    with telemetry.query_scope(
        tmp_path, query="deploy with sk-ant-api03-XXXXXXXXXXXXXXXXXXXX"
    ):
        pass

    payload = telemetry_files.raw_telemetry_path(tmp_path).read_text(encoding="utf-8")
    assert "sk-ant-api03-XXXXXXXXXXXXXXXXXXXX" not in payload
    assert "deploy with" in payload


# ----------------------------------------------------------------------
# Writer pool: shutdown, backpressure, failure isolation
# ----------------------------------------------------------------------


def test_the_pending_queue_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Telemetry drops rather than growing without bound behind a slow disk."""
    monkeypatch.delenv("KNOWCODE_TESTING", raising=False)
    monkeypatch.setattr(telemetry, "MAX_PENDING_EVENTS", 4)
    telemetry.shutdown_telemetry(timeout=TIMEOUT)

    release = threading.Event()
    original = telemetry._write_event_sync

    def blocking_write(store_path: Any, event: Any) -> None:
        assert release.wait(TIMEOUT), "the writer never got its release"
        original(store_path, event)

    monkeypatch.setattr(telemetry, "_write_event_sync", blocking_write)
    before = telemetry.dropped_event_count()
    for _ in range(20):
        telemetry.log_event(
            tmp_path, {"event_type": "reranker_latency", "num_chunks": 1}
        )
    release.set()

    assert telemetry.shutdown_telemetry(timeout=TIMEOUT)
    assert telemetry.dropped_event_count() > before


def test_a_write_failure_is_isolated_from_the_query_path(tmp_path: Path) -> None:
    """Telemetry must never be able to fail a user's query."""
    telemetry.log_event(
        Path("/nonexistent_directory/invalid_path/xyz"),
        {"event_type": "reranker_latency", "num_chunks": 1},
    )


def test_concurrent_writers_produce_whole_lines(tmp_path: Path) -> None:
    """Interleaved appends must stay parseable JSONL."""

    def emit() -> None:
        for _ in range(20):
            _query_event(tmp_path)

    threads = [threading.Thread(target=emit) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=TIMEOUT)
        assert not thread.is_alive()

    assert len(_records(tmp_path)) == 80


# ----------------------------------------------------------------------
# Summary, including legacy JSONL
# ----------------------------------------------------------------------


def test_summary_of_an_empty_store(tmp_path: Path) -> None:
    summary = telemetry.get_telemetry_summary(tmp_path)

    assert summary["total_queries"] == 0
    assert summary["local_routing_rate"] == 0.0
    assert summary["average_sufficiency_score"] == 0.0
    assert summary["user_marked_misses"] == 0
    assert summary["schema_version"] == telemetry_policy.TELEMETRY_SCHEMA_VERSION


def test_summary_aggregates_the_new_schema(tmp_path: Path) -> None:
    _query_event(tmp_path, local_or_escalated="local", sufficiency_score=0.8)
    _query_event(tmp_path, local_or_escalated="escalated", sufficiency_score=0.4)
    _query_event(
        tmp_path,
        local_or_escalated="local",
        sufficiency_score=0.9,
        user_marked_miss=True,
    )

    summary = telemetry.get_telemetry_summary(tmp_path)

    assert summary["total_queries"] == 3
    assert summary["local_routing_rate"] == pytest.approx(2 / 3)
    assert summary["average_sufficiency_score"] == pytest.approx(0.7)
    assert summary["user_marked_misses"] == 1


def test_summary_still_reads_legacy_records(tmp_path: Path) -> None:
    """A pre-Step-20 file keeps reporting the same totals it used to."""
    legacy = [
        {"query": "Q1", "local_or_escalated": "local", "sufficiency_score": 0.8},
        {"query": "Q2", "local_or_escalated": "escalated", "sufficiency_score": 0.4},
        {
            "query": "Q3",
            "source": "local",
            "sufficiency_score": 0.9,
            "user_marked_miss": True,
        },
    ]
    telemetry_files.telemetry_path(tmp_path).write_text(
        "".join(json.dumps(event) + "\n" for event in legacy), encoding="utf-8"
    )

    summary = telemetry.get_telemetry_summary(tmp_path)

    assert summary["total_queries"] == 3
    assert summary["local_routing_rate"] == pytest.approx(2 / 3)
    assert summary["average_sufficiency_score"] == pytest.approx(0.7)
    assert summary["user_marked_misses"] == 1


def test_a_corrupt_line_does_not_break_the_summary(tmp_path: Path) -> None:
    _query_event(tmp_path)
    with open(
        telemetry_files.telemetry_path(tmp_path), "a", encoding="utf-8"
    ) as handle:
        handle.write("{not json\n")

    assert telemetry.get_telemetry_summary(tmp_path)["total_queries"] == 1


# ----------------------------------------------------------------------
# Failure branches
# ----------------------------------------------------------------------


def test_a_relative_store_path_resolves_to_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--store .` is the CLI default and must not resolve to nothing.

    ``Path(".").parts`` is empty, so an unanchored relative path used to fall
    through the "no store root" branch and drop every event silently.
    """
    monkeypatch.chdir(tmp_path)

    _query_event(Path("."))

    assert (
        telemetry_files.telemetry_path(".")
        == tmp_path / telemetry_files.TELEMETRY_FILENAME
    )
    assert telemetry.get_telemetry_summary(".")["total_queries"] == 1


def test_a_scope_without_a_store_root_still_yields_a_usable_scope() -> None:
    """A caller with no store must not crash, and must write nothing."""
    before = telemetry.dropped_event_count()

    with telemetry.query_scope(None, query="q") as scope:
        scope.annotate(local_or_escalated="local")

    assert telemetry.dropped_event_count() > before


def test_the_correlation_id_is_empty_outside_a_scope() -> None:
    """Callers may ask for the id unconditionally."""
    assert telemetry.current_query_id() == ""
    assert telemetry.current_query_scope() is None


def test_an_oversized_record_is_dropped_rather_than_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record bound is a backstop behind the per-field bounds."""
    monkeypatch.setattr(telemetry_files, "MAX_RECORD_CHARS", 32)
    before = telemetry.dropped_event_count()

    _query_event(tmp_path)

    assert _records(tmp_path) == []
    assert telemetry.dropped_event_count() == before + 1


def test_an_unserializable_sanitized_event_is_dropped(tmp_path: Path) -> None:
    """Serialization is the last gate; a failure must not write a torn line."""
    before = telemetry.dropped_event_count()

    telemetry._write_event_sync(tmp_path, {"event_type": "query", "bad": object()})

    assert _records(tmp_path) == []
    assert telemetry.dropped_event_count() == before + 1


def test_a_failing_raw_write_is_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opt-in capture must not be able to fail the query that enabled it."""
    monkeypatch.setenv("KNOWCODE_TELEMETRY_RAW", "1")
    telemetry_files.raw_telemetry_path(tmp_path).mkdir()  # a directory, not a file
    before = telemetry.dropped_event_count()

    _query_event(tmp_path)

    assert telemetry.dropped_event_count() > before
    assert len(_records(tmp_path)) == 1


def test_an_unreadable_summary_reports_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A summary failure is reported as no data, never as an exception."""
    _query_event(tmp_path)

    def _explode(_path: Path) -> Any:
        raise OSError("telemetry directory vanished")

    monkeypatch.setattr(telemetry_files, "iter_records", _explode)

    assert telemetry.get_telemetry_summary(tmp_path)["total_queries"] == 0
