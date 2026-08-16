"""Bounded scanner for Vue Single File Component top-level sections.

Vue SFC blocks may carry attributes in any order, quoted with either single or
double quotes, unquoted, or valueless (``setup``, ``scoped``). Tag names are
matched case-insensitively. The scanner returns exact content offsets so parsed
declarations can be rebased onto ``.vue`` line numbers, and reports malformed or
unterminated blocks instead of dropping them silently.
"""

from __future__ import annotations

from dataclasses import dataclass


SECTION_TAGS = frozenset({"template", "script", "style"})

_NESTABLE_TAGS = frozenset({"template"})
_ATTRIBUTE_NAME_STOP = frozenset({"=", ">", "/"})
_QUOTES = frozenset({'"', "'"})


@dataclass(frozen=True)
class VueSection:
    """One top-level SFC block with exact ``.vue`` coordinates."""

    tag: str
    attributes: dict[str, str]
    content: str
    tag_line_start: int
    content_line_start: int
    line_end: int

    @property
    def lang(self) -> str | None:
        """Return the lowercased ``lang`` attribute when present."""
        value = self.attributes.get("lang")
        return value.lower() if value else None

    @property
    def is_setup(self) -> bool:
        """Return whether this is a ``<script setup>`` block."""
        return "setup" in self.attributes


@dataclass(frozen=True)
class SfcScanResult:
    """Sections discovered in a component plus any malformed-section errors."""

    sections: tuple[VueSection, ...]
    errors: tuple[str, ...]

    def first(self, tag: str) -> VueSection | None:
        """Return the first section for ``tag``."""
        for section in self.sections:
            if section.tag == tag:
                return section
        return None

    def all(self, tag: str) -> tuple[VueSection, ...]:
        """Return every section for ``tag`` in document order."""
        return tuple(section for section in self.sections if section.tag == tag)

    def script(self) -> VueSection | None:
        """Return the block that defines the template-facing script surface.

        A ``<script setup>`` block wins over a plain ``<script>`` block because
        Vue exposes its top-level bindings directly to the template.
        """
        scripts = [section for section in self.sections if section.tag == "script"]
        if not scripts:
            return None
        for section in scripts:
            if section.is_setup:
                return section
        return scripts[0]


@dataclass(frozen=True)
class _OpenTag:
    tag: str
    attributes: dict[str, str]
    content_start: int
    self_closing: bool


def _line_of(source: str, index: int) -> int:
    """Return the one-based line number containing ``index``."""
    return source.count("\n", 0, index) + 1


def _is_tag_boundary(source: str, index: int) -> bool:
    return index >= len(source) or source[index].isspace() or source[index] in "></"


def _section_tag_at(source: str, index: int) -> str | None:
    """Return the block tag name opening at ``index``, if any."""
    cursor = index + 1
    while cursor < len(source) and (source[cursor].isalnum() or source[cursor] in "-_"):
        cursor += 1

    tag = source[index + 1 : cursor].lower()
    if tag in SECTION_TAGS and _is_tag_boundary(source, cursor):
        return tag
    return None


def _read_attribute_value(source: str, cursor: int) -> tuple[str, int] | None:
    """Read a quoted or unquoted attribute value starting at ``cursor``."""
    if source[cursor] in _QUOTES:
        quote = source[cursor]
        cursor += 1
        end = source.find(quote, cursor)
        if end == -1:
            return None
        return source[cursor:end], end + 1

    start = cursor
    while (
        cursor < len(source) and not source[cursor].isspace() and source[cursor] != ">"
    ):
        cursor += 1
    return source[start:cursor], cursor


def _scan_open_tag(source: str, index: int) -> _OpenTag | None:
    """Parse an SFC block open tag at ``index``, tolerating any attribute form."""
    tag = _section_tag_at(source, index)
    if tag is None:
        return None

    cursor = index + 1 + len(tag)
    attributes: dict[str, str] = {}
    while cursor < len(source):
        while cursor < len(source) and source[cursor].isspace():
            cursor += 1
        if cursor >= len(source):
            return None

        char = source[cursor]
        if char == ">":
            return _OpenTag(tag, attributes, cursor + 1, self_closing=False)
        if char == "/":
            if source.startswith("/>", cursor):
                return _OpenTag(tag, attributes, cursor + 2, self_closing=True)
            cursor += 1
            continue

        name_start = cursor
        while (
            cursor < len(source)
            and not source[cursor].isspace()
            and source[cursor] not in _ATTRIBUTE_NAME_STOP
        ):
            cursor += 1
        name = source[name_start:cursor].lower()
        if not name:
            return None

        after_name = cursor
        while cursor < len(source) and source[cursor].isspace():
            cursor += 1
        if cursor < len(source) and source[cursor] == "=":
            cursor += 1
            while cursor < len(source) and source[cursor].isspace():
                cursor += 1
            if cursor >= len(source):
                return None
            read = _read_attribute_value(source, cursor)
            if read is None:
                return None
            value, cursor = read
        else:
            cursor = after_name
            value = ""

        attributes.setdefault(name, value)

    return None


def _find_close_tag(source: str, tag: str, start: int) -> tuple[int, int] | None:
    """Return ``(content_end, tag_end)`` for the matching close tag."""
    lowered = source.lower()
    open_token = f"<{tag}"
    close_token = f"</{tag}"
    nestable = tag in _NESTABLE_TAGS
    depth = 1
    cursor = start

    while cursor < len(source):
        next_close = lowered.find(close_token, cursor)
        if next_close == -1:
            return None

        if nestable:
            next_open = lowered.find(open_token, cursor)
            if (
                next_open != -1
                and next_open < next_close
                and _is_tag_boundary(source, next_open + len(open_token))
            ):
                depth += 1
                cursor = next_open + len(open_token)
                continue

        probe = next_close + len(close_token)
        while probe < len(source) and source[probe].isspace():
            probe += 1
        if probe < len(source) and source[probe] == ">":
            depth -= 1
            if depth == 0:
                return next_close, probe + 1
        cursor = next_close + len(close_token)

    return None


def scan_sfc_sections(source: str) -> SfcScanResult:
    """Scan top-level ``<template>``, ``<script>``, and ``<style>`` blocks."""
    sections: list[VueSection] = []
    errors: list[str] = []
    index = 0

    while index < len(source):
        opening = source.find("<", index)
        if opening == -1:
            break

        open_tag = _scan_open_tag(source, opening)
        if open_tag is None:
            malformed = _section_tag_at(source, opening)
            if malformed is not None:
                errors.append(
                    f"Malformed <{malformed}> tag on line {_line_of(source, opening)}"
                )
            index = opening + 1
            continue

        tag_line_start = _line_of(source, opening)
        if open_tag.self_closing:
            sections.append(
                VueSection(
                    tag=open_tag.tag,
                    attributes=open_tag.attributes,
                    content="",
                    tag_line_start=tag_line_start,
                    content_line_start=tag_line_start,
                    line_end=_line_of(source, open_tag.content_start - 1),
                )
            )
            index = open_tag.content_start
            continue

        closing = _find_close_tag(source, open_tag.tag, open_tag.content_start)
        if closing is None:
            errors.append(
                f"Unclosed <{open_tag.tag}> section starting on line {tag_line_start}"
            )
            break

        content_end, tag_end = closing
        sections.append(
            VueSection(
                tag=open_tag.tag,
                attributes=open_tag.attributes,
                content=source[open_tag.content_start : content_end],
                tag_line_start=tag_line_start,
                content_line_start=_line_of(source, open_tag.content_start),
                line_end=_line_of(source, tag_end - 1),
            )
        )
        index = tag_end

    return SfcScanResult(tuple(sections), tuple(errors))
