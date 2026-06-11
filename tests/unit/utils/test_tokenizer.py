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


def test_tokenizer_includes_compound_identifiers() -> None:
    """Compound identifiers should appear as both subtokens AND joined form.

    getUserById -> ['get', 'user', 'by', 'id', 'getuserbyid']
    This is critical for exact-identifier BM25 matching in FTS5.
    """
    tokens = tokenize_code("getUserById")
    # Subtokens must be present
    assert "get" in tokens
    assert "user" in tokens
    # Joined compound must also be present
    assert "getuserbyid" in tokens


def test_tokenizer_compound_snake_case() -> None:
    """snake_case identifiers should include the joined form."""
    tokens = tokenize_code("reconcile_ledger_entries")
    assert "reconcile" in tokens
    assert "ledger" in tokens
    assert "entries" in tokens
    assert "reconcileledgerentries" in tokens
