"""End-to-End Search Pipeline Test."""

import pytest
import time
from pathlib import Path
from knowcode.indexing.indexer import Indexer
from knowcode.retrieval.search_engine import SearchEngine
from knowcode.llm.embedding import OpenAIEmbeddingProvider
from knowcode.data_models import EmbeddingConfig, CodeChunk
from knowcode.retrieval.hybrid_index import HybridIndex
from knowcode.storage.chunk_repository import InMemoryChunkRepository
from knowcode.storage.vector_store import VectorStore
from knowcode.storage.knowledge_store import KnowledgeStore

class MockEmbeddingProvider(OpenAIEmbeddingProvider):
    def __init__(self):
        self.config = EmbeddingConfig(dimension=8)
        
    def embed(self, texts: list[str]) -> list[list[float]]:
        # Deterministic dummy embedding
        return [[0.1] * 8 for _ in texts]
        
    def embed_single(self, text: str) -> list[float]:
        return [0.1] * 8

def test_full_search_flow(tmp_path):
    """Test full pipeline: Indexing -> Search -> Results."""
    # 1. Setup
    repo = InMemoryChunkRepository()
    vs = VectorStore(dimension=8)
    provider = MockEmbeddingProvider()
    
    indexer = Indexer(provider, chunk_repo=repo, vector_store=vs)
    
    # 2. Create content
    f1 = tmp_path / "app.py"
    f1.write_text("""
def calculate_metrics(data):
    '''Calculate important business metrics.'''
    return data * 2
""")
    
    # 3. Index
    indexer.index_file(f1)
    
    # 4. Search
    hybrid = HybridIndex(repo, vs)
    # Mock knowledge store for dependency expansion
    class MockStore:
        def get_callers(self, _): return []
        def get_callees(self, _): return []

    engine = SearchEngine(repo, provider, hybrid, MockStore())
    
    results = engine.search("metrics", limit=5)
    
    # 5. Verify
    assert len(results) > 0
    assert "calculate_metrics" in results[0].content
