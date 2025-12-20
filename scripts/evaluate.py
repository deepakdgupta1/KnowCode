"""Evaluation script for retrieval quality."""

import json
import sys
from pathlib import Path
from knowcode.storage.chunk_repository import InMemoryChunkRepository
from knowcode.storage.vector_store import VectorStore
from knowcode.retrieval.hybrid_index import HybridIndex
from knowcode.llm.embedding import OpenAIEmbeddingProvider
from knowcode.models import EmbeddingConfig, CodeChunk

def evaluate(ground_truth_path: Path, index_path: Path) -> dict:
    """Evaluate retrieval quality against ground truth."""
    if not ground_truth_path.exists():
        return {"error": "Ground truth file not found"}
        
    with open(ground_truth_path) as f:
        ground_truth = json.load(f)
    
    # Load index components
    repo = InMemoryChunkRepository()
    # Assuming index_path is directory containing chunks.json and vectors used by Indexer.load
    # Note: Indexer.load logic:
    # chunks_file = path / "chunks.json"
    # vector_path = path / "vectors"
    
    chunks_file = index_path / "chunks.json"
    if chunks_file.exists():
        with open(chunks_file) as f:
            data = json.load(f)
            for c_data in data["chunks"]:
                repo.add(CodeChunk(**c_data))
                
    vs = VectorStore(dimension=1536, index_path=index_path / "vectors")
    # Note: We need a real provider for queries, or mock if vectors are precomputed?
    # For evaluation we assume we have an API key or use the same provider used for indexing.
    # Here we assume OpenAI.
    try:
        provider = OpenAIEmbeddingProvider(EmbeddingConfig())
    except:
        print("Skipping evaluation: No OpenAI API Key found")
        return {}

    hybrid = HybridIndex(repo, vs)
    
    # Metrics
    hits_at_5 = 0
    hits_at_10 = 0
    mrr_sum = 0.0
    total_queries = len(ground_truth)
    
    for item in ground_truth:
        query = item.get("query")
        expected_ids = set(item.get("expected_ids", []))
        
        if not query or not expected_ids:
            continue
            
        q_vec = provider.embed_single(query)
        # Search directly on hybrid index (skipping SearchEngine wrapper for raw retrieval eval)
        results = hybrid.search(query, q_vec, limit=10)
        
        found_ids = [c.id for c, _ in results]
        
        # Recall@k
        if any(fid in expected_ids for fid in found_ids[:5]):
            hits_at_5 += 1
        if any(fid in expected_ids for fid in found_ids[:10]):
            hits_at_10 += 1
            
        # MRR
        rank = 0
        for i, fid in enumerate(found_ids):
            if fid in expected_ids:
                rank = i + 1
                break
        if rank > 0:
            mrr_sum += 1.0 / rank

    return {
        "precision_at_5": hits_at_5 / total_queries if total_queries else 0,
        "recall_at_10": hits_at_10 / total_queries if total_queries else 0,
        "mrr": mrr_sum / total_queries if total_queries else 0,
    }


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python evaluate.py <ground_truth.json> <index_dir>")
        sys.exit(1)
        
    gt_path = Path(sys.argv[1])
    idx_path = Path(sys.argv[2])
    print(evaluate(gt_path, idx_path))
