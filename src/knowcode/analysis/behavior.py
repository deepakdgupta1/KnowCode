"""Static behavior hints for source entities."""

from __future__ import annotations

import ast
import textwrap
from dataclasses import asdict, dataclass
from pathlib import Path

from knowcode.data_models import Entity, EntityKind

_MUTATION_METHODS = {
    "add",
    "append",
    "clear",
    "discard",
    "extend",
    "insert",
    "pop",
    "popitem",
    "remove",
    "setdefault",
    "sort",
    "update",
    "write",
    "writelines",
}

_IO_CALLS = {"input", "open", "print"}
_NETWORK_PREFIXES = ("httpx.", "requests.", "urllib.", "urllib3.")
_NON_DETERMINISTIC_CALLS = {
    "datetime.now",
    "datetime.utcnow",
    "os.environ.get",
    "random",
    "random.random",
    "time.time",
    "uuid.uuid4",
}
_PURE_BUILTINS = {
    "abs",
    "all",
    "any",
    "bool",
    "dict",
    "enumerate",
    "float",
    "int",
    "isinstance",
    "len",
    "list",
    "max",
    "min",
    "range",
    "repr",
    "reversed",
    "round",
    "set",
    "sorted",
    "str",
    "sum",
    "tuple",
    "zip",
}


@dataclass(frozen=True)
class BehaviorSummary:
    """Rule-based behavior summary for a function-like entity."""

    side_effect_class: str
    side_effects: list[str]
    reads: list[str]
    writes: list[str]
    calls: list[str]
    confidence: float


class _BehaviorVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.reads: set[str] = set()
        self.writes: set[str] = set()
        self.calls: set[str] = set()
        self.side_effects: set[str] = set()
        self.global_names: set[str] = set()
        self.nonlocal_names: set[str] = set()

    def visit_Global(self, node: ast.Global) -> None:
        self.global_names.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.nonlocal_names.update(node.names)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.reads.add(node.id)
        elif isinstance(node.ctx, ast.Store):
            self.writes.add(node.id)
            if node.id in self.global_names or node.id in self.nonlocal_names:
                self.side_effects.add("state_mutation")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        name = _expr_name(node)
        if name and isinstance(node.ctx, ast.Store):
            self.writes.add(name)
            self.side_effects.add("state_mutation")
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            name = _expr_name(node.value)
            if name:
                self.writes.add(name)
            self.side_effects.add("state_mutation")
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:
        self.side_effects.add("state_mutation")
        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> None:
        self.side_effects.add("raises")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        call_name = _expr_name(node.func)
        if call_name:
            self.calls.add(call_name)
            self._classify_call(call_name)
        self.generic_visit(node)

    def _classify_call(self, call_name: str) -> None:
        root_name = call_name.split(".", 1)[0]
        method_name = call_name.rsplit(".", 1)[-1]

        if root_name in _IO_CALLS or call_name in _IO_CALLS:
            self.side_effects.add("io")
        if call_name.startswith(_NETWORK_PREFIXES):
            self.side_effects.add("network")
        if (
            call_name in _NON_DETERMINISTIC_CALLS
            or root_name in _NON_DETERMINISTIC_CALLS
        ):
            self.side_effects.add("non_deterministic")
        if method_name in _MUTATION_METHODS:
            self.side_effects.add("state_mutation")


class PythonBehaviorAnalyzer:
    """Extract lightweight behavior metadata from Python function source."""

    def analyze_source(self, source_code: str) -> BehaviorSummary:
        """Analyze a Python function/method source snippet."""
        try:
            tree = ast.parse(textwrap.dedent(source_code))
        except SyntaxError:
            return BehaviorSummary(
                side_effect_class="unknown",
                side_effects=["parse_error"],
                reads=[],
                writes=[],
                calls=[],
                confidence=0.0,
            )

        visitor = _BehaviorVisitor()
        visitor.visit(tree)
        side_effects = sorted(visitor.side_effects)
        calls = sorted(visitor.calls)
        return BehaviorSummary(
            side_effect_class=_primary_side_effect_class(side_effects),
            side_effects=side_effects,
            reads=sorted(visitor.reads),
            writes=sorted(visitor.writes),
            calls=calls,
            confidence=_confidence(calls, side_effects),
        )


def annotate_entity_behavior(entity: Entity) -> None:
    """Attach Python behavior metadata to function-like entities when possible."""
    if entity.kind not in {EntityKind.FUNCTION, EntityKind.METHOD}:
        return
    if not entity.source_code or Path(entity.location.file_path).suffix != ".py":
        return

    summary = PythonBehaviorAnalyzer().analyze_source(entity.source_code)
    entity.metadata["behavior"] = asdict(summary)

    confidence = entity.metadata.get("confidence")
    if not isinstance(confidence, dict):
        confidence = {}
        entity.metadata["confidence"] = confidence
    confidence["behavior"] = summary.confidence


def _primary_side_effect_class(side_effects: list[str]) -> str:
    if "network" in side_effects or "io" in side_effects:
        return "io"
    if "state_mutation" in side_effects:
        return "state_mutating"
    if "non_deterministic" in side_effects:
        return "non_deterministic"
    if "raises" in side_effects:
        return "raises"
    return "pure_or_read_only"


def _confidence(calls: list[str], side_effects: list[str]) -> float:
    if "parse_error" in side_effects:
        return 0.0

    unknown_calls = [
        call
        for call in calls
        if call not in _PURE_BUILTINS
        and call not in _IO_CALLS
        and call not in _NON_DETERMINISTIC_CALLS
        and not call.startswith(_NETWORK_PREFIXES)
        and call.rsplit(".", 1)[-1] not in _MUTATION_METHODS
    ]
    return 0.65 if unknown_calls else 0.9


def _expr_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        value = _expr_name(node.value)
        return f"{value}.{node.attr}" if value else node.attr
    if isinstance(node, ast.Call):
        return _expr_name(node.func)
    return None
