"""Tokenize code for BM25 indexing."""

import re

# Pre-compiled patterns for performance.
_CAMEL_SPLIT_RE = re.compile(r'([a-z])([A-Z])')
_PUNCT_RE = re.compile(r'[^\w\s]')
_IDENTIFIER_RE = re.compile(r'[a-zA-Z_][a-zA-Z0-9_]*')


def tokenize_code(text: str) -> list[str]:
    """Tokenize code for BM25 indexing.

    Handles:
    - CamelCase splitting (getUserById → get, user, by, id)
    - snake_case splitting (reconcile_ledger → reconcile, ledger)
    - Punctuation removal
    - Lowercasing
    - Compound identifier preservation (getUserById → getuserbyid)

    The compound form is critical for exact-identifier matching through
    FTS5 BM25 — without it, searching for 'getUserById' degrades to
    searching for the very common subtokens 'get', 'user', etc.

    Args:
        text: Raw code or text to tokenize.

    Returns:
        List of normalized tokens suitable for BM25 matching.
    """
    # 1. Find compound identifiers BEFORE splitting, so we can add
    #    their joined form to the output.
    compounds: list[str] = []
    for match in _IDENTIFIER_RE.finditer(text):
        identifier = match.group()
        # Only care about multi-word identifiers (camelCase or snake_case).
        if '_' in identifier or _CAMEL_SPLIT_RE.search(identifier):
            joined = re.sub(r'_', '', identifier).lower()
            if len(joined) > 1:
                compounds.append(joined)

    # 2. Split camelCase
    text = _CAMEL_SPLIT_RE.sub(r'\1 \2', text)
    # 3. Split snake_case
    text = text.replace('_', ' ')
    # 4. Remove punctuation except spaces
    text = _PUNCT_RE.sub(' ', text)
    # 5. Lowercase and split
    tokens = text.lower().split()
    # 6. Filter short tokens
    subtokens = [t for t in tokens if len(t) > 1]

    # 7. Append compound forms (deduplicated, after subtokens).
    seen = set(subtokens)
    for compound in compounds:
        if compound not in seen:
            subtokens.append(compound)
            seen.add(compound)

    return subtokens
