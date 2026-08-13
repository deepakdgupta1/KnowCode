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
            hint="Run `knowcode build <dir>` first.",
        )


class MissingSemanticIndexError(KnowCodePrerequisiteError):
    """Raised when the semantic index directory is missing."""

    def __init__(self, index_path: Path) -> None:
        super().__init__(
            f"Semantic index not found: {index_path}",
            code="missing_semantic_index",
            hint="Run `knowcode build <dir>` first.",
        )


class RepositoryClosedError(RuntimeError):
    """Raised when an operation is attempted on a closed repository handle.

    A closed handle is not a missing prerequisite (the file may still exist),
    so this deliberately derives from ``RuntimeError`` rather than from
    :class:`KnowCodePrerequisiteError`, whose ``__init__`` requires a
    ``code``/``hint`` pair. Step 09 (ADR 2) introduces it so that a reader or
    writer touching a connection torn down by ``close()``/``load()`` gets an
    actionable, typed error instead of a low-level ``sqlite3.ProgrammingError``.
    """
