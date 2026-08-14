"""Hostile inputs must not reach the telemetry file (Step 20).

The allowlist in ``test_telemetry.py`` is the primary control: a secret in an
unknown field is dropped because the field is dropped. These cases pin the
*second* control, which exists because the first one can be widened by a future
caller: every value that survives the allowlist is recursively redacted and
length-bounded before it is serialized.

Each case names a credential format that real repositories and real queries
contain. The assertion is always the same shape — the exact sensitive
substring is not in the bytes on disk — because that is the only claim a
privacy control can make honestly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from knowcode import telemetry, telemetry_files, telemetry_redaction

TIMEOUT = 5.0

#: (label, the secret, a plausible carrier string containing it)
SECRETS: list[tuple[str, str, str]] = [
    ("openai", "sk-proj-Ab3dEfGh1jKlMn0pQrStUv", "export OPENAI_API_KEY=sk-proj-Ab3dEfGh1jKlMn0pQrStUv"),
    ("anthropic", "sk-ant-api03-Ab3dEfGh1jKlMn0pQrStUvWx", "sk-ant-api03-Ab3dEfGh1jKlMn0pQrStUvWx"),
    ("google", "AIzaSyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6", "key=AIzaSyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"),
    ("github", "ghp_16C7e42F292c6912E7710c838347Ae178B4a", "ghp_16C7e42F292c6912E7710c838347Ae178B4a"),
    ("aws", "AKIAIOSFODNN7EXAMPLE", "aws_access_key_id = AKIAIOSFODNN7EXAMPLE"),
    ("slack", "xoxb-test-token-fixture-00000000", "xoxb-test-token-fixture-00000000"),
    ("bearer", "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln", "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln"),
    ("jwt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhZG1pbiJ9.9f8e7d6c5b", "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhZG1pbiJ9.9f8e7d6c5b"),
    ("private-key", "-----BEGIN RSA PRIVATE KEY-----", "-----BEGIN RSA PRIVATE KEY-----\nMIIEow=="),
    ("url-credentials", "deploy:hunter2", "postgres://deploy:hunter2@db.internal:5432/app"),
    ("assignment", "s3cr3t-value-not-guessable", "password=s3cr3t-value-not-guessable"),
    ("long-opaque", "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFiY2RlZmdoaWprbG1ub3A", "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFiY2RlZmdoaWprbG1ub3A"),
]

SECRET_IDS = [label for label, _, _ in SECRETS]


@pytest.fixture(autouse=True)
def _restore_executor() -> Any:
    yield
    telemetry.shutdown_telemetry(timeout=TIMEOUT)


def _payload(store: Path) -> str:
    """Everything telemetry wrote under this store, as raw bytes-on-disk text."""
    return "".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(store.rglob("*.jsonl*"))
    )


# ----------------------------------------------------------------------
# The redaction primitive
# ----------------------------------------------------------------------


@pytest.mark.parametrize(("label", "secret", "carrier"), SECRETS, ids=SECRET_IDS)
def test_redaction_removes_each_credential_format(label: str, secret: str, carrier: str) -> None:
    redacted = telemetry_redaction.redact(carrier)

    assert secret not in redacted
    assert telemetry_redaction.REDACTED in redacted


def test_redaction_reaches_into_nested_containers() -> None:
    """A secret one level down is the normal case, not the exotic one."""
    value = {"a": [{"b": ("nested", "AKIAIOSFODNN7EXAMPLE")}]}

    assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(telemetry_redaction.redact(value))


def test_redaction_leaves_ordinary_text_intact() -> None:
    """Over-redaction destroys the metric; the control must be targeted."""
    assert telemetry_redaction.redact("semantic") == "semantic"
    assert telemetry_redaction.redact({"num_chunks": 12}) == {"num_chunks": 12}


def test_redaction_bounds_a_long_string() -> None:
    bounded = telemetry_redaction.redact("a" * 10_000)

    assert len(bounded) <= telemetry_redaction.MAX_STRING_CHARS + len(telemetry_redaction.TRUNCATED)
    assert bounded.endswith(telemetry_redaction.TRUNCATED)


def test_redaction_bounds_depth_and_width() -> None:
    """A caller cannot make the sink walk an unbounded structure."""
    deep: Any = "leaf"
    for _ in range(50):
        deep = {"next": deep}

    redacted = telemetry_redaction.redact(deep)

    assert json.dumps(redacted).count("next") <= telemetry_redaction.MAX_DEPTH
    assert len(telemetry_redaction.redact(list(range(1000)))) <= telemetry_redaction.MAX_ITEMS


# ----------------------------------------------------------------------
# The sink, through its public entry point
# ----------------------------------------------------------------------


@pytest.mark.parametrize(("label", "secret", "carrier"), SECRETS, ids=SECRET_IDS)
def test_no_credential_survives_an_allowlisted_field(
    tmp_path: Path, label: str, secret: str, carrier: str
) -> None:
    """`tool_name` is client-supplied and allowlisted; it is still scrubbed."""
    telemetry.log_event(
        tmp_path, {"event_type": "tool_call", "tool_name": carrier, "argument_count": 1}
    )

    assert secret not in _payload(tmp_path)


@pytest.mark.parametrize(("label", "secret", "carrier"), SECRETS, ids=SECRET_IDS)
def test_no_credential_survives_a_query_string(
    tmp_path: Path, label: str, secret: str, carrier: str
) -> None:
    """The query itself is the most likely carrier of a pasted credential."""
    with telemetry.query_scope(tmp_path, query=f"why does {carrier} fail?") as scope:
        scope.record_retrieval(local_or_escalated="local", sufficiency_score=0.5)

    assert secret not in _payload(tmp_path)


def test_a_secret_in_a_dropped_field_never_reaches_disk(tmp_path: Path) -> None:
    """The allowlist is the first control; this is the case it covers alone."""
    telemetry.log_event(
        tmp_path,
        {
            "event_type": "agent_decision",
            "source": "llm",
            "prompt": "here is the key: sk-ant-api03-Ab3dEfGh1jKlMn0pQrStUvWx",
        },
    )

    assert "sk-ant-api03" not in _payload(tmp_path)


def test_unicode_text_is_bounded_and_still_valid_json(tmp_path: Path) -> None:
    """PII-like Unicode must not defeat the bound or corrupt the record."""
    name = "Ana María Gutiérrez-Ñuñez 山田太郎 " * 40

    telemetry.log_event(tmp_path, {"event_type": "tool_call", "tool_name": name})

    record = json.loads(telemetry_files.telemetry_path(tmp_path).read_text(encoding="utf-8").strip())
    assert len(record["tool_name"]) <= telemetry_redaction.MAX_STRING_CHARS + len(
        telemetry_redaction.TRUNCATED
    )
    assert "山田太郎" in record["tool_name"]


def test_a_whole_record_is_size_bounded(tmp_path: Path) -> None:
    """One event cannot write an unbounded line even with legal fields."""
    telemetry.log_event(
        tmp_path,
        {
            "event_type": "tool_call",
            "tool_name": "search" * 500,
            "argument_count": 3,
        },
    )

    line = telemetry_files.telemetry_path(tmp_path).read_text(encoding="utf-8").strip()
    assert len(line) <= telemetry_files.MAX_RECORD_CHARS


def test_a_record_that_cannot_be_serialized_is_dropped_not_partially_written(
    tmp_path: Path,
) -> None:
    """A serialization failure must not leave half a line in the file."""
    class Unserializable:
        def __repr__(self) -> str:  # pragma: no cover - defensive
            return "sk-ant-api03-Ab3dEfGh1jKlMn0pQrStUvWx"

    telemetry.log_event(
        tmp_path, {"event_type": "tool_call", "tool_name": Unserializable()}
    )

    payload = _payload(tmp_path)
    assert "sk-ant-api03" not in payload
    for line in payload.splitlines():
        json.loads(line)
