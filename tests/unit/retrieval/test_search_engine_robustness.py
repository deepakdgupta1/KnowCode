"""Unit tests for search engine error handling."""

import pytest
from unittest.mock import MagicMock
from knowcode.retrieval.search_engine import SearchEngine
from knowcode.storage.chunk_repository import InMemoryChunkRepository
from knowcode.storage.knowledge_store import KnowledgeStore


class FailingEmbeddingProvider:
    def embed_single(self, _text):  # type: ignore
        raise Exception("Embedding API down")


def test_search_engine_handles_embedding_failure() -> None:
    """SearchEngine should propagate or handle embedding failures."""
    repo = InMemoryChunkRepository()
    hybrid = MagicMock()
    store = KnowledgeStore()
    
    engine = SearchEngine(repo, FailingEmbeddingProvider(), hybrid, store)  # type: ignore
    
    with pytest.raises(Exception) as excinfo:
        engine.search_scored("query")
    assert "Embedding API down" in str(excinfo.value)


def test_search_engine_empty_hybrid_results() -> None:
    """SearchEngine should handle case where hybrid index returns nothing."""
    repo = InMemoryChunkRepository()
    embedding = MagicMock()
    embedding.embed_single.return_value = [0.0] * 1024
    
    hybrid = MagicMock()
    hybrid.search.return_value = []
    
    store = KnowledgeStore()
    engine = SearchEngine(repo, embedding, hybrid, store)
    
    results = engine.search_scored("query")
    assert results == []
