"""Structure-aware parent-child chunker for prose / SDLC document collateral.

Prototype for **P0** of ``docs/research/document_collateral_retrieval_2026.md``.

Unlike the code :class:`~knowcode.indexing.chunker.Chunker` (which chunks by AST
entity) and unlike naive fixed-window chunking, this chunker:

* splits on the document's **heading hierarchy** (markdown ATX or
  reStructuredText adornments, per :class:`ProseDialect`), never mid-section;
* keeps **code fences and tables atomic** where they fit, and **hard-bounds**
  every chunk to ``max_tokens`` (token-window split as a last resort, flagged);
* emits **child** chunks (a section's own body) linked to their **parent**
  section id, enabling small-to-big retrieval;
* **merges** adjacent under-sized siblings so the corpus is not shredded into
  micro-chunks;
* records faithful original-file ``line_range`` pointers (content is derived
  index data; the source remains authoritative, per the reference design);
* prepends a cheap, no-LLM **context header** (``title > h1 > h2``) so a chunk
  carries its document context even out of context.

No new dependencies: ``pyyaml`` (frontmatter) + ``tiktoken`` (via
:class:`~knowcode.utils.token_counter.TokenCounter`) are already core deps.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol, cast

import yaml

from knowcode.utils.logger import get_logger
from knowcode.utils.token_counter import TokenCounter

logger = get_logger(__name__)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_TABLE_RE = re.compile(r"^\s*\|")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

# (content, start_line, end_line, is_oversize)
_RawChunk = tuple[str, int, int, bool]


@dataclass(frozen=True)
class ProseChunkingConfig:
    """Tunables for prose chunking (token budgets are tiktoken/cl100k)."""

    target_tokens: int = 400
    max_tokens: int = 512
    min_tokens: int = 128  # below this, merge with an adjacent same-parent sibling
    prepend_context_header: bool = True


@dataclass(frozen=True)
class _Block:
    """A contiguous atomic unit within a section body."""

    kind: str  # "prose" | "code" | "table"
    start_line: int  # original file, 1-indexed
    end_line: int  # original file, 1-indexed inclusive


@dataclass(frozen=True)
class _Section:
    """A heading node and the span of its own body (excludes subsections)."""

    level: int  # 1..6; 0 = preamble / document root
    title: str
    heading_path: tuple[str, ...]
    section_id: str
    parent_id: str | None
    body_start_line: int  # original file, 1-indexed
    body_end_line: int  # original file, 1-indexed inclusive (< start if empty)


@dataclass(frozen=True)
class ProseChunk:
    """A retrieval unit: a section's body (or a token-bounded slice of it)."""

    id: str
    doc_id: str
    doc_type: str
    title: str
    heading_path: tuple[str, ...]
    context_header: str
    section_id: str
    parent_id: str | None
    level: int
    start_line: int
    end_line: int
    token_count: int
    content: str
    content_hash: str
    is_oversize: bool
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def embedding_text(self) -> str:
        """Text to embed: context header + content (cheap contextualization)."""
        if self.context_header:
            return f"{self.context_header}\n\n{self.content}"
        return self.content


class ProseChunker:
    """Chunks a prose document into structure-aware parent-child units.

    The chunking *algorithm* (section tree -> atomic blocks -> token packing ->
    bounding -> sibling merge) is format-agnostic. Only frontmatter splitting,
    heading detection, and block-boundary detection are format-specific; those
    live behind a :class:`ProseDialect`. Pass ``dialect`` to force a format, or
    leave it ``None`` to auto-detect per file by extension in :meth:`chunk_file`
    (defaulting to markdown for raw text via :meth:`chunk_text`).
    """

    def __init__(
        self,
        config: ProseChunkingConfig | None = None,
        dialect: ProseDialect | None = None,
    ) -> None:
        self.config = config or ProseChunkingConfig()
        self._counter = TokenCounter()
        self._dialect = dialect  # None -> auto-detect per file / default markdown

    # -- public API ---------------------------------------------------------

    def chunk_file(self, path: str | Path, doc_id: str | None = None) -> list[ProseChunk]:
        """Chunk a prose file, picking the dialect from its extension."""
        p = Path(path)
        text = p.read_text(encoding="utf-8", errors="replace")
        dialect = self._dialect or dialect_for_suffix(p.suffix)
        return self.chunk_text(text, doc_id or str(path), dialect=dialect)

    def chunk_text(
        self, text: str, doc_id: str, dialect: ProseDialect | None = None
    ) -> list[ProseChunk]:
        """Chunk raw prose text identified by ``doc_id`` (markdown by default)."""
        dialect = dialect or self._dialect or MarkdownDialect()
        lines = text.splitlines()
        metadata, body_start = dialect.split_frontmatter(lines)
        doc_type = _infer_doc_type(metadata, doc_id)
        headings = dialect.scan_headings(lines, body_start)
        title = dialect.infer_title(metadata, doc_id, headings)
        base_meta = _select_metadata(metadata)

        chunks: list[ProseChunk] = []
        for section in _build_sections(lines, body_start, doc_id, title, headings):
            chunks.extend(
                self._chunk_section(
                    section, lines, doc_id, doc_type, title, base_meta, dialect
                )
            )
        return self._merge_small(chunks, lines)

    # -- section -> chunks --------------------------------------------------

    def _chunk_section(
        self,
        section: _Section,
        lines: list[str],
        doc_id: str,
        doc_type: str,
        title: str,
        base_meta: dict[str, object],
        dialect: ProseDialect,
    ) -> list[ProseChunk]:
        if section.body_end_line < section.body_start_line:
            return []  # heading with no body (pure parent) — exists only as a node
        blocks = dialect.blocks_in_span(lines, section.body_start_line, section.body_end_line)
        if not blocks:
            return []

        raw = self._pack_blocks(blocks, lines)
        context_header = _context_header(title, section.heading_path)
        return [
            self._assemble(
                content, start, end, section, doc_id, doc_type, title,
                context_header, base_meta, idx, oversize,
            )
            for idx, (content, start, end, oversize) in enumerate(raw)
        ]

    def _pack_blocks(self, blocks: list[_Block], lines: list[str]) -> list[_RawChunk]:
        """Greedily pack blocks toward ``target_tokens``; bound oversize blocks."""
        raw: list[_RawChunk] = []
        pending: list[_Block] = []
        pending_tokens = 0

        def flush() -> None:
            nonlocal pending, pending_tokens
            if not pending:
                return
            s0, e0 = pending[0].start_line, pending[-1].end_line
            raw.append((_span_text(lines, s0, e0), s0, e0, False))
            pending = []
            pending_tokens = 0

        for block in blocks:
            btext = _span_text(lines, block.start_line, block.end_line)
            btokens = self._counter.count_tokens(btext)
            if btokens > self.config.max_tokens:
                flush()
                raw.extend(self._bound_block(block, btext))
                continue
            if pending and pending_tokens + btokens > self.config.max_tokens:
                flush()
            pending.append(block)
            pending_tokens += btokens
            if pending_tokens >= self.config.target_tokens:
                flush()
        flush()
        return raw

    def _bound_block(self, block: _Block, text: str) -> list[_RawChunk]:
        """Split a single over-max block into <= max_tokens pieces.

        Code/tables are token-window split and flagged oversize (atomicity
        could not be preserved). Prose is sentence-packed, with a token-window
        fallback for sentence-less blobs.
        """
        span = (block.start_line, block.end_line)
        if block.kind != "prose":
            logger.debug("hard-splitting oversize %s block at L%d", block.kind, span[0])
            return self._token_window(text, span, oversize=True)

        out: list[_RawChunk] = []
        buf: list[str] = []
        for sentence in _SENTENCE_RE.split(text):
            if self._counter.count_tokens(sentence) > self.config.max_tokens:
                if buf:
                    out.append((" ".join(buf), span[0], span[1], False))
                    buf = []
                out.extend(self._token_window(sentence, span, oversize=True))
                continue
            if buf and self._counter.count_tokens(" ".join([*buf, sentence])) > self.config.max_tokens:
                out.append((" ".join(buf), span[0], span[1], False))
                buf = []
            buf.append(sentence)
        if buf:
            out.append((" ".join(buf), span[0], span[1], False))
        return out

    def _token_window(self, text: str, span: tuple[int, int], *, oversize: bool) -> list[_RawChunk]:
        ids = self._counter.encoding.encode(text)
        step = self.config.max_tokens
        return [
            (self._counter.encoding.decode(ids[i:i + step]), span[0], span[1], oversize)
            for i in range(0, len(ids), step)
        ]

    # -- merge + assemble ---------------------------------------------------

    def _merge_small(self, chunks: list[ProseChunk], lines: list[str]) -> list[ProseChunk]:
        """Merge an under-sized chunk into the next line-adjacent same-parent one."""
        if not chunks:
            return chunks
        merged: list[ProseChunk] = [chunks[0]]
        for cur in chunks[1:]:
            prev = merged[-1]
            # Only merge a small chunk into the next line-adjacent (<=3 lines of
            # heading/blank gap) same-parent sibling, and re-verify the spanned
            # content stays under budget — the span can pull in more than the two
            # chunks' own text, so the token sum alone is not a safe guard.
            adjacent = 0 < cur.start_line - prev.end_line <= 3
            if (
                prev.parent_id == cur.parent_id
                and not prev.is_oversize and not cur.is_oversize
                and prev.token_count < self.config.min_tokens
                and adjacent
            ):
                content = _span_text(lines, prev.start_line, cur.end_line)
                if self._counter.count_tokens(content) <= self.config.max_tokens:
                    merged[-1] = replace(
                        prev, end_line=cur.end_line, content=content,
                        token_count=self._counter.count_tokens(content),
                        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    )
                    continue
            merged.append(cur)
        return merged

    def _assemble(
        self, content: str, start_line: int, end_line: int, section: _Section,
        doc_id: str, doc_type: str, title: str, context_header: str,
        base_meta: dict[str, object], idx: int, oversize: bool,
    ) -> ProseChunk:
        return ProseChunk(
            id=f"{section.section_id}::c{idx}",
            doc_id=doc_id,
            doc_type=doc_type,
            title=title,
            heading_path=section.heading_path,
            context_header=context_header if self.config.prepend_context_header else "",
            section_id=section.section_id,
            parent_id=section.parent_id,
            level=section.level,
            start_line=start_line,
            end_line=end_line,
            token_count=self._counter.count_tokens(content),
            content=content,
            content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            is_oversize=oversize,
            metadata=dict(base_meta),
        )


# -- module-level helpers ---------------------------------------------------


def _split_frontmatter(lines: list[str]) -> tuple[dict[str, object], int]:
    """Return (metadata, body_start_line) where body_start_line is 1-indexed.

    A leading ``---`` ... ``---`` block is parsed as YAML. Malformed YAML is
    skipped (metadata empty) but the delimiter block is still stripped.
    """
    if not lines or lines[0].strip() != "---":
        return {}, 1
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            raw = "\n".join(lines[1:i])
            try:
                data = yaml.safe_load(raw) or {}
                meta = data if isinstance(data, dict) else {}
            except yaml.YAMLError:
                logger.debug("unparseable frontmatter; skipping")
                meta = {}
            return meta, i + 2  # line after the closing delimiter (1-indexed)
    return {}, 1  # unterminated frontmatter -> treat all as body


def _scan_headings(lines: list[str], body_start: int) -> list[tuple[int, int, str]]:
    """Return (line_no, level, title) for ATX headings outside fenced code."""
    headings: list[tuple[int, int, str]] = []
    in_fence = False
    marker = ""
    for line_no in range(body_start, len(lines) + 1):
        raw = lines[line_no - 1]
        fence = _FENCE_RE.match(raw)
        if fence:
            tok = fence.group(1)[:3]
            if not in_fence:
                in_fence, marker = True, tok
            elif tok[0] == marker[0]:
                in_fence = False
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(raw)
        if m:
            headings.append((line_no, len(m.group(1)), m.group(2).strip()))
    return headings


def _build_sections(
    lines: list[str],
    body_start: int,
    doc_id: str,
    title: str,
    headings: list[tuple[int, int, str]] | None = None,
) -> list[_Section]:
    """Build the section list (preamble + one node per heading) with parents.

    ``headings`` is dialect-supplied ``(line_no, level, title)`` triples; when
    omitted it falls back to the markdown ATX scanner (backward-compatible).
    """
    last_line = len(lines)
    if headings is None:
        headings = _scan_headings(lines, body_start)
    # Normalize to (heading_start_line, level, title, body_start_line). Markdown
    # headings occupy one line (body starts the next); RST headings span 2-3
    # lines (title/underline +/- overline) and carry their own body start.
    norm = [
        h if len(h) == 4 else (h[0], h[1], h[2], h[0] + 1)
        for h in headings
    ]
    root_id = f"{doc_id}::sec::_root"
    sections: list[_Section] = []

    first_heading_line = norm[0][0] if norm else last_line + 1
    if first_heading_line - 1 >= body_start:  # preamble before the first heading
        sections.append(_Section(
            level=0, title=title, heading_path=(), section_id=root_id,
            parent_id=None, body_start_line=body_start,
            body_end_line=first_heading_line - 1,
        ))

    stack: list[tuple[int, str, str]] = []  # (level, title, section_id)
    for i, (line_no, level, htitle, body_start_line) in enumerate(norm):
        while stack and stack[-1][0] >= level:
            stack.pop()
        heading_path = tuple(t for _, t, _ in stack) + (htitle,)
        section_id = f"{doc_id}::sec::{_slug(heading_path)}::L{line_no}"
        parent_id = stack[-1][2] if stack else root_id
        next_line = norm[i + 1][0] if i + 1 < len(norm) else last_line + 1
        sections.append(_Section(
            level=level, title=htitle, heading_path=heading_path,
            section_id=section_id, parent_id=parent_id,
            body_start_line=body_start_line, body_end_line=next_line - 1,
        ))
        stack.append((level, htitle, section_id))
    return sections


def _blocks_in_span(lines: list[str], start_line: int, end_line: int) -> list[_Block]:
    """Group a body span into atomic blocks (code fences/tables/prose paras)."""
    blocks: list[_Block] = []
    i = start_line
    while i <= end_line:
        raw = lines[i - 1]
        fence = _FENCE_RE.match(raw)
        if fence:
            marker = fence.group(1)[:3]
            j = i + 1
            while j <= end_line and not lines[j - 1].lstrip().startswith(marker):
                j += 1
            blocks.append(_Block("code", i, min(j, end_line)))
            i = min(j, end_line) + 1
            continue
        if not raw.strip():
            i += 1
            continue
        if _TABLE_RE.match(raw):
            j = i
            while j <= end_line and _TABLE_RE.match(lines[j - 1]):
                j += 1
            blocks.append(_Block("table", i, j - 1))
            i = j
            continue
        j = i  # prose paragraph: until blank line or start of code/table
        while j <= end_line:
            r = lines[j - 1]
            if not r.strip() or _FENCE_RE.match(r) or _TABLE_RE.match(r):
                break
            j += 1
        blocks.append(_Block("prose", i, j - 1))
        i = j
    return blocks


def _span_text(lines: list[str], start_line: int, end_line: int) -> str:
    return "\n".join(lines[start_line - 1:end_line]).strip("\n")


def _context_header(title: str, heading_path: tuple[str, ...]) -> str:
    parts = [title, *heading_path]
    return " > ".join(p for p in parts if p)


def _slug(heading_path: tuple[str, ...]) -> str:
    joined = "/".join(heading_path) if heading_path else "_intro"
    slug = re.sub(r"[^a-z0-9]+", "-", joined.lower()).strip("-")
    return slug or "_intro"


def _infer_doc_type(metadata: dict[str, object], doc_id: str) -> str:
    explicit = metadata.get("type")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    parts = Path(doc_id).parts
    if len(parts) >= 2:
        return parts[-2]  # parent directory as a coarse type (e.g. "articles")
    return "document"


def _select_metadata(metadata: dict[str, object]) -> dict[str, object]:
    """Keep a small, retrieval-useful subset of frontmatter for filtering."""
    keys = ("title", "type", "tags", "source", "author", "date", "date_added", "summary")
    return {k: metadata[k] for k in keys if k in metadata}


# -- dialects ---------------------------------------------------------------


class ProseDialect(Protocol):
    """Format-specific hooks the chunker needs; everything else is shared.

    A dialect localizes the three places prose formats diverge: stripping any
    leading metadata block, finding section headings (with a *level* per
    heading), and grouping a body span into atomic blocks. ``scan_headings``
    returns ``(line_no, level, title)`` triples (1-indexed ``line_no``; lower
    ``level`` = shallower).
    """

    def split_frontmatter(self, lines: list[str]) -> tuple[dict[str, object], int]:
        """Return ``(metadata, body_start_line)`` (1-indexed body start)."""
        ...

    def scan_headings(self, lines: list[str], body_start: int) -> list[tuple[int, int, str]]:
        """Return ``(line_no, level, title)`` for each heading in the body."""
        ...

    def blocks_in_span(self, lines: list[str], start_line: int, end_line: int) -> list[_Block]:
        """Group a body span into atomic prose/code/table blocks."""
        ...

    def infer_title(
        self, metadata: dict[str, object], doc_id: str, headings: list[tuple[int, int, str]]
    ) -> str:
        """Best-effort document title for the context header."""
        ...


class MarkdownDialect:
    """CommonMark/markdown: ``---`` frontmatter, ATX headings, fenced code."""

    def split_frontmatter(self, lines: list[str]) -> tuple[dict[str, object], int]:
        return _split_frontmatter(lines)

    def scan_headings(self, lines: list[str], body_start: int) -> list[tuple[int, int, str]]:
        return _scan_headings(lines, body_start)

    def blocks_in_span(self, lines: list[str], start_line: int, end_line: int) -> list[_Block]:
        return _blocks_in_span(lines, start_line, end_line)

    def infer_title(
        self, metadata: dict[str, object], doc_id: str, headings: list[tuple[int, int, str]]
    ) -> str:
        return str(metadata.get("title") or Path(doc_id).stem)


# -- reStructuredText helpers -----------------------------------------------

# Any printable non-alphanumeric ASCII punctuation may adorn a section title.
_RST_ADORNMENT_CHARS = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
_RST_DIRECTIVE_RE = re.compile(r"^\s*\.\.\s+([\w-]+)::")
# A simple-table ruler is >= 2 column groups of '='; a single run is a heading
# underline, so the ``(?:\s+=+)+`` (at least one further group) disambiguates.
_RST_SIMPLE_TABLE_RE = re.compile(r"^\s*=+(?:\s+=+)+\s*$")
_RST_CODE_DIRECTIVES = frozenset(
    {"code-block", "code", "sourcecode", "literalinclude", "parsed-literal", "math"}
)


def _rst_is_adornment(line: str) -> bool:
    """True if ``line`` is a section adornment run (>= 2 identical punctuation)."""
    s = line.strip()
    if len(s) < 2:
        return False
    head = s[0]
    return head in _RST_ADORNMENT_CHARS and all(ch == head for ch in s)


def _rst_is_grid_line(line: str) -> bool:
    s = line.strip()
    return s.startswith("+") and len(s) >= 2 and set(s) <= {"+", "-", "="}


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _rst_indented_block_end(
    lines: list[str], marker_line: int, end_line: int, marker_indent: int
) -> int:
    """Last line (1-indexed, inclusive) of the indented body after ``marker_line``.

    The body spans blank lines and lines indented deeper than ``marker_indent``,
    stopping at the first non-blank line dedented to the marker. Returns
    ``marker_line`` itself when there is no indented body.
    """
    last = marker_line
    j = marker_line + 1
    while j <= end_line:
        raw = lines[j - 1]
        if not raw.strip():
            j += 1
            continue
        if _indent(raw) > marker_indent:
            last = j
            j += 1
        else:
            break
    return last


def _scan_rst_headings(lines: list[str], body_start: int) -> list[tuple[int, int, str, int]]:
    """Return ``(heading_start, level, title, body_start)`` for RST sections.

    Levels follow reStructuredText's rule: each distinct adornment *style* —
    keyed on ``(character, has_overline)`` — takes the next level in the order
    it is first encountered (so ``=`` is not inherently level 1). Indented
    literal blocks (after ``::`` or a code directive) are skipped so adornments
    inside them are never mistaken for section underlines.
    """
    headings: list[tuple[int, int, str, int]] = []
    style_to_level: dict[tuple[str, bool], int] = {}
    n = len(lines)
    i = body_start
    while i <= n:
        raw = lines[i - 1]
        stripped = raw.strip()
        if not stripped:
            i += 1
            continue

        directive = _RST_DIRECTIVE_RE.match(raw)
        if directive and directive.group(1).lower() in _RST_CODE_DIRECTIVES:
            i = _rst_indented_block_end(lines, i, n, _indent(raw)) + 1
            continue

        if _rst_is_adornment(raw):
            # Possible overline: adornment / title / matching underline.
            if i + 2 <= n:
                title_line = lines[i]       # line i+1
                under = lines[i + 1]        # line i+2
                ch = stripped[0]
                title_text = title_line.strip()
                if (
                    title_text
                    and not _rst_is_adornment(title_line)
                    and _rst_is_adornment(under)
                    and under.strip()[0] == ch
                    and len(stripped) >= len(title_text)
                    and len(under.strip()) >= len(title_text)
                ):
                    level = style_to_level.setdefault((ch, True), len(style_to_level) + 1)
                    headings.append((i, level, title_text, i + 3))
                    i += 3
                    continue
            # A lone adornment run is a transition (or stray) — never a heading.
            i += 1
            continue

        # Underline-only heading: title line immediately followed by adornment.
        if i + 1 <= n and _rst_is_adornment(lines[i]):
            under = lines[i].strip()
            if len(under) >= len(stripped):
                ch = under[0]
                level = style_to_level.setdefault((ch, False), len(style_to_level) + 1)
                headings.append((i, level, stripped, i + 2))
                i += 2
                continue

        # Prose line opening a literal block — skip its indented body.
        if stripped.endswith("::"):
            i = _rst_indented_block_end(lines, i, n, _indent(raw)) + 1
            continue

        i += 1
    return headings


def _rst_blocks_in_span(lines: list[str], start_line: int, end_line: int) -> list[_Block]:
    """Group an RST body span into atomic prose/code/table blocks."""
    blocks: list[_Block] = []
    i = start_line
    while i <= end_line:
        raw = lines[i - 1]
        if not raw.strip():
            i += 1
            continue
        indent = _indent(raw)

        directive = _RST_DIRECTIVE_RE.match(raw)
        if directive:
            end = _rst_indented_block_end(lines, i, end_line, indent)
            kind = "code" if directive.group(1).lower() in _RST_CODE_DIRECTIVES else "prose"
            blocks.append(_Block(kind, i, end))
            i = end + 1
            continue

        if _rst_is_grid_line(raw):
            j = i
            while j <= end_line and lines[j - 1].lstrip()[:1] in ("+", "|"):
                j += 1
            blocks.append(_Block("table", i, j - 1))
            i = j
            continue

        if _RST_SIMPLE_TABLE_RE.match(raw):
            j = i
            last_ruler = i
            while j <= end_line and lines[j - 1].strip():
                if _RST_SIMPLE_TABLE_RE.match(lines[j - 1]):
                    last_ruler = j
                j += 1
            blocks.append(_Block("table", i, last_ruler))
            i = last_ruler + 1
            continue

        # Prose paragraph: until a blank line or a structural opener. A line
        # ending in ``::`` closes the paragraph and opens an indented literal.
        j = i
        opens_literal = False
        while j <= end_line:
            r = lines[j - 1]
            if (
                not r.strip()
                or _RST_DIRECTIVE_RE.match(r)
                or _rst_is_grid_line(r)
                or _RST_SIMPLE_TABLE_RE.match(r)
            ):
                break
            if r.rstrip().endswith("::"):
                j += 1
                opens_literal = True
                break
            j += 1
        blocks.append(_Block("prose", i, j - 1))
        i = j

        if opens_literal:
            k = i
            while k <= end_line and not lines[k - 1].strip():
                k += 1
            if k <= end_line and _indent(lines[k - 1]) > indent:
                end = _rst_indented_block_end(lines, k - 1, end_line, indent)
                blocks.append(_Block("code", k, end))
                i = end + 1
    return blocks


class RstDialect:
    """reStructuredText: adornment headings, indentation/ruler-delimited blocks.

    No YAML frontmatter; the title is taken from the first section heading.
    """

    def split_frontmatter(self, lines: list[str]) -> tuple[dict[str, object], int]:
        return {}, 1

    def scan_headings(
        self, lines: list[str], body_start: int
    ) -> list[tuple[int, int, str, int]]:
        return _scan_rst_headings(lines, body_start)

    def blocks_in_span(self, lines: list[str], start_line: int, end_line: int) -> list[_Block]:
        return _rst_blocks_in_span(lines, start_line, end_line)

    def infer_title(
        self, metadata: dict[str, object], doc_id: str, headings: list[tuple[int, int, str]]
    ) -> str:
        explicit = metadata.get("title")
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip()
        if headings:
            return str(headings[0][2])
        return Path(doc_id).stem


def dialect_for_suffix(suffix: str) -> ProseDialect:
    """Pick a dialect by file extension; markdown is the default fallback."""
    # The dialect classes are structurally compatible with ProseDialect; the
    # ``dict[str, type]`` registry erases that, so assert it back explicitly.
    return cast(ProseDialect, _DIALECTS.get(suffix.lower(), MarkdownDialect)())


_DIALECTS: dict[str, type] = {
    ".md": MarkdownDialect,
    ".markdown": MarkdownDialect,
    ".mdown": MarkdownDialect,
    ".rst": RstDialect,
}
