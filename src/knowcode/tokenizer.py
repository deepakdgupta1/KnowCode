"""Tokenize code for BM25 indexing."""

import re


def tokenize_code(text: str) -> list[str]:
    """Tokenize code for BM25 indexing.
    
    Handles:
    - CamelCase splitting
    - snake_case splitting  
    - Punctuation removal
    - Lowercasing

    Args:
        text: Raw code or text to tokenize.

    Returns:
        List of normalized tokens suitable for BM25 matching.
    """
    # Split camelCase
    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
    # Split snake_case
    text = text.replace('_', ' ')
    # Remove punctuation except spaces
    text = re.sub(r'[^\w\s]', ' ', text)
    # Lowercase and split
    tokens = text.lower().split()
    # Filter short tokens
    return [t for t in tokens if len(t) > 1]
