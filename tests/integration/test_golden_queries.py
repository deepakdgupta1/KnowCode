"""Retrieval Golden-Query Tests."""

import pytest
from knowcode.indexing.indexer import Indexer
from knowcode.retrieval.search_engine import SearchEngine
from knowcode.data_models import EmbeddingConfig
from knowcode.retrieval.hybrid_index import HybridIndex
from knowcode.storage.sqlite_chunk_repository import SqliteChunkRepository
from knowcode.storage.vector_store import VectorStore
from knowcode.storage.knowledge_store import KnowledgeStore

# Mocking the embedding provider so we don't hit external APIs in tests.
class DeterministicEmbeddingProvider:
    def __init__(self) -> None:
        self.config = EmbeddingConfig(dimension=4)
        
    def embed(self, texts: list[str]) -> list[list[float]]:
        # Return deterministic dummy vectors.
        # BM25 will do the heavy lifting for relevance in this test.
        return [[0.1, 0.1, 0.1, 0.1] for _ in texts]
        
    def embed_single(self, text: str) -> list[float]:
        return [0.1, 0.1, 0.1, 0.1]

@pytest.fixture
def search_engine(tmp_path):
    repo = SqliteChunkRepository(":memory:")
    vs = VectorStore(dimension=4)
    provider = DeterministicEmbeddingProvider()
    
    indexer = Indexer(provider, chunk_repo=repo, vector_store=vs)
    
    # Create a small realistic codebase
    files = {
        "auth.py": '''
def login(username, password):
    """Handle user authentication and login flow."""
    return True
''',
        "database.py": '''
def connect_to_db(connection_string):
    """Establish connection to the main postgres database."""
    pass
''',
        "utils.py": '''
def reverse_string(s):
    """A helper function to reverse a string."""
    return s[::-1]
''',
    }
    
    for filename, content in files.items():
        p = tmp_path / filename
        p.write_text(content)
        indexer.index_file(p)
        
    hybrid = HybridIndex(repo, vs)
    store = KnowledgeStore()
    engine = SearchEngine(repo, provider, hybrid, store)
    
    return engine

@pytest.mark.parametrize("query, expected_filename", [
    ("user login authentication flow", "auth.py"),
    ("postgres database connection", "database.py"),
    ("helper reverse string", "utils.py"),
])
def test_golden_queries(search_engine, query, expected_filename):
    """Golden-query test: ensure specific queries return expected files as top result."""
    results = search_engine.search(query, limit=3)
    assert len(results) > 0
    
    top_result = results[0]
    # Check if the expected filename is in the chunk ID (e.g. auth.py::login::chunk_0)
    assert expected_filename in top_result.id
