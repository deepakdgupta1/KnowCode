import shutil
import tempfile
from pathlib import Path
from dataclasses import dataclass

from knowcode.indexing.indexer import Indexer
from knowcode.storage.sqlite_chunk_repository import SqliteChunkRepository

class MockEmbeddingProvider:
    @dataclass
    class Config:
        dimension: int = 3
    config = Config()
    
    def __init__(self):
        self.call_count = 0
        
    def embed_single(self, text: str) -> list[float]:
        self.call_count += 1
        return [0.1, 0.2, 0.3]
        
    def embed(self, texts: list[str]) -> list[list[float]]:
        self.call_count += len(texts)
        return [[0.1, 0.2, 0.3] for _ in texts]


def test_incremental_indexer_reuses_embeddings() -> None:
    # 1. Setup a dummy repo
    temp_dir = tempfile.mkdtemp()
    try:
        repo_dir = Path(temp_dir) / "repo"
        repo_dir.mkdir()
        
        file1 = repo_dir / "file1.py"
        file1.write_text("def foo():\n    return 42\n")
        
        # Git init and commit
        import subprocess
        subprocess.run(["git", "init"], cwd=str(repo_dir), check=True)
        subprocess.run(["git", "add", "."], cwd=str(repo_dir), check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=str(repo_dir), check=True)
        
        # 2. Build initial index
        index_dir = Path(temp_dir) / "index"
        provider = MockEmbeddingProvider()
        chunk_repo = SqliteChunkRepository(index_dir / "chunks.db")
        # vector_store not passed, indexer will use VectorStore (which falls back to MockVectorStore)
        indexer = Indexer(provider, chunk_repo=chunk_repo)
        
        count = indexer.index_directory(repo_dir)
        assert count > 0
        assert provider.call_count == count
        indexer.save(index_dir)
        
        # 3. Modify repo (add a file, modify file1)
        file2 = repo_dir / "file2.py"
        file2.write_text("def bar():\n    return 'hello'\n")
        
        # Commit the change
        subprocess.run(["git", "add", "."], cwd=str(repo_dir), check=True)
        subprocess.run(["git", "commit", "-m", "second"], cwd=str(repo_dir), check=True)
        
        # 4. Incremental update
        provider2 = MockEmbeddingProvider()
        chunk_repo2 = SqliteChunkRepository(index_dir / "chunks.db")
        indexer2 = Indexer(provider2, chunk_repo=chunk_repo2)
        indexer2.load(index_dir)
        
        indexer2.index_incremental(repo_dir)
        
        # Since file1 only had an addition of file2, if file1 wasn't modified, its chunks are skipped entirely.
        # But wait, file1 WAS NOT MODIFIED. Only file2 was added.
        # file2 has 1 chunk.
        assert provider2.call_count == 1
        
        # Let's modify file1 but keep a chunk the same
        file1.write_text("def foo():\n    return 42\n\ndef baz():\n    return 99\n")
        subprocess.run(["git", "add", "."], cwd=str(repo_dir), check=True)
        subprocess.run(["git", "commit", "-m", "third"], cwd=str(repo_dir), check=True)
        
        indexer2.save(index_dir)
        
        provider3 = MockEmbeddingProvider()
        chunk_repo3 = SqliteChunkRepository(index_dir / "chunks.db")
        indexer3 = Indexer(provider3, chunk_repo=chunk_repo3)
        indexer3.load(index_dir)
        
        indexer3.index_incremental(repo_dir)
        
        # file1 was modified. It has 2 functions now: foo and baz.
        # foo is the SAME content hash, so it should NOT be re-embedded.
        # baz is new, so it SHOULD be embedded.
        assert provider3.call_count == 1
        
    finally:
        shutil.rmtree(temp_dir)
