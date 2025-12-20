"""Indexing pipeline for code chunks."""

from pathlib import Path
from typing import Optional

from knowcode.chunk_repository import InMemoryChunkRepository
from knowcode.chunker import Chunker
from knowcode.embedding import EmbeddingProvider
from knowcode.graph_builder import GraphBuilder
from knowcode.scanner import Scanner
from knowcode.vector_store import VectorStore
from knowcode.logger import get_logger

logger = get_logger(__name__)


class Indexer:
    """Orchestrates scan -> chunk -> embed -> index pipeline."""

    def __init__(
        self,
        embedding_provider: EmbeddingProvider,
        chunk_repo: Optional[InMemoryChunkRepository] = None,
        vector_store: Optional[VectorStore] = None,
    ) -> None:
        """Initialize an indexer with optional storage backends.

        Args:
            embedding_provider: Provider used to generate chunk embeddings.
            chunk_repo: Optional chunk repository (defaults to in-memory).
            vector_store: Optional vector store (defaults to FAISS-backed store).
        """
        self.embedding_provider = embedding_provider
        self.chunk_repo = chunk_repo or InMemoryChunkRepository()
        self.vector_store = vector_store or VectorStore(dimension=embedding_provider.config.dimension)
        self.chunker = Chunker()

    def index_directory(self, root_dir: str | Path) -> int:
        """Index all supported files under a directory.

        Args:
            root_dir: Root directory to scan for supported files.

        Returns:
            Total number of chunks added to the index.
        """
        root_path = Path(root_dir)
        
        # Use existing GraphBuilder to get semantic entities
        builder = GraphBuilder()
        builder.build_from_directory(root_path)
        
        # Extract files from scanner
        scanner = Scanner(root_path)
        files = scanner.scan_all()
        
        total_chunks = 0
        for file_info in files:
            # Build ParseResult-like data or use parser directly
            # For simplicity in this Task, we use the Chunker which can take a ParseResult or we can adapt it.
            # I'll use the graph builder's internal logic or build the parse results first.
            
            # Re-parse file to get entities (ideally we reuse builder.entities but we need them per file)
            # For now, let's assume we use the PythonParser etc via a helper
            parse_result = builder._parse_file(file_info)
            chunks = self.chunker.process_parse_result(parse_result)
            
            if not chunks:
                continue
                
            # Process embeddings in batches
            contents = [c.content for c in chunks]
            embeddings = self.embedding_provider.embed(contents)
            
            for chunk, emb in zip(chunks, embeddings):
                chunk.embedding = emb
                self.chunk_repo.add(chunk)
                self.vector_store.add(chunk.id, emb)
                total_chunks += 1
                
        return total_chunks

    def save(self, path: str | Path) -> None:
        """Persist vector index and chunk metadata to disk.

        Args:
            path: Directory path to write index files into.
        """
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        
        # Save vector store
        self.vector_store.save(path / "vectors")
        
        # Save chunk metadata (BM25 tokens and content)
        import json
        metadata = {
            "chunks": [
                {
                    "id": c.id,
                    "entity_id": c.entity_id,
                    "content": c.content,
                    "tokens": c.tokens,
                    "metadata": c.metadata
                }
                for c in self.chunk_repo._chunks.values()
            ]
        }
        with open(path / "chunks.json", "w") as f:
            json.dump(metadata, f)

    def load(self, path: str | Path) -> None:
        """Load the entire vector index and chunk metadata from disk.

        Args:
            path: Directory path containing previously saved index files.
        """
        path = Path(path)
        
        # Load vector store
        self.vector_store.load(path / "vectors")
        
        # Load chunks
        import json
        from knowcode.models import CodeChunk
        
        chunks_file = path / "chunks.json"
        if chunks_file.exists():
            with open(chunks_file) as f:
                data = json.load(f)
                for c_data in data["chunks"]:
                    chunk = CodeChunk(**c_data)
                    self.chunk_repo.add(chunk)
                    
    def index_file(self, file_path: str | Path) -> int:
        """Index a single file for incremental updates.

        Args:
            file_path: File path to process.

        Returns:
            Number of chunks created for the file.
        """
        file_path = Path(file_path)
        # Simplified for Task 3.6
        builder = GraphBuilder()
        from knowcode.scanner import FileInfo
        file_info = FileInfo(file_path, str(file_path), file_path.suffix, file_path.stat().st_size)
        parse_result = builder._parse_file(file_info)
        chunks = self.chunker.process_parse_result(parse_result)
        
        if chunks:
            contents = [c.content for c in chunks]
            embeddings = self.embedding_provider.embed(contents)
            for chunk, emb in zip(chunks, embeddings):
                chunk.embedding = emb
                self.chunk_repo.add(chunk)
                self.vector_store.add(chunk.id, emb)
        return len(chunks)
