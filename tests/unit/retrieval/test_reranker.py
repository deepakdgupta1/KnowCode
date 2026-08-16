"""Unit tests for reranking logic and fallbacks."""

import pytest
from unittest.mock import MagicMock, patch
from knowcode.data_models import CodeChunk
from knowcode.retrieval.reranker import Reranker
from knowcode.config import AppConfig, ModelConfig


def test_reranker_initialization_no_config() -> None:
    """Reranker should initialize with defaults when no config is provided."""
    reranker = Reranker(use_voyageai=False)
    assert reranker.voyage_client is None
    assert reranker.model == "rerank-2.5"


def test_reranker_initialization_with_config() -> None:
    """Reranker should use model name from config."""
    config = AppConfig(
        reranking_models=[ModelConfig(name="custom-reranker", api_key_env="ENV_VAR")]
    )
    reranker = Reranker(use_voyageai=False, config=config)
    assert reranker.model == "custom-reranker"


def test_rerank_empty_chunks() -> None:
    """Reranking empty list should return empty list."""
    reranker = Reranker(use_voyageai=False)
    assert reranker.rerank("query", []) == []


def test_signal_based_reranking_heuristics() -> None:
    """Signal-based reranking should apply boosts for docstrings and exact matches."""
    c1 = CodeChunk(
        id="c1", entity_id="e1", content="def foo(): pass", tokens=[], metadata={}
    )
    c2 = CodeChunk(
        id="c2",
        entity_id="e2",
        content="def bar(): pass",
        tokens=[],
        metadata={"has_docstring": "true"},
    )

    reranker = Reranker(use_voyageai=False)
    chunks = [(c1, 0.5), (c2, 0.5)]

    # c2 should rank higher due to has_docstring metadata boost (1.2x)
    results = reranker.rerank("query", chunks)
    assert results[0][0].id == "c2"
    assert results[0][1] == pytest.approx(0.5 * 1.2)


def test_signal_based_reranking_query_match() -> None:
    """Signal-based reranking should boost exact query matches in content."""
    c1 = CodeChunk(
        id="c1", entity_id="e1", content="some random code", tokens=[], metadata={}
    )
    c2 = CodeChunk(
        id="c2",
        entity_id="e2",
        content="this matches secret_function",
        tokens=[],
        metadata={},
    )

    reranker = Reranker(use_voyageai=False)
    chunks = [(c1, 0.5), (c2, 0.5)]

    # c2 should rank higher due to exact match boost (1.5x)
    results = reranker.rerank("secret_function", chunks)
    assert results[0][0].id == "c2"
    assert results[0][1] == pytest.approx(0.5 * 1.5)


@patch("knowcode.llm.voyageai_client.get_voyageai_client")
def test_voyageai_rerank_fallback_on_failure(mock_get_client: MagicMock) -> None:
    """Reranker should fallback to signals if VoyageAI fails."""
    mock_client = MagicMock()
    mock_client.rerank.side_effect = Exception("API Error")
    mock_get_client.return_value = mock_client

    reranker = Reranker(use_voyageai=True, api_key_env="TEST_KEY")
    reranker.voyage_client = mock_client  # Force client assignment

    c1 = CodeChunk(id="c1", entity_id="e1", content="A", tokens=[])
    chunks = [(c1, 0.5)]

    # Should not raise, but fallback
    results = reranker.rerank("query", chunks)
    assert len(results) == 1
    assert results[0][0].id == "c1"


def test_rerank_top_k_truncation() -> None:
    """Reranker should respect top_k limit."""
    c1 = CodeChunk(id="c1", entity_id="e1", content="A", tokens=[])
    c2 = CodeChunk(id="c2", entity_id="e2", content="B", tokens=[])
    reranker = Reranker(use_voyageai=False)
    chunks = [(c1, 0.5), (c2, 0.4)]

    results = reranker.rerank("query", chunks, top_k=1)
    assert len(results) == 1
    assert results[0][0].id == "c1"
