"""reStructuredText document parser.

Mirrors :class:`~knowcode.parsers.markdown_parser.MarkdownParser` so ``.rst``
files (e.g. the Linux kernel ``Documentation/`` tree) get the same DOCUMENT /
SECTION graph entities and CONTAINS hierarchy as markdown. Heading detection is
delegated to the shared reStructuredText scanner used by the prose chunker, so
adornment-style level assignment stays consistent across the graph and the
retrieval index.
"""

from __future__ import annotations

from pathlib import Path

from knowcode.data_models import (
    Entity,
    EntityKind,
    Location,
    ParseResult,
    Relationship,
    RelationshipKind,
)
from knowcode.parsers.prose_identity import HeadingScope
from knowcode.indexing.prose_chunker import (
    _RST_DIRECTIVE_RE,
    _rst_is_adornment,
    _scan_rst_headings,
)


class RstParser:
    """Parses reStructuredText files into entities based on section structure."""

    def parse_file(self, file_path: str | Path) -> ParseResult:
        """Parse an ``.rst`` file into a document + section entities.

        Args:
            file_path: Path to the reStructuredText file.

        Returns:
            ParseResult with one DOCUMENT entity, one SECTION entity per
            heading, and CONTAINS relationships following section nesting.
        """
        file_path = Path(file_path)

        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception as e:
            return ParseResult(
                file_path=str(file_path),
                entities=[],
                relationships=[],
                errors=[f"Failed to read file: {e}"],
            )

        lines = content.splitlines()
        headings = _scan_rst_headings(lines, 1)

        doc_name = file_path.stem
        scope = HeadingScope(str(file_path), doc_name)
        doc_entity = Entity(
            id=scope.document_id,
            kind=EntityKind.DOCUMENT,
            name=doc_name,
            qualified_name=doc_name,
            location=Location(
                file_path=str(file_path),
                line_start=1,
                line_end=len(lines),
            ),
            docstring=self._extract_description(lines, headings),
        )

        entities: list[Entity] = [doc_entity]
        relationships: list[Relationship] = []

        for idx, (heading_start, level, title, _body_start) in enumerate(headings):
            section = scope.enter(level, title)
            next_start = (
                headings[idx + 1][0] if idx + 1 < len(headings) else len(lines) + 1
            )
            entities.append(
                Entity(
                    id=section.entity_id,
                    kind=EntityKind.SECTION,
                    name=title,
                    qualified_name=section.qualified_name,
                    location=Location(
                        file_path=str(file_path),
                        line_start=heading_start,
                        line_end=next_start - 1,
                    ),
                    metadata={"level": str(level)},
                )
            )

            relationships.append(
                Relationship(
                    source_id=section.parent_id,
                    target_id=section.entity_id,
                    kind=RelationshipKind.CONTAINS,
                )
            )

        return ParseResult(
            file_path=str(file_path),
            entities=entities,
            relationships=relationships,
            errors=[],
        )

    def _extract_description(
        self, lines: list[str], headings: list[tuple[int, int, str, int]]
    ) -> str:
        """First prose paragraph after the document title (skips adornments)."""
        start = headings[0][3] if headings else 1  # body_start of first heading
        paragraph: list[str] = []
        for line_no in range(start, len(lines) + 1):
            raw = lines[line_no - 1]
            stripped = raw.strip()
            if not stripped:
                if paragraph:
                    break
                continue
            if _rst_is_adornment(raw) or _RST_DIRECTIVE_RE.match(raw):
                if paragraph:
                    break
                continue
            paragraph.append(stripped)
        return " ".join(paragraph)[:500]
