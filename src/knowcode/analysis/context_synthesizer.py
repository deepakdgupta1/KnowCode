"""Context synthesizer for generating AI-ready context bundles."""

from dataclasses import dataclass
from typing import Optional

from knowcode.storage.knowledge_store import KnowledgeStore
from knowcode.models import Entity, EntityKind
from knowcode.utils.token_counter import TokenCounter


@dataclass
class ContextBundle:
    """A bundle of context for an entity."""

    target_entity: Entity
    context_text: str
    included_entities: list[str]
    total_chars: int
    total_tokens: int
    truncated: bool


class ContextSynthesizer:
    """Synthesizes context bundles for entities."""

    DEFAULT_MAX_TOKENS = 2000

    def __init__(
        self,
        store: KnowledgeStore,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        model: str = "gpt-4",
    ) -> None:
        """Initialize context synthesizer.

        Args:
            store: Knowledge store to query.
            max_tokens: Maximum tokens in context bundle.
            model: Model name for token counting.
        """
        self.store = store
        self.max_tokens = max_tokens
        self.tokenizer = TokenCounter(model)

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
        
        # STRATEGY: 
        # We construct the context bundle by adding sections in order of "Semantic Priority".
        # 1. Core Identity (Header, Signature, Docstring) - Essential
        # 2. Source Code - High value, but expensive. Truncated if necessary.
        # 3. Parent Context - Helps LLM understand where this fits.
        # 4. Incoming/Outgoing Relationships - Additional context, added greedily until budget fills.
        
        # We build sections in priority order but display them in logical order usually.
        # However, for simplicity, we'll append and check budget.
        
        # Priority 1: Entity Core (Header, Signature, Description)
        header = self._format_entity_header(entity)
        current_tokens = self.tokenizer.count_tokens(header)
        sections.append(header)
        
        desc = ""
        if entity.docstring:
            desc = f"## Description\n\n{entity.docstring}"
            
        sig = ""
        if entity.signature:
            sig = f"## Signature\n\n```python\n{entity.signature}\n```"
            
        # Add high priority sections if they fit
        if desc:
            t = self.tokenizer.count_tokens(desc)
            if current_tokens + t < self.max_tokens:
                sections.append(desc)
                current_tokens += t
        
        if sig:
            t = self.tokenizer.count_tokens(sig)
            if current_tokens + t < self.max_tokens:
                sections.append(sig)
                current_tokens += t

        # Priority 2: Source Code (Huge consumer, often truncated)
        if entity.source_code:
            code_header = "## Source Code\n\n```python\n"
            code_footer = "\n```"
            overhead = self.tokenizer.count_tokens(code_header + code_footer)
            remaining = self.max_tokens - current_tokens - overhead
            
            if remaining > 100: # Only add if we have decent space
                code_body = entity.source_code
                code_tokens = self.tokenizer.count_tokens(code_body)
                
                if code_tokens > remaining:
                    code_body = self.tokenizer.truncate(code_body, remaining) + "\n# ... (truncated)"
                    # We technically truncated the content
                    # But we will rely on full budget exhaustion check often
                
                sections.append(f"{code_header}{code_body}{code_footer}")
                current_tokens += self.tokenizer.count_tokens(sections[-1])
            else:
                 # Skipped source code due to budget
                 # We consider this truncation/loss of info
                 pass 

        # Priority 3: Parent Context
        parent = self.store.get_parent(entity_id)
        if parent:
            parent_section = self._format_parent_context(parent)
            t = self.tokenizer.count_tokens(parent_section)
            if current_tokens + t < self.max_tokens:
                sections.append(parent_section)
                included.append(parent.id)
                current_tokens += t

        # Priority 4: Relationships (Callers, Callees, Children)
        # We add them greedily until budget exhaust
        
        # Unified list of potential sections
        rel_sections = []
        
        callers = self.store.get_callers(entity_id)
        if callers:
            rel_sections.append((self._format_callers(callers), [c.id for c in callers]))

        callees = self.store.get_callees(entity_id)
        if callees:
             rel_sections.append((self._format_callees(callees), [c.id for c in callees]))
             
        if entity.kind in {EntityKind.CLASS, EntityKind.MODULE, EntityKind.DOCUMENT}:
            children = self.store.get_children(entity_id)
            if children:
                rel_sections.append((self._format_children(children), [c.id for c in children]))

        is_truncated = False
        
        for text, ids in rel_sections:
            t = self.tokenizer.count_tokens(text)
            if current_tokens + t < self.max_tokens:
                sections.append(text)
                included.extend(ids)
                current_tokens += t
            else:
                is_truncated = True

        context_text = "\n\n---\n\n".join(sections)
        
        # Check if we skipped source code but had it
        if entity.source_code and "## Source Code" not in context_text:
             is_truncated = True

        return ContextBundle(
            target_entity=entity,
            context_text=context_text,
            included_entities=included,
            total_chars=len(context_text),
            total_tokens=current_tokens,
            truncated=is_truncated or (current_tokens >= self.max_tokens),
        )

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
