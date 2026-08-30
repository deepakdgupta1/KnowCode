"""Unit tests for the pre-flight codebase quality assessment engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from knowcode.analysis.preflight import (
    DEFAULT_WEIGHTS,
    DimensionScore,
    PreflightReport,
    _score_to_grade,
    _score_parse_success_rate,
    _score_language_coverage,
    _score_documentation_density,
    _score_naming_quality,
    _score_structural_depth,
    _score_relationship_density,
    _score_type_annotation_coverage,
    _score_complexity_distribution,
    _score_behavior_analyzability,
    _score_unresolved_references,
    _generate_recommendations,
    assess_codebase,
)
from knowcode.data_models import (
    Entity,
    EntityKind,
    Location,
    Relationship,
    RelationshipKind,
)
from knowcode.indexing.scanner import FileInfo


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _entity(
    name: str,
    kind: EntityKind = EntityKind.FUNCTION,
    *,
    file_path: str = "mod.py",
    docstring: str | None = None,
    signature: str | None = None,
    source_code: str | None = None,
    metadata: dict | None = None,
) -> Entity:
    """Build a minimal Entity for testing."""
    return Entity(
        id=f"{file_path}::{name}",
        kind=kind,
        name=name,
        qualified_name=f"mod.{name}",
        location=Location(file_path=file_path, line_start=1, line_end=10),
        docstring=docstring,
        signature=signature,
        source_code=source_code,
        metadata=metadata or {},
    )


def _file_info(
    name: str,
    extension: str,
    size_bytes: int = 100,
) -> FileInfo:
    """Build a minimal FileInfo for testing."""
    return FileInfo(
        path=Path(name),
        relative_path=name,
        extension=extension,
        size_bytes=size_bytes,
    )


# ──────────────────────────────────────────────────────────────────────
# Grade mapping
# ──────────────────────────────────────────────────────────────────────


class TestScoreToGrade:
    def test_perfect_score(self) -> None:
        assert _score_to_grade(1.0) == "A"

    def test_a_boundary(self) -> None:
        assert _score_to_grade(0.90) == "A"

    def test_b_boundary(self) -> None:
        assert _score_to_grade(0.75) == "B"
        assert _score_to_grade(0.89) == "B"

    def test_c_boundary(self) -> None:
        assert _score_to_grade(0.55) == "C"
        assert _score_to_grade(0.74) == "C"

    def test_d_boundary(self) -> None:
        assert _score_to_grade(0.35) == "D"
        assert _score_to_grade(0.54) == "D"

    def test_f_boundary(self) -> None:
        assert _score_to_grade(0.0) == "F"
        assert _score_to_grade(0.34) == "F"


# ──────────────────────────────────────────────────────────────────────
# Parse success rate
# ──────────────────────────────────────────────────────────────────────


class TestParseSuccessRate:
    def test_no_parseable_files(self) -> None:
        files = [_file_info("image.png", ".png")]
        result = _score_parse_success_rate(files, [])
        assert result.score == 0.0
        assert result.grade == "F"

    def test_all_files_parsed(self) -> None:
        files = [_file_info("a.py", ".py"), _file_info("b.py", ".py")]
        result = _score_parse_success_rate(files, [])
        assert result.score == 1.0
        assert result.grade == "A"

    def test_some_parse_errors(self) -> None:
        files = [_file_info("a.py", ".py"), _file_info("b.py", ".py")]
        errors = ["Syntax error: invalid syntax"]
        result = _score_parse_success_rate(files, errors)
        assert result.score == 0.5
        assert result.grade == "D"

    def test_unsupported_errors_excluded(self) -> None:
        files = [_file_info("a.py", ".py")]
        errors = ["Unsupported file type: .go"]
        result = _score_parse_success_rate(files, errors)
        assert result.score == 1.0  # unsupported errors don't count


# ──────────────────────────────────────────────────────────────────────
# Language coverage
# ──────────────────────────────────────────────────────────────────────


class TestLanguageCoverage:
    def test_all_supported(self) -> None:
        files = [_file_info("a.py", ".py"), _file_info("b.js", ".js")]
        result = _score_language_coverage(files)
        assert result.score == 1.0

    def test_mixed_languages(self) -> None:
        files = [
            _file_info("a.py", ".py"),
            _file_info("b.go", ".go"),
        ]
        result = _score_language_coverage(files)
        assert result.score == 0.5  # 1/2 code files supported

    def test_no_code_files(self) -> None:
        files = [_file_info("readme.txt", ".txt")]
        result = _score_language_coverage(files)
        assert result.score == 1.0  # no code = nothing unsupported


# ──────────────────────────────────────────────────────────────────────
# Documentation density
# ──────────────────────────────────────────────────────────────────────


class TestDocumentationDensity:
    def test_all_documented(self) -> None:
        entities = {
            "a": _entity("func_a", docstring="Does something."),
            "b": _entity("func_b", docstring="Does other."),
        }
        result = _score_documentation_density(entities)
        assert result.score == 1.0

    def test_none_documented(self) -> None:
        entities = {
            "a": _entity("func_a"),
            "b": _entity("func_b"),
        }
        result = _score_documentation_density(entities)
        assert result.score == 0.0

    def test_half_documented(self) -> None:
        entities = {
            "a": _entity("func_a", docstring="Documented."),
            "b": _entity("func_b"),
        }
        result = _score_documentation_density(entities)
        assert result.score == 0.5

    def test_non_documentable_excluded(self) -> None:
        """Modules and documents are not counted."""
        entities = {
            "mod": _entity("mod", EntityKind.MODULE),
            "func": _entity("func", docstring="Yes."),
        }
        result = _score_documentation_density(entities)
        assert result.score == 1.0  # only func counts, and it has a docstring


# ──────────────────────────────────────────────────────────────────────
# Naming quality
# ──────────────────────────────────────────────────────────────────────


class TestNamingQuality:
    def test_good_names(self) -> None:
        entities = {
            "a": _entity("process_data"),
            "b": _entity("validate_input"),
            "c": _entity("MyClass", EntityKind.CLASS),
        }
        result = _score_naming_quality(entities)
        assert result.score > 0.7

    def test_single_char_names(self) -> None:
        entities = {
            "a": _entity("f"),
            "b": _entity("g"),
        }
        result = _score_naming_quality(entities)
        assert result.score < 0.5

    def test_no_scoreable(self) -> None:
        entities = {
            "mod": _entity("mod", EntityKind.MODULE),
        }
        result = _score_naming_quality(entities)
        assert result.score == 0.0


# ──────────────────────────────────────────────────────────────────────
# Structural depth
# ──────────────────────────────────────────────────────────────────────


class TestStructuralDepth:
    def test_well_structured(self) -> None:
        entities = {
            "mod": _entity("mod", EntityKind.MODULE),
            "cls": _entity("MyClass", EntityKind.CLASS),
            "meth": _entity("do_stuff", EntityKind.METHOD),
        }
        rels = [
            Relationship("mod::mod", "mod.py::MyClass", RelationshipKind.CONTAINS),
            Relationship(
                "mod.py::MyClass", "mod.py::do_stuff", RelationshipKind.CONTAINS
            ),
        ]
        # Fix entity ids to match
        entities = {
            "mod::mod": _entity("mod", EntityKind.MODULE),
            "mod.py::MyClass": _entity("MyClass", EntityKind.CLASS),
            "mod.py::do_stuff": _entity("do_stuff", EntityKind.METHOD),
        }
        result = _score_structural_depth(entities, rels)
        assert result.score > 0.5

    def test_no_code_entities(self) -> None:
        entities = {
            "doc": _entity("readme", EntityKind.DOCUMENT),
        }
        result = _score_structural_depth(entities, [])
        assert result.score == 0.0


# ──────────────────────────────────────────────────────────────────────
# Relationship density
# ──────────────────────────────────────────────────────────────────────


class TestRelationshipDensity:
    def test_well_connected(self) -> None:
        entities = {
            "mod.py::func_a": _entity("func_a"),
            "mod.py::func_b": _entity("func_b"),
        }
        rels = [
            Relationship("mod.py::func_a", "mod.py::func_b", RelationshipKind.CALLS),
            Relationship("mod.py::func_b", "mod.py::func_a", RelationshipKind.CALLS),
            Relationship("mod.py::func_a", "external::os", RelationshipKind.IMPORTS),
            Relationship("mod.py::func_b", "external::sys", RelationshipKind.IMPORTS),
        ]
        result = _score_relationship_density(entities, rels)
        assert result.score > 0.7

    def test_disconnected(self) -> None:
        entities = {
            "a": _entity("func_a"),
            "b": _entity("func_b"),
        }
        result = _score_relationship_density(entities, [])
        assert result.score == 0.0


# ──────────────────────────────────────────────────────────────────────
# Type annotation coverage
# ──────────────────────────────────────────────────────────────────────


class TestTypeAnnotationCoverage:
    def test_all_annotated(self) -> None:
        entities = {
            "a": _entity("func_a", signature="def func_a(x: int) -> bool"),
            "b": _entity("func_b", signature="def func_b() -> None"),
        }
        result = _score_type_annotation_coverage(entities)
        assert result.score == 1.0

    def test_none_annotated(self) -> None:
        entities = {
            "a": _entity("func_a", signature="def func_a(x)"),
            "b": _entity("func_b", signature="def func_b()"),
        }
        result = _score_type_annotation_coverage(entities)
        assert result.score == 0.0

    def test_non_python_excluded(self) -> None:
        entities = {
            "a": _entity("func_a", file_path="mod.js", signature="function func_a()"),
        }
        result = _score_type_annotation_coverage(entities)
        assert result.score == 1.0  # no Python functions → N/A → 1.0


# ──────────────────────────────────────────────────────────────────────
# Complexity distribution
# ──────────────────────────────────────────────────────────────────────


class TestComplexityDistribution:
    def test_no_data(self) -> None:
        entities = {"a": _entity("func_a")}
        result = _score_complexity_distribution(entities)
        assert result.score == 0.7  # neutral default
        assert result.grade == "B"

    def test_low_complexity(self) -> None:
        entities = {
            "a": _entity("func_a", metadata={"complexity": 3}),
            "b": _entity("func_b", metadata={"complexity": 5}),
        }
        result = _score_complexity_distribution(entities)
        assert result.score > 0.8

    def test_high_complexity(self) -> None:
        entities = {
            "a": _entity("func_a", metadata={"complexity": 30}),
            "b": _entity("func_b", metadata={"complexity": 25}),
        }
        result = _score_complexity_distribution(entities)
        assert result.score < 0.4


# ──────────────────────────────────────────────────────────────────────
# Behavior analyzability
# ──────────────────────────────────────────────────────────────────────


class TestBehaviorAnalyzability:
    def test_all_analyzable(self) -> None:
        entities = {
            "a": _entity(
                "func_a",
                metadata={"behavior": {"confidence": 0.9}},
            ),
            "b": _entity(
                "func_b",
                metadata={"behavior": {"confidence": 0.65}},
            ),
        }
        result = _score_behavior_analyzability(entities)
        assert result.score == 1.0

    def test_low_confidence(self) -> None:
        entities = {
            "a": _entity(
                "func_a",
                metadata={"behavior": {"confidence": 0.3}},
            ),
        }
        result = _score_behavior_analyzability(entities)
        assert result.score == 0.0

    def test_no_python_functions(self) -> None:
        entities = {
            "a": _entity("func_a", file_path="mod.js"),
        }
        result = _score_behavior_analyzability(entities)
        assert result.score == 1.0  # N/A


# ──────────────────────────────────────────────────────────────────────
# Unresolved references
# ──────────────────────────────────────────────────────────────────────


class TestUnresolvedReferences:
    def test_all_resolved(self) -> None:
        rels = [
            Relationship("a", "b", RelationshipKind.CALLS),
            Relationship("b", "c", RelationshipKind.INHERITS),
        ]
        result = _score_unresolved_references(rels)
        assert result.score == 1.0

    def test_some_unresolved(self) -> None:
        rels = [
            Relationship("a", "b", RelationshipKind.CALLS),
            Relationship("a", "ref::UnknownThing", RelationshipKind.CALLS),
        ]
        result = _score_unresolved_references(rels)
        assert result.score == 0.5

    def test_no_resolvable(self) -> None:
        rels = [
            Relationship("a", "b", RelationshipKind.CONTAINS),
        ]
        result = _score_unresolved_references(rels)
        assert result.score == 1.0  # no resolvable refs


# ──────────────────────────────────────────────────────────────────────
# Recommendations
# ──────────────────────────────────────────────────────────────────────


class TestRecommendations:
    def test_no_recs_for_perfect_scores(self) -> None:
        dims = [
            DimensionScore("parse_success_rate", 1.0, "A", "ok", {"errors": 0}),
            DimensionScore("language_coverage", 1.0, "A", "ok"),
            DimensionScore("documentation_density", 1.0, "A", "ok"),
            DimensionScore("naming_quality", 1.0, "A", "ok"),
            DimensionScore("relationship_density", 1.0, "A", "ok"),
            DimensionScore("type_annotation_coverage", 1.0, "A", "ok"),
            DimensionScore("complexity_distribution", 1.0, "A", "ok"),
            DimensionScore("unresolved_references", 1.0, "A", "ok"),
        ]
        recs = _generate_recommendations(dims)
        assert len(recs) == 0

    def test_recs_for_low_docs(self) -> None:
        dims = [
            DimensionScore("documentation_density", 0.2, "F", "low"),
        ]
        recs = _generate_recommendations(dims)
        assert len(recs) == 1
        assert "docstrings" in recs[0].lower()


# ──────────────────────────────────────────────────────────────────────
# Full assessment
# ──────────────────────────────────────────────────────────────────────


class TestAssessCodebase:
    def test_well_written_codebase(self) -> None:
        """A codebase with good practices scores high."""
        entities = {
            f"mod.py::func_{i}": _entity(
                f"process_item_{i}",
                docstring=f"Process item {i}.",
                signature=f"def process_item_{i}(x: int) -> bool",
                metadata={"behavior": {"confidence": 0.9}},
            )
            for i in range(10)
        }
        # Add a module
        entities["mod.py::mod"] = _entity("mod", EntityKind.MODULE)
        # Add a class
        entities["mod.py::DataProcessor"] = _entity(
            "DataProcessor",
            EntityKind.CLASS,
            docstring="Processes data.",
        )

        rels = [
            Relationship(
                f"mod.py::func_{i}",
                f"mod.py::func_{(i + 1) % 10}",
                RelationshipKind.CALLS,
            )
            for i in range(10)
        ]
        rels.append(
            Relationship(
                "mod.py::mod", "mod.py::DataProcessor", RelationshipKind.CONTAINS
            )
        )

        files = [_file_info("mod.py", ".py")]

        report = assess_codebase(entities, rels, files, [])
        assert isinstance(report, PreflightReport)
        assert report.overall_score > 0.6
        assert report.overall_grade in ("A", "B", "C")
        assert len(report.dimensions) == 10
        assert report.entity_count == 12
        assert report.scanned_file_count == 1

    def test_poor_codebase(self) -> None:
        """A codebase with poor practices scores low."""
        entities = {f"mod.py::f{i}": _entity("f", metadata={}) for i in range(5)}
        files = [
            _file_info("mod.py", ".py"),
            _file_info("util.go", ".go"),
        ]
        errors = ["Syntax error: unexpected EOF"]

        report = assess_codebase(entities, [], files, errors)
        assert report.overall_score < 0.5
        assert len(report.recommendations) > 0

    def test_report_serialization(self) -> None:
        entities = {"a": _entity("func_a", docstring="ok.")}
        report = assess_codebase(entities, [], [_file_info("a.py", ".py")], [])
        d = report.to_dict()
        assert isinstance(d, dict)
        assert "overall_score" in d
        assert "dimensions" in d
        assert isinstance(d["dimensions"], list)
        assert "recommendations" in d
        assert "language_breakdown" in d

    def test_custom_weights(self) -> None:
        """All the weight on one dimension makes the composite that dimension.

        Rewritten twice over. It used to pass partial overrides -- weights that
        summed to 1.70 -- which BL-21 now rejects, because a set that does not
        normalise silently rescales every score. And it only asserted that both
        calls returned a float, so it could not have detected a composite that
        ignored the weights entirely. Concentrating the whole weight on one
        dimension gives an exact expected value instead.
        """
        entities = {"a": _entity("func_a", docstring="Documented.")}
        files = [_file_info("a.py", ".py")]

        def _only(dimension: str) -> tuple[float, float]:
            weights = {key: 0.0 for key in DEFAULT_WEIGHTS}
            weights[dimension] = 1.0
            report = assess_codebase(entities, [], files, [], weights=weights)
            scored = {d.dimension: d.score for d in report.dimensions}
            return report.overall_score, scored[dimension]

        doc_overall, doc_dimension = _only("documentation_density")
        name_overall, name_dimension = _only("naming_quality")

        assert doc_overall == pytest.approx(doc_dimension)
        assert name_overall == pytest.approx(name_dimension)
        assert doc_overall != name_overall

    def test_empty_codebase(self) -> None:
        """Empty codebase produces a valid report with low scores."""
        report = assess_codebase({}, [], [], [])
        assert isinstance(report, PreflightReport)
        assert report.entity_count == 0
        assert len(report.dimensions) == 10
