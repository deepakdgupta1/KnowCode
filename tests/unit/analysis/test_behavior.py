"""Tests for static behavior analysis."""

from __future__ import annotations

from pathlib import Path

from knowcode.analysis.behavior import PythonBehaviorAnalyzer
from knowcode.indexing.graph_builder import GraphBuilder


def test_behavior_analyzer_marks_pure_read_only_function() -> None:
    summary = PythonBehaviorAnalyzer().analyze_source(
        """
        def total(values):
            return sum(values)
        """
    )

    assert summary.side_effect_class == "pure_or_read_only"
    assert summary.side_effects == []
    assert "sum" in summary.calls
    assert summary.confidence == 0.9


def test_behavior_analyzer_detects_state_mutation_and_io() -> None:
    summary = PythonBehaviorAnalyzer().analyze_source(
        """
        def update_cache(cache, key, value):
            print(key)
            cache[key] = value
            cache.update({"last": key})
        """
    )

    assert summary.side_effect_class == "io"
    assert "io" in summary.side_effects
    assert "state_mutation" in summary.side_effects
    assert "cache" in summary.writes
    assert "cache.update" in summary.calls


def test_graph_builder_attaches_behavior_metadata_to_python_functions(
    tmp_path: Path,
) -> None:
    module = tmp_path / "sample.py"
    module.write_text(
        """
def load(path):
    with open(path) as fh:
        return fh.read()
""",
        encoding="utf-8",
    )

    builder = GraphBuilder().build_from_directory(tmp_path)
    entity = next(e for e in builder.entities.values() if e.name == "load")

    assert entity.metadata["behavior"]["side_effect_class"] == "io"
    assert "io" in entity.metadata["behavior"]["side_effects"]
    assert entity.metadata["confidence"]["behavior"] == 0.65
