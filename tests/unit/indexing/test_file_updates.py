"""Validation contract for a prepared file update (Step 15).

Preparation is the last point at which an update can be rejected without
touching live state, so everything that would make a commit inconsistent —
a missing, mis-sized, or non-finite embedding, or a duplicate chunk id — has to
be caught here rather than halfway through the vector writes.
"""

from __future__ import annotations

import pytest

from knowcode.data_models import CodeChunk
from knowcode.indexing.file_updates import (
    FileMoveCommit,
    FileUpdateCommit,
    FileUpdateCommitError,
    FileUpdatePreparationError,
    PreparedFileUpdate,
    validate_prepared_chunks,
)


def _chunk(chunk_id: str, embedding: list[float] | None) -> CodeChunk:
    return CodeChunk(id=chunk_id, entity_id="e", content="c", embedding=embedding)


def test_a_valid_batch_passes() -> None:
    validate_prepared_chunks(
        "/repo/m.py", [_chunk("a", [0.1, 0.2]), _chunk("b", [0.3, 0.4])], 2
    )


def test_a_missing_embedding_is_rejected() -> None:
    with pytest.raises(FileUpdatePreparationError) as excinfo:
        validate_prepared_chunks("/repo/m.py", [_chunk("a", None)], 2)

    assert "has no embedding" in excinfo.value.reasons[0]
    assert excinfo.value.file_path == "/repo/m.py"


def test_a_wrong_dimension_is_rejected() -> None:
    with pytest.raises(FileUpdatePreparationError) as excinfo:
        validate_prepared_chunks("/repo/m.py", [_chunk("a", [0.1])], 2)

    assert "expected 2" in excinfo.value.reasons[0]


def test_a_non_finite_embedding_is_rejected() -> None:
    with pytest.raises(FileUpdatePreparationError) as excinfo:
        validate_prepared_chunks("/repo/m.py", [_chunk("a", [float("nan"), 0.2])], 2)

    assert "non-finite" in excinfo.value.reasons[0]


def test_a_duplicate_chunk_id_is_rejected() -> None:
    """Two rows for one id would make chunk and vector counts disagree."""
    with pytest.raises(FileUpdatePreparationError) as excinfo:
        validate_prepared_chunks(
            "/repo/m.py", [_chunk("a", [0.1, 0.2]), _chunk("a", [0.3, 0.4])], 2
        )

    assert "duplicate chunk id" in excinfo.value.reasons[0]


def test_every_reason_is_reported_together() -> None:
    with pytest.raises(FileUpdatePreparationError) as excinfo:
        validate_prepared_chunks(
            "/repo/m.py", [_chunk("a", None), _chunk("b", [0.1])], 2
        )

    assert len(excinfo.value.reasons) == 2


def test_a_prepared_update_reports_its_chunk_ids() -> None:
    update = PreparedFileUpdate(
        "/repo/m.py", (_chunk("a", [0.1, 0.2]), _chunk("b", [0.3, 0.4])), 2
    )

    assert update.chunk_ids == ("a", "b")
    assert update.is_deletion is False


def test_an_empty_prepared_update_is_a_deletion() -> None:
    assert PreparedFileUpdate("/repo/m.py", (), 2).is_deletion is True


def test_a_commit_record_counts_only_committed_ids() -> None:
    commit = FileUpdateCommit(
        file_path="/repo/m.py",
        previous_chunk_ids=("a", "b"),
        committed_chunk_ids=("a",),
        removed_chunk_ids=("b",),
    )

    assert commit.chunk_count == 1
    assert commit.is_deletion is False
    assert FileUpdateCommit(file_path="/repo/m.py").is_deletion is True


def test_a_move_commit_names_both_sides() -> None:
    move = FileMoveCommit(
        committed=FileUpdateCommit(file_path="/repo/new.py"),
        removed=FileUpdateCommit(file_path="/repo/old.py"),
    )

    assert move.committed.file_path == "/repo/new.py"
    assert move.removed.file_path == "/repo/old.py"


def test_a_commit_error_is_never_marked_recovered() -> None:
    """Recovery is reported on the commit record, failure on the exception."""
    error = FileUpdateCommitError("/repo/m.py", "vector backend unavailable")

    assert error.recovered is False
    assert "vector backend unavailable" in str(error)
