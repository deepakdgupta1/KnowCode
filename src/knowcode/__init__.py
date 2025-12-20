"""KnowCode - Transform your codebase into an effective knowledge base."""

__version__ = "0.1.0"

from knowcode.models import CodeChunk, EmbeddingConfig
from knowcode.storage.chunk_repository import ChunkRepository, InMemoryChunkRepository
