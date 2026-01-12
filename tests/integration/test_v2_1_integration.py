"""Integration tests for v2.1 search pipeline."""

from unittest.mock import MagicMock
from knowcode.data_models import EmbeddingConfig
from knowcode.llm.embedding import EmbeddingProvider
from knowcode.indexing.indexer import Indexer
from knowcode.storage.chunk_repository import InMemoryChunkRepository
from knowcode.storage.vector_store import VectorStore
from knowcode.retrieval.hybrid_index import HybridIndex
from knowcode.retrieval.search_engine import SearchEngine


class MockEmbeddingProvider(EmbeddingProvider):
    def embed(self, texts: list[str]) -> list[list[float]]:
        # Return dummy vectors of correct dimension
        return [[0.1] * self.config.dimension for _ in texts]
        
    def embed_single(self, text: str) -> list[float]:
        return [0.1] * self.config.dimension


def test_indexer_flow(tmp_path):
    config = EmbeddingConfig(dimension=8)
    provider = MockEmbeddingProvider(config)
    repo = InMemoryChunkRepository()
    vs = VectorStore(dimension=8)
    
    indexer = Indexer(provider, chunk_repo=repo, vector_store=vs)
    
    # Mock a file content or create one
    test_file = tmp_path / "test.py"
    test_file.write_text("def my_func():\n    pass")
    
    # We need to mock GraphBuilder._parse_file or use real one
    # For integration test, we use the real one if possible, but it depends on scanner.
    # Let's mock index_file's internal parsing call if needed, but here we'll just test the orchestration
    
    count = indexer.index_file(test_file)
    assert count > 0
    assert len(repo._chunks) > 0
    assert vs.index.ntotal > 0


def test_search_engine_orchestration():
    config = EmbeddingConfig(dimension=8)
    provider = MockEmbeddingProvider(config)
    repo = InMemoryChunkRepository()
    vs = VectorStore(dimension=8)
    
    # Add a chunk
    from knowcode.models import CodeChunk
    chunk = CodeChunk(id="c1", entity_id="e1", content="find me", tokens=["find", "me"])
    repo.add(chunk)
    vs.add("c1", [0.1]*8)
    
    hybrid = HybridIndex(repo, vs)
    # Generic KnowledgeStore mock
    ks = MagicMock()
    ks.get_callees.return_value = []
    
    engine = SearchEngine(repo, provider, hybrid, ks)
    results = engine.search("find me", limit=1, expand_deps=False)
    
    assert len(results) == 1
    assert results[0].id == "c1"
