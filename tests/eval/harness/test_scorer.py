"""Unit tests for retrieval-eval scoring helpers."""

from __future__ import annotations

from tests.eval.harness import scorer


def test_score_record_preserves_judged_correct_when_structural_match() -> None:
    golden = {
        "query_id": "q_test",
        "query_text": "Where is the thing implemented?",
        "task_type": "locate",
        "difficulty": "easy",
        "expected_entities": ["src/example.py::thing"],
        "expected_files": ["src/example.py"],
        "correct": True,
    }
    retrieval = {
        "context_text": "context",
        "sufficiency_score": 0.9,
        "selected_entities": [{"entity_id": "src/example.py::thing"}],
    }

    score = scorer.score_record(golden, retrieval)

    assert score["correct"] is True


def test_score_record_downgrades_judged_correct_when_structural_match_fails() -> None:
    golden = {
        "query_id": "q_test",
        "query_text": "Where is the thing implemented?",
        "task_type": "locate",
        "difficulty": "easy",
        "expected_entities": ["src/example.py::thing"],
        "expected_files": ["src/example.py"],
        "correct": True,
    }
    retrieval = {
        "context_text": "context",
        "sufficiency_score": 0.9,
        "selected_entities": [{"entity_id": "src/other.py::other"}],
    }

    score = scorer.score_record(golden, retrieval)

    assert score["correct"] is False


def test_score_record_omits_correct_without_judgment() -> None:
    golden = {
        "query_id": "q_test",
        "query_text": "Where is the thing implemented?",
        "task_type": "locate",
        "difficulty": "easy",
        "expected_entities": ["src/example.py::thing"],
        "expected_files": ["src/example.py"],
    }
    retrieval = {
        "context_text": "context",
        "sufficiency_score": 0.9,
        "selected_entities": [{"entity_id": "src/example.py::thing"}],
    }

    score = scorer.score_record(golden, retrieval)

    assert "correct" not in score
