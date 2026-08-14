"""Orchestration for retrieval flows used by KnowCodeService."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Protocol
import logging

from knowcode.llm.query_classifier import classify_query
from knowcode.telemetry import current_query_scope, query_scope

if TYPE_CHECKING:
    from knowcode.data_models import TaskType
    from knowcode.retrieval.search_engine import ScoredChunk


class SearchEngineProtocol(Protocol):
    """Protocol for search engines used in semantic retrieval."""

    def search_scored(
        self,
        query: str,
        limit: int = 10,
        expand_deps: bool = True,
    ) -> list["ScoredChunk"]:
        """Run retrieval and return scored chunks."""


class RetrievalServiceProtocol(Protocol):
    """Surface that RetrievalOrchestrator needs from KnowCodeService."""

    def _assert_store_exists(self) -> Path:
        """Validate persisted knowledge store artifact is present."""

    def _assert_index_exists(self) -> Path:
        """Validate persisted semantic index artifact is present."""

    def _validate_index_compatibility(self, index_path: Path) -> None:
        """Validate configured embedding model against loaded index metadata."""

    def get_search_engine(
        self, index_path: Optional[str | Path] = None
    ) -> SearchEngineProtocol:
        """Return a search engine instance."""

    def get_exact_query_engine(
        self, index_path: Optional[str | Path] = None
    ) -> SearchEngineProtocol:
        """Return an exact query engine instance."""

    def search(self, pattern: str) -> list[dict[str, Any]]:
        """Run lexical search for entities."""

    def get_context(
        self,
        target: str,
        max_tokens: int = 2000,
        task_type: Optional["TaskType"] = None,
        summarize: bool = False,
        is_stale: bool = False,
    ) -> dict[str, Any]:
        """Build context for a target entity."""

    def _extract_query_keywords(self, query: str) -> list[str]:
        """Extract identifier-like terms from a natural-language query."""


class RetrievalOrchestrator:
    """Coordinate semantic/lexical retrieval and context synthesis."""

    def __init__(self, service: RetrievalServiceProtocol) -> None:
        self._service = service

    @staticmethod
    def _record_retrieval(**fields: Any) -> None:
        """Report one retrieval attempt to the active query scope, if any."""
        scope = current_query_scope()
        if scope is None:  # pragma: no cover - the public method always opens one
            return
        try:
            scope.record_retrieval(**fields)
        except Exception as exc:  # noqa: BLE001 - telemetry never fails a query
            logging.getLogger(__name__).warning("Failed to log telemetry: %s", exc)

    def retrieve_context_for_query(
        self,
        query: str,
        max_tokens: int = 4000,
        task_type: Optional["TaskType"] = None,
        limit_entities: int = 3,
        per_entity_max_tokens: Optional[int] = None,
        expand_deps: bool = True,
        verbosity: str = "minimal",
        include_metadata: bool = False,
        is_stale: bool = False,
    ) -> dict[str, Any]:
        """Retrieve an evidence-backed context bundle for a query.

        The whole retrieval runs inside one telemetry query scope (Step 20).
        The scope is joined rather than opened when a caller above already has
        one, so an agent's three retrieval attempts stay one counted query, and
        components with no store handle of their own — the reranker — resolve
        their telemetry destination from it instead of the working directory.
        """
        with query_scope(
            getattr(self._service, "store_path", None),
            query=query,
            entry_point="orchestrator",
        ):
            return self._retrieve_context(
                query,
                max_tokens=max_tokens,
                task_type=task_type,
                limit_entities=limit_entities,
                per_entity_max_tokens=per_entity_max_tokens,
                expand_deps=expand_deps,
                verbosity=verbosity,
                include_metadata=include_metadata,
                is_stale=is_stale,
            )

    def _retrieve_context(
        self,
        query: str,
        max_tokens: int = 4000,
        task_type: Optional["TaskType"] = None,
        limit_entities: int = 3,
        per_entity_max_tokens: Optional[int] = None,
        expand_deps: bool = True,
        verbosity: str = "minimal",
        include_metadata: bool = False,
        is_stale: bool = False,
    ) -> dict[str, Any]:
        """Run one retrieval attempt and report it to the active query scope."""
        errors: list[str] = []
        self._service._assert_store_exists()
        index_path = self._service._assert_index_exists()

        detected_task_type, confidence = classify_query(query)
        resolved_task_type = task_type or detected_task_type
        task_confidence = 1.0 if task_type is not None else confidence

        if limit_entities <= 0 or max_tokens <= 0:
            self._record_retrieval(
                task_type=resolved_task_type.value,
                task_confidence=task_confidence,
                retrieval_mode="none",
                sufficiency_score=0.0,
                total_tokens=0,
                max_tokens=max_tokens,
                truncated=False,
                selected_entity_count=0,
                evidence_count=0,
                error_count=1,
            )
            return {
                "query": query,
                "task_type": resolved_task_type.value,
                "task_confidence": task_confidence,
                "retrieval_mode": "none",
                "context_text": "",
                "total_tokens": 0,
                "max_tokens": max_tokens,
                "truncated": False,
                "sufficiency_score": 0.0,
                "selected_entities": [],
                "evidence": [],
                "errors": ["Invalid token or entity limits."],
            }

        if per_entity_max_tokens is None:
            per_entity_max_tokens = max(200, min(2000, max_tokens // limit_entities))

        selected_entity_ids: list[str] = []
        evidence: list[dict[str, Any]] = []
        retrieval_mode = "semantic"

        try:
            self._service._validate_index_compatibility(index_path)
            
            if query.startswith('"') and query.endswith('"') and len(query) >= 2:
                engine = self._service.get_exact_query_engine(index_path)
                retrieval_mode = "exact"
            else:
                engine = self._service.get_search_engine(index_path)
                
            scored = engine.search_scored(
                query,
                limit=max(10, limit_entities * 5),
                expand_deps=expand_deps,
            )

            primary = [s for s in scored if s.source == "retrieved"]
            seen_entities: set[str] = set()
            for s in primary:
                if s.chunk.entity_id in seen_entities:
                    continue
                seen_entities.add(s.chunk.entity_id)
                selected_entity_ids.append(s.chunk.entity_id)
                if len(selected_entity_ids) >= limit_entities:
                    break

            for rank, s in enumerate(scored, start=1):
                evidence.append(
                    {
                        "rank": rank,
                        "chunk_id": s.chunk.id,
                        "entity_id": s.chunk.entity_id,
                        "score": s.score,
                        "source": s.source,
                    }
                )

        except Exception as exc:
            errors.append(f"Semantic retrieval failed; falling back to lexical: {exc}")
            retrieval_mode = "lexical"

        if retrieval_mode == "lexical":
            candidates: list[str] = []
            seen: set[str] = set()

            def add_entity_ids(items: list[dict[str, Any]]) -> None:
                for item in items:
                    entity_id = item.get("id")
                    if not entity_id or entity_id in seen:
                        continue
                    seen.add(entity_id)
                    candidates.append(entity_id)

            add_entity_ids(self._service.search(query))
            if len(candidates) < limit_entities:
                for kw in self._service._extract_query_keywords(query):
                    add_entity_ids(self._service.search(kw))
                    if len(candidates) >= limit_entities:
                        break

            selected_entity_ids = candidates[:limit_entities]
            for rank, entity_id in enumerate(selected_entity_ids, start=1):
                evidence.append(
                    {"rank": rank, "entity_id": entity_id, "source": "lexical"}
                )

        selected_entities: list[dict[str, Any]] = []
        context_parts: list[str] = []
        sufficiency_scores: list[float] = []
        total_tokens = 0
        truncated = False

        for entity_id in selected_entity_ids:
            try:
                bundle = self._service.get_context(
                    entity_id,
                    max_tokens=per_entity_max_tokens,
                    task_type=resolved_task_type,
                    summarize=(verbosity == "minimal"),
                    is_stale=is_stale,
                )
            except Exception as exc:
                errors.append(f"Failed to synthesize context for {entity_id}: {exc}")
                continue

            context_parts.append(bundle.get("context_text", ""))
            total_tokens += int(bundle.get("total_tokens", 0))
            truncated = truncated or bool(bundle.get("truncated", False))

            score = bundle.get("sufficiency_score")
            if isinstance(score, (int, float)):
                sufficiency_scores.append(float(score))

            selected_entities.append(
                {
                    "entity_id": bundle.get("entity_id", entity_id),
                    "task_type": bundle.get("task_type", resolved_task_type.value),
                    "total_tokens": bundle.get("total_tokens", 0),
                    "truncated": bundle.get("truncated", False),
                    "sufficiency_score": bundle.get("sufficiency_score", 0.0),
                }
            )

        context_text = "\n\n---\n\n".join([part for part in context_parts if part])
        sufficiency = (
            round(sum(sufficiency_scores) / len(sufficiency_scores), 2)
            if sufficiency_scores
            else 0.0
        )

        full_response = {
            "query": query,
            "task_type": resolved_task_type.value,
            "task_confidence": task_confidence,
            "retrieval_mode": retrieval_mode,
            "context_text": context_text,
            "total_tokens": total_tokens,
            "max_tokens": max_tokens,
            "truncated": truncated,
            "sufficiency_score": sufficiency,
            "selected_entities": selected_entities,
            "evidence": evidence,
            "errors": errors,
        }

        # Counts, not identities: the entity ids this retrieval selected are
        # absolute source paths, which are repository content rather than the
        # aggregate metadata ADR 5 permits.
        self._record_retrieval(
            task_type=resolved_task_type.value,
            task_confidence=task_confidence,
            retrieval_mode=retrieval_mode,
            sufficiency_score=sufficiency,
            total_tokens=total_tokens,
            max_tokens=max_tokens,
            truncated=truncated,
            selected_entity_count=len(selected_entities),
            evidence_count=len(evidence),
            error_count=len(errors),
        )

        if verbosity == "diagnostic":
            return full_response

        filtered_response: dict[str, Any] = {
            "context_text": context_text,
            "sufficiency_score": sufficiency,
            "total_tokens": total_tokens,
        }

        if verbosity == "minimal":
            filtered_response["reduction_summary"] = {
                "omitted_raw_source_count": len(selected_entity_ids),
                "omitted_evidence_count": len(evidence),
                "hint": "Call again with verbosity='standard' to get raw source code, or 'verbose' to see evidence chunks.",
            }
            if errors:
                filtered_response["errors"] = errors
        elif verbosity == "standard":
            filtered_response.update(
                {
                    "query": query,
                    "task_type": resolved_task_type.value,
                    "task_confidence": task_confidence,
                    "retrieval_mode": retrieval_mode,
                    "max_tokens": max_tokens,
                    "truncated": truncated,
                }
            )
            if errors:
                filtered_response["errors"] = errors
        elif verbosity == "verbose":
            filtered_response.update(
                {
                    "query": query,
                    "task_type": resolved_task_type.value,
                    "task_confidence": task_confidence,
                    "retrieval_mode": retrieval_mode,
                    "max_tokens": max_tokens,
                    "truncated": truncated,
                    "evidence": evidence,
                }
            )
            if errors:
                filtered_response["errors"] = errors

        if include_metadata:
            filtered_response.update(
                {
                    "query": query,
                    "task_type": resolved_task_type.value,
                    "task_confidence": task_confidence,
                    "retrieval_mode": retrieval_mode,
                    "max_tokens": max_tokens,
                    "truncated": truncated,
                    "selected_entities": selected_entities,
                    "evidence": evidence,
                    "errors": errors,
                }
            )

        return filtered_response
