"""Vector store for dense retrieval using FAISS."""

import json
import importlib
from pathlib import Path
from typing import Any, Optional

import numpy as np

faiss: Any | None
try:
    faiss = importlib.import_module("faiss")
except ImportError:
    # Optional dependency
    faiss = None


class SimpleIndex:
    """Fallback index implementation when FAISS is unavailable."""

    def __init__(self, dimension: int) -> None:
        self.dimension = dimension
        self.index = np.empty((0, dimension), dtype="float32")
        self.ntotal = 0

    def add(self, x: np.ndarray) -> None:
        self.index = np.vstack([self.index, x])
        self.ntotal = self.index.shape[0]

    def search(self, x: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
        # Simple cosine similarity
        norm_a = np.linalg.norm(self.index, axis=1, keepdims=True)
        norm_b = np.linalg.norm(x, axis=1, keepdims=True)
        scores = np.dot(self.index, x.T) / (norm_a * norm_b.T + 1e-9)
        scores = scores.flatten()

        # Sort indices by score descending
        idx = np.argsort(-scores)[:k]
        return scores[idx].reshape(1, -1), idx.reshape(1, -1)


class VectorStore:
    """FAISS-based vector store for code embeddings."""

    SCHEMA_VERSION = 2
    LEGACY_SCHEMA_VERSION = 1
    SUPPORTED_SCHEMA_VERSIONS = {SCHEMA_VERSION}

    def __init__(
        self, dimension: int = 1536, index_path: Optional[Path] = None
    ) -> None:
        """Initialize the vector index.

        Args:
            dimension: Expected embedding dimensionality.
            index_path: Optional path to load an existing index from disk.
        """
        self.dimension = dimension
        self.index_path = index_path
        self.index: Any

        if faiss:
            # Task 3.4: Use Inner Product for cosine similarity (with normalized vectors)
            self.index = faiss.IndexFlatIP(dimension)
        else:
            self.index = SimpleIndex(dimension)

        self.id_map: dict[int, str] = {}  # index -> chunk_id

        if index_path and index_path.exists():
            self.load(index_path)

    def add(self, chunk_id: str, embedding: list[float]) -> None:
        """Add a chunk embedding to the index.

        No-op if FAISS is unavailable.
        """
        if not self.index:
            return

        vec = np.array([embedding]).astype("float32")
        # Add the vector first, then capture the new index position
        self.index.add(vec)
        idx = self.index.ntotal - 1  # the index of the newly added vector
        # self.index.add(vec)  # moved above
        self.id_map[idx] = chunk_id

    def search(
        self, embedding: list[float], limit: int = 10
    ) -> list[tuple[str, float]]:
        """Search for similar embeddings.

        Args:
            embedding: Query embedding to search for.
            limit: Maximum number of results to return.

        Returns:
            List of (chunk_id, score) tuples.
        """
        if not self.index:
            return []

        vec = np.array([embedding]).astype("float32")
        distances, indices = self.index.search(vec, limit)

        results = []
        for dist, idx in zip(distances[0], indices[0]):
            if idx in self.id_map:
                results.append((self.id_map[int(idx)], float(dist)))

        return results

    def save(self, path: Path) -> None:
        """Save index and ID map to disk."""
        if not self.index:
            return

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Save index if FAISS is available
        if faiss:
            faiss.write_index(self.index, str(path.with_suffix(".index")))

        # Save ID map
        with open(path.with_suffix(".json"), "w") as f:
            json.dump(
                {
                    "schema_version": self.SCHEMA_VERSION,
                    "id_map": {str(k): v for k, v in self.id_map.items()},
                    "dimension": self.dimension,
                },
                f,
            )

    def load(self, path: Path) -> None:
        """Load index and ID map from disk.

        No-op if FAISS is unavailable.
        """
        if not faiss:
            return

        path = Path(path)
        index_file = path.with_suffix(".index")
        json_file = path.with_suffix(".json")

        if index_file.exists():
            self.index = faiss.read_index(str(index_file))

        if json_file.exists():
            with open(json_file) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError(
                    f"Invalid vector metadata format in {json_file}. "
                    "Expected a JSON object."
                )
            validated = self._validate_and_migrate_metadata(data)
            id_map_data = validated.get("id_map", {})
            if not isinstance(id_map_data, dict):
                raise ValueError(
                    f"Invalid vector metadata format in {json_file}. "
                    "Expected 'id_map' to be an object."
                )
            # Task 3.3: Fix persistence bug (ensure we don't reset after loading)
            self.id_map = {int(k): str(v) for k, v in id_map_data.items()}
            self.dimension = int(validated.get("dimension", self.dimension))

    def clear(self) -> None:
        """Clear the index and reset the ID map."""
        # Reset index appropriately based on backend
        if faiss:
            self.index = faiss.IndexFlatIP(self.dimension)
        else:
            self.index = SimpleIndex(self.dimension)
        self.id_map = {}

    @classmethod
    def _validate_and_migrate_metadata(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Validate vector metadata schema and migrate legacy payloads."""
        schema_version = data.get("schema_version")
        if schema_version is None:
            migrated = dict(data)
            migrated["schema_version"] = cls.SCHEMA_VERSION
            return migrated

        normalized = cls._normalize_schema_version(schema_version)
        if normalized == cls.LEGACY_SCHEMA_VERSION:
            migrated = dict(data)
            migrated["schema_version"] = cls.SCHEMA_VERSION
            return migrated
        if normalized in cls.SUPPORTED_SCHEMA_VERSIONS:
            migrated = dict(data)
            migrated["schema_version"] = normalized
            return migrated

        raise ValueError(
            "Unsupported vector index schema version "
            f"{schema_version!r}. Supported versions: "
            f"{sorted(cls.SUPPORTED_SCHEMA_VERSIONS)}. "
            "Rebuild with `knowcode build`."
        )

    @staticmethod
    def _normalize_schema_version(value: Any) -> int:
        """Normalize schema version values represented as int/str."""
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        raise ValueError(f"Invalid vector schema version value: {value!r}")
