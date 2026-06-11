"""Telemetry and observability logging for KnowCode."""

import json
import time
from pathlib import Path
from typing import Any, Dict
from concurrent.futures import ThreadPoolExecutor

_executor = ThreadPoolExecutor(max_workers=1)

def log_event(store_path: str | Path, event: Dict[str, Any]) -> None:
    """Log telemetry event asynchronously to prevent blocking the query path."""
    import os
    if os.environ.get("KNOWCODE_TESTING") == "1":
        _write_event_sync(store_path, event)
    else:
        _executor.submit(_write_event_sync, store_path, event)

def _write_event_sync(store_path: str | Path, event: Dict[str, Any]) -> None:
    """Synchronously write event to JSONL telemetry log file."""
    try:
        store_dir = Path(store_path)
        if not store_dir.is_dir():
            store_dir = store_dir.parent
            
        log_file = store_dir / "knowcode_telemetry.jsonl"
        
        # Ensure default timestamp
        if "timestamp" not in event:
            event["timestamp"] = int(time.time())
            
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except Exception as e:
        import logging
        logging.error(f"Telemetry log failed: {e}")


def get_telemetry_summary(store_path: str | Path) -> Dict[str, Any]:
    """Read telemetry logs and return aggregate metrics for trend review."""
    try:
        store_dir = Path(store_path)
        if not store_dir.is_dir():
            store_dir = store_dir.parent
            
        log_file = store_dir / "knowcode_telemetry.jsonl"
        
        if not log_file.exists():
            return {
                "total_queries": 0,
                "local_routing_rate": 0.0,
                "average_sufficiency_score": 0.0,
                "user_marked_misses": 0,
            }
            
        queries_count = 0
        local_count = 0
        total_sufficiency = 0.0
        misses_count = 0
        
        with open(log_file, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    if "query" in event:
                        queries_count += 1
                        if event.get("local_or_escalated") == "local" or event.get("source") == "local":
                            local_count += 1
                        total_sufficiency += event.get("sufficiency_score", 0.0)
                    if event.get("user_marked_miss") or event.get("user_marked_misses"):
                        misses_count += 1
                except Exception:
                    continue
                    
        return {
            "total_queries": queries_count,
            "local_routing_rate": (local_count / queries_count) if queries_count > 0 else 0.0,
            "average_sufficiency_score": (total_sufficiency / queries_count) if queries_count > 0 else 0.0,
            "user_marked_misses": misses_count,
        }
    except Exception as e:
        import logging
        logging.error(f"Telemetry summary failed: {e}")
        return {
            "total_queries": 0,
            "local_routing_rate": 0.0,
            "average_sufficiency_score": 0.0,
            "user_marked_misses": 0,
        }

