"""LanceDB-backed vector store for disk-based retrieval."""

import json
import shutil
from pathlib import Path
from typing import Any

from knowcode.errors import VectorDimensionError

try:
    import lancedb
except ImportError:
    lancedb = None


class LanceDBVectorStore:
    """LanceDB-based vector store for persistent disk-based retrieval."""

    TABLE_NAME = "vectors"
    SCHEMA_VERSION = 1

    def __init__(self, dimension: int, path: str | Path | None = None) -> None:
        """Initialize LanceDB vector store.
        
        Args:
            dimension: Expected embedding dimensionality.
            path: Directory path for LanceDB. If None, uses in-memory DB.
        """
        if lancedb is None:
            raise ImportError(
                "lancedb is not installed. Install with 'pip install lancedb' "
                "or 'pip install knowcode[search]'"
            )
        self.dimension = dimension
        self.path = str(path) if path else "memory://"
        self.db = lancedb.connect(self.path)
        self._buffer: list[dict[str, Any]] = []
        self._table = None
        
        if self.TABLE_NAME in self.db.table_names():
            self._table = self.db.open_table(self.TABLE_NAME)

    def add(self, chunk_id: str, embedding: list[float]) -> None:
        """Add a chunk embedding to the index."""
        self._require_dimension(embedding)
        self._buffer.append({"id": chunk_id, "vector": embedding})
        if len(self._buffer) >= 1000:
            self._flush()

    def _require_dimension(self, embedding: list[float]) -> None:
        """Raise VectorDimensionError when the embedding length disagrees.

        LanceDB otherwise raises a low-level Arrow ValueError at *flush* time
        on an unrelated later call, so validate eagerly at add/upsert time.
        """
        actual = len(embedding)
        if actual != self.dimension:
            raise VectorDimensionError(self.dimension, actual)

    def upsert(self, chunk_id: str, embedding: list[float]) -> None:
        """Exact-ID idempotent add-or-replace (remove then add)."""
        self._require_dimension(embedding)
        self.remove(chunk_id)
        self.add(chunk_id, embedding)

    def flush(self) -> None:
        """Flush the internal buffer so buffered writes are visible to reads."""
        self._flush()

    def _flush(self) -> None:
        """Flush the internal buffer to LanceDB."""
        if not self._buffer:
            return

        if self._table is None:
            self._table = self.db.create_table(self.TABLE_NAME, data=self._buffer)
        else:
            self._table.add(self._buffer)

        self._buffer = []

    def search(self, embedding: list[float], limit: int = 10) -> list[tuple[str, float]]:
        """Search for similar embeddings.

        Returns:
            List of (chunk_id, similarity_score) tuples.
        """
        self._flush()
        
        if self._table is None:
            return []
            
        # We use metric="cosine" to get cosine distance.
        results = self._table.search(embedding).metric("cosine").limit(limit).to_list()
        
        # LanceDB cosine distance is 1 - cosine similarity.
        return [(r["id"], 1.0 - r["_distance"]) for r in results]

    def get_embedding(self, chunk_id: str) -> list[float] | None:
        """Fetch the embedding for a chunk ID."""
        self._flush()
        if self._table is None:
            return None
        try:
            res = self._table.search().where(f"id = '{chunk_id}'").limit(1).to_list()
            if res:
                return [float(x) for x in res[0]["vector"]]
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("Ignored exception: %s", e)
        return None

    def save(self, path: Path) -> None:
        """Persist vector index artifacts."""
        self._flush()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        
        if not self.path.startswith("memory://"):
            target = path.with_suffix(".lancedb")
            if target.resolve() != Path(self.path).resolve():
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(self.path, target)
                
        with open(path.with_suffix(".json"), "w") as f:
            json.dump(
                {
                    "schema_version": self.SCHEMA_VERSION,
                    "dimension": self.dimension,
                    "backend": "lancedb"
                },
                f,
            )

    def load(self, path: Path) -> None:
        """Load vector index artifacts."""
        if lancedb is None:
            raise ImportError(
                "lancedb is not installed. Install with 'pip install lancedb' "
                "or 'pip install knowcode[search]'"
            )
        path = Path(path)
        target = path.with_suffix(".lancedb")
        if target.exists():
            self.path = str(target)
            self.db = lancedb.connect(self.path)
            if self.TABLE_NAME in self.db.table_names():
                self._table = self.db.open_table(self.TABLE_NAME)
            
        json_file = path.with_suffix(".json")
        if json_file.exists():
            with open(json_file) as f:
                data = json.load(f)
            self.dimension = int(data.get("dimension", self.dimension))

    def clear(self) -> None:
        """Clear the index."""
        self._buffer = []
        if self._table is not None:
            self.db.drop_table(self.TABLE_NAME)
            self._table = None

    def remove(self, chunk_id: str) -> None:
        """Remove a chunk from the index."""
        self._flush()
        if self._table is not None:
            self._table.delete(f"id = '{chunk_id}'")

    def count(self) -> int:
        """Return the number of vectors in the index."""
        self._flush()
        if self._table is None:
            return 0
        return int(self._table.count_rows())
