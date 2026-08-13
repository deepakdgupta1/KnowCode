"""Vector backend registry, factory, and persisted-index inspection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from knowcode.protocols import VectorStoreProtocol


@dataclass(frozen=True)
class VectorBackendSpec:
    """Static metadata for a vector backend."""

    key: str
    display_name: str
    metadata_schema_version: int
    artifact_description: str


@dataclass(frozen=True)
class VectorIndexInspection:
    """Inspection result for vector index metadata and artifacts."""

    backend: str
    raw_metadata: dict[str, Any]
    metadata: dict[str, Any]
    failures: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Return True when no failures were found."""
        return not self.failures


VECTOR_BACKENDS: dict[str, VectorBackendSpec] = {
    "lancedb": VectorBackendSpec(
        key="lancedb",
        display_name="LanceDB",
        metadata_schema_version=2,
        artifact_description="vectors.lancedb",
    ),
    "faiss": VectorBackendSpec(
        key="faiss",
        display_name="FAISS/NumPy",
        metadata_schema_version=2,
        artifact_description="vectors.index or vectors.npy",
    ),
}


def create_vector_store(
    backend: str,
    *,
    dimension: int,
    index_dir: Path | None = None,
) -> VectorStoreProtocol:
    """Create a vector store for the configured backend."""
    normalized = _normalize_backend(backend)
    if normalized == "lancedb":
        from knowcode.storage.lancedb_vector_store import LanceDBVectorStore

        path = index_dir / "vectors.lancedb" if index_dir is not None else None
        return LanceDBVectorStore(dimension=dimension, path=path)

    from knowcode.storage.vector_store import VectorStore

    return VectorStore(dimension=dimension)


def inspect_vector_index(
    index_path: Path,
    *,
    configured_backend: str = "lancedb",
) -> VectorIndexInspection:
    """Inspect vector metadata and backend-specific artifacts."""
    index_path = Path(index_path)
    failures: list[str] = []
    warnings: list[str] = []
    raw_metadata: dict[str, Any] = {}
    metadata: dict[str, Any] = {}

    vectors_file = index_path / "vectors.json"
    if vectors_file.exists():
        try:
            raw_metadata = _read_json_object(vectors_file)
            metadata = _validate_vector_metadata(raw_metadata)
        except Exception as exc:
            failures.append(f"invalid vectors.json: {exc}")
    else:
        failures.append("missing vectors.json")

    backend = _infer_backend(
        metadata=metadata or raw_metadata,
        index_path=index_path,
        configured_backend=configured_backend,
    )
    spec = VECTOR_BACKENDS.get(backend)
    if spec is None:
        failures.append(f"unsupported vector backend: {backend!r}")
        return VectorIndexInspection(
            backend=backend,
            raw_metadata=raw_metadata,
            metadata=metadata,
            failures=tuple(failures),
            warnings=tuple(warnings),
        )

    missing_artifacts = _missing_artifacts(index_path, backend)
    failures.extend(missing_artifacts)

    # No producer of ``warnings`` remains: both backends now fail closed on a
    # legacy envelope (FAISS in Step 11, LanceDB in Step 12), so an
    # out-of-date artifact is a failure with rebuild guidance rather than a
    # migration warning. The channel stays for Steps 13-14 parity reporting.
    return VectorIndexInspection(
        backend=backend,
        raw_metadata=raw_metadata,
        metadata=metadata,
        failures=tuple(failures),
        warnings=tuple(warnings),
    )


def _validate_vector_metadata(data: dict[str, Any]) -> dict[str, Any]:
    backend = data.get("backend")
    if backend == "lancedb":
        # Importable without the optional extra: the envelope check is a
        # classmethod that never touches the lancedb package.
        from knowcode.storage.lancedb_vector_store import LanceDBVectorStore

        return LanceDBVectorStore._validate_metadata(data)
    if backend and backend not in VECTOR_BACKENDS:
        return dict(data)

    from knowcode.storage.vector_store import VectorStore

    return VectorStore._validate_metadata(data)


def _infer_backend(
    *,
    metadata: dict[str, Any],
    index_path: Path,
    configured_backend: str,
) -> str:
    backend = metadata.get("backend")
    if isinstance(backend, str) and backend:
        return backend
    if (index_path / "vectors.lancedb").is_dir():
        return "lancedb"
    if (index_path / "vectors.index").exists() or (index_path / "vectors.npy").exists():
        return "faiss"
    return _normalize_backend(configured_backend)


def _missing_artifacts(index_path: Path, backend: str) -> tuple[str, ...]:
    if backend == "lancedb":
        if not (index_path / "vectors.lancedb").is_dir():
            return ("missing vectors.lancedb",)
        return ()

    if not ((index_path / "vectors.index").exists() or (index_path / "vectors.npy").exists()):
        return ("missing vectors.index or vectors.npy",)
    return ()


def _normalize_backend(backend: str) -> str:
    normalized = backend.lower()
    if normalized not in VECTOR_BACKENDS:
        raise ValueError(f"Unsupported vector backend: {backend!r}")
    return normalized


def _read_json_object(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}.")
    return data
