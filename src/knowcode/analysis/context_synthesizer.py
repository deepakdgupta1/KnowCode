"""Context synthesizer for generating AI-ready context bundles."""

from dataclasses import dataclass, field
from typing import Optional

from knowcode.storage.knowledge_store import KnowledgeStore
from knowcode.data_models import Entity, EntityKind, TaskType
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
    task_type: TaskType = TaskType.GENERAL
    sufficiency_score: float = 0.0  # 0.0-1.0 confidence in context adequacy


# Task-specific templates defining content priorities (from KnowCode.md Layer 9)
# Higher numbers = higher priority
TASK_TEMPLATES = {
    TaskType.DEBUG: {
        "priority": ["source_code", "callers", "callees", "signature", "docstring"],
        "boost": {"source_code": 2.0, "callers": 1.5},  # Relative weight boost
        "include_tests": True,
        "include_recent_changes": True,
    },
    TaskType.EXTEND: {
        "priority": ["signature", "docstring", "children", "parent", "source_code"],
        "boost": {"signature": 1.5, "children": 1.3},
        "include_tests": True,
        "include_recent_changes": False,
    },
    TaskType.REVIEW: {
        "priority": ["source_code", "callers", "callees", "signature"],
        "boost": {"callers": 1.5, "callees": 1.5},
        "include_tests": True,
        "include_recent_changes": True,
    },
    TaskType.EXPLAIN: {
        "priority": ["docstring", "signature", "source_code", "callees", "parent"],
        "boost": {"docstring": 1.5, "callees": 1.3},
        "include_tests": False,
        "include_recent_changes": False,
    },
    TaskType.LOCATE: {
        "priority": ["signature", "docstring", "parent"],
        "boost": {},
        "include_tests": False,
        "include_recent_changes": False,
    },
    TaskType.GENERAL: {
        "priority": ["docstring", "signature", "source_code", "parent", "callers", "callees"],
        "boost": {},
        "include_tests": False,
        "include_recent_changes": False,
    },
}


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

    def synthesize_with_task(
        self,
        entity_id: str,
        task_type: TaskType = TaskType.GENERAL,
    ) -> Optional[ContextBundle]:
        """Synthesize context bundle with task-specific prioritization.

        Uses task templates from KnowCode.md Layer 9 to prioritize content
        based on the type of task (debug, extend, review, explain, locate).

        Args:
            entity_id: ID of the target entity.
            task_type: Type of task for context prioritization.

        Returns:
            ContextBundle with task_type and sufficiency_score, or None if not found.
        """
        entity = self.store.get_entity(entity_id)
        if not entity:
            return None

        template = TASK_TEMPLATES.get(task_type, TASK_TEMPLATES[TaskType.GENERAL])
        priority_order = template["priority"]
        boosts = template.get("boost", {})

        sections: list[str] = []
        included: list[str] = [entity_id]
        
        # Track what we've included for sufficiency scoring
        content_included = {
            "signature": False,
            "docstring": False,
            "source_code": False,
            "parent": False,
            "callers": False,
            "callees": False,
            "children": False,
        }
        
        # Always include header
        header = self._format_entity_header(entity)
        current_tokens = self.tokenizer.count_tokens(header)
        sections.append(header)
        
        # Build content sections based on priority order
        content_sections = {}
        
        if entity.signature:
            content_sections["signature"] = f"## Signature\n\n```python\n{entity.signature}\n```"
            
        if entity.docstring:
            content_sections["docstring"] = f"## Description\n\n{entity.docstring}"
            
        if entity.source_code:
            code_header = "## Source Code\n\n```python\n"
            code_footer = "\n```"
            code_body = entity.source_code
            # Pre-truncate if too long
            max_code_tokens = int(self.max_tokens * 0.5)  # Reserve half for code max
            code_tokens = self.tokenizer.count_tokens(code_body)
            if code_tokens > max_code_tokens:
                code_body = self.tokenizer.truncate(code_body, max_code_tokens) + "\n# ... (truncated)"
            content_sections["source_code"] = f"{code_header}{code_body}{code_footer}"
            
        parent = self.store.get_parent(entity_id)
        if parent:
            content_sections["parent"] = self._format_parent_context(parent)
            
        callers = self.store.get_callers(entity_id)
        if callers:
            content_sections["callers"] = self._format_callers(callers)
            
        callees = self.store.get_callees(entity_id)
        if callees:
            content_sections["callees"] = self._format_callees(callees)
            
        if entity.kind in {EntityKind.CLASS, EntityKind.MODULE, EntityKind.DOCUMENT}:
            children = self.store.get_children(entity_id)
            if children:
                content_sections["children"] = self._format_children(children)
        
        # Add sections in priority order until budget exhausted
        is_truncated = False
        
        for section_name in priority_order:
            if section_name not in content_sections:
                continue
                
            section_text = content_sections[section_name]
            t = self.tokenizer.count_tokens(section_text)
            
            # Apply boost: if boosted, allocate more budget
            boost = boosts.get(section_name, 1.0)
            effective_budget = self.max_tokens * boost
            
            if current_tokens + t < min(effective_budget, self.max_tokens):
                sections.append(section_text)
                current_tokens += t
                content_included[section_name] = True
                
                # Track included entity IDs
                if section_name == "parent" and parent:
                    included.append(parent.id)
                elif section_name == "callers" and callers:
                    included.extend(c.id for c in callers[:10])
                elif section_name == "callees" and callees:
                    included.extend(c.id for c in callees[:10])
                elif section_name == "children":
                    children_list = self.store.get_children(entity_id)
                    if children_list:
                        included.extend(c.id for c in children_list[:15])
            else:
                is_truncated = True
        
        context_text = "\n\n---\n\n".join(sections)
        
        # Calculate sufficiency score based on task requirements
        sufficiency = self._calculate_sufficiency(
            task_type, content_included, entity, context_text
        )
        
        return ContextBundle(
            target_entity=entity,
            context_text=context_text,
            included_entities=included,
            total_chars=len(context_text),
            total_tokens=current_tokens,
            truncated=is_truncated,
            task_type=task_type,
            sufficiency_score=sufficiency,
        )

    def _calculate_sufficiency(
        self,
        task_type: TaskType,
        content_included: dict[str, bool],
        entity: Entity,
        context_text: str,
    ) -> float:
        """Calculate sufficiency score (0.0-1.0) for local-first answering.
        
        Higher scores indicate the context is likely sufficient to answer
        without needing an external LLM.
        
        Args:
            task_type: The type of task being performed.
            content_included: Dict tracking what content was included.
            entity: The target entity.
            context_text: The synthesized context.
            
        Returns:
            Sufficiency score from 0.0 to 1.0.
        """
        template = TASK_TEMPLATES.get(task_type, TASK_TEMPLATES[TaskType.GENERAL])
        priority_order = template["priority"]
        
        score = 0.0
        max_score = 0.0
        
        # Weight each priority item by its position (higher priority = more weight)
        for i, section_name in enumerate(priority_order):
            weight = 1.0 / (i + 1)  # Decreasing weight by position
            max_score += weight
            
            if content_included.get(section_name, False):
                score += weight
        
        # Bonus for having source code (always valuable)
        if entity.source_code and "## Source Code" in context_text:
            score += 0.2
            max_score += 0.2
            
        # Bonus for having docstring (helps LLM understand intent)
        if entity.docstring and len(entity.docstring) > 50:
            score += 0.1
            max_score += 0.1
            
        # Penalize if context is very short
        min_useful_tokens = 100
        if len(context_text) < min_useful_tokens:
            score *= 0.5
            
        return min(1.0, round(score / max_score, 2)) if max_score > 0 else 0.0

