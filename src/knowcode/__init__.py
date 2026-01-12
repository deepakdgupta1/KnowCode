"""KnowCode - Transform your codebase into an effective knowledge base."""

__version__ = "0.2.1"

from knowcode.data_models import CodeChunk as CodeChunk
from knowcode.data_models import EmbeddingConfig as EmbeddingConfig
from knowcode.storage.chunk_repository import ChunkRepository as ChunkRepository
from knowcode.storage.chunk_repository import InMemoryChunkRepository as InMemoryChunkRepository

__all__ = [
    "__version__",
    "ChunkRepository",
    "CodeChunk",
    "EmbeddingConfig",
    "InMemoryChunkRepository",
]
