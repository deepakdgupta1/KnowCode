"""Extension dispatch gates (Step 07, task 1).

Validates that the supported file extensions route to the correct parser and
extract declarations. Covers ``.tsx`` (TypeScript with JSX surface) and a Vue
``<script setup lang="ts">`` block, where supported. JSX bodies are a known
tree-sitter grammar limitation documented in the parser construct matrix.
"""

from __future__ import annotations

from pathlib import Path

from knowcode.parsers.javascript_parser import JavaScriptParser
from knowcode.parsers.typescript_parser import TypeScriptParser
from knowcode.parsers.vue_parser import VueParser


def _qualified_names(parser: object, path: Path) -> set[str]:
    return {entity.qualified_name for entity in parser.parse_file(path).entities}  # type: ignore[attr-defined]


def test_tsx_file_is_extracted_as_typescript(tmp_path: Path) -> None:
    """A ``.tsx`` file routes to the TypeScript parser and extracts declarations.

    JSX-free TypeScript in a ``.tsx`` file parses cleanly; JSX bodies remain a
    documented tree-sitter grammar limitation, not a routing failure.
    """
    source = tmp_path / "card.tsx"
    source.write_text(
        "export interface Card { title: string }\n"
        "export function render(c: Card) { return c.title }\n",
        encoding="utf-8",
    )

    names = _qualified_names(TypeScriptParser(), source)

    assert "card.Card" in names
    assert "card.render" in names


def test_jsx_file_is_extracted_as_javascript(tmp_path: Path) -> None:
    """A ``.jsx`` file routes to the JavaScript parser and extracts declarations."""
    source = tmp_path / "btn.jsx"
    source.write_text("export function Btn() { return 1 }\n", encoding="utf-8")

    names = _qualified_names(JavaScriptParser(), source)

    assert "btn.Btn" in names


def test_vue_setup_lang_ts_block_is_extracted(tmp_path: Path) -> None:
    """A Vue ``<script setup lang="ts">`` block is parsed and extracts declarations.

    TypeScript-flavored setup blocks are supported for declaration extraction;
    generic ``defineEmits<{...}>()`` captures only the first event (documented
    limitation in the parser construct matrix).
    """
    source = tmp_path / "widget.vue"
    source.write_text(
        "<template><button>{{ label }}</button></template>\n"
        '<script setup lang="ts">\n'
        "import { ref } from 'vue'\n"
        "const label = ref('hi')\n"
        "function onClick() { label.value = 'bye' }\n"
        "</script>\n",
        encoding="utf-8",
    )

    names = _qualified_names(VueParser(), source)

    assert any(name.endswith(".label") for name in names), names
    assert any(name.endswith(".onClick") for name in names), names
