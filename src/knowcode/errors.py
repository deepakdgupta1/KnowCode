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


class VectorContractError(RuntimeError):
    """Base class for vector-store contract violations.

    Step 10 (ADR 7) freezes the :class:`~knowcode.protocols.VectorStoreProtocol`
    contract: exact-ID upsert/removal, dimension validation, live count, flush,
    and unique search results. Backends that violate one of those invariants
    raise a typed subclass so callers get an actionable error instead of a
    low-level numpy/FAISS/Arrow failure. ``hasattr`` is not capability
    negotiation: a missing required operation is a contract violation.
    """


class VectorDimensionError(VectorContractError):
    """Raised when an embedding's dimension disagrees with the store's.

    Verified against the Step 10 baseline: a mismatch already fails today, but
    unhelpfully — FAISS raises a bare ``AssertionError`` with an empty message,
    the numpy fallback raises ``ValueError`` from ``np.vstack``, and LanceDB
    raises a low-level Arrow ``ValueError`` at *flush* time on an unrelated
    later call. The contract requires validation at ``add``/``upsert`` time so
    the error names the offending dimension and the expected one.
    """

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(
            f"Embedding dimension mismatch: expected {expected}, got {actual}."
        )
        self.expected = expected
        self.actual = actual


class VectorArtifactVersionError(VectorContractError):
    """Raised when a persisted vector artifact has an incompatible version.

    Step 10 freezes the contract surface (the metadata envelope version,
    rebuild guidance, and this error type) without changing either backend's
    on-disk format. Steps 11 and 12 stamp and validate the version; this error
    is the fail-closed signal a loader raises for a missing, malformed, newer,
    or unsupported artifact version. No loader silently stamps the current
    version onto an unverified legacy payload.
    """
