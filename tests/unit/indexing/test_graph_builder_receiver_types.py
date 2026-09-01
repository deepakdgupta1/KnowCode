"""Unit tests for GraphBuilder classification of receiver-typed references.

The Python parser states what a file knows about a receiver — a class
lexically or through an import, a member-imported module, or a cross-module
factory. These tests pin how ``GraphBuilder`` turns each statement into
either a link to the repo entity carrying the called method, an
``external::`` answer, or a kept hole with its metadata when linking would
be a guess.
"""

from __future__ import annotations

from pathlib import Path

from knowcode.data_models import RelationshipKind
from knowcode.indexing.graph_builder import GraphBuilder
from knowcode.indexing.scanner import FileInfo
from knowcode.utils.entity_identity import (
    EndpointKind,
    classify_endpoint_id,
)


def _file_info(path: Path) -> FileInfo:
    return FileInfo(
        path=path,
        relative_path=path.name,
        extension=path.suffix,
        size_bytes=path.stat().st_size,
    )


def _build(tmp_path: Path, sources: dict[str, str]) -> GraphBuilder:
    infos = []
    for name, text in sources.items():
        src = tmp_path / name
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(text, encoding="utf-8")
        infos.append(_file_info(src))
    return GraphBuilder().build_from_files(infos)


def _calls(builder: GraphBuilder, source_suffix: str) -> list[tuple[str, dict]]:
    return [
        (rel.target_id, rel.metadata)
        for rel in builder.relationships
        if rel.kind is RelationshipKind.CALLS and rel.source_id.endswith(source_suffix)
    ]


# ---------------------------------------------------------------------------
# Typed receivers: annotations and constructors
# ---------------------------------------------------------------------------


def test_annotated_in_repo_class_links_method(tmp_path: Path) -> None:
    builder = _build(
        tmp_path,
        {
            "stores/service.py": (
                "class Store:\n    def get(self, key):\n        return key\n"
            ),
            "caller.py": (
                "from stores.service import Store\n\n"
                "def f(store: Store):\n    return store.get('k')\n"
            ),
        },
    )
    targets = _calls(builder, "caller.py::caller.f")
    assert [t for t, _ in targets] == [
        f"{tmp_path / 'stores' / 'service.py'}::service.Store.get"
    ]
    assert classify_endpoint_id(targets[0][0]) is EndpointKind.INTERNAL


def test_in_file_constructor_links_method(tmp_path: Path) -> None:
    builder = _build(
        tmp_path,
        {
            "caller.py": (
                "class Store:\n"
                "    def get(self, key):\n"
                "        return key\n"
                "\n"
                "def f():\n"
                "    store = Store()\n"
                "    return store.get('k')\n"
            ),
        },
    )
    targets = _calls(builder, "caller.py::caller.f")
    assert [t for t, _ in targets] == [
        f"{tmp_path / 'caller.py'}::caller.Store",
        f"{tmp_path / 'caller.py'}::caller.Store.get",
    ]


def test_imported_constructor_resolves_through_class_identity(tmp_path: Path) -> None:
    """``from m import Store; x = Store()`` may be a class or a factory; the
    graph settles it through the callee entity's kind."""
    builder = _build(
        tmp_path,
        {
            "stores/service.py": (
                "class Store:\n    def get(self, key):\n        return key\n"
            ),
            "caller.py": (
                "from stores.service import Store\n\n"
                "def f():\n"
                "    store = Store()\n"
                "    return store.get('k')\n"
            ),
        },
    )
    targets = _calls(builder, "caller.py::caller.f")
    assert [t for t, _ in targets] == [
        f"{tmp_path / 'stores' / 'service.py'}::service.Store",
        f"{tmp_path / 'stores' / 'service.py'}::service.Store.get",
    ]


def test_reexported_class_from_package_links(tmp_path: Path) -> None:
    """``from pkg import Thing`` resolves through the package subtree when
    the class lives in a module the binding never names."""
    builder = _build(
        tmp_path,
        {
            "stores/__init__.py": "",
            "stores/service.py": (
                "class Store:\n    def get(self, key):\n        return key\n"
            ),
            "caller.py": (
                "from stores import Store\n\n"
                "def f(store: Store):\n    return store.get('k')\n"
            ),
        },
    )
    targets = _calls(builder, "caller.py::caller.f")
    assert [t for t, _ in targets] == [
        f"{tmp_path / 'stores' / 'service.py'}::service.Store.get"
    ]


def test_in_repo_module_without_the_class_keeps_the_hole(tmp_path: Path) -> None:
    """A type from an in-repo module that exports no such class is unknown,
    not external: the method may live anywhere in the repository."""
    builder = _build(
        tmp_path,
        {
            "stores/__init__.py": "",
            "caller.py": (
                "from stores import Store\n\n"
                "def f(store: Store):\n    return store.get('k')\n"
            ),
        },
    )
    targets = _calls(builder, "caller.py::caller.f")
    assert len(targets) == 1
    assert classify_endpoint_id(targets[0][0]) is EndpointKind.UNRESOLVED
    assert targets[0][1]["receiver_type_name"] == "Store"


def test_external_type_is_an_answer(tmp_path: Path) -> None:
    builder = _build(
        tmp_path,
        {
            "caller.py": (
                "from pytest import MonkeyPatch\n\n"
                "def f(monkeypatch: MonkeyPatch):\n"
                "    return monkeypatch.setattr('a.b', 1)\n"
            ),
        },
    )
    targets = _calls(builder, "caller.py::caller.f")
    assert [t for t, _ in targets] == ["external::pytest::MonkeyPatch.setattr"]


def test_builtin_generic_annotation_answers_builtins(tmp_path: Path) -> None:
    builder = _build(
        tmp_path,
        {
            "caller.py": (
                "def f(mapping: dict[str, int]):\n    return mapping.get(1)\n"
            ),
        },
    )
    targets = _calls(builder, "caller.py::caller.f")
    assert [t for t, _ in targets] == ["external::builtins::dict.get"]


# ---------------------------------------------------------------------------
# Class-internal receivers: self attributes, self/cls fallbacks, bases
# ---------------------------------------------------------------------------


def test_self_attribute_construction_links_method(tmp_path: Path) -> None:
    builder = _build(
        tmp_path,
        {
            "stores/service.py": (
                "class Store:\n    def get(self, key):\n        return key\n"
            ),
            "caller.py": (
                "from stores.service import Store\n\n"
                "class Service:\n"
                "    def __init__(self):\n"
                "        self.store = Store()\n"
                "\n"
                "    def work(self):\n"
                "        return self.store.get('k')\n"
            ),
        },
    )
    targets = _calls(builder, "caller.py::caller.Service.work")
    assert [t for t, _ in targets] == [
        f"{tmp_path / 'stores' / 'service.py'}::service.Store.get"
    ]


def test_self_method_falls_back_to_in_repo_base(tmp_path: Path) -> None:
    """``self.m()`` with no sibling declaration links the base class that
    declares the method, through the resolved INHERITS edge."""
    builder = _build(
        tmp_path,
        {
            "caller.py": (
                "class Base:\n"
                "    def shared(self):\n"
                "        return 1\n"
                "\n"
                "class Sub(Base):\n"
                "    def work(self):\n"
                "        return self.shared()\n"
            ),
        },
    )
    targets = _calls(builder, "caller.py::caller.Sub.work")
    assert [t for t, _ in targets] == [f"{tmp_path / 'caller.py'}::caller.Base.shared"]


def test_two_bases_providing_the_method_keeps_the_hole(tmp_path: Path) -> None:
    builder = _build(
        tmp_path,
        {
            "caller.py": (
                "class A:\n"
                "    def shared(self):\n"
                "        return 1\n"
                "\n"
                "class B:\n"
                "    def shared(self):\n"
                "        return 2\n"
                "\n"
                "class C(A, B):\n"
                "    def work(self):\n"
                "        return self.shared()\n"
            ),
        },
    )
    targets = _calls(builder, "caller.py::caller.C.work")
    assert len(targets) == 1
    assert classify_endpoint_id(targets[0][0]) is EndpointKind.UNRESOLVED


def test_method_inherited_from_external_base_keeps_typed_hole(tmp_path: Path) -> None:
    """A method the repository never declares is a hole, with the receiver
    type still stated on the edge for the evidence split."""
    builder = _build(
        tmp_path,
        {
            "caller.py": (
                "import ast\n\n"
                "class Visitor(ast.NodeVisitor):\n"
                "    def work(self):\n"
                "        return self.generic_visit(None)\n"
            ),
        },
    )
    targets = _calls(builder, "caller.py::caller.Visitor.work")
    assert len(targets) == 1
    assert classify_endpoint_id(targets[0][0]) is EndpointKind.UNRESOLVED
    assert targets[0][1]["receiver_type_qname"] == "caller.Visitor"


# ---------------------------------------------------------------------------
# Member-imported receivers
# ---------------------------------------------------------------------------


def test_member_imported_module_symbol_links(tmp_path: Path) -> None:
    builder = _build(
        tmp_path,
        {
            "stores/sub.py": "def load():\n    return 1\n",
            "caller.py": (
                "from stores import sub\n\ndef f():\n    return sub.load()\n"
            ),
        },
    )
    targets = _calls(builder, "caller.py::caller.f")
    assert [t for t, _ in targets] == [f"{tmp_path / 'stores' / 'sub.py'}::sub.load"]


def test_member_imported_class_classmethod_links(tmp_path: Path) -> None:
    builder = _build(
        tmp_path,
        {
            "stores/__init__.py": "",
            "stores/service.py": (
                "class Store:\n"
                "    @classmethod\n"
                "    def open(cls):\n"
                "        return cls()\n"
            ),
            "caller.py": (
                "from stores import Store\n\ndef f():\n    return Store.open()\n"
            ),
        },
    )
    targets = _calls(builder, "caller.py::caller.f")
    assert [t for t, _ in targets] == [
        f"{tmp_path / 'stores' / 'service.py'}::service.Store.open"
    ]


def test_member_imported_external_origin_answers_external(tmp_path: Path) -> None:
    builder = _build(
        tmp_path,
        {
            "caller.py": (
                "from pathlib import Path\n\ndef f(p):\n    return Path.cwd() / p\n"
            ),
        },
    )
    targets = _calls(builder, "caller.py::caller.f")
    assert [t for t, _ in targets] == ["external::pathlib::Path.cwd"]


# ---------------------------------------------------------------------------
# Factory-produced receivers
# ---------------------------------------------------------------------------


def test_factory_signature_hop_links_returned_class_method(tmp_path: Path) -> None:
    builder = _build(
        tmp_path,
        {
            "helpers.py": (
                "class Store:\n"
                "    def get(self, key):\n"
                "        return key\n"
                "\n"
                "def make_store() -> Store:\n"
                "    return Store()\n"
            ),
            "caller.py": (
                "from helpers import make_store\n\n"
                "def f():\n"
                "    store = make_store()\n"
                "    return store.get('k')\n"
            ),
        },
    )
    targets = _calls(builder, "caller.py::caller.f")
    assert [t for t, _ in targets] == [
        f"{tmp_path / 'helpers.py'}::helpers.make_store",
        f"{tmp_path / 'helpers.py'}::helpers.Store.get",
    ]


def test_factory_without_return_annotation_keeps_the_hole(tmp_path: Path) -> None:
    builder = _build(
        tmp_path,
        {
            "helpers.py": (
                "class Store:\n"
                "    def get(self, key):\n"
                "        return key\n"
                "\n"
                "def make_store():\n"
                "    return Store()\n"
            ),
            "caller.py": (
                "from helpers import make_store\n\n"
                "def f():\n"
                "    store = make_store()\n"
                "    return store.get('k')\n"
            ),
        },
    )
    targets = _calls(builder, "caller.py::caller.f")
    assert [classify_endpoint_id(t) for t, _ in targets] == [
        EndpointKind.INTERNAL,  # make_store() itself
        EndpointKind.UNRESOLVED,  # store.get(): nothing stated the type
    ]


def test_external_factory_answers_external(tmp_path: Path) -> None:
    builder = _build(
        tmp_path,
        {
            "caller.py": (
                "import sqlite3\n\n"
                "def f(path):\n"
                "    conn = sqlite3.connect(path)\n"
                "    return conn.execute('SELECT 1')\n"
            ),
        },
    )
    targets = _calls(builder, "caller.py::caller.f")
    assert [t for t, _ in targets] == [
        "external::sqlite3::connect",
        "external::sqlite3::connect.execute",
    ]


def test_factory_returning_builtin_answers_builtins(tmp_path: Path) -> None:
    builder = _build(
        tmp_path,
        {
            "helpers.py": ("def label() -> str:\n    return 'x'\n"),
            "caller.py": (
                "from helpers import label\n\n"
                "def f():\n"
                "    text = label()\n"
                "    return text.startswith('x')\n"
            ),
        },
    )
    targets = _calls(builder, "caller.py::caller.f")
    assert [t for t, _ in targets] == [
        f"{tmp_path / 'helpers.py'}::helpers.label",
        "external::builtins::str.startswith",
    ]
