"""Vector store for dense retrieval using FAISS."""

import json
from pathlib import Path
from typing import Optional

import numpy as np

try:
    import faiss
except ImportError:
    # Optional dependency
    faiss = None


class VectorStore:
    """FAISS-based vector store for code embeddings."""

    def __init__(self, dimension: int = 1536, index_path: Optional[Path] = None) -> None:
        self.dimension = dimension
        self.index_path = index_path
        
        if faiss:
            # Task 3.4: Use Inner Product for cosine similarity (with normalized vectors)
            self.index = faiss.IndexFlatIP(dimension)
        else:
            self.index = None
            
        self.id_map: dict[int, str] = {}  # index -> chunk_id
        
        if index_path and index_path.exists():
            self.load(index_path)

    def add(self, chunk_id: str, embedding: list[float]) -> None:
        """Add a chunk embedding to the index."""
        if not self.index:
             return
             
        vec = np.array([embedding]).astype("float32")
        idx = self.index.ntotal
        self.index.add(vec)
        self.id_map[idx] = chunk_id

    def search(self, embedding: list[float], limit: int = 10) -> list[tuple[str, float]]:
        """Search for similar embeddings."""
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
        
        # Save FAISS index
        faiss.write_index(self.index, str(path.with_suffix(".index")))
        
        # Save ID map
        with open(path.with_suffix(".json"), "w") as f:
            json.dump({"id_map": {str(k): v for k, v in self.id_map.items()}, "dimension": self.dimension}, f)

    def load(self, path: Path) -> None:
        """Load index and ID map from disk."""
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
                # Task 3.3: Fix persistence bug (ensure we don't reset after loading)
                self.id_map = {int(k): v for k, v in data["id_map"].items()}
                self.dimension = data.get("dimension", self.dimension)
                
    def clear(self) -> None:
        """Clear the index."""
        if faiss:
            self.index = faiss.IndexFlatIP(self.dimension)
        self.id_map = {}
