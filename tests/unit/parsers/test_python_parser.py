"""Scope-aware Python parser contract and behavior tests.

These tests drive Step 03 of the hardening plan: nested definitions must receive
qualified names, decorators must be retained with exact locations, module
assignments must become entities, and calls must never be attributed across
nested-definition boundaries.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from knowcode.data_models import EntityKind, RelationshipKind
from knowcode.parsers.python_parser import PythonParser
from knowcode.utils.entity_identity import (
    EndpointKind,
    classify_endpoint_id,
)
from tests.helpers.parser_assertions import (
    assert_exact_parse_result,
    load_parser_fixture_contract,
)


FIXTURE_ROOT = Path(__file__).parents[2] / "fixtures" / "parser_contracts" / "python"


# ---------------------------------------------------------------------------
# Committed fixture contracts (exact graph assertions)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source_path",
    sorted(FIXTURE_ROOT.glob("*.py")),
    ids=lambda p: p.stem,
)
def test_python_parser_matches_committed_fixture_contract(source_path: Path) -> None:
    """Each committed Python fixture must parse to its exact expected graph."""
    contract = load_parser_fixture_contract(source_path)
    result = PythonParser().parse_file(source_path)
    assert_exact_parse_result(result, contract)


# ---------------------------------------------------------------------------
# Scope-aware call attribution
# ---------------------------------------------------------------------------


def test_nested_function_call_resolves_locally_without_leaking(tmp_path: Path) -> None:
    """A bare-name call resolves to the nested function in the same scope, and a
    call inside the nested function is attributed to that nested function, not to
    the outer one (no ast.walk leak across definition boundaries)."""
    src = tmp_path / "scope.py"
    src.write_text(
        "def outer():\n    def duplicate():\n        helper()\n    duplicate()\n",
        encoding="utf-8",
    )
    result = PythonParser().parse_file(src)
    by_qname = {entity.qualified_name: entity for entity in result.entities}
    assert {"scope.outer", "scope.outer.duplicate"} <= set(by_qname)

    outer_id = by_qname["scope.outer"].id
    duplicate_id = by_qname["scope.outer.duplicate"].id

    calls = [rel for rel in result.relationships if rel.kind is RelationshipKind.CALLS]
    # outer() calls its locally defined duplicate() -> internal target.
    assert any(
        rel.source_id == outer_id and rel.target_id == duplicate_id for rel in calls
    ), "expected outer to call its local duplicate"

    # outer.duplicate() calls helper() -> unresolved, and ONLY the nested fn owns it.
    duplicate_calls = [rel for rel in calls if rel.source_id == duplicate_id]
    assert len(duplicate_calls) == 1, "helper() must be attributed to outer.duplicate"
    assert classify_endpoint_id(duplicate_calls[0].target_id) is EndpointKind.UNRESOLVED

    # The outer function must NOT carry a CALLS edge for helper (no scope leak).
    outer_calls = [rel for rel in calls if rel.source_id == outer_id]
    assert all(
        classify_endpoint_id(rel.target_id) is not EndpointKind.UNRESOLVED
        or "helper" not in rel.target_id
        for rel in outer_calls
    ), "helper() call leaked from outer.duplicate into outer"


def test_method_local_call_resolves_to_nested_function(tmp_path: Path) -> None:
    """The same local-resolution rule applies inside methods: a call to a nested
    function resolves to that nested entity, and unrelated calls stay unresolved."""
    src = tmp_path / "cls.py"
    src.write_text(
        "class Container:\n"
        "    def method(self):\n"
        "        def duplicate():\n"
        "            nested_helper()\n"
        "        duplicate()\n",
        encoding="utf-8",
    )
    result = PythonParser().parse_file(src)
    by_qname = {entity.qualified_name: entity for entity in result.entities}
    assert by_qname["cls.Container"].kind is EntityKind.CLASS
    assert by_qname["cls.Container.method"].kind is EntityKind.METHOD
    assert by_qname["cls.Container.method.duplicate"].kind is EntityKind.FUNCTION

    method_id = by_qname["cls.Container.method"].id
    dup_id = by_qname["cls.Container.method.duplicate"].id
    calls = [
        rel
        for rel in result.relationships
        if rel.kind is RelationshipKind.CALLS and rel.source_id == method_id
    ]
    assert len(calls) == 1
    assert calls[0].target_id == dup_id


def test_async_nested_function_keeps_function_kind_and_scope(tmp_path: Path) -> None:
    """Async nested functions are FUNCTION entities with async signatures, and
    calls inside them (including awaited calls) are owned by the nested scope."""
    src = tmp_path / "amod.py"
    src.write_text(
        "async def outer():\n"
        "    async def inner():\n"
        "        await fetch()\n"
        "    inner()\n",
        encoding="utf-8",
    )
    result = PythonParser().parse_file(src)
    by_qname = {entity.qualified_name: entity for entity in result.entities}
    assert by_qname["amod.outer"].kind is EntityKind.FUNCTION
    assert by_qname["amod.outer"].signature is not None
    assert by_qname["amod.outer"].signature.startswith("async def outer(")
    assert by_qname["amod.outer.inner"].kind is EntityKind.FUNCTION
    assert by_qname["amod.outer.inner"].signature is not None
    assert by_qname["amod.outer.inner"].signature.startswith("async def inner(")

    inner_id = by_qname["amod.outer.inner"].id
    outer_id = by_qname["amod.outer"].id
    calls = [rel for rel in result.relationships if rel.kind is RelationshipKind.CALLS]
    assert any(rel.source_id == outer_id and rel.target_id == inner_id for rel in calls)
    inner_calls = [rel for rel in calls if rel.source_id == inner_id]
    assert len(inner_calls) == 1
    assert classify_endpoint_id(inner_calls[0].target_id) is EndpointKind.UNRESOLVED


# ---------------------------------------------------------------------------
# Decorators and exact locations
# ---------------------------------------------------------------------------


def test_decorated_definition_spans_decorators_and_records_them(tmp_path: Path) -> None:
    """A decorated class/function location starts at its first decorator and its
    decorator expressions are recorded deterministically in source order."""
    src = tmp_path / "dec.py"
    src.write_text(
        "@register('service')\n"
        "@trace\n"
        "class Service:\n"
        "    @cached\n"
        "    def run(self):\n"
        "        return 1\n",
        encoding="utf-8",
    )
    result = PythonParser().parse_file(src)
    by_qname = {entity.qualified_name: entity for entity in result.entities}

    service = by_qname["dec.Service"]
    assert service.kind is EntityKind.CLASS
    assert (service.location.line_start, service.location.line_end) == (1, 6)
    assert service.metadata["decorators"] == ["register('service')", "trace"]

    run = by_qname["dec.Service.run"]
    assert run.kind is EntityKind.METHOD
    assert (run.location.line_start, run.location.line_end) == (4, 6)
    assert run.metadata["decorators"] == ["cached"]


def test_undecorated_definitions_keep_def_line_as_start(tmp_path: Path) -> None:
    src = tmp_path / "plain.py"
    src.write_text("def plain():\n    return 0\n", encoding="utf-8")
    result = PythonParser().parse_file(src)
    by_qname = {entity.qualified_name: entity for entity in result.entities}
    assert by_qname["plain"].location.line_start == 1
    assert "decorators" not in by_qname["plain"].metadata


# ---------------------------------------------------------------------------
# Module variables
# ---------------------------------------------------------------------------


def test_module_variables_cover_annotation_chaining_and_unpacking(
    tmp_path: Path,
) -> None:
    src = tmp_path / "vars.py"
    src.write_text(
        "threshold: int = 3\nprimary = secondary = 'ready'\nleft, right = (1, 2)\n",
        encoding="utf-8",
    )
    result = PythonParser().parse_file(src)
    by_qname = {entity.qualified_name: entity for entity in result.entities}

    expected = {
        "vars.threshold": 1,
        "vars.primary": 2,
        "vars.secondary": 2,
        "vars.left": 3,
        "vars.right": 3,
    }
    for name, line in expected.items():
        entity = by_qname[name]
        assert entity.kind is EntityKind.VARIABLE, name
        assert entity.location.line_start == line, name
        assert entity.location.line_end == line, name

    module_id = by_qname["vars"].id
    contains = {
        rel.target_id
        for rel in result.relationships
        if rel.kind is RelationshipKind.CONTAINS and rel.source_id == module_id
    }
    for name in expected:
        assert by_qname[name].id in contains, name


def test_local_assignments_are_not_treated_as_module_variables(
    tmp_path: Path,
) -> None:
    """Only module-level assignments become entities; function-local names do not."""
    src = tmp_path / "assigns.py"
    src.write_text(
        "GLOBAL = 1\ndef f():\n    inner = 2\n    other = 3\n",
        encoding="utf-8",
    )
    result = PythonParser().parse_file(src)
    variables = {
        entity.qualified_name
        for entity in result.entities
        if entity.kind is EntityKind.VARIABLE
    }
    # Only the module-level GLOBAL becomes a variable entity.
    assert variables == {"assigns.GLOBAL"}
    qnames = {entity.qualified_name for entity in result.entities}
    assert "assigns.f" in qnames


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_python_parser_output_is_deterministic(tmp_path: Path) -> None:
    src = tmp_path / "det.py"
    src.write_text(
        "def a():\n"
        "    def b():\n"
        "        c()\n"
        "    b()\n"
        "\n"
        "class D:\n"
        "    def m(self):\n"
        "        n()\n",
        encoding="utf-8",
    )
    first = PythonParser().parse_file(src)
    second = PythonParser().parse_file(src)

    assert [e.id for e in first.entities] == [e.id for e in second.entities]
    assert [(r.source_id, r.target_id, r.kind) for r in first.relationships] == [
        (r.source_id, r.target_id, r.kind) for r in second.relationships
    ]


# ---------------------------------------------------------------------------
# Preserved behavior: errors, imports, inheritance, signatures, edge cases
# ---------------------------------------------------------------------------


def test_syntax_error_is_reported_without_raising(tmp_path: Path) -> None:
    src = tmp_path / "broken.py"
    src.write_text("def (\n", encoding="utf-8")
    result = PythonParser().parse_file(src)
    assert result.entities == []
    assert len(result.errors) == 1


def test_missing_file_is_reported_without_raising(tmp_path: Path) -> None:
    result = PythonParser().parse_file(tmp_path / "absent.py")
    assert result.entities == []
    assert len(result.errors) == 1


def test_imports_become_external_module_edges(tmp_path: Path) -> None:
    src = tmp_path / "imp.py"
    src.write_text(
        "import os\nfrom collections import OrderedDict\n",
        encoding="utf-8",
    )
    result = PythonParser().parse_file(src)
    module = next(e for e in result.entities if e.kind is EntityKind.MODULE)
    imports = [
        rel
        for rel in result.relationships
        if rel.kind is RelationshipKind.IMPORTS and rel.source_id == module.id
    ]
    targets = {rel.target_id for rel in imports}
    assert any("external::python::os" in target for target in targets)
    assert any("external::python::collections" in target for target in targets)


def test_inheritance_emits_ref_edge_for_base(tmp_path: Path) -> None:
    """Inheritance is preserved as a ``ref::`` target so GraphBuilder can still
    link it to a local base entity."""
    src = tmp_path / "inh.py"
    src.write_text(
        "class Base:\n    pass\nclass Child(Base):\n    pass\n", encoding="utf-8"
    )
    result = PythonParser().parse_file(src)
    child = next(e for e in result.entities if e.qualified_name == "inh.Child")
    inherits = [
        rel for rel in result.relationships if rel.kind is RelationshipKind.INHERITS
    ]
    assert len(inherits) == 1
    assert inherits[0].source_id == child.id
    assert inherits[0].target_id == "ref::Base"


def test_dotted_bases_and_attribute_calls_become_unresolved_references(
    tmp_path: Path,
) -> None:
    """Dotted inheritance bases and attribute/method calls cannot be bound to a
    local definition; they become scoped unresolved references."""
    src = tmp_path / "dotted.py"
    src.write_text(
        "def f(pkg):\n    pkg.run()\n    a.b.c()\nclass Sub(a.b.Base):\n    pass\n",
        encoding="utf-8",
    )
    result = PythonParser().parse_file(src)
    f_id = next(e.id for e in result.entities if e.qualified_name == "dotted.f")
    calls = [
        rel
        for rel in result.relationships
        if rel.kind is RelationshipKind.CALLS and rel.source_id == f_id
    ]
    assert len(calls) == 2
    assert all(
        classify_endpoint_id(rel.target_id) is EndpointKind.UNRESOLVED for rel in calls
    )
    inherits = [
        rel for rel in result.relationships if rel.kind is RelationshipKind.INHERITS
    ]
    assert len(inherits) == 1
    assert inherits[0].target_id == "ref::a.b.Base"


def test_starred_unpacking_creates_variable_entities(tmp_path: Path) -> None:
    src = tmp_path / "star.py"
    src.write_text("first, *rest = items\n", encoding="utf-8")
    result = PythonParser().parse_file(src)
    variables = {
        entity.qualified_name
        for entity in result.entities
        if entity.kind is EntityKind.VARIABLE
    }
    assert variables == {"star.first", "star.rest"}


def test_duplicate_module_variable_name_is_not_duplicated(tmp_path: Path) -> None:
    src = tmp_path / "dup.py"
    src.write_text("x = 1\nx = 2\n", encoding="utf-8")
    result = PythonParser().parse_file(src)
    x_entities = [
        entity for entity in result.entities if entity.qualified_name == "dup.x"
    ]
    assert len(x_entities) == 1
    # The first assignment wins.
    assert x_entities[0].location.line_start == 1


def test_signature_captures_annotations_varargs_and_kwargs(tmp_path: Path) -> None:
    src = tmp_path / "sig.py"
    src.write_text(
        "def f(a: int, *args: str, **kwargs: float) -> bool:\n    return True\n",
        encoding="utf-8",
    )
    result = PythonParser().parse_file(src)
    fn = next(entity for entity in result.entities if entity.qualified_name == "sig.f")
    signature = fn.signature
    assert signature is not None
    assert "a: int" in signature
    assert "*args: str" in signature
    assert "**kwargs: float" in signature
    assert "-> bool" in signature


def test_lambda_body_calls_do_not_leak_into_enclosing_function(
    tmp_path: Path,
) -> None:
    """A lambda is a scope boundary: calls inside it are not attributed to the
    enclosing function."""
    src = tmp_path / "lam.py"
    src.write_text(
        "def f():\n    handler = lambda: handle()\n    handler()\n",
        encoding="utf-8",
    )
    result = PythonParser().parse_file(src)
    f_id = next(e.id for e in result.entities if e.qualified_name == "lam.f")
    calls = [
        rel
        for rel in result.relationships
        if rel.kind is RelationshipKind.CALLS and rel.source_id == f_id
    ]
    targets = [rel.target_id for rel in calls]
    # handler() is owned by f (unresolved); the lambda's handle() must not leak.
    assert any(target.endswith("::handler") for target in targets)
    assert not any(target.endswith("::handle") for target in targets)


def test_calls_with_unnameable_targets_are_skipped(tmp_path: Path) -> None:
    """A call whose target is neither a bare name nor an attribute (e.g. a
    subscripted callable) contributes no CALLS edge rather than raising."""
    src = tmp_path / "idx.py"
    src.write_text("def f():\n    fns[0]()\n", encoding="utf-8")
    result = PythonParser().parse_file(src)
    f_id = next(e.id for e in result.entities if e.qualified_name == "idx.f")
    calls = [
        rel
        for rel in result.relationships
        if rel.kind is RelationshipKind.CALLS and rel.source_id == f_id
    ]
    assert calls == []


# ---------------------------------------------------------------------------
# Builtin externalisation, self-method binding, and import bindings
# ---------------------------------------------------------------------------


def test_builtin_calls_resolve_to_external_ids(tmp_path: Path) -> None:
    """A bare call to a Python builtin is an answer, not a hole: it points at
    ``external::builtins::<name>`` instead of a scoped unresolved id."""
    src = tmp_path / "builtin_calls.py"
    src.write_text(
        "def f(x):\n    print(x)\n    return len(x)\n",
        encoding="utf-8",
    )
    result = PythonParser().parse_file(src)
    f_id = next(e.id for e in result.entities if e.qualified_name == "builtin_calls.f")
    targets = sorted(
        rel.target_id
        for rel in result.relationships
        if rel.kind is RelationshipKind.CALLS and rel.source_id == f_id
    )
    assert targets == ["external::builtins::len", "external::builtins::print"]
    assert all(
        classify_endpoint_id(target) is EndpointKind.EXTERNAL for target in targets
    )


def test_local_definition_shadows_builtin(tmp_path: Path) -> None:
    """A local ``len`` definition wins over the builtin of the same name."""
    src = tmp_path / "shadow.py"
    src.write_text(
        "def run(x):\n    return len(x)\n\n\ndef len(y):\n    return y\n",
        encoding="utf-8",
    )
    result = PythonParser().parse_file(src)
    run_id = next(e.id for e in result.entities if e.qualified_name == "shadow.run")
    calls = [
        rel
        for rel in result.relationships
        if rel.kind is RelationshipKind.CALLS and rel.source_id == run_id
    ]
    assert len(calls) == 1
    assert calls[0].target_id.endswith(".len")
    assert classify_endpoint_id(calls[0].target_id) is EndpointKind.INTERNAL


def test_self_method_call_binds_to_sibling_method(tmp_path: Path) -> None:
    """``self.helper()`` from a method binds to the sibling method entity."""
    src = tmp_path / "thing.py"
    src.write_text(
        "class Thing:\n"
        "    def run(self):\n"
        "        self.helper()\n"
        ""
        "\n"
        "    def helper(self):\n"
        "        pass\n",
        encoding="utf-8",
    )
    result = PythonParser().parse_file(src)
    run_id = next(
        e.id for e in result.entities if e.qualified_name == "thing.Thing.run"
    )
    calls = [
        rel
        for rel in result.relationships
        if rel.kind is RelationshipKind.CALLS and rel.source_id == run_id
    ]
    assert len(calls) == 1
    assert calls[0].target_id.endswith(".Thing.helper")
    assert classify_endpoint_id(calls[0].target_id) is EndpointKind.INTERNAL


def test_missing_self_method_stays_unresolved(tmp_path: Path) -> None:
    src = tmp_path / "missing.py"
    src.write_text(
        "class Thing:\n    def run(self):\n        self.absent()\n",
        encoding="utf-8",
    )
    result = PythonParser().parse_file(src)
    run_id = next(
        e.id for e in result.entities if e.qualified_name == "missing.Thing.run"
    )
    calls = [
        rel
        for rel in result.relationships
        if rel.kind is RelationshipKind.CALLS and rel.source_id == run_id
    ]
    assert len(calls) == 1
    assert classify_endpoint_id(calls[0].target_id) is EndpointKind.UNRESOLVED


def test_self_call_in_module_function_never_binds(tmp_path: Path) -> None:
    """``self.`` outside a class has no enclosing class; it must not bind to a
    same-named module-level function."""
    src = tmp_path / "flat.py"
    src.write_text(
        "def f(self):\n    return self.g()\n\n\ndef g():\n    return 1\n",
        encoding="utf-8",
    )
    result = PythonParser().parse_file(src)
    f_id = next(e.id for e in result.entities if e.qualified_name == "flat.f")
    calls = [
        rel
        for rel in result.relationships
        if rel.kind is RelationshipKind.CALLS and rel.source_id == f_id
    ]
    assert len(calls) == 1
    assert classify_endpoint_id(calls[0].target_id) is EndpointKind.UNRESOLVED


def test_self_call_shadowed_by_nested_def_stays_unresolved(tmp_path: Path) -> None:
    """A nested ``helper`` inside the caller is not ``Thing.helper``; the
    self-call must keep its unresolved form."""
    src = tmp_path / "nested_shadow.py"
    src.write_text(
        "class Thing:\n"
        "    def run(self):\n"
        "        def helper():\n"
        "            pass\n"
        ""
        "\n"
        "        self.helper()\n",
        encoding="utf-8",
    )
    result = PythonParser().parse_file(src)
    run_id = next(
        e.id for e in result.entities if e.qualified_name == "nested_shadow.Thing.run"
    )
    calls = [
        rel
        for rel in result.relationships
        if rel.kind is RelationshipKind.CALLS and rel.source_id == run_id
    ]
    assert len(calls) == 1
    assert classify_endpoint_id(calls[0].target_id) is EndpointKind.UNRESOLVED


def test_module_receiver_call_records_binding(tmp_path: Path) -> None:
    """``json.loads(...)`` keeps its unresolved id but carries the receiver's
    import binding so GraphBuilder can classify it with repo knowledge."""
    src = tmp_path / "receiver.py"
    src.write_text(
        "import json\n\n\ndef f(s):\n    return json.loads(s)\n",
        encoding="utf-8",
    )
    result = PythonParser().parse_file(src)
    f_id = next(e.id for e in result.entities if e.qualified_name == "receiver.f")
    calls = [
        rel
        for rel in result.relationships
        if rel.kind is RelationshipKind.CALLS and rel.source_id == f_id
    ]
    assert len(calls) == 1
    assert classify_endpoint_id(calls[0].target_id) is EndpointKind.UNRESOLVED
    assert calls[0].metadata["receiver_module"] == "json"


def test_aliased_module_receiver_records_imported_module(tmp_path: Path) -> None:
    src = tmp_path / "aliasmod.py"
    src.write_text(
        "import numpy as np\n\n\ndef f(x):\n    return np.array(x)\n",
        encoding="utf-8",
    )
    result = PythonParser().parse_file(src)
    f_id = next(e.id for e in result.entities if e.qualified_name == "aliasmod.f")
    calls = [
        rel
        for rel in result.relationships
        if rel.kind is RelationshipKind.CALLS and rel.source_id == f_id
    ]
    assert len(calls) == 1
    assert calls[0].metadata["receiver_module"] == "numpy"


def test_from_import_call_records_origin(tmp_path: Path) -> None:
    """``from os.path import join`` binds ``join`` to its origin module; the
    call edge carries that origin for GraphBuilder to classify."""
    src = tmp_path / "fromimport.py"
    src.write_text(
        "from os.path import join\n\n\ndef f(p):\n    return join('a', p)\n",
        encoding="utf-8",
    )
    result = PythonParser().parse_file(src)
    f_id = next(e.id for e in result.entities if e.qualified_name == "fromimport.f")
    calls = [
        rel
        for rel in result.relationships
        if rel.kind is RelationshipKind.CALLS and rel.source_id == f_id
    ]
    assert len(calls) == 1
    assert calls[0].metadata["imported_from"] == "os.path"
    assert calls[0].metadata["imported_symbol"] == "join"


def test_aliased_from_import_records_original_name(tmp_path: Path) -> None:
    src = tmp_path / "aliasfrom.py"
    src.write_text(
        "from os.path import join as j\n\n\ndef f(p):\n    return j('a', p)\n",
        encoding="utf-8",
    )
    result = PythonParser().parse_file(src)
    f_id = next(e.id for e in result.entities if e.qualified_name == "aliasfrom.f")
    calls = [
        rel
        for rel in result.relationships
        if rel.kind is RelationshipKind.CALLS and rel.source_id == f_id
    ]
    assert len(calls) == 1
    assert calls[0].metadata["imported_from"] == "os.path"
    assert calls[0].metadata["imported_symbol"] == "join"


def test_local_definition_shadows_from_import(tmp_path: Path) -> None:
    src = tmp_path / "shadowimport.py"
    src.write_text(
        "from os.path import join\n\n\ndef f(p):\n    return join('a', p)\n\n\ndef join(a, b):\n    return a + b\n",
        encoding="utf-8",
    )
    result = PythonParser().parse_file(src)
    f_id = next(e.id for e in result.entities if e.qualified_name == "shadowimport.f")
    calls = [
        rel
        for rel in result.relationships
        if rel.kind is RelationshipKind.CALLS and rel.source_id == f_id
    ]
    assert len(calls) == 1
    assert calls[0].target_id.endswith(".join")
    assert classify_endpoint_id(calls[0].target_id) is EndpointKind.INTERNAL
    assert calls[0].metadata == {}


def test_imported_base_class_records_origin(tmp_path: Path) -> None:
    """``class Color(Enum)`` keeps ``ref::Enum`` but carries where Enum came
    from, so the builder can tell stdlib bases from repo bases."""
    src = tmp_path / "colors.py"
    src.write_text(
        "from enum import Enum\n\n\nclass Color(Enum):\n    pass\n",
        encoding="utf-8",
    )
    result = PythonParser().parse_file(src)
    inherits = [
        rel for rel in result.relationships if rel.kind is RelationshipKind.INHERITS
    ]
    assert len(inherits) == 1
    assert inherits[0].target_id == "ref::Enum"
    assert inherits[0].metadata["imported_from"] == "enum"
    assert inherits[0].metadata["imported_symbol"] == "Enum"


def test_receiver_expression_call_is_flagged_not_name_called(tmp_path: Path) -> None:
    """``Path(p).read_text()`` loses its receiver; the edge is flagged so
    scoped name matching never mistakes it for a call to a bare ``read_text``."""
    src = tmp_path / "chain.py"
    src.write_text(
        "def f(p):\n    return open(p).read()\n",
        encoding="utf-8",
    )
    result = PythonParser().parse_file(src)
    f_id = next(e.id for e in result.entities if e.qualified_name == "chain.f")
    calls = [
        rel
        for rel in result.relationships
        if rel.kind is RelationshipKind.CALLS and rel.source_id == f_id
    ]
    by_symbol = {rel.target_id.rsplit("::", 1)[-1]: rel for rel in calls}
    assert by_symbol["read"].metadata["receiver_unknown"] is True
    assert "receiver_unknown" not in by_symbol["open"].metadata
