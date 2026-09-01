"""Pre-flight codebase quality assessment for KnowCode.

Evaluates a target codebase *after* parsing but *before* semantic indexing,
producing a structured report card of quality dimensions that predict how
well KnowCode's surfaces will perform on this codebase.

The assessment is designed to run cheaply — it inspects the already-parsed
``GraphBuilder`` state and the scanned file list, never re-reading source
files or invoking external services.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional

from knowcode.data_models import Entity, EntityKind, Relationship, RelationshipKind
from knowcode.indexing.scanner import FileInfo, Scanner
from knowcode.utils.entity_identity import EndpointKind, classify_endpoint_id


# ──────────────────────────────────────────────────────────────────────
# Data models
# ──────────────────────────────────────────────────────────────────────

_GRADE_THRESHOLDS: list[tuple[float, str]] = [
    (0.90, "A"),
    (0.75, "B"),
    (0.55, "C"),
    (0.35, "D"),
    (0.00, "F"),
]


def _score_to_grade(score: float) -> str:
    """Map a 0.0–1.0 score to a letter grade."""
    for threshold, grade in _GRADE_THRESHOLDS:
        if score >= threshold:
            return grade
    return "F"  # pragma: no cover — the 0.00 entry catches everything


@dataclass(frozen=True)
class DimensionScore:
    """Score for one quality dimension."""

    dimension: str
    score: float
    grade: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PreflightReport:
    """Complete quality assessment report for a target codebase."""

    overall_score: float
    overall_grade: str
    dimensions: list[DimensionScore]
    summary: str
    recommendations: list[str]
    timestamp: float
    scanned_file_count: int
    entity_count: int
    relationship_count: int
    language_breakdown: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        """Serialize the report for JSON output."""
        return {
            "overall_score": round(self.overall_score, 3),
            "overall_grade": self.overall_grade,
            "dimensions": [asdict(d) for d in self.dimensions],
            "summary": self.summary,
            "recommendations": self.recommendations,
            "timestamp": self.timestamp,
            "scanned_file_count": self.scanned_file_count,
            "entity_count": self.entity_count,
            "relationship_count": self.relationship_count,
            "language_breakdown": self.language_breakdown,
        }


# ──────────────────────────────────────────────────────────────────────
# Default dimension weights (summing to 1.0)
# ──────────────────────────────────────────────────────────────────────

DEFAULT_WEIGHTS: dict[str, float] = {
    "parse_success_rate": 0.20,
    "language_coverage": 0.10,
    "documentation_density": 0.20,
    "naming_quality": 0.10,
    "structural_depth": 0.05,
    "relationship_density": 0.15,
    "type_annotation_coverage": 0.05,
    "complexity_distribution": 0.05,
    "behavior_analyzability": 0.05,
    "unresolved_references": 0.05,
}

# Extensions KnowCode supports (mirrored from Scanner.SUPPORTED_EXTENSIONS)
_SUPPORTED_EXTENSIONS = Scanner.SUPPORTED_EXTENSIONS

# Extensions commonly found in codebases that KnowCode does *not* parse
_KNOWN_CODE_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".dart",
    ".go",
    ".h",
    ".hpp",
    ".jl",
    ".kt",
    ".lua",
    ".m",
    ".mm",
    ".php",
    ".pl",
    ".r",
    ".rb",
    ".scala",
    ".sh",
    ".swift",
    ".zig",
}


# ──────────────────────────────────────────────────────────────────────
# Individual dimension scorers
# ──────────────────────────────────────────────────────────────────────


def _score_parse_success_rate(
    scanned_files: list[FileInfo],
    parse_errors: list[str],
) -> DimensionScore:
    """Fraction of scanned source files that parsed without errors.

    Each error string in ``parse_errors`` represents one file that failed to
    parse (syntax error, read error, etc.). Files with unsupported extensions
    report an error too, but they are not source files KnowCode tried to
    parse — so they are excluded.
    """
    # Only count files whose extension KnowCode actually has a parser for.
    parseable_files = [f for f in scanned_files if f.extension in _SUPPORTED_EXTENSIONS]
    total = len(parseable_files)
    if total == 0:
        return DimensionScore(
            dimension="parse_success_rate",
            score=0.0,
            grade="F",
            detail="No parseable source files found.",
            evidence={"parseable_files": 0, "errors": 0},
        )

    # Real parse errors (syntax, read failures) — not unsupported-extension
    real_errors = [
        e for e in parse_errors if not e.startswith("Unsupported file type:")
    ]
    error_count = len(real_errors)
    score = max(0.0, 1.0 - (error_count / total))
    return DimensionScore(
        dimension="parse_success_rate",
        score=score,
        grade=_score_to_grade(score),
        detail=f"{total - error_count}/{total} files parsed successfully.",
        evidence={
            "parseable_files": total,
            "errors": error_count,
            "sample_errors": real_errors[:5],
        },
    )


def _score_language_coverage(
    scanned_files: list[FileInfo],
) -> DimensionScore:
    """Fraction of source-code files (by known code extension) that KnowCode supports.

    Non-code files (images, data, config) are excluded entirely. Only files
    whose extension appears in ``_SUPPORTED_EXTENSIONS`` or
    ``_KNOWN_CODE_EXTENSIONS`` are considered.
    """
    all_code_extensions = _SUPPORTED_EXTENSIONS | _KNOWN_CODE_EXTENSIONS
    code_files = [f for f in scanned_files if f.extension in all_code_extensions]
    total = len(code_files)
    if total == 0:
        return DimensionScore(
            dimension="language_coverage",
            score=1.0,
            grade="A",
            detail="No code files detected; nothing unsupported.",
            evidence={"total_code_files": 0, "supported": 0, "unsupported": {}},
        )

    supported = sum(1 for f in code_files if f.extension in _SUPPORTED_EXTENSIONS)
    unsupported_breakdown: dict[str, int] = {}
    for f in code_files:
        if f.extension not in _SUPPORTED_EXTENSIONS:
            unsupported_breakdown[f.extension] = (
                unsupported_breakdown.get(f.extension, 0) + 1
            )

    score = supported / total
    return DimensionScore(
        dimension="language_coverage",
        score=score,
        grade=_score_to_grade(score),
        detail=f"{supported}/{total} code files in supported languages.",
        evidence={
            "total_code_files": total,
            "supported": supported,
            "unsupported": unsupported_breakdown,
        },
    )


def _score_documentation_density(
    entities: dict[str, Entity],
) -> DimensionScore:
    """Fraction of function/method/class entities that carry a docstring.

    Modules and document entities are excluded — they are structural nodes
    where documentation presence has less impact on retrieval quality.
    """
    documentable_kinds = {EntityKind.FUNCTION, EntityKind.METHOD, EntityKind.CLASS}
    documentable = [e for e in entities.values() if e.kind in documentable_kinds]
    total = len(documentable)
    if total == 0:
        return DimensionScore(
            dimension="documentation_density",
            score=0.0,
            grade="F",
            detail="No documentable entities found.",
            evidence={"documentable_entities": 0, "with_docstring": 0},
        )

    with_docstring = sum(1 for e in documentable if e.docstring)
    score = with_docstring / total
    return DimensionScore(
        dimension="documentation_density",
        score=score,
        grade=_score_to_grade(score),
        detail=f"{with_docstring}/{total} entities have docstrings.",
        evidence={
            "documentable_entities": total,
            "with_docstring": with_docstring,
            "by_kind": {
                kind.value: {
                    "total": sum(1 for e in documentable if e.kind == kind),
                    "documented": sum(
                        1 for e in documentable if e.kind == kind and e.docstring
                    ),
                }
                for kind in documentable_kinds
            },
        },
    )


# Naming heuristics
_SINGLE_CHAR_RE = re.compile(r"^[a-zA-Z_]$")
_SNAKE_CASE_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_CAMEL_CASE_RE = re.compile(r"^[A-Z][a-zA-Z0-9]*$")
_MIXED_CASE_RE = re.compile(r"^[a-z][a-zA-Z0-9]*$")


def _score_naming_quality(
    entities: dict[str, Entity],
) -> DimensionScore:
    """Assess naming quality of function/method/class entities.

    Heuristics:
    - Penalise single-character names (functions/classes, not loop vars)
    - Reward consistent convention (snake_case for functions, CamelCase for classes)
    - Penalise very short average name lengths (< 4 characters)
    """
    scoreable_kinds = {EntityKind.FUNCTION, EntityKind.METHOD, EntityKind.CLASS}
    scoreable = [e for e in entities.values() if e.kind in scoreable_kinds]
    total = len(scoreable)
    if total == 0:
        return DimensionScore(
            dimension="naming_quality",
            score=0.0,
            grade="F",
            detail="No scoreable entities for naming analysis.",
            evidence={"total": 0},
        )

    single_char_count = sum(1 for e in scoreable if _SINGLE_CHAR_RE.match(e.name))
    avg_name_length = sum(len(e.name) for e in scoreable) / total

    # Convention adherence: functions/methods should be snake_case,
    # classes should be CamelCase
    convention_hits = 0
    for e in scoreable:
        if e.kind in {EntityKind.FUNCTION, EntityKind.METHOD}:
            if _SNAKE_CASE_RE.match(e.name) or _MIXED_CASE_RE.match(e.name):
                convention_hits += 1
        elif e.kind == EntityKind.CLASS:
            if _CAMEL_CASE_RE.match(e.name):
                convention_hits += 1

    # Composite: 40% convention, 30% no single-char, 30% reasonable length
    convention_score = convention_hits / total
    single_char_score = 1.0 - (single_char_count / total)
    # Map avg length: 1→0.0, 4→0.7, 8+→1.0
    length_score = min(1.0, max(0.0, (avg_name_length - 1) / 7))

    score = 0.4 * convention_score + 0.3 * single_char_score + 0.3 * length_score
    return DimensionScore(
        dimension="naming_quality",
        score=score,
        grade=_score_to_grade(score),
        detail=(
            f"Avg name length: {avg_name_length:.1f} chars, "
            f"{convention_hits}/{total} follow convention, "
            f"{single_char_count} single-char names."
        ),
        evidence={
            "total": total,
            "avg_name_length": round(avg_name_length, 2),
            "convention_adherence": round(convention_score, 3),
            "single_char_count": single_char_count,
        },
    )


def _score_structural_depth(
    entities: dict[str, Entity],
    relationships: list[Relationship],
) -> DimensionScore:
    """Evaluate structural nesting (containment hierarchy) and orphan ratio.

    Good codebases have a meaningful containment tree: modules → classes →
    methods. Orphan entities (those with no containment edge inbound or
    outbound) indicate flat, poorly-organised code that produces less useful
    dependency expansion.
    """
    code_kinds = {
        EntityKind.MODULE,
        EntityKind.CLASS,
        EntityKind.FUNCTION,
        EntityKind.METHOD,
    }
    code_entities = {eid: e for eid, e in entities.items() if e.kind in code_kinds}
    total = len(code_entities)
    if total == 0:
        return DimensionScore(
            dimension="structural_depth",
            score=0.0,
            grade="F",
            detail="No code entities for structural analysis.",
            evidence={"total": 0},
        )

    containment_edges = [
        r
        for r in relationships
        if r.kind == RelationshipKind.CONTAINS
        and r.source_id in code_entities
        and r.target_id in code_entities
    ]

    # Entities that participate in at least one containment edge
    in_containment = set()
    for r in containment_edges:
        in_containment.add(r.source_id)
        in_containment.add(r.target_id)

    orphan_count = sum(1 for eid in code_entities if eid not in in_containment)
    # Modules are naturally roots, don't penalise them as orphans
    module_count = sum(1 for e in code_entities.values() if e.kind == EntityKind.MODULE)
    effective_orphans = max(0, orphan_count - module_count)
    non_module_total = total - module_count

    orphan_ratio = effective_orphans / max(non_module_total, 1)
    structure_score = 1.0 - orphan_ratio

    # Bonus for multi-level nesting (depth > 1)
    children_of: dict[str, list[str]] = {}
    for r in containment_edges:
        children_of.setdefault(r.source_id, []).append(r.target_id)

    max_depth = 0
    for root in code_entities:
        depth = _tree_depth(root, children_of, visited=set())
        max_depth = max(max_depth, depth)

    # Moderate depth (2-3) is ideal; very deep (>6) or flat (0-1) is worse
    depth_score = (
        min(1.0, max_depth / 3)
        if max_depth <= 6
        else max(0.5, 1.0 - (max_depth - 6) / 10)
    )

    score = 0.6 * structure_score + 0.4 * depth_score
    return DimensionScore(
        dimension="structural_depth",
        score=score,
        grade=_score_to_grade(score),
        detail=(
            f"{effective_orphans} orphan entities of {non_module_total} non-module, "
            f"max containment depth {max_depth}."
        ),
        evidence={
            "total_code_entities": total,
            "orphan_count": effective_orphans,
            "max_depth": max_depth,
            "containment_edges": len(containment_edges),
        },
    )


def _tree_depth(
    node: str,
    children_of: dict[str, list[str]],
    visited: set[str],
) -> int:
    """Compute max depth of a containment tree rooted at ``node``."""
    if node in visited:
        return 0
    visited.add(node)
    children = children_of.get(node, [])
    if not children:
        return 1
    return 1 + max(_tree_depth(c, children_of, visited) for c in children)


def _score_relationship_density(
    entities: dict[str, Entity],
    relationships: list[Relationship],
) -> DimensionScore:
    """Edges-per-entity ratio and fraction of entities with ≥1 relationship.

    Sparse relationship graphs cripple ``trace_calls`` and ``expand_deps``.
    The ideal target is ≥2 meaningful edges per code entity.
    """
    code_kinds = {
        EntityKind.FUNCTION,
        EntityKind.METHOD,
        EntityKind.CLASS,
        EntityKind.MODULE,
    }
    code_entities = {eid for eid, e in entities.items() if e.kind in code_kinds}
    total = len(code_entities)
    if total == 0:
        return DimensionScore(
            dimension="relationship_density",
            score=0.0,
            grade="F",
            detail="No code entities for relationship analysis.",
            evidence={"total": 0},
        )

    # Count meaningful edges (calls, imports, inherits, implements)
    meaningful_kinds = {
        RelationshipKind.CALLS,
        RelationshipKind.IMPORTS,
        RelationshipKind.INHERITS,
        RelationshipKind.IMPLEMENTS,
        RelationshipKind.USES_TYPE,
    }
    meaningful_rels = [r for r in relationships if r.kind in meaningful_kinds]
    edge_count = len(meaningful_rels)

    # Entities that appear in at least one meaningful relationship
    connected = set()
    for r in meaningful_rels:
        if r.source_id in code_entities:
            connected.add(r.source_id)
        if r.target_id in code_entities:
            connected.add(r.target_id)

    connected_ratio = len(connected) / total
    edges_per_entity = edge_count / total

    # Score: 50% connectivity (0→0, 1→1), 50% density (0→0, 2→1, capped)
    connectivity_score = connected_ratio
    density_score = min(1.0, edges_per_entity / 2.0)
    score = 0.5 * connectivity_score + 0.5 * density_score

    return DimensionScore(
        dimension="relationship_density",
        score=score,
        grade=_score_to_grade(score),
        detail=(
            f"{len(connected)}/{total} entities connected, "
            f"{edges_per_entity:.1f} meaningful edges/entity."
        ),
        evidence={
            "total_code_entities": total,
            "connected_entities": len(connected),
            "meaningful_edges": edge_count,
            "edges_per_entity": round(edges_per_entity, 2),
        },
    )


def _score_type_annotation_coverage(
    entities: dict[str, Entity],
) -> DimensionScore:
    """Fraction of Python function/method entities with return type annotations.

    Type annotations appear in the entity's ``signature`` field as ``-> Type``.
    Non-Python entities are excluded — this dimension is language-specific.
    """
    python_funcs = [
        e
        for e in entities.values()
        if e.kind in {EntityKind.FUNCTION, EntityKind.METHOD}
        and e.location.file_path.endswith(".py")
    ]
    total = len(python_funcs)
    if total == 0:
        return DimensionScore(
            dimension="type_annotation_coverage",
            score=1.0,
            grade="A",
            detail="No Python functions found; dimension not applicable.",
            evidence={"python_functions": 0, "annotated": 0},
        )

    annotated = sum(1 for e in python_funcs if e.signature and "->" in e.signature)
    score = annotated / total
    return DimensionScore(
        dimension="type_annotation_coverage",
        score=score,
        grade=_score_to_grade(score),
        detail=f"{annotated}/{total} Python functions have return type annotations.",
        evidence={"python_functions": total, "annotated": annotated},
    )


def _score_complexity_distribution(
    entities: dict[str, Entity],
) -> DimensionScore:
    """Assess the distribution of cyclomatic complexity across entities.

    Complexity is stored in ``entity.metadata["complexity"]`` when available.
    High complexity (>20) produces poor chunk boundaries and diluted relevance.
    If no complexity data is available, this dimension is neutral (0.7).
    """
    func_kinds = {EntityKind.FUNCTION, EntityKind.METHOD}
    with_complexity = [
        e
        for e in entities.values()
        if e.kind in func_kinds
        and isinstance(e.metadata.get("complexity"), (int, float))
    ]

    if not with_complexity:
        # No complexity data available — neutral score, not a penalty
        return DimensionScore(
            dimension="complexity_distribution",
            score=0.7,
            grade="B",
            detail="No complexity data available; defaulting to neutral.",
            evidence={"entities_with_complexity": 0},
        )

    complexities = [float(e.metadata["complexity"]) for e in with_complexity]
    total = len(complexities)
    high_complexity_count = sum(1 for c in complexities if c > 20)
    avg_complexity = sum(complexities) / total

    # Penalise: >20% of functions with complexity >20 → score drops fast
    high_ratio = high_complexity_count / total
    ratio_score = max(0.0, 1.0 - high_ratio * 2.5)

    # Also penalise high average: avg=5→1.0, avg=15→0.5, avg=30→0.0
    avg_score = max(0.0, 1.0 - (avg_complexity - 5) / 25)

    score = 0.6 * ratio_score + 0.4 * avg_score
    return DimensionScore(
        dimension="complexity_distribution",
        score=score,
        grade=_score_to_grade(score),
        detail=(
            f"Avg complexity: {avg_complexity:.1f}, "
            f"{high_complexity_count}/{total} functions with complexity >20."
        ),
        evidence={
            "entities_with_complexity": total,
            "avg_complexity": round(avg_complexity, 2),
            "high_complexity_count": high_complexity_count,
        },
    )


def _score_behavior_analyzability(
    entities: dict[str, Entity],
) -> DimensionScore:
    """Fraction of function entities where behavior analysis achieved ≥0.65 confidence.

    The ``behavior`` annotation is set by ``annotate_entity_behavior()`` and
    stored in ``entity.metadata["behavior"]["confidence"]``. Low-confidence
    means the behavior classifier encountered unknown calls or parse errors.
    """
    func_kinds = {EntityKind.FUNCTION, EntityKind.METHOD}
    python_funcs = [
        e
        for e in entities.values()
        if e.kind in func_kinds and e.location.file_path.endswith(".py")
    ]
    total = len(python_funcs)
    if total == 0:
        return DimensionScore(
            dimension="behavior_analyzability",
            score=1.0,
            grade="A",
            detail="No Python functions found; dimension not applicable.",
            evidence={"python_functions": 0, "analyzable": 0},
        )

    analyzable = 0
    for e in python_funcs:
        behavior = e.metadata.get("behavior")
        if isinstance(behavior, dict):
            confidence = behavior.get("confidence", 0.0)
            if isinstance(confidence, (int, float)) and confidence >= 0.65:
                analyzable += 1

    score = analyzable / total
    return DimensionScore(
        dimension="behavior_analyzability",
        score=score,
        grade=_score_to_grade(score),
        detail=f"{analyzable}/{total} Python functions have analyzable behavior (confidence ≥ 0.65).",
        evidence={"python_functions": total, "analyzable": analyzable},
    )


def _target_is_unresolved(target_id: str) -> bool:
    """Classify one edge target as resolved or not.

    Two unresolved spellings are live in the graph today, not one legacy
    and one current. The scoped canonical form -- ``unresolved::<language>
    ::<file>::<scope>::<symbol>`` from ``build_unresolved_reference_id`` --
    is what the Rust, JavaScript, TypeScript and Vue parsers mint. The bare
    ``ref::Name`` form is what the Python and Java parsers emit at parse
    time; ``GraphBuilder._resolve_references`` links what it can and keeps
    the rest verbatim. Matching only one spelling scored every edge of the
    other as resolved and reported a perfect resolution rate on graphs
    full of holes.
    """
    if classify_endpoint_id(target_id) == EndpointKind.UNRESOLVED:
        return True
    return target_id.startswith("ref::")


def _score_unresolved_references(
    relationships: list[Relationship],
) -> DimensionScore:
    """Fraction of references that remain unresolved after graph building.

    An unresolved target -- the scoped ``unresolved::`` ids and the bare
    ``ref::`` markers, both still emitted by current parsers -- means the
    ``GraphBuilder`` could not resolve a call or type reference to a
    concrete entity in the graph. ``external::`` targets are resolved on
    purpose: the symbol is known to live outside the repository.
    """
    # Only consider relationship kinds where resolution matters
    resolvable_kinds = {
        RelationshipKind.CALLS,
        RelationshipKind.INHERITS,
        RelationshipKind.IMPLEMENTS,
        RelationshipKind.USES_TYPE,
    }
    resolvable = [r for r in relationships if r.kind in resolvable_kinds]
    total = len(resolvable)
    if total == 0:
        return DimensionScore(
            dimension="unresolved_references",
            score=1.0,
            grade="A",
            detail="No resolvable references found.",
            evidence={"total_resolvable": 0, "unresolved": 0},
        )

    unresolved_targets = [
        r.target_id for r in resolvable if _target_is_unresolved(r.target_id)
    ]
    unresolved = len(unresolved_targets)
    resolved_ratio = 1.0 - (unresolved / total)

    # A single number merges two different defects: a bare name that failed
    # to link is fixable resolution work; a receiver-qualified symbol needs
    # dataflow the graph does not have. The split keeps the evidence honest
    # about both without changing what counts as resolved.
    attribute_calls = sum(1 for t in unresolved_targets if _is_attribute_symbol(t))
    external = sum(
        1
        for r in resolvable
        if classify_endpoint_id(r.target_id) == EndpointKind.EXTERNAL
    )
    # A hole that still carries receiver-type metadata is a different
    # residual than one with no type knowledge at all: the file stated the
    # receiver's type and the graph could not place the member — inherited
    # from an external base, or declared in a module the graph did not
    # index. Holes without metadata need type inference, not graph work.
    typed_holes = sum(
        1
        for r in resolvable
        if _target_is_unresolved(r.target_id)
        and any(
            key in r.metadata
            for key in (
                "receiver_type_name",
                "receiver_member_module",
                "receiver_from_call_name",
            )
        )
    )

    return DimensionScore(
        dimension="unresolved_references",
        score=resolved_ratio,
        grade=_score_to_grade(resolved_ratio),
        detail=f"{total - unresolved}/{total} references resolved.",
        evidence={
            "total_resolvable": total,
            "unresolved": unresolved,
            "resolution_rate": round(resolved_ratio, 3),
            "unresolved_attribute_calls": attribute_calls,
            "unresolved_bare_names": unresolved - attribute_calls,
            "unresolved_typed_receivers": typed_holes,
            "external_targets": external,
        },
    )


def _is_attribute_symbol(target_id: str) -> bool:
    """Whether an unresolved target's symbol is receiver-qualified."""
    if target_id.startswith("ref::"):
        return "." in target_id[len("ref::") :]
    parts = target_id.split("::")
    if len(parts) == 5:
        return "." in parts[4]
    return False


# ──────────────────────────────────────────────────────────────────────
# Recommendations engine
# ──────────────────────────────────────────────────────────────────────


def _generate_recommendations(
    dimensions: list[DimensionScore],
) -> list[str]:
    """Generate actionable recommendations based on low-scoring dimensions."""
    recommendations: list[str] = []

    dim_map = {d.dimension: d for d in dimensions}

    parse = dim_map.get("parse_success_rate")
    if parse and parse.score < 0.9:
        errors = parse.evidence.get("errors", 0)
        recommendations.append(
            f"Fix {errors} parse error(s) — every failed file is invisible to "
            "all KnowCode surfaces."
        )

    lang = dim_map.get("language_coverage")
    if lang and lang.score < 0.8:
        unsupported = lang.evidence.get("unsupported", {})
        exts = ", ".join(sorted(unsupported.keys())[:5])
        recommendations.append(
            f"Unsupported languages detected ({exts}). Consider filing a "
            "KnowCode feature request or extracting the unsupported code "
            "into a separately-documented module."
        )

    doc = dim_map.get("documentation_density")
    if doc and doc.score < 0.5:
        recommendations.append(
            "Add docstrings to functions and classes. Docstrings are the "
            "strongest signal for KnowCode's retrieval — they power BM25 "
            "relevance and the EXPLAIN task template."
        )

    naming = dim_map.get("naming_quality")
    if naming and naming.score < 0.6:
        recommendations.append(
            "Improve naming conventions: use snake_case for functions/methods "
            "and CamelCase for classes. Avoid single-character names for "
            "non-trivial entities — they degrade search precision."
        )

    rel = dim_map.get("relationship_density")
    if rel and rel.score < 0.5:
        recommendations.append(
            "Low relationship density detected. This may indicate heavy use "
            "of dynamic dispatch, string-based references, or framework magic "
            "that static analysis cannot follow. Consider adding type hints "
            "and explicit imports to improve traceability."
        )

    types = dim_map.get("type_annotation_coverage")
    if types and types.score < 0.5:
        recommendations.append(
            "Add return type annotations to Python functions. Type "
            "annotations improve entity signatures, which improve "
            "get_entity_context output quality."
        )

    complexity = dim_map.get("complexity_distribution")
    if complexity and complexity.score < 0.5:
        recommendations.append(
            "Refactor high-complexity functions (cyclomatic complexity >20). "
            "Complex functions produce poor chunk boundaries and diluted "
            "retrieval relevance."
        )

    unresolved = dim_map.get("unresolved_references")
    if unresolved and unresolved.score < 0.7:
        recommendations.append(
            "Many references remain unresolved in the graph. This typically "
            "means dynamic imports, metaprogramming, or inconsistent naming. "
            "Unresolved references break trace_calls and dependency expansion."
        )

    return recommendations


# ──────────────────────────────────────────────────────────────────────
# Summary generator
# ──────────────────────────────────────────────────────────────────────


def _generate_summary(
    overall_score: float,
    overall_grade: str,
    dimensions: list[DimensionScore],
    entity_count: int,
) -> str:
    """Generate a 2-3 sentence narrative summary of the assessment."""
    low_dims = [d for d in dimensions if d.score < 0.5]
    high_dims = [d for d in dimensions if d.score >= 0.9]

    parts: list[str] = [
        f"Overall quality grade: {overall_grade} ({overall_score:.0%}). "
        f"Assessed {entity_count} entities across "
        f"{len(dimensions)} quality dimensions."
    ]

    if high_dims:
        names = ", ".join(d.dimension.replace("_", " ") for d in high_dims[:3])
        parts.append(f"Strengths: {names}.")

    if low_dims:
        names = ", ".join(d.dimension.replace("_", " ") for d in low_dims[:3])
        parts.append(
            f"Areas needing improvement: {names}. "
            "These will reduce the accuracy and comprehensiveness of "
            "KnowCode's output for this codebase."
        )

    if overall_score >= 0.8:
        parts.append("KnowCode should perform well on this codebase.")
    elif overall_score >= 0.55:
        parts.append(
            "KnowCode will produce useful results, but accuracy may be "
            "reduced in the weak areas."
        )
    else:
        parts.append(
            "KnowCode's output quality will be significantly limited. "
            "Address the recommendations before relying on results."
        )

    return " ".join(parts)


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────


#: How far a weight set may sum from 1.0 and still be treated as normalised.
#: Generous beside float noise -- the ten shipped defaults sum to
#: 1.0000000000000002 -- and far tighter than any misconfiguration worth
#: catching.
WEIGHT_SUM_TOLERANCE = 1e-6


def validate_preflight_weights(weights: Mapping[str, float]) -> None:
    """Raise unless ``weights`` can normalise a composite score.

    The composite divides by the sum of the weights, so a set that does not sum
    to 1.0 silently rescales every dimension, and a set summing to zero used to
    divide by a ``max(weight_sum, 1e-9)`` floor and clamp to a perfect score --
    a codebase that parsed nothing graded A, clearing a ``min_score`` build
    gate (BL-21). Negative weights are rejected outright: a dimension scoring
    well can never make a codebase worse.

    Args:
        weights: The *effective* weight set, after any merge with the defaults.

    Raises:
        ValueError: If any weight is negative, or the set does not sum to 1.0
            within ``WEIGHT_SUM_TOLERANCE``.
    """
    negative = sorted(key for key, value in weights.items() if value < 0)
    if negative:
        raise ValueError(
            "Preflight weights must not be negative: " + ", ".join(negative) + "."
        )

    total = sum(weights.values())
    if abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
        raise ValueError(
            f"Preflight weights must sum to 1.0, not {total:.6g}. Overriding one "
            "weight rescales the whole composite, so rebalance the rest to "
            "compensate."
        )


def assess_codebase(
    entities: dict[str, Entity],
    relationships: list[Relationship],
    scanned_files: list[FileInfo],
    parse_errors: list[str],
    *,
    weights: Optional[dict[str, float]] = None,
) -> PreflightReport:
    """Run all quality dimension assessments against a parsed codebase.

    Args:
        entities: Entity map from ``GraphBuilder.entities``.
        relationships: Relationship list from ``GraphBuilder.relationships``.
        scanned_files: File list from ``GraphBuilder.scanned_files``.
        parse_errors: Error list from ``GraphBuilder.errors``.
        weights: Optional custom dimension weights (must sum to ~1.0).

    Returns:
        A complete ``PreflightReport`` with dimension scores and recommendations.
    """
    effective_weights = dict(DEFAULT_WEIGHTS)
    if weights is not None:
        for key, value in weights.items():
            if key in effective_weights:
                effective_weights[key] = value
    validate_preflight_weights(effective_weights)

    # Compute each dimension
    dimensions: list[DimensionScore] = [
        _score_parse_success_rate(scanned_files, parse_errors),
        _score_language_coverage(scanned_files),
        _score_documentation_density(entities),
        _score_naming_quality(entities),
        _score_structural_depth(entities, relationships),
        _score_relationship_density(entities, relationships),
        _score_type_annotation_coverage(entities),
        _score_complexity_distribution(entities),
        _score_behavior_analyzability(entities),
        _score_unresolved_references(relationships),
    ]

    # Weighted composite
    # Divided by the sum rather than by 1.0 so a future dimension added
    # without a weight still normalises. Validated above, so no floor is
    # needed -- and a floor here could only mask the bug it used to cause.
    overall_score = sum(
        d.score * effective_weights.get(d.dimension, 0.0) for d in dimensions
    ) / sum(effective_weights.values())
    overall_score = max(0.0, min(1.0, overall_score))
    overall_grade = _score_to_grade(overall_score)

    recommendations = _generate_recommendations(dimensions)
    summary = _generate_summary(overall_score, overall_grade, dimensions, len(entities))

    # Language breakdown
    lang_breakdown: dict[str, int] = {}
    for f in scanned_files:
        lang_breakdown[f.extension] = lang_breakdown.get(f.extension, 0) + 1

    return PreflightReport(
        overall_score=overall_score,
        overall_grade=overall_grade,
        dimensions=dimensions,
        summary=summary,
        recommendations=recommendations,
        timestamp=time.time(),
        scanned_file_count=len(scanned_files),
        entity_count=len(entities),
        relationship_count=len(relationships),
        language_breakdown=lang_breakdown,
    )
