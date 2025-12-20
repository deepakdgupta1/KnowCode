"""Unit tests for code tokenization."""

from knowcode.utils.tokenizer import tokenize_code


def test_tokenizer_splits_cases() -> None:
    tokens = tokenize_code("myFunctionName_snake_case")
    assert "my" in tokens
    assert "function" in tokens
    assert "name" in tokens
    assert "snake" in tokens
    assert "case" in tokens


def test_tokenizer_strips_punctuation() -> None:
    tokens = tokenize_code("foo(bar); baz.qux!")
    assert "foo" in tokens
    assert "bar" in tokens
    assert "baz" in tokens
    assert "qux" in tokens
