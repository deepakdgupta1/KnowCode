"""Evaluation script for retrieval quality."""

import argparse
import json
import os
import sys
from pathlib import Path
from knowcode.storage.chunk_repository import InMemoryChunkRepository
from knowcode.llm.embedding import OpenAIEmbeddingProvider, VoyageAIEmbeddingProvider
from knowcode.data_models import EmbeddingConfig, CodeChunk

def normalize_entity_id(entity_id: str) -> str:
    """Normalize entity ID by making the file path component relative to project root."""
    if "::" not in entity_id:
        return entity_id
    path_part, symbol_part = entity_id.split("::", 1)
    
    path = Path(path_part)
    if not path.is_absolute():
        return entity_id
        
    try:
        rel_path = path.relative_to(Path.cwd().resolve())
        return f"{rel_path}::{symbol_part}"
    except ValueError:
        pass
        
    parts = path.parts
    for idx, part in enumerate(parts):
        if part in ("src", "tests"):
            rel_path = Path(*parts[idx:])
            return f"{rel_path}::{symbol_part}"
            
    return entity_id

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
            print(f"Index detected: {provider_name} with dimension {dimension}", file=sys.stderr)

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
    from knowcode.storage.vector_store import VectorStore
    vs = VectorStore(dimension=dimension)
    vs.load(index_path / "vectors")
    
    # Setup provider
    # ... (rest of provider setup)
    if provider_name == "voyageai":
        if not os.environ.get("VOYAGE_API_KEY_1") and not os.environ.get("VOYAGE_API_KEY"):
            return {"error": "VOYAGE_API_KEY_1 or VOYAGE_API_KEY not set"}
        provider = VoyageAIEmbeddingProvider(EmbeddingConfig(provider="voyageai", dimension=dimension))
    else:
        if not os.environ.get("OPENAI_API_KEY"):
            return {"error": "OPENAI_API_KEY not set"}
        provider = OpenAIEmbeddingProvider(EmbeddingConfig(provider="openai", dimension=dimension))

    from knowcode.retrieval.hybrid_index import HybridIndex
    hybrid = HybridIndex(repo, vs)
    
    # Metrics
    hits_at_1 = 0
    hits_at_5 = 0
    hits_at_10 = 0
    mrr_sum = 0.0
    total_queries = len(ground_truth)
    
    print(f"Evaluating {total_queries} queries...", file=sys.stderr)

    per_query_results = []

    for item in ground_truth:
        query = item.get("query_text") or item.get("query")
        query_id = item.get("query_id")
        
        # Support both new/old expected format
        raw_expected = item.get("expected_entities") or item.get("expected_ids", [])
        if raw_expected and isinstance(raw_expected[0], dict):
            expected_ids = {normalize_entity_id(e["entity_id"]) for e in raw_expected}
        else:
            expected_ids = {normalize_entity_id(e) for e in raw_expected}
        
        if not query or not expected_ids:
            continue
            
        q_vec = provider.embed_single(query)
        results = hybrid.search(query, q_vec, limit=10)
        
        # Extract entity IDs from returned chunk IDs
        found_entity_ids = []
        for c, _ in results:
            cid = c.id
            if "::" in cid:
                parts = cid.rsplit("::", 1)
                if parts[1].isdigit():
                    cid = parts[0]
            found_entity_ids.append(normalize_entity_id(cid))
        
        # Recall@k
        q_mrr = 0.0
        q_prec_1 = 1.0 if found_entity_ids and found_entity_ids[0] in expected_ids else 0.0
        
        if any(fid in expected_ids for fid in found_entity_ids[:1]):
            hits_at_1 += 1
        if any(fid in expected_ids for fid in found_entity_ids[:5]):
            hits_at_5 += 1
        if any(fid in expected_ids for fid in found_entity_ids[:10]):
            hits_at_10 += 1
            
        # MRR
        rank = 0
        for i, fid in enumerate(found_entity_ids):
            if fid in expected_ids:
                rank = i + 1
                break
        if rank > 0:
            q_mrr = 1.0 / rank
            mrr_sum += q_mrr

        per_query_results.append({
            "query_id": query_id,
            "query": query,
            "mrr": round(q_mrr, 4),
            "precision_at_1": q_prec_1,
            "expected": list(expected_ids),
            "retrieved": found_entity_ids[:5]
        })

    aggregate_results = {
        "queries": total_queries,
        "precision_at_1": round(hits_at_1 / total_queries, 4) if total_queries else 0,
        "precision_at_5": round(hits_at_5 / total_queries, 4) if total_queries else 0,
        "recall_at_10": round(hits_at_10 / total_queries, 4) if total_queries else 0,
        "mrr": round(mrr_sum / total_queries, 4) if total_queries else 0,
        "results": per_query_results
    }

    return aggregate_results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evaluate retrieval quality.")
    parser.add_argument("ground_truth", type=str, help="Path to ground truth JSON file")
    parser.add_argument("index_dir", type=str, help="Path to semantic index directory")
    parser.add_argument("--threshold", type=float, default=None, help="Fail if mean MRR is below this threshold")
    
    args = parser.parse_args()
    
    gt_path = Path(args.ground_truth)
    idx_path = Path(args.index_dir)
    
    results = evaluate(gt_path, idx_path)
    
    if "error" in results:
        print(f"Error: {results['error']}", file=sys.stderr)
        sys.exit(1)
        
    print(json.dumps(results, indent=2))
    
    if args.threshold is not None:
        mean_mrr = results.get("mrr", 0.0)
        if mean_mrr < args.threshold:
            print(f"FAILED: Mean MRR {mean_mrr:.4f} is below threshold {args.threshold:.4f}", file=sys.stderr)
            sys.exit(1)
        else:
            print(f"PASSED: Mean MRR {mean_mrr:.4f} is above threshold {args.threshold:.4f}", file=sys.stderr)
