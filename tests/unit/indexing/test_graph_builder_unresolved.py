"""Unit tests for GraphBuilder classification of import-bound references.

The Python parser records where a receiver or imported name came from; these
tests pin how ``GraphBuilder`` turns that binding into either a link to a
repo entity, an ``external::`` answer, or a kept hole when linking would be a
guess.
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


def _calls(builder: GraphBuilder, source_suffix: str) -> list[str]:
    return [
        rel.target_id
        for rel in builder.relationships
        if rel.kind is RelationshipKind.CALLS and rel.source_id.endswith(source_suffix)
    ]


def test_external_module_receiver_becomes_external(tmp_path: Path) -> None:
    builder = _build(
        tmp_path,
        {"caller.py": "import json\n\n\ndef f(s):\n    return json.loads(s)\n"},
    )
    targets = _calls(builder, "caller.py::caller.f")
    assert targets == ["external::json::loads"]
    assert classify_endpoint_id(targets[0]) is EndpointKind.EXTERNAL


def test_in_repo_module_receiver_links_to_entity(tmp_path: Path) -> None:
    builder = _build(
        tmp_path,
        {
            "helpers.py": "def load():\n    return 1\n",
            "caller.py": "import helpers\n\n\ndef f():\n    return helpers.load()\n",
        },
    )
    targets = _calls(builder, "caller.py::caller.f")
    assert len(targets) == 1
    assert targets[0].endswith("helpers.py::helpers.load")
    assert classify_endpoint_id(targets[0]) is EndpointKind.INTERNAL


def test_in_repo_receiver_without_symbol_keeps_the_hole(tmp_path: Path) -> None:
    builder = _build(
        tmp_path,
        {
            "helpers.py": "def load():\n    return 1\n",
            "caller.py": "import helpers\n\n\ndef f():\n    return helpers.absent()\n",
        },
    )
    targets = _calls(builder, "caller.py::caller.f")
    assert len(targets) == 1
    assert classify_endpoint_id(targets[0]) is EndpointKind.UNRESOLVED


def test_from_import_from_external_module_externalizes(tmp_path: Path) -> None:
    builder = _build(
        tmp_path,
        {
            "caller.py": (
                "from os.path import join\n\n\ndef f(p):\n    return join('a', p)\n"
            ),
        },
    )
    targets = _calls(builder, "caller.py::caller.f")
    assert targets == ["external::os.path::join"]
    assert classify_endpoint_id(targets[0]) is EndpointKind.EXTERNAL


def test_from_import_from_repo_module_links(tmp_path: Path) -> None:
    builder = _build(
        tmp_path,
        {
            "helpers.py": "def load():\n    return 1\n",
            "caller.py": "from helpers import load\n\n\ndef f():\n    return load()\n",
        },
    )
    targets = _calls(builder, "caller.py::caller.f")
    assert len(targets) == 1
    assert targets[0].endswith("helpers.py::helpers.load")
    assert classify_endpoint_id(targets[0]) is EndpointKind.INTERNAL


def test_imported_base_class_from_stdlib_externalizes(tmp_path: Path) -> None:
    builder = _build(
        tmp_path,
        {
            "colors.py": "from enum import Enum\n\n\nclass Color(Enum):\n    pass\n",
        },
    )
    inherits = [
        rel.target_id
        for rel in builder.relationships
        if rel.kind is RelationshipKind.INHERITS
    ]
    assert inherits == ["external::enum::Enum"]
    assert classify_endpoint_id(inherits[0]) is EndpointKind.EXTERNAL


def test_imported_base_class_from_repo_module_links(tmp_path: Path) -> None:
    builder = _build(
        tmp_path,
        {
            "base.py": "class Base:\n    pass\n",
            "sub.py": "from base import Base\n\n\nclass Sub(Base):\n    pass\n",
        },
    )
    inherits = [
        rel.target_id
        for rel in builder.relationships
        if rel.kind is RelationshipKind.INHERITS
    ]
    assert len(inherits) == 1
    assert inherits[0].endswith("base.py::base.Base")
    assert classify_endpoint_id(inherits[0]) is EndpointKind.INTERNAL


def test_bare_unresolved_links_repo_unique_name(tmp_path: Path) -> None:
    builder = _build(
        tmp_path,
        {
            "helpers.py": "def load():\n    return 1\n",
            "caller.py": "def f():\n    return load()\n",
        },
    )
    targets = _calls(builder, "caller.py::caller.f")
    assert len(targets) == 1
    assert targets[0].endswith("helpers.py::helpers.load")


def test_bare_unresolved_ambiguous_name_keeps_the_hole(tmp_path: Path) -> None:
    builder = _build(
        tmp_path,
        {
            "one.py": "def load():\n    return 1\n",
            "two.py": "def load():\n    return 2\n",
            "caller.py": "def f():\n    return load()\n",
        },
    )
    targets = _calls(builder, "caller.py::caller.f")
    assert len(targets) == 1
    assert classify_endpoint_id(targets[0]) is EndpointKind.UNRESOLVED


def test_receiver_expression_call_never_links_by_name(tmp_path: Path) -> None:
    builder = _build(
        tmp_path,
        {
            "read.py": "def read():\n    return 1\n",
            "caller.py": "def f(p):\n    return open(p).read()\n",
        },
    )
    targets = _calls(builder, "caller.py::caller.f")
    # ``open`` is a builtin answer; ``read`` lost its receiver and must not be
    # linked to the repo's bare ``read`` function by name.
    assert sorted(t.rsplit("::", 1)[-1] for t in targets) == ["open", "read"]
    by_symbol = {t.rsplit("::", 1)[-1]: t for t in targets}
    assert classify_endpoint_id(by_symbol["open"]) is EndpointKind.EXTERNAL
    assert classify_endpoint_id(by_symbol["read"]) is EndpointKind.UNRESOLVED


def test_local_receiver_call_keeps_its_hole(tmp_path: Path) -> None:
    builder = _build(
        tmp_path,
        {"caller.py": "def f(pkg):\n    return pkg.run()\n"},
    )
    targets = _calls(builder, "caller.py::caller.f")
    assert len(targets) == 1
    assert classify_endpoint_id(targets[0]) is EndpointKind.UNRESOLVED


def test_bare_unresolved_does_not_link_across_languages(tmp_path: Path) -> None:
    builder = _build(
        tmp_path,
        {
            "helper.py": "def visible():\n    return 1\n",
            "caller.js": "export function run() {\n    return visible();\n}\n",
        },
    )
    js_calls = [
        rel.target_id
        for rel in builder.relationships
        if rel.kind is RelationshipKind.CALLS
        and rel.source_id.split("::")[0].endswith("caller.js")
    ]
    assert len(js_calls) == 1
    assert classify_endpoint_id(js_calls[0]) is EndpointKind.UNRESOLVED
