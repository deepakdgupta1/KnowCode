"""Unit tests for telemetry logging."""

import json
import time
from pathlib import Path
import pytest
from knowcode.telemetry import log_event

def test_telemetry_appends_jsonl_non_blocking(tmp_path: Path) -> None:
    """Test that log_event appends valid JSONL record asynchronously."""
    event = {
        "query": "Who is foo?",
        "verbosity": "minimal",
        "sufficiency_score": 0.85,
        "is_stale": False,
        "local_or_escalated": "local",
        "user_marked_miss": False,
    }
    
    log_event(tmp_path, event)
    time.sleep(0.1)
    
    log_file = tmp_path / "knowcode_telemetry.jsonl"
    assert log_file.exists()
    
    lines = log_file.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    
    record = json.loads(lines[0])
    assert record["query"] == "Who is foo?"
    assert record["local_or_escalated"] == "local"
    assert "timestamp" in record


def test_telemetry_failure_isolation(tmp_path: Path) -> None:
    """Test that log_event failure is isolated and doesn't crash the program."""
    # Pass an invalid directory path that raises OSError/PermissionError
    log_event(Path("/nonexistent_directory/invalid_path/xyz"), {"query": "fail?"})
    # Should not raise exception
    time.sleep(0.05)


def test_telemetry_user_marked_miss_and_extensibility(tmp_path: Path) -> None:
    """Test that telemetry supports user marked miss and extensibility with future fields."""
    event = {
        "query": "Trace callers",
        "user_marked_miss": True,
        "future_field_test": 12345,
    }
    
    log_event(tmp_path, event)
    time.sleep(0.1)
    
    log_file = tmp_path / "knowcode_telemetry.jsonl"
    record = json.loads(log_file.read_text(encoding="utf-8").strip())
    
    assert record["user_marked_miss"] is True
    assert record["future_field_test"] == 12345


def test_get_telemetry_summary(tmp_path: Path) -> None:
    """Test that get_telemetry_summary aggregates events correctly."""
    from knowcode.telemetry import get_telemetry_summary
    
    # 1. Test empty logs
    summary = get_telemetry_summary(tmp_path)
    assert summary["total_queries"] == 0
    assert summary["local_routing_rate"] == 0.0
    assert summary["average_sufficiency_score"] == 0.0
    assert summary["user_marked_misses"] == 0
    
    # 2. Add some events
    log_event(tmp_path, {"query": "Q1", "local_or_escalated": "local", "sufficiency_score": 0.8})
    log_event(tmp_path, {"query": "Q2", "local_or_escalated": "escalated", "sufficiency_score": 0.4})
    log_event(tmp_path, {"query": "Q3", "source": "local", "sufficiency_score": 0.9, "user_marked_miss": True})
    
    # Wait a bit for async writing to complete
    time.sleep(0.15)
    
    summary = get_telemetry_summary(tmp_path)
    assert summary["total_queries"] == 3
    assert summary["local_routing_rate"] == pytest.approx(2/3)
    assert summary["average_sufficiency_score"] == pytest.approx(0.7)
    assert summary["user_marked_misses"] == 1

