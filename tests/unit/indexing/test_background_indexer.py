"""Unit tests for background indexing."""

import time
from pathlib import Path

from knowcode.indexing.background_indexer import BackgroundIndexer


class DummyIndexer:
    def __init__(self) -> None:
        self.calls: list[Path] = []

    def index_file(self, path: Path) -> int:
        self.calls.append(path)
        return 1


def test_background_indexer_processes_queue(tmp_path: Path) -> None:
    """Queued files should be processed by the worker thread."""
    indexer = DummyIndexer()
    bg = BackgroundIndexer(indexer)
    bg.start()

    target = tmp_path / "file.py"
    target.write_text("print('hi')", encoding="utf-8")
    bg.queue_file(target)

    for _ in range(20):
        if indexer.calls:
            break
        time.sleep(0.05)

    bg.stop()

    assert indexer.calls == [target]
