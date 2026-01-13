"""Evaluation script for retrieval quality."""

import json
import os
import sys
from pathlib import Path
from knowcode.storage.chunk_repository import InMemoryChunkRepository
from knowcode.storage.vector_store import VectorStore
from knowcode.retrieval.hybrid_index import HybridIndex
from knowcode.llm.embedding import OpenAIEmbeddingProvider, VoyageAIEmbeddingProvider
from knowcode.data_models import EmbeddingConfig, CodeChunk

def evaluate(ground_truth_path: Path, index_path: Path) -> dict:
    """Evaluate retrieval quality against ground truth."""
    if not ground_truth_path.exists():
        return {"error": f"Ground truth file not found at {ground_truth_path}"}
    
    if not index_path.exists():
        return {"error": f"Index directory not found at {index_path}"}
        
    with open(ground_truth_path) as f:
        ground_truth = json.load(f)
    
    # Load manifest to determine model and dimension
    manifest_file = index_path / "index_manifest.json"
    dimension = 1536
    provider_name = "openai"
    
    if manifest_file.exists():
        with open(manifest_file) as f:
            manifest = json.load(f)
            embedding_meta = manifest.get("embedding", {})
            dimension = embedding_meta.get("dimension", dimension)
            provider_name = embedding_meta.get("provider", provider_name)
            print(f"Index detected: {provider_name} with dimension {dimension}")

    # Load chunk metadata
    repo = InMemoryChunkRepository()
    chunks_file = index_path / "chunks.json"
    if chunks_file.exists():
        with open(chunks_file) as f:
            data = json.load(f)
            for c_data in data["chunks"]:
                # Ensure we don't pass embedding if it's not in the data
                repo.add(CodeChunk(**c_data))
    else:
        return {"error": "chunks.json not found in index directory"}
                
    # Load vector store (VectorStore.load expects the base name 'vectors')
    vs = VectorStore(dimension=dimension)
    vs.load(index_path / "vectors")
    
    # Setup provider
    if provider_name == "voyageai":
        if not os.environ.get("VOYAGE_API_KEY_1") and not os.environ.get("VOYAGE_API_KEY"):
            return {"error": "VOYAGE_API_KEY_1 or VOYAGE_API_KEY not set"}
        provider = VoyageAIEmbeddingProvider(EmbeddingConfig(provider="voyageai", dimension=dimension))
    else:
        if not os.environ.get("OPENAI_API_KEY"):
            return {"error": "OPENAI_API_KEY not set"}
        provider = OpenAIEmbeddingProvider(EmbeddingConfig(provider="openai", dimension=dimension))

    hybrid = HybridIndex(repo, vs)
    
    # Metrics
    hits_at_1 = 0
    hits_at_5 = 0
    hits_at_10 = 0
    mrr_sum = 0.0
    total_queries = len(ground_truth)
    
    print(f"Evaluating {total_queries} queries...")

    for item in ground_truth:
        query = item.get("query")
        expected_ids = set(item.get("expected_ids", []))
        
        if not query or not expected_ids:
            continue
            
        q_vec = provider.embed_single(query)
        # Search directly on hybrid index
        results = hybrid.search(query, q_vec, limit=10)
        
        found_ids = [c.id for c, _ in results]
        
        # Recall@k
        if any(fid in expected_ids for fid in found_ids[:1]):
            hits_at_1 += 1
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
        "queries": total_queries,
        "precision_at_1": round(hits_at_1 / total_queries, 4) if total_queries else 0,
        "precision_at_5": round(hits_at_5 / total_queries, 4) if total_queries else 0,
        "recall_at_10": round(hits_at_10 / total_queries, 4) if total_queries else 0,
        "mrr": round(mrr_sum / total_queries, 4) if total_queries else 0,
    }


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python evaluate.py <ground_truth.json> <index_dir>")
        sys.exit(1)
        
    gt_path = Path(sys.argv[1])
    idx_path = Path(sys.argv[2])
    results = evaluate(gt_path, idx_path)
    print(json.dumps(results, indent=2))
