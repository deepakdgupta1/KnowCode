"""Custom exceptions for KnowCode runtime prerequisites."""

from __future__ import annotations

from pathlib import Path


class KnowCodePrerequisiteError(RuntimeError):
    """Base class for missing prerequisite artifacts."""

    def __init__(self, message: str, *, code: str, hint: str) -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint


class MissingKnowledgeStoreError(KnowCodePrerequisiteError):
    """Raised when the knowledge store file is missing."""

    def __init__(self, store_file: Path) -> None:
        super().__init__(
            f"Knowledge store not found: {store_file}",
            code="missing_knowledge_store",
            hint="Run `knowcode analyze <dir>` first.",
        )


class MissingSemanticIndexError(KnowCodePrerequisiteError):
    """Raised when the semantic index directory is missing."""

    def __init__(self, index_path: Path) -> None:
        super().__init__(
            f"Semantic index not found: {index_path}",
            code="missing_semantic_index",
            hint="Run `knowcode index <dir>` first.",
        )
