"""Unit tests for token counting."""

from knowcode.utils.token_counter import TokenCounter


def test_token_counter_unknown_model_fallback() -> None:
    """Unknown models should fall back to a default encoding."""
    counter = TokenCounter("nonexistent-model")
    assert counter.count_tokens("hello world") > 0
