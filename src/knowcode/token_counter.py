"""Token counting utility using tiktoken."""

from functools import lru_cache
from typing import Optional

import tiktoken


class TokenCounter:
    """Token counter utility."""

    DEFAULT_MODEL = "gpt-4"

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        """Initialize token counter.

        Args:
            model: Model name to use for encoding.
        """
        self.model = model
        try:
            self.encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            # Fallback to cl100k_base (used by gpt-4, gpt-3.5-turbo)
            self.encoding = tiktoken.get_encoding("cl100k_base")

    def count_tokens(self, text: str) -> int:
        """Count tokens in text.

        Args:
            text: Text to count tokens for.

        Returns:
            Number of tokens.
        """
        if not text:
            return 0
        return len(self.encoding.encode(text))

    def truncate(self, text: str, max_tokens: int) -> str:
        """Truncate text to max_tokens.

        Args:
            text: Text to truncate.
            max_tokens: Maximum tokens allowed.

        Returns:
            Truncated text.
        """
        if not text:
            return ""
            
        tokens = self.encoding.encode(text)
        if len(tokens) <= max_tokens:
            return text
            
        truncated_tokens = tokens[:max_tokens]
        return self.encoding.decode(truncated_tokens)
