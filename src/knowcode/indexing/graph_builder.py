"""Graph builder that orchestrates parsing and constructs the semantic graph."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

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

# The return annotation of a function entity's recorded signature, e.g.
# ``def make_store(root) -> SqliteKnowledgeStore``. Signature strings are
# produced by the parsers from source, so this reads what the file stated.
_RETURN_ANNOTATION_RE = re.compile(r"\)\s*->\s*(.+)$")
_SIMPLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Return annotations that name a builtin type outright: a factory promising
# ``-> str`` produced an object whose methods are known to live in builtins,
# whatever the factory's own module.
_BUILTIN_RETURN_NAMES = frozenset(
    {
        "bool",
        "bytearray",
        "bytes",
        "dict",
        "float",
        "frozenset",
        "int",
        "list",
        "set",
        "str",
        "tuple",
    }
)


class _ReceiverIndex:
    """Repository-scale indexes for classifying receiver-typed call edges.

    Built once per graph from the merged entities and relationships: the
    per-file module/class/function indexes every classification consults, and
    the class-to-base map the method walk follows. Nothing here guesses —
    every lookup either names exactly one entity or reports failure, and the
    caller keeps the hole on failure.
    """

    def __init__(
        self,
        entities: dict[str, Entity],
        relationships: list[Relationship],
    ) -> None:
        self.entities = entities
        self.files: list[str] = sorted(
            {Path(e.location.file_path).as_posix() for e in entities.values()}
        )
        self.entity_by_qname: dict[tuple[str, str], Entity] = {}
        self.classes_by_file_last: dict[tuple[str, str], list[Entity]] = {}
        self.linkable_by_file_last: dict[tuple[str, str], list[Entity]] = {}
        for entity in entities.values():
            file = Path(entity.location.file_path).as_posix()
            self.entity_by_qname[(file, entity.qualified_name)] = entity
            last = entity.qualified_name.rsplit(".", 1)[-1]
            if entity.kind is EntityKind.CLASS:
                self.classes_by_file_last.setdefault((file, last), []).append(entity)
            if entity.kind in _LINKABLE_KINDS:
                self.linkable_by_file_last.setdefault((file, last), []).append(entity)

        self.bases: dict[str, list[str]] = {}
        for rel in relationships:
            if rel.kind is not RelationshipKind.INHERITS:
                continue
            if not rel.source_id or rel.target_id.startswith(
                ("external::", "unresolved::", "ref::")
            ):
                continue
            self.bases.setdefault(rel.source_id, []).append(rel.target_id)

        self._module_files_cache: dict[str, set[str]] = {}

    def module_files(self, module: str) -> set[str]:
        """Files that implement one dotted module path, by the same suffix
        rule the import-bound classification uses."""
        cached = self._module_files_cache.get(module)
        if cached is None:
            rel = module.replace(".", "/")
            suffixes = (f"{rel}.py", f"{rel}/__init__.py")
            cached = {
                path
                for path in self.files
                if any(
                    path == suffix or path.endswith(f"/{suffix}") for suffix in suffixes
                )
            }
            self._module_files_cache[module] = cached
        return cached

    def _top_level_by_last(
        self,
        index: dict[tuple[str, str], list[Entity]],
        files: set[str],
        name: str,
    ) -> list[Entity]:
        """Entities named ``name`` in ``files`` that sit at module top level.

        A qualified name rooted at the file stem plus one component is what
        ``from M import n`` can actually bind; deeper names belong to nested
        declarations and are not importable from the module path alone.
        """
        matches: list[Entity] = []
        for file in files:
            for entity in index.get((file, name), []):
                if entity.qualified_name.count(".") == 1:
                    matches.append(entity)
        return matches

    def subtree_of(self, files: set[str]) -> set[str]:
        """Repository files under the package directories of ``files``.

        A package's ``__init__.py`` re-exports symbols defined elsewhere in
        the package; the subtree is where those definitions live.
        """
        package_dirs = sorted(
            {f.rsplit("/", 1)[0] for f in files if f.endswith("/__init__.py")}
        )
        if not package_dirs:
            return set()
        return {
            f
            for directory in package_dirs
            for f in self.files
            if f.startswith(f"{directory}/")
        }

    def unique_class(self, module: str, class_name: str) -> Optional[Entity]:
        """The one class ``module`` exports under ``class_name``.

        Searched in the module's own files first; when the module is an
        in-repo package, its whole subtree once — the re-export home of
        ``from pkg import Thing`` is ``pkg/__init__.py`` but the class lives
        wherever the package defines it. Zero or several matches report
        ``None``: ambiguity keeps the hole.
        """
        files = self.module_files(module)
        if not files:
            return None
        matches = self._top_level_by_last(self.classes_by_file_last, files, class_name)
        if len(matches) == 1:
            return matches[0]
        if matches:
            return None
        matches = self._top_level_by_last(
            self.classes_by_file_last, self.subtree_of(files), class_name
        )
        return matches[0] if len(matches) == 1 else None

    def unique_top_level(
        self,
        module: str,
        name: str,
    ) -> Optional[Entity]:
        """The one top-level linkable entity ``module`` exports under ``name``."""
        matches = self._top_level_by_last(
            self.linkable_by_file_last, self.module_files(module), name
        )
        return matches[0] if len(matches) == 1 else None

    def method_target(
        self,
        class_entity: Entity,
        method: str,
    ) -> Optional[str]:
        """The entity id of ``method`` on ``class_entity`` or its bases.

        The class's own declaration wins; otherwise resolved in-repo base
        classes are walked breadth-first and the first level carrying the
        method must carry it exactly once — two bases providing the same
        method is real ambiguity, not a choice to make.
        """
        file = Path(class_entity.location.file_path).as_posix()
        direct = self.entity_by_qname.get(
            (file, f"{class_entity.qualified_name}.{method}")
        )
        if direct is not None and direct.kind in _LINKABLE_KINDS:
            return direct.id

        visited = {class_entity.id}
        level = list(self.bases.get(class_entity.id, []))
        while level:
            providers: list[str] = []
            next_level: list[str] = []
            for base_id in level:
                if base_id in visited:
                    continue
                visited.add(base_id)
                base = self.entities.get(base_id)
                if base is None or base.kind is not EntityKind.CLASS:
                    continue
                base_file = Path(base.location.file_path).as_posix()
                provider = self.entity_by_qname.get(
                    (base_file, f"{base.qualified_name}.{method}")
                )
                if provider is not None and provider.kind in _LINKABLE_KINDS:
                    providers.append(provider.id)
                    continue
                next_level.extend(self.bases.get(base_id, []))
            if len(providers) == 1:
                return providers[0]
            if providers:
                return None
            level = next_level
        return None


def _return_annotation_name(signature: str) -> Optional[str]:
    """The bare class name a function signature promises to return.

    ``Optional[X]`` and ``X | None`` peel to ``X``; anything that does not
    name a simple identifier (generics, unions of two types, strings)
    reports ``None`` rather than a guess.
    """
    match = _RETURN_ANNOTATION_RE.search(signature)
    if match is None:
        return None
    annotation = match.group(1).strip()
    if annotation.startswith("Optional[") and annotation.endswith("]"):
        annotation = annotation[len("Optional[") : -1].strip()
    for part in annotation.split("|"):
        part = part.strip()
        if part and part != "None":
            annotation = part
            break
    else:
        return None
    name = annotation.rsplit(".", 1)[-1]
    return name if _SIMPLE_NAME_RE.match(name) else None


def _qname_carries(qualified_name: str, member: str) -> bool:
    """Whether a qualified name is ``<stem>.<member>`` exactly.

    Module-scope access through an import binding (``sub.load``,
    ``service.Klass.method``) reaches a top-level path in the module: the
    file stem followed by exactly the member's components. Deeper matches
    belong to nested declarations the binding cannot name.
    """
    qname_parts = qualified_name.split(".")
    member_parts = member.split(".")
    return len(qname_parts) == len(member_parts) + 1 and qname_parts[1:] == member_parts


def _unique_symbol_in_files(
    index: _ReceiverIndex,
    files: set[str],
    member: str,
) -> Optional[str]:
    """The unique linkable entity in ``files`` carrying ``member``."""
    matches = [
        entity
        for file in files
        for entity in index.linkable_by_file_last.get(
            (file, member.rsplit(".", 1)[-1]), []
        )
        if _qname_carries(entity.qualified_name, member)
    ]
    return matches[0].id if len(matches) == 1 else None


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
        # first, then binding-driven classification, then receiver-typed
        # classification, then scoped name links.
        self._resolve_references()
        self._resolve_import_bound_references()
        self._resolve_receiver_typed_references()
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
        candidates_for: Callable[[str], set[str]] | None = None,
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

    def _resolve_receiver_typed_references(self) -> None:
        """Classify receiver-typed call edges with repository knowledge.

        The Python parser states what a file knows about a receiver — its
        class (lexically or through an import origin), the member-imported
        module behind it, or the cross-module factory that produced it. Only
        the builder can decide what that means here: an in-repo class links
        the entity carrying the called method (walking resolved in-repo base
        classes when the class itself does not declare it); an external
        origin becomes an ``external::`` answer; a factory links through the
        callee entity's own recorded signature. Anything absent or ambiguous
        keeps the hole verbatim, metadata and all.
        """
        index = _ReceiverIndex(self.entities, self.relationships)

        resolved: list[Relationship] = []
        for rel in self.relationships:
            if rel.kind is not RelationshipKind.CALLS or not rel.target_id.startswith(
                "unresolved::"
            ):
                resolved.append(rel)
                continue
            metadata = rel.metadata
            if "receiver_member_module" in metadata:
                target = self._classify_member_module_receiver(index, metadata)
            elif "receiver_type_name" in metadata:
                target = self._classify_typed_receiver(index, rel, metadata)
            elif "receiver_from_call_name" in metadata:
                target = self._classify_factory_receiver(index, metadata)
            else:
                target = None
            if target is None:
                resolved.append(rel)
            else:
                resolved.append(
                    Relationship(
                        source_id=rel.source_id,
                        target_id=target,
                        kind=rel.kind,
                        metadata=rel.metadata,
                    )
                )
        self.relationships = resolved

    def _classify_member_module_receiver(
        self,
        index: _ReceiverIndex,
        metadata: dict[str, Any],
    ) -> Optional[str]:
        """Classify ``from M import n; n.member()``.

        ``M.n`` naming an in-repo module links the unique entity carrying
        ``member`` at that module's top level. Otherwise ``n`` may be a class
        ``M`` exports — a classmethod-style call — which links like any typed
        receiver. An origin outside the repository is an ``external::``
        answer naming the imported binding.
        """
        module_path = metadata["receiver_member_module"]
        member = metadata["receiver_method"]
        files = index.module_files(module_path)
        if files:
            target = _unique_symbol_in_files(index, files, member)
            if target is not None:
                return target
            return _unique_symbol_in_files(index, index.subtree_of(files), member)

        parent, _, imported = module_path.rpartition(".")
        if not parent:
            return None
        if index.module_files(parent):
            # ``M`` is in the repository; ``n`` may name a class it exports.
            class_entity = index.unique_class(parent, imported)
            if class_entity is not None and "." not in member:
                return index.method_target(class_entity, member)
            return None
        return build_external_reference_id(parent, f"{imported}.{member}")

    def _classify_typed_receiver(
        self,
        index: _ReceiverIndex,
        rel: Relationship,
        metadata: dict[str, Any],
    ) -> Optional[str]:
        """Classify a receiver whose type the file stated.

        An in-file class (``receiver_type_qname``) and an import origin
        (``receiver_type_module``) are the two spellings; both end in the
        same method link. A type from a module outside the repository —
        including ``builtins`` — is an ``external::`` answer.
        """
        name = metadata["receiver_type_name"]
        method = metadata["receiver_method"]
        qname = metadata.get("receiver_type_qname")
        module = metadata.get("receiver_type_module")

        if qname is not None:
            file = rel.source_id.rsplit("::", 1)[0]
            class_entity = index.entity_by_qname.get((file, qname))
            if class_entity is None or class_entity.kind is not EntityKind.CLASS:
                return None
            return index.method_target(class_entity, method)

        if module is None:  # pragma: no cover -- parser always sets one of both
            return None
        if index.module_files(module):
            class_entity = index.unique_class(module, name)
            if class_entity is None:
                # The module is in the repository but exports no unique such
                # class: where the method would live is unknown, not external.
                return None
            return index.method_target(class_entity, method)
        return build_external_reference_id(module, f"{name}.{method}")

    def _classify_factory_receiver(
        self,
        index: _ReceiverIndex,
        metadata: dict[str, Any],
    ) -> Optional[str]:
        """Classify a receiver a cross-module factory produced.

        The factory entity is found in its module; its recorded signature
        names the returned class, which must live in the factory's own file
        — the only origin the signature itself can vouch for. A factory name
        that is in fact a class (``mod.Class()`` as a constructor through a
        ``from``-import) binds the same way.
        """
        module = metadata["receiver_from_call_module"]
        name = metadata["receiver_from_call_name"]
        method = metadata["receiver_method"]

        if not index.module_files(module):
            # The callee's module is outside the repository: the object it
            # produced is external wherever the class actually lives.
            return build_external_reference_id(module, f"{name}.{method}")

        factory = index.unique_top_level(module, name)
        if factory is None:
            return None
        if factory.kind is EntityKind.CLASS:
            return index.method_target(factory, method)

        if not factory.signature:
            return None
        class_name = _return_annotation_name(factory.signature)
        if class_name is None:
            return None
        factory_file = Path(factory.location.file_path).as_posix()
        candidates = index.classes_by_file_last.get((factory_file, class_name), [])
        top_level = [
            entity for entity in candidates if entity.qualified_name.count(".") == 1
        ]
        if len(top_level) == 1:
            return index.method_target(top_level[0], method)
        if class_name in _BUILTIN_RETURN_NAMES and not candidates:
            return build_external_reference_id("builtins", f"{class_name}.{method}")
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
