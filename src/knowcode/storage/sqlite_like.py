"""Literal-substring matching over SQLite ``LIKE``.

``LIKE`` reads ``_`` as any single character and ``%`` as any run. Both are
ordinary characters in source code, and ``_`` is in most Python and Rust
identifiers, so a caller asking for a literal substring has to escape them and
declare an escape character. Building the pattern and naming the clause in one
place keeps the two from drifting apart: an escaped pattern run without the
clause matches the escape character itself.
"""

LIKE_ESCAPE_CHAR = "\\"
LIKE_ESCAPE_CLAUSE = "ESCAPE '\\'"


def like_contains(text: str) -> str:
    """Build a ``LIKE`` pattern matching ``text`` as a literal substring.

    The result is only correct in a statement carrying ``LIKE_ESCAPE_CLAUSE``.
    """
    escaped = text
    for char in (LIKE_ESCAPE_CHAR, "%", "_"):
        escaped = escaped.replace(char, LIKE_ESCAPE_CHAR + char)
    return f"%{escaped}%"
