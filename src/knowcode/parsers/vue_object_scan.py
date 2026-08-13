"""Brace-aware scanning of Vue Options API and ``defineProps`` object literals.

Vue declares its template-facing names as keys of object literals: ``data()``
returns one, ``methods``/``computed`` are ones, and runtime ``defineProps`` takes
one. A regex over such a block also matches everything nested inside it, so
``methods: { save() { if (ok) {} } }`` yields a method named ``if``, and
``defineProps({ label: { type: String } })`` yields props named ``type``.

Those fabricated names are worse than a missing one: once they reach the
component symbol table, a template reference resolves to an entity that
corresponds to no declaration. This module returns only the keys at the block's
own nesting level.
"""

from __future__ import annotations

import re

_MEMBER_NAME = re.compile(r"[A-Za-z_$][\w$]*")
_OPENING = "{[("
_CLOSING = "}])"
_QUOTES = "\"'`"


def _skip_string(text: str, index: int) -> int:
    """Return the index just past the string literal starting at ``index``."""
    quote = text[index]
    index += 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == quote:
            return index + 1
        index += 1
    return index


def _skip_comment(text: str, index: int) -> int | None:
    """Return the index just past a comment at ``index``, or ``None``."""
    if not text.startswith("//", index):
        if not text.startswith("/*", index):
            return None
        end = text.find("*/", index + 2)
        return len(text) if end == -1 else end + 2
    end = text.find("\n", index)
    return len(text) if end == -1 else end


def find_balanced_block(text: str, open_index: int) -> str | None:
    """Return the body between the brace at ``open_index`` and its partner.

    Returns ``None`` when the block is never closed, so an unterminated literal
    is reported rather than silently treated as running to end of file.
    """
    if open_index >= len(text) or text[open_index] != "{":
        return None

    depth = 0
    index = open_index
    while index < len(text):
        char = text[index]
        if char in _QUOTES:
            index = _skip_string(text, index)
            continue
        after_comment = _skip_comment(text, index)
        if after_comment is not None:
            index = after_comment
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[open_index + 1 : index]
        index += 1
    return None


def top_level_keys(body: str) -> list[str]:
    """Return the object keys declared directly in ``body``, in source order.

    A key is an identifier at the body's own nesting level followed by ``:`` (a
    property) or ``(`` (a shorthand method). Names inside nested objects, arrays,
    argument lists, strings, and comments are ignored.
    """
    keys: list[str] = []
    depth = 0
    index = 0

    while index < len(body):
        char = body[index]

        if char in _QUOTES:
            index = _skip_string(body, index)
            continue

        after_comment = _skip_comment(body, index)
        if after_comment is not None:
            index = after_comment
            continue

        if char in _OPENING:
            depth += 1
            index += 1
            continue

        if char in _CLOSING:
            depth -= 1
            index += 1
            continue

        if depth == 0:
            match = _MEMBER_NAME.match(body, index)
            if match is not None:
                cursor = match.end()
                while cursor < len(body) and body[cursor] in " \t\r\n":
                    cursor += 1
                if cursor < len(body) and body[cursor] in ":(":
                    keys.append(match.group(0))
                index = match.end()
                continue

        index += 1

    return keys
