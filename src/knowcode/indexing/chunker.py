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
        if source_code:
            self._emit_module_chunks(file_path, source_code)

        # 2. Entity Chunks (Classes, Functions, Methods)
        for entity in result.entities:
            if entity.kind == EntityKind.MODULE:
                continue
            self._chunk_entity(entity, last_modified)

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
            # md5 like every other chunk: this column is the embedding-reuse
            # key, so two chunks holding the same text must hash the same.
            "content_hash": hashlib.md5(prose.content.encode("utf-8")).hexdigest(),
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

    def _emit_module_chunks(self, file_path: str, source: str) -> None:
        """Extract module-level header and imports into dedicated chunks.

        Args:
            file_path: File path used to namespace chunk IDs.
            source: Full source code for the module.
        """
        # Module Header
        header = self._extract_module_header(source)
        if header:
            module_chunk = CodeChunk(
                id=f"{file_path}::module::0",
                entity_id=f"{file_path}::module",
                content=header,
                tokens=tokenize_code(header),
                metadata={
                    "type": "module_header",
                    "content_hash": hashlib.md5(header.encode("utf-8")).hexdigest(),
                },
            )
            self.chunks.append(module_chunk)

        # Imports
        imports = self._extract_imports(source)
        if imports:
            import_chunk = CodeChunk(
                id=f"{file_path}::imports::0",
                entity_id=f"{file_path}::module",
                content=imports,
                tokens=tokenize_code(imports),
                metadata={
                    "type": "imports",
                    "content_hash": hashlib.md5(imports.encode("utf-8")).hexdigest(),
                },
            )
            self.chunks.append(import_chunk)

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

    def _chunk_entity(
        self, entity: Entity, last_modified: Optional[str] = None
    ) -> None:
        """Create chunks for an entity and append them to the in-memory list.

        Args:
            entity: Entity to chunk (class, function, method, etc.).
            last_modified: Optional timestamp used for ranking signals.
        """
        content = ""

        if self.config.include_signatures and entity.signature:
            content += entity.signature + "\n"

        if self.config.include_docstrings and entity.docstring:
            content += f'"""{entity.docstring}"""\n'

        if entity.source_code:
            content += entity.source_code
        else:
            content += entity.name

        # Sliding window chunking
        has_docstring = "true" if entity.docstring else "false"

        if len(content) <= self.config.max_chunk_size:
            metadata = {
                "kind": entity.kind.value,
                "has_docstring": has_docstring,
                "content_hash": hashlib.md5(content.encode("utf-8")).hexdigest(),
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
                    "content_hash": hashlib.md5(
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
