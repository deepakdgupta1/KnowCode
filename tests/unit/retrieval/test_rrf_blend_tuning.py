"""Tests for RRF blend tuning and alpha configuration."""

import pytest
from knowcode.retrieval.hybrid_index import HybridIndex
from knowcode.data_models import CodeChunk, Location
from enum import Enum

class MockKind(Enum):
    FUNCTION = "function"

class MockChunkRepo:
    def search_by_tokens(self, tokens, limit=10):
        # Return chunks where ID is in tokens
        return [CodeChunk(id=t, entity_id=t, content="mock") for t in tokens[:limit]]

    def get(self, chunk_id: str):
        return CodeChunk(id=chunk_id, entity_id=chunk_id, content="mock")

class MockVectorStore:
    def search(self, embedding, limit=10):
        # Embedding is a list of tuples (chunk_id, score) or similar.
        # But here embedding can just be a list of expected IDs.
        return [(str(i), 0.9) for i in embedding[:limit]]

def test_alpha_propagates():
    repo = MockChunkRepo()
    vec = MockVectorStore()
    index = HybridIndex(repo, vec, alpha=0.3)
    assert index.alpha == 0.3

def test_different_alphas_change_ordering():
    repo = MockChunkRepo()
    vec = MockVectorStore()
    
    # Query tokens favor chunks 'search_function', 'token_string'
    query = "search_function token_string"
    # Vector store favors chunks 'C_vector', 'D_vector'
    query_embedding = ['C_vector', 'D_vector']
    
    # alpha=0.0 -> 100% sparse
    index_sparse = HybridIndex(repo, vec, alpha=0.0)
    results_sparse = index_sparse.search(query, query_embedding, limit=2)
    assert results_sparse[0][0].id == 'search'
    assert results_sparse[1][0].id == 'function'
    
    # alpha=1.0 -> 100% dense
    index_dense = HybridIndex(repo, vec, alpha=1.0)
    # The returned chunks are only the ones returned by get(chunk_id).
    # Since MockChunkRepo get() is not implemented, we need to mock it.
    repo.get = lambda x: CodeChunk(id=x, entity_id=x, content="mock")
    
    results_dense = index_dense.search(query, query_embedding, limit=2)
    assert results_dense[0][0].id == 'C_vector'
    assert results_dense[1][0].id == 'D_vector'

