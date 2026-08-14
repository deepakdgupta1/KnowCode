"""Telemetry and observability logging for KnowCode."""

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from concurrent.futures import Future, ThreadPoolExecutor, wait

#: The writer pool is created on first use and released by
#: :func:`shutdown_telemetry`, so a server shutdown (Step 17) can drain queued
#: writes instead of letting the process exit with them still in the pool. It is
#: re-created on demand, so one server's shutdown never breaks the next one's
#: logging in the same process.
_executor: Optional[ThreadPoolExecutor] = None
_pending: List["Future[None]"] = []
_executor_lock = threading.Lock()


def _submit(store_path: str | Path, event: Dict[str, Any]) -> None:
    """Queue one write on the pool, creating it if needed."""
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="knowcode-telemetry"
            )
        _pending[:] = [future for future in _pending if not future.done()]
        _pending.append(_executor.submit(_write_event_sync, store_path, event))


def shutdown_telemetry(timeout: float = 5.0) -> bool:
    """Drain queued telemetry writes and release the pool. Idempotent.

    Args:
        timeout: Upper bound on the drain, so shutdown stays bounded even when
            the telemetry directory has become unwritable.

    Returns:
        Whether every accepted write finished. ``False`` means some were still
        running when the budget ran out — the caller reports that rather than
        claiming telemetry is durable.
    """
    global _executor
    with _executor_lock:
        executor, _executor = _executor, None
        pending, _pending[:] = list(_pending), []
    if executor is None:
        return True
    _, not_done = wait(pending, timeout=timeout)
    executor.shutdown(wait=False)
    return not not_done


def log_event(store_path: str | Path, event: Dict[str, Any]) -> None:
    """Log telemetry event asynchronously to prevent blocking the query path."""
    import os
    if os.environ.get("KNOWCODE_TESTING") == "1":
        _write_event_sync(store_path, event)
    else:
        _submit(store_path, event)

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

