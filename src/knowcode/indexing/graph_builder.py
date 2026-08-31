"""Graph builder that orchestrates parsing and constructs the semantic graph."""

from __future__ import annotations
from pathlib import Path
from typing import Optional

from knowcode.data_models import (
    Entity,
    EntityKind,
    ParseResult,
    Relationship,
    RelationshipKind,
)
from knowcode.parsers import MarkdownParser, PythonParser, RstParser, YamlParser
from knowcode.parsers.javascript_parser import JavaScriptParser
from knowcode.parsers.typescript_parser import TypeScriptParser
from knowcode.parsers.java_parser import JavaParser
from knowcode.parsers.rust_parser import RustParser
from knowcode.parsers.vue_parser import VueParser
from knowcode.indexing.scanner import FileInfo, Scanner
from knowcode.analysis.signals import CoverageProcessor
from knowcode.analysis.temporal import TemporalAnalyzer
from knowcode.analysis.behavior import annotate_entity_behavior
from knowcode.utils.entity_identity import (
    build_external_reference_id,
    ensure_entity_content_hash,
)

# Edges whose target is a callee or base name, where resolution matters.
_REFERENCE_KINDS = frozenset({RelationshipKind.CALLS, RelationshipKind.INHERITS})

# Entity kinds a name-based link may point at; modules, documents, and
# sections are containers, not callees.
_LINKABLE_KINDS = frozenset({EntityKind.FUNCTION, EntityKind.METHOD, EntityKind.CLASS})

# Maps a repository file suffix onto the language component of the canonical
# unresolved ids, so name links never cross languages.
_LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".rs": "rust",
    ".vue": "vue",
}


class GraphBuilder:
    """Builds semantic graph from source files."""

    def __init__(self) -> None:
        """Initialize the graph builder with parsers."""
        self.python_parser = PythonParser()
        self.markdown_parser = MarkdownParser()
        self.rst_parser = RstParser()
        self.yaml_parser = YamlParser()
        self.js_parser = JavaScriptParser()
        self.ts_parser = TypeScriptParser()
        self.java_parser = JavaParser()
        self.rust_parser = RustParser()
        self.vue_parser = VueParser()

        self.entities: dict[str, Entity] = {}
        self.relationships: list[Relationship] = []
        self.errors: list[str] = []

        # Retained so a generation build chunks exactly the parse that produced
        # the graph (Step 14). Re-scanning would apply a different ignore set
        # and re-parsing would risk a chunk set that disagrees with the
        # entities committed to ``knowledge.db`` in the same generation.
        self.scanned_files: list[FileInfo] = []
        self.parse_results: list[ParseResult] = []

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
            analyze_temporal: Whether to analyze temporal history.
            coverage_path: Optional path to coverage report.

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
        self.scanned_files = list(files)
        self.parse_results = []
        for file_info in files:
            parse_result = self._parse_file(file_info)
            self.parse_results.append(parse_result)
            self._merge_result(parse_result)

        # Resolve references after all files are parsed: name-based linking
        # first, then binding-driven classification, then scoped name links.
        self._resolve_references()
        self._resolve_import_bound_references()
        self._resolve_scoped_unresolved_references()

        return self

    def _parse_file(self, file_info: FileInfo) -> ParseResult:
        """Parse a single file based on its extension."""
        if file_info.extension == ".py":
            return self.python_parser.parse_file(file_info.path)
        elif file_info.extension == ".md":
            return self.markdown_parser.parse_file(file_info.path)
        elif file_info.extension == ".rst":
            return self.rst_parser.parse_file(file_info.path)
        elif file_info.extension in {".yaml", ".yml"}:
            return self.yaml_parser.parse_file(file_info.path)
        elif file_info.extension in {".js", ".jsx"}:
            return self.js_parser.parse_file(file_info.path)
        elif file_info.extension in {".ts", ".tsx"}:
            return self.ts_parser.parse_file(file_info.path)
        elif file_info.extension == ".java":
            return self.java_parser.parse_file(file_info.path)
        elif file_info.extension == ".rs":
            return self.rust_parser.parse_file(file_info.path)
        elif file_info.extension == ".vue":
            return self.vue_parser.parse_file(file_info.path)
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
            annotate_entity_behavior(entity)
            ensure_entity_content_hash(entity)
            self.entities[entity.id] = entity

        self.relationships.extend(result.relationships)
        self.errors.extend(result.errors)

    def _resolve_references(self) -> None:
        """Resolve reference-based relationships to actual entity IDs.

        Some parsers (like Tree-sitter) may produce relationships pointing to
        'ref::SomeName' because they don't know the full qualified name at parse time.
        This pass iterates through all relationships and attempts to link these
        placeholders to concrete Entity IDs in the graph. An unlinked base whose
        edge carries an import origin is classified through that origin.
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
                    rewritten = self._rewrite_bound_reference(rel)
                    resolved_relationships.append(rewritten if rewritten else rel)
            else:
                resolved_relationships.append(rel)

        self.relationships = resolved_relationships

    def _resolve_import_bound_references(self) -> None:
        """Classify unresolved edges whose metadata carries an import binding.

        The Python parser records where a receiver (``json.loads``) or an
        imported name (``from os.path import join``) came from. Only the
        builder can decide whether that module lives in this repository: an
        in-repo module links to the unique entity carrying the symbol — never
        a guess — and any other module becomes ``external::<module>::<symbol>``,
        an answer rather than a hole.
        """
        paths = {
            Path(entity.location.file_path).as_posix()
            for entity in self.entities.values()
        }
        module_files: dict[str, set[str]] = {}
        by_last_name: dict[str, list[tuple[str, str]]] = {}
        for entity_id, entity in self.entities.items():
            last = entity.qualified_name.rsplit(".", 1)[-1]
            by_last_name.setdefault(last, []).append(
                (Path(entity.location.file_path).as_posix(), entity_id)
            )

        def candidates_for(module: str) -> set[str]:
            cached = module_files.get(module)
            if cached is None:
                rel = module.replace(".", "/")
                suffixes = (f"{rel}.py", f"{rel}/__init__.py")
                cached = {
                    path
                    for path in paths
                    if any(
                        path == suffix or path.endswith(f"/{suffix}")
                        for suffix in suffixes
                    )
                }
                module_files[module] = cached
            return cached

        resolved: list[Relationship] = []
        for rel in self.relationships:
            if (
                rel.kind in _REFERENCE_KINDS
                and rel.target_id.startswith("unresolved::")
                and (
                    "receiver_module" in rel.metadata or "imported_from" in rel.metadata
                )
            ):
                rewritten = self._rewrite_bound_reference(
                    rel, candidates_for, by_last_name
                )
                resolved.append(rewritten if rewritten else rel)
            else:
                resolved.append(rel)
        self.relationships = resolved

    def _rewrite_bound_reference(
        self,
        rel: Relationship,
        candidates_for=None,
        by_last_name: dict[str, list[tuple[str, str]]] | None = None,
    ) -> Relationship | None:
        """Rewrite one import-bound edge, or return None to keep it verbatim.

        The module comes from the edge's binding metadata. A module outside
        the repository yields an ``external::`` target. A module inside the
        repository yields a link only when exactly one entity in the module's
        files carries the symbol; ambiguity keeps the hole.
        """
        module = rel.metadata.get("receiver_module") or rel.metadata.get(
            "imported_from"
        )
        if module is None:
            return None
        symbol = rel.metadata.get("imported_symbol")
        if symbol is None:
            raw = rel.target_id.split("::")[-1]
            # A receiver edge carries the whole callee; the receiver itself is
            # not part of the symbol (``np.array`` on module ``numpy`` is
            # ``array``).
            symbol = raw.split(".", 1)[1] if "." in raw else raw

        if candidates_for is None:
            paths = {
                Path(entity.location.file_path).as_posix()
                for entity in self.entities.values()
            }
            rel_suffix = module.replace(".", "/")
            suffixes = (f"{rel_suffix}.py", f"{rel_suffix}/__init__.py")
            candidates = {
                path
                for path in paths
                if any(
                    path == suffix or path.endswith(f"/{suffix}") for suffix in suffixes
                )
            }
        else:
            candidates = candidates_for(module)

        if not candidates:
            return Relationship(
                source_id=rel.source_id,
                target_id=build_external_reference_id(module, symbol),
                kind=rel.kind,
                metadata=rel.metadata,
            )

        if by_last_name is None:
            by_last_name = {}
            for entity_id, entity in self.entities.items():
                last = entity.qualified_name.rsplit(".", 1)[-1]
                by_last_name.setdefault(last, []).append(
                    (Path(entity.location.file_path).as_posix(), entity_id)
                )
        matches = [
            entity_id
            for path, entity_id in by_last_name.get(symbol, [])
            if path in candidates
        ]
        if len(matches) == 1:
            return Relationship(
                source_id=rel.source_id,
                target_id=matches[0],
                kind=rel.kind,
                metadata=rel.metadata,
            )
        return None

    def _resolve_scoped_unresolved_references(self) -> None:
        """Link bare unresolved names to a unique same-language entity.

        Only edges without binding knowledge and without a receiver arrive
        here: a bare callee the file did not define — typically a relative
        import or a same-package call. A link happens only when exactly one
        linkable entity of the same language carries that name in the whole
        graph; ambiguity keeps the hole rather than guessing.
        """
        name_index: dict[tuple[str, str], list[str]] = {}
        for entity_id, entity in self.entities.items():
            if entity.kind not in _LINKABLE_KINDS:
                continue
            language = _LANGUAGE_BY_SUFFIX.get(
                Path(entity.location.file_path).suffix, ""
            )
            if language:
                name_index.setdefault((language, entity.name), []).append(entity_id)

        resolved: list[Relationship] = []
        for rel in self.relationships:
            target = rel.target_id
            if (
                rel.kind not in _REFERENCE_KINDS
                or not target.startswith("unresolved::")
                or rel.metadata.get("receiver_unknown")
            ):
                resolved.append(rel)
                continue
            parts = target.split("::")
            if len(parts) != 5 or "." in parts[4]:
                resolved.append(rel)
                continue
            matches = name_index.get((parts[1], parts[4]), [])
            if len(matches) == 1:
                resolved.append(
                    Relationship(
                        source_id=rel.source_id,
                        target_id=matches[0],
                        kind=rel.kind,
                        metadata=rel.metadata,
                    )
                )
            else:
                resolved.append(rel)
        self.relationships = resolved

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
            e
            for e in self.entities.values()
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
