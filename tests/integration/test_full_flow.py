"""End-to-End Search Pipeline Test."""

from typing import Any
from knowcode.indexing.indexer import Indexer
from knowcode.retrieval.search_engine import SearchEngine
from knowcode.llm.embedding import OpenAIEmbeddingProvider
from knowcode.data_models import EmbeddingConfig
from knowcode.retrieval.hybrid_index import HybridIndex
from knowcode.storage.sqlite_chunk_repository import SqliteChunkRepository
from knowcode.storage.vector_store import VectorStore

class MockEmbeddingProvider(OpenAIEmbeddingProvider):
    def __init__(self) -> Any:  # type: ignore
        self.config = EmbeddingConfig(dimension=8)
        
    def embed(self, texts: list[str]) -> list[list[float]]:
        # Deterministic dummy embedding
        return [[0.1] * 8 for _ in texts]
        
    def embed_single(self, text: str) -> list[float]:
        return [0.1] * 8

def test_full_search_flow(tmp_path) -> None:  # type: ignore
    """Test full pipeline: Indexing -> Search -> Results."""
    # 1. Setup
    repo = SqliteChunkRepository(":memory:")
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
        def get_callers(self, _): return []  # type: ignore
        def get_callees(self, _): return []  # type: ignore

    engine = SearchEngine(repo, provider, hybrid, MockStore())  # type: ignore
    
    results = engine.search("metrics", limit=5)
    
    # 5. Verify
    assert len(results) > 0
    assert "calculate_metrics" in results[0].content
