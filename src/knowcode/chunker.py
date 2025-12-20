"""Code chunker for breaking down entities into searchable units."""

from __future__ import annotations
from pathlib import Path
from typing import Optional

from knowcode.models import ChunkingConfig, CodeChunk, ParseResult, Entity, EntityKind
from knowcode.tokenizer import tokenize_code
from knowcode.logger import get_logger

logger = get_logger(__name__)


class Chunker:
    """Chunks code entities into smaller, searchable units."""

    def __init__(self, config: Optional[ChunkingConfig] = None) -> None:
        self.config = config or ChunkingConfig()
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
        source_code = ""
        
        # Try to find module source code if available
        module_entity = next((e for e in result.entities if e.kind == EntityKind.MODULE), None)
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
                metadata={"type": "module_header"}
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
                metadata={"type": "imports"}
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

    def _chunk_entity(self, entity: Entity, last_modified: Optional[str] = None) -> None:
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
            metadata = {"kind": entity.kind.value, "has_docstring": has_docstring}
            if last_modified:
                metadata["last_modified"] = last_modified

            chunk = CodeChunk(
                id=f"{entity.id}::0",
                entity_id=entity.id,
                content=content,
                tokens=tokenize_code(content),
                metadata=metadata
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
                }
                if last_modified:
                    metadata["last_modified"] = last_modified
                
                chunk = CodeChunk(
                    id=f"{entity.id}::{chunk_index}",
                    entity_id=entity.id,
                    content=chunk_content,
                    tokens=tokenize_code(chunk_content),
                    metadata=metadata
                )
                self.chunks.append(chunk)
                
                if end == len(content):
                    break
                    
                start += (self.config.max_chunk_size - self.config.overlap)
                chunk_index += 1
