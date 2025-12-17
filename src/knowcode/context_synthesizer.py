"""Context synthesizer for generating AI-ready context bundles."""

from dataclasses import dataclass
from typing import Optional

from knowcode.knowledge_store import KnowledgeStore
from knowcode.models import Entity, EntityKind


@dataclass
class ContextBundle:
    """A bundle of context for an entity."""

    target_entity: Entity
    context_text: str
    included_entities: list[str]
    total_chars: int
    truncated: bool


class ContextSynthesizer:
    """Synthesizes context bundles for entities."""

    DEFAULT_MAX_CHARS = 8000  # Rough proxy for ~2K tokens

    def __init__(
        self,
        store: KnowledgeStore,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> None:
        """Initialize context synthesizer.

        Args:
            store: Knowledge store to query.
            max_chars: Maximum characters in context bundle.
        """
        self.store = store
        self.max_chars = max_chars

    def synthesize(self, entity_id: str) -> Optional[ContextBundle]:
        """Synthesize context bundle for an entity.

        Args:
            entity_id: ID of the target entity.

        Returns:
            ContextBundle or None if entity not found.
        """
        entity = self.store.get_entity(entity_id)
        if not entity:
            return None

        sections: list[str] = []
        included: list[str] = [entity_id]
        truncated = False

        # Section 1: Entity header
        sections.append(self._format_entity_header(entity))

        # Section 2: Docstring/description
        if entity.docstring:
            sections.append(f"## Description\n\n{entity.docstring}")

        # Section 3: Signature (for functions/methods)
        if entity.signature:
            sections.append(f"## Signature\n\n```python\n{entity.signature}\n```")

        # Section 4: Source code (if available and fits)
        if entity.source_code:
            code_section = f"## Source Code\n\n```python\n{entity.source_code}\n```"
            if self._would_fit(sections, code_section):
                sections.append(code_section)

        # Section 5: Parent context
        parent = self.store.get_parent(entity_id)
        if parent:
            parent_section = self._format_parent_context(parent)
            if self._would_fit(sections, parent_section):
                sections.append(parent_section)
                included.append(parent.id)

        # Section 6: Callers (who calls this?)
        callers = self.store.get_callers(entity_id)
        if callers:
            callers_section = self._format_callers(callers)
            if self._would_fit(sections, callers_section):
                sections.append(callers_section)
                included.extend(c.id for c in callers)

        # Section 7: Callees (what does this call?)
        callees = self.store.get_callees(entity_id)
        if callees:
            callees_section = self._format_callees(callees)
            if self._would_fit(sections, callees_section):
                sections.append(callees_section)
                included.extend(c.id for c in callees)

        # Section 8: Children (for classes/modules)
        if entity.kind in {EntityKind.CLASS, EntityKind.MODULE, EntityKind.DOCUMENT}:
            children = self.store.get_children(entity_id)
            if children:
                children_section = self._format_children(children)
                if self._would_fit(sections, children_section):
                    sections.append(children_section)
                    included.extend(c.id for c in children)

        # Build final context
        context_text = "\n\n---\n\n".join(sections)

        # Final truncation if still too long
        if len(context_text) > self.max_chars:
            context_text = context_text[: self.max_chars - 20] + "\n\n[TRUNCATED]"
            truncated = True

        return ContextBundle(
            target_entity=entity,
            context_text=context_text,
            included_entities=included,
            total_chars=len(context_text),
            truncated=truncated,
        )

    def _would_fit(self, current_sections: list[str], new_section: str) -> bool:
        """Check if adding a section would stay within budget."""
        current_len = sum(len(s) for s in current_sections)
        new_len = current_len + len(new_section) + 10  # +10 for separators
        return new_len < self.max_chars

    def _format_entity_header(self, entity: Entity) -> str:
        """Format entity header."""
        lines = [
            f"# {entity.kind.value.title()}: `{entity.qualified_name}`",
            "",
            f"**File**: `{entity.location.file_path}`",
            f"**Lines**: {entity.location.line_start}-{entity.location.line_end}",
        ]
        return "\n".join(lines)

    def _format_parent_context(self, parent: Entity) -> str:
        """Format parent context section."""
        lines = [
            "## Parent Context",
            "",
            f"**{parent.kind.value.title()}**: `{parent.qualified_name}`",
        ]
        if parent.docstring:
            # Include first line of docstring
            first_line = parent.docstring.split("\n")[0]
            lines.append(f"> {first_line}")
        return "\n".join(lines)

    def _format_callers(self, callers: list[Entity]) -> str:
        """Format callers section."""
        lines = ["## Called By", ""]
        for caller in callers[:10]:  # Limit to 10
            sig = f" - `{caller.signature.split('(')[0]}(...)`" if caller.signature else ""
            lines.append(f"- `{caller.qualified_name}`{sig}")
        if len(callers) > 10:
            lines.append(f"- ... and {len(callers) - 10} more")
        return "\n".join(lines)

    def _format_callees(self, callees: list[Entity]) -> str:
        """Format callees section."""
        lines = ["## Calls", ""]
        for callee in callees[:10]:  # Limit to 10
            lines.append(f"- `{callee.qualified_name}`")
        if len(callees) > 10:
            lines.append(f"- ... and {len(callees) - 10} more")
        return "\n".join(lines)

    def _format_children(self, children: list[Entity]) -> str:
        """Format children section."""
        lines = ["## Contains", ""]
        for child in children[:15]:  # Limit to 15
            kind = child.kind.value
            lines.append(f"- [{kind}] `{child.name}`")
        if len(children) > 15:
            lines.append(f"- ... and {len(children) - 15} more")
        return "\n".join(lines)

    def synthesize_for_search(
        self,
        query: str,
        max_results: int = 5,
    ) -> str:
        """Synthesize context for a search query.

        Args:
            query: Search pattern.
            max_results: Maximum entities to include.

        Returns:
            Combined context string for matching entities.
        """
        matches = self.store.search(query)[:max_results]

        if not matches:
            return f"No entities found matching '{query}'"

        sections = [f"# Search Results for '{query}'", ""]

        for entity in matches:
            sections.append(f"## {entity.kind.value.title()}: `{entity.qualified_name}`")
            sections.append(f"File: `{entity.location.file_path}:{entity.location.line_start}`")
            if entity.docstring:
                # First 200 chars of docstring
                doc_preview = entity.docstring[:200]
                if len(entity.docstring) > 200:
                    doc_preview += "..."
                sections.append(f"> {doc_preview}")
            sections.append("")

        return "\n".join(sections)
