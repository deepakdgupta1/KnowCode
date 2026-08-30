"""Chunker for breaking a parsed file down into searchable units.

Code is chunked by entity. Prose is chunked by heading hierarchy, which is a
different enough job that :class:`~knowcode.indexing.prose_chunker.ProseChunker`
owns it; this class routes by file type and joins the result back to the graph.
"""

from __future__ import annotations

import bisect
from pathlib import Path
from typing import Optional

from knowcode.data_models import (
    ChunkingConfig,
    CodeChunk,
    ParseResult,
    Entity,
    EntityKind,
    RelationshipKind,
)
from knowcode.indexing.prose_chunker import (
    ProseChunk,
    ProseChunker,
    ProseChunkingConfig,
)
from knowcode.utils.tokenizer import tokenize_code
from knowcode.utils.logger import get_logger
import hashlib

logger = get_logger(__name__)

#: File types chunked by heading hierarchy rather than by entity. Running the
#: code path over these truncated them at the first line looking like a Python
#: import or definition, which inside a fenced block is ordinary prose.
PROSE_SUFFIXES = frozenset({".md", ".markdown", ".rst"})


class Chunker:
    """Chunks a parsed file into smaller, searchable units."""

    def __init__(
        self,
        config: Optional[ChunkingConfig] = None,
        prose_config: Optional[ProseChunkingConfig] = None,
    ) -> None:
        self.config = config or ChunkingConfig()
        self.prose_config = prose_config or ProseChunkingConfig()
        self._prose_chunker = ProseChunker(self.prose_config)
        self.chunks: list[CodeChunk] = []

    def process_parse_result(self, result: ParseResult) -> list[CodeChunk]:
        """Convert a ParseResult into a list of CodeChunk objects.

        Args:
            result: Parsed entities, relationships, and errors for a single file.

        Returns:
            List of generated CodeChunk objects in priority order.
        """
        self.chunks = []  # Single initialization at start of process

        file_path = result.file_path
        if Path(file_path).suffix.lower() in PROSE_SUFFIXES:
            self.chunks = self._chunk_prose(result)
            return self.chunks

        source_code = ""

        # Try to find module source code if available
        module_entity = next(
            (e for e in result.entities if e.kind == EntityKind.MODULE), None
        )
        if module_entity and module_entity.source_code:
            source_code = module_entity.source_code
        elif Path(file_path).exists():
            source_code = Path(file_path).read_text(encoding="utf-8")

        last_modified = None
        if Path(file_path).exists():
            last_modified = str(Path(file_path).stat().st_mtime)

        # 1. Module and Import Chunks
        file_entity = module_entity or next(
            (e for e in result.entities if e.kind == EntityKind.DOCUMENT), None
        )
        if source_code and file_entity is not None:
            self._emit_module_chunks(file_entity.id, source_code)
        elif source_code:
            # Nothing in the graph stands for this file, so these chunks would
            # have no entity to hang on. Emitting them anyway is what put 14%
            # of chunk text off the graph (BL-6).
            logger.debug(
                "No module or document entity for %s; skipping its module chunks",
                file_path,
            )

        # 2. Entity Chunks (Classes, Functions, Methods)
        boundaries = self._member_boundaries(result.entities)
        for entity in result.entities:
            if entity.kind == EntityKind.MODULE:
                continue
            self._chunk_entity(entity, last_modified, boundaries.get(entity.id))

        return self.chunks

    # ------------------------------------------------------------------
    # Prose
    # ------------------------------------------------------------------

    def _chunk_prose(self, result: ParseResult) -> list[CodeChunk]:
        """Chunk a document by heading hierarchy, hanging each chunk on its section.

        The prose chunker keeps a section identity of its own that nothing else
        in the index knows about. What gets stored is the graph entity whose
        span the chunk's lines fall inside, so ``chunk.entity_id`` remains a
        usable graph handle rather than a dangling one (BL-6).
        """
        path = Path(result.file_path)
        if not path.is_file():
            return []

        anchors = self._section_anchors(result)
        if not anchors:
            # No document entity to hang text on. Emitting orphans here would
            # put content in the index that no graph path can reach.
            return []

        anchor_lines = [line for line, _ in anchors]
        kinds = {entity.id: entity.kind.value for entity in result.entities}
        parents = {
            relationship.target_id: relationship.source_id
            for relationship in result.relationships
            if relationship.kind == RelationshipKind.CONTAINS
        }
        last_modified = str(path.stat().st_mtime)

        emitted: dict[str, int] = {}
        chunks: list[CodeChunk] = []
        for prose in self._prose_chunker.chunk_file(path, doc_id=result.file_path):
            position = bisect.bisect_right(anchor_lines, prose.start_line)
            entity_id = anchors[position - 1][1]
            index = emitted.get(entity_id, 0)
            emitted[entity_id] = index + 1
            chunks.append(
                self._prose_chunk(
                    prose,
                    entity_id=entity_id,
                    index=index,
                    kind=kinds.get(entity_id, EntityKind.SECTION.value),
                    parent_id=parents.get(entity_id),
                    last_modified=last_modified,
                )
            )
        return chunks

    @staticmethod
    def _section_anchors(result: ParseResult) -> list[tuple[int, str]]:
        """Each entity's first line and id, ascending, for a line-to-entity join.

        The document anchors line 0 so that a preamble before the first heading
        belongs to the document rather than to nothing.
        """
        document = next(
            (e for e in result.entities if e.kind == EntityKind.DOCUMENT), None
        )
        if document is None:
            return []
        sections = sorted(
            (entity.location.line_start, entity.id)
            for entity in result.entities
            if entity.kind == EntityKind.SECTION
        )
        return [(0, document.id), *sections]

    def _prose_chunk(
        self,
        prose: ProseChunk,
        *,
        entity_id: str,
        index: int,
        kind: str,
        parent_id: Optional[str],
        last_modified: str,
    ) -> CodeChunk:
        """Convert one ProseChunk into the CodeChunk the stores speak (B4)."""
        metadata = {
            "type": "prose",
            "kind": kind,
            "doc_type": prose.doc_type,
            "level": str(prose.level),
            "line_range": f"{prose.start_line}-{prose.end_line}",
            "token_count": str(prose.token_count),
            "context_header": prose.context_header,
            # The ProseChunker already computed this over the same bytes.
            # Hashing them a second time is a second place for the algorithm
            # to drift, and this column is the embedding-reuse key (BL-11).
            "content_hash": prose.content_hash,
            "last_modified": last_modified,
        }
        if parent_id:
            metadata["parent_id"] = parent_id
        if prose.is_oversize:
            metadata["oversize"] = "true"

        return CodeChunk(
            id=f"{entity_id}::{index}",
            entity_id=entity_id,
            content=prose.content,
            tokens=tokenize_code(prose.content),
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Code
    # ------------------------------------------------------------------

    def _emit_module_chunks(self, file_entity_id: str, source: str) -> None:
        """Extract module-level header and imports into dedicated chunks.

        Both hang on the entity that stands for the file itself. They used to
        hang on ``<file>::module``, an id no parser emits, so the chunk holding
        the largest single block of a file's text sat off the graph and every
        path treating ``chunk.entity_id`` as a handle silently did nothing with
        it (BL-6).

        Args:
            file_entity_id: ID of the file's MODULE or DOCUMENT entity.
            source: Full source code for the module.
        """
        for kind, content in (
            ("module_header", self._extract_module_header(source)),
            ("imports", self._extract_imports(source)),
        ):
            if not content:
                continue
            self.chunks.append(
                CodeChunk(
                    id=f"{file_entity_id}::{kind}::0",
                    entity_id=file_entity_id,
                    content=content,
                    tokens=tokenize_code(content),
                    metadata={
                        "type": kind,
                        "content_hash": hashlib.sha256(
                            content.encode("utf-8")
                        ).hexdigest(),
                    },
                )
            )

    def _extract_module_header(self, source: str) -> str:
        """Extract the leading module header and docstring block."""
        lines = source.splitlines()
        header_lines = []
        in_docstring = False
        quote_type = None

        for line in lines:
            stripped = line.strip()
            if not stripped and not in_docstring:
                continue

            # Simple docstring detection
            if '"""' in stripped or "'''" in stripped:
                if not in_docstring:
                    in_docstring = True
                    quote_type = '"""' if '"""' in stripped else "'''"
                    header_lines.append(line)
                    if stripped.count(quote_type) == 2:
                        in_docstring = False
                        break
                else:
                    header_lines.append(line)
                    in_docstring = False
                    break
            elif in_docstring:
                header_lines.append(line)
            elif stripped.startswith(("import ", "from ", "class ", "def ")):
                # Stop at first code definition
                break
            else:
                header_lines.append(line)

        return "\n".join(header_lines).strip()

    def _extract_imports(self, source: str) -> str:
        """Extract all import statements from the source."""
        lines = []
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                lines.append(line)
        return "\n".join(lines).strip()

    @staticmethod
    def _member_boundaries(entities: list[Entity]) -> dict[str, int]:
        """First line each class's own text runs out at, keyed by class id.

        A class's ``source_code`` contains its members' source, and each member
        is already an entity with its own chunk, so storing the whole thing
        stored every member body twice (B1). The boundary comes from the entity
        model rather than from a regex over the stored text: a member is an
        entity whose first line falls inside the class span.
        """
        boundaries: dict[str, int] = {}
        for parent in entities:
            if parent.kind != EntityKind.CLASS or not parent.source_code:
                continue
            starts = [
                entity.location.line_start
                for entity in entities
                if entity is not parent
                and entity.location.file_path == parent.location.file_path
                and parent.location.line_start
                < entity.location.line_start
                <= parent.location.line_end
            ]
            if starts:
                boundaries[parent.id] = min(starts)
        return boundaries

    @staticmethod
    def _own_source(entity: Entity, member_line: Optional[int]) -> str:
        """The entity's source down to the first member it contains."""
        source = entity.source_code or ""
        if member_line is None:
            return source
        kept = source.splitlines()[: member_line - entity.location.line_start]
        return "\n".join(kept).rstrip()

    def _chunk_entity(
        self,
        entity: Entity,
        last_modified: Optional[str] = None,
        member_line: Optional[int] = None,
    ) -> None:
        """Create chunks for an entity and append them to the in-memory list.

        Args:
            entity: Entity to chunk (class, function, method, etc.).
            last_modified: Optional timestamp used for ranking signals.
            member_line: First line belonging to a member this entity contains.
                The entity's own text stops there; the member has its own chunk.
        """
        content = ""

        if self.config.include_signatures and entity.signature:
            content += entity.signature + "\n"

        if self.config.include_docstrings and entity.docstring:
            content += f'"""{entity.docstring}"""\n'

        content += self._own_source(entity, member_line)

        if not content.strip():
            # Nothing but a label. A YAML key or a bare heading used to fall
            # back to its own name here and pay a full-width vector for a
            # handful of bytes (B3). It is a graph node, not a retrieval unit.
            return

        # Sliding window chunking
        has_docstring = "true" if entity.docstring else "false"

        if len(content) <= self.config.max_chunk_size:
            metadata = {
                "kind": entity.kind.value,
                "has_docstring": has_docstring,
                "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            }
            if last_modified:
                metadata["last_modified"] = last_modified

            chunk = CodeChunk(
                id=f"{entity.id}::0",
                entity_id=entity.id,
                content=content,
                tokens=tokenize_code(content),
                metadata=metadata,
            )
            self.chunks.append(chunk)
        else:
            # multiple chunks
            start = 0
            chunk_index = 0
            while start < len(content):
                end = min(start + self.config.max_chunk_size, len(content))
                chunk_content = content[start:end]

                metadata = {
                    "kind": entity.kind.value,
                    "chunk_index": str(chunk_index),
                    "has_docstring": has_docstring,
                    "content_hash": hashlib.sha256(
                        chunk_content.encode("utf-8")
                    ).hexdigest(),
                }
                if last_modified:
                    metadata["last_modified"] = last_modified

                chunk = CodeChunk(
                    id=f"{entity.id}::{chunk_index}",
                    entity_id=entity.id,
                    content=chunk_content,
                    tokens=tokenize_code(chunk_content),
                    metadata=metadata,
                )
                self.chunks.append(chunk)

                if end == len(content):
                    break

                start += self.config.max_chunk_size - self.config.overlap
                chunk_index += 1
