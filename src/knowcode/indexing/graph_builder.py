"""Graph builder that orchestrates parsing and constructs the semantic graph."""

from __future__ import annotations
from pathlib import Path
from typing import Optional

from knowcode.models import Entity, ParseResult, Relationship
from knowcode.parsers import MarkdownParser, PythonParser, YamlParser
from knowcode.parsers.javascript_parser import JavaScriptParser
from knowcode.parsers.java_parser import JavaParser
from knowcode.indexing.scanner import FileInfo, Scanner
from knowcode.analysis.signals import CoverageProcessor
from knowcode.analysis.temporal import TemporalAnalyzer


class GraphBuilder:
    """Builds semantic graph from source files."""

    def __init__(self) -> None:
        """Initialize the graph builder with parsers."""
        self.python_parser = PythonParser()
        self.markdown_parser = MarkdownParser()
        self.yaml_parser = YamlParser()
        self.js_parser = JavaScriptParser()
        self.java_parser = JavaParser()

        self.entities: dict[str, Entity] = {}
        self.relationships: list[Relationship] = []
        self.errors: list[str] = []

    def build_from_directory(
        self,
        root_dir: str | Path,
        additional_ignores: Optional[list[str]] = None,
        analyze_temporal: bool = False,
        coverage_path: Optional[Path] = None,
    ) -> "GraphBuilder":
        """Build graph by scanning and parsing a directory.

        Args:
            root_dir: Root directory to scan.
            additional_ignores: Additional patterns to ignore.

        Returns:
            Self for method chaining.
        """
        scanner = Scanner(
            root_dir=root_dir,
            respect_gitignore=True,
            additional_ignores=additional_ignores,
        )

        files = scanner.scan_all()
        
        # Static Analysis
        self.build_from_files(files)

        # Temporal Analysis
        if analyze_temporal:
            temporal_analyzer = TemporalAnalyzer(root_dir)
            result = temporal_analyzer.analyze_history()
            self._merge_result(result)
            
        # Coverage Analysis
        if coverage_path:
            coverage_processor = CoverageProcessor(root_dir)
            result = coverage_processor.process_cobertura(coverage_path)
            self._merge_result(result)
            
        return self

    def build_from_files(self, files: list[FileInfo]) -> "GraphBuilder":
        """Build graph from a list of files.

        Args:
            files: List of FileInfo objects to parse.

        Returns:
            Self for method chaining.
        """
        for file_info in files:
            parse_result = self._parse_file(file_info)
            self._merge_result(parse_result)

        # Resolve references after all files are parsed
        self._resolve_references()

        return self

    def _parse_file(self, file_info: FileInfo) -> ParseResult:
        """Parse a single file based on its extension."""
        if file_info.extension == ".py":
            return self.python_parser.parse_file(file_info.path)
        elif file_info.extension == ".md":
            return self.markdown_parser.parse_file(file_info.path)
        elif file_info.extension in {".yaml", ".yml"}:
            return self.yaml_parser.parse_file(file_info.path)
        elif file_info.extension in {".js", ".ts"}:
            return self.js_parser.parse_file(file_info.path)
        elif file_info.extension == ".java":
            return self.java_parser.parse_file(file_info.path)
        else:
            return ParseResult(
                file_path=str(file_info.path),
                entities=[],
                relationships=[],
                errors=[f"Unsupported file type: {file_info.extension}"],
            )

    def _merge_result(self, result: ParseResult) -> None:
        """Merge parse result into the graph."""
        for entity in result.entities:
            self.entities[entity.id] = entity

        self.relationships.extend(result.relationships)
        self.errors.extend(result.errors)

    def _resolve_references(self) -> None:
        """Resolve reference-based relationships to actual entity IDs.
        
        Some parsers (like Tree-sitter) may produce relationships pointing to 
        'ref::SomeName' because they don't know the full qualified name at parse time.
        This pass iterates through all relationships and attempts to link these 
        placeholders to concrete Entity IDs in the graph.
        """
        resolved_relationships: list[Relationship] = []

        for rel in self.relationships:
            # Check if target is a reference that needs resolution
            if rel.target_id.startswith("ref::"):
                ref_name = rel.target_id[5:]  # Remove "ref::" prefix
                resolved_id = self._find_entity_by_name(ref_name)
                if resolved_id:
                    resolved_relationships.append(
                        Relationship(
                            source_id=rel.source_id,
                            target_id=resolved_id,
                            kind=rel.kind,
                            metadata=rel.metadata,
                        )
                    )
                else:
                    # Keep as unresolved reference
                    resolved_relationships.append(rel)
            else:
                resolved_relationships.append(rel)

        self.relationships = resolved_relationships

    def _find_entity_by_name(self, name: str) -> Optional[str]:
        """Find entity ID by name or qualified name."""
        # Exact match on name or qualified_name
        for entity_id, entity in self.entities.items():
            if entity.name == name or entity.qualified_name == name:
                return entity_id

        # Try matching the last part of qualified names
        for entity_id, entity in self.entities.items():
            if entity.qualified_name.endswith(f".{name}"):
                return entity_id

        return None

    def get_entity(self, entity_id: str) -> Optional[Entity]:
        """Get entity by ID."""
        return self.entities.get(entity_id)

    def get_entities_by_kind(self, kind: str) -> list[Entity]:
        """Get all entities of a specific kind."""
        return [e for e in self.entities.values() if e.kind.value == kind]

    def get_outgoing_relationships(self, entity_id: str) -> list[Relationship]:
        """Get all relationships where entity is the source."""
        return [r for r in self.relationships if r.source_id == entity_id]

    def get_incoming_relationships(self, entity_id: str) -> list[Relationship]:
        """Get all relationships where entity is the target."""
        return [r for r in self.relationships if r.target_id == entity_id]

    def search_entities(self, pattern: str) -> list[Entity]:
        """Search entities by name pattern (case-insensitive substring)."""
        pattern_lower = pattern.lower()
        return [
            e for e in self.entities.values()
            if pattern_lower in e.name.lower()
            or pattern_lower in e.qualified_name.lower()
        ]

    def stats(self) -> dict[str, int]:
        """Return statistics about the graph."""
        kind_counts: dict[str, int] = {}
        for entity in self.entities.values():
            kind = entity.kind.value
            kind_counts[kind] = kind_counts.get(kind, 0) + 1

        rel_counts: dict[str, int] = {}
        for rel in self.relationships:
            kind = rel.kind.value
            rel_counts[kind] = rel_counts.get(kind, 0) + 1

        return {
            "total_entities": len(self.entities),
            "total_relationships": len(self.relationships),
            "total_errors": len(self.errors),
            **{f"entities_{k}": v for k, v in kind_counts.items()},
            **{f"relationships_{k}": v for k, v in rel_counts.items()},
        }
