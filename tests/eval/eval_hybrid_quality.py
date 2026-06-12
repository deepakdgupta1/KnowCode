"""Evaluation script for hybrid retrieval quality and alpha parameter sweep."""

import sys
import math
import argparse
import json
from pathlib import Path
from typing import Any, Optional

# Ensure project root is in python path
sys.path.append(str(Path(__file__).resolve().parents[2]))

from knowcode.service import KnowCodeService
from tests.eval.harness import scorer


def compute_ndcg_at_k(retrieved: list[str], expected: set[str], k: int) -> float:
    """Compute Normalized Discounted Cumulative Gain (nDCG) at K."""
    if k <= 0 or not retrieved or not expected:
        return 0.0
    dcg = 0.0
    for idx, entity_id in enumerate(retrieved[:k]):
        if entity_id in expected:
            dcg += 1.0 / math.log2(idx + 2)
            
    idcg = 0.0
    for idx in range(min(k, len(expected))):
        idcg += 1.0 / math.log2(idx + 2)
        
    if idcg == 0.0:
        return 0.0
    return dcg / idcg


def evaluate_configuration(
    service: KnowCodeService,
    records: list[dict[str, Any]],
    alpha: float,
    use_reranker: bool,
    use_voyage_rerank: bool,
    query_embeddings_cache: dict[str, list[float]],
    rerank_cache: dict[tuple[str, str], float],
) -> dict[str, float]:
    """Evaluate retrieval metrics for a specific configuration across all records."""
    # 1. Update alpha in AppConfig
    service.app_config.hybrid_alpha = alpha
    
    # 2. Patch embedding provider to use cache
    search_engine = service.get_search_engine()
    search_engine.hybrid_index.alpha = alpha
    
    original_embed_single = search_engine.embedding_provider.embed_single
    search_engine.embedding_provider.embed_single = lambda q: query_embeddings_cache[q]
    
    # 3. Patch VoyageAI rerank if needed
    original_voyage_rerank = None
    if search_engine.reranker.voyage_client:
        original_voyage_rerank = search_engine.reranker.voyage_client.rerank
        
        def cached_rerank(query: str, documents: list[str], model: str = "rerank-2.5", top_k: Optional[int] = None) -> list[dict[str, Any]]:
            uncached = [doc for doc in documents if (query, doc) not in rerank_cache]
            if uncached and original_voyage_rerank:
                try:
                    real_results = original_voyage_rerank(
                        query=query,
                        documents=uncached,
                        model=model,
                        top_k=len(uncached)
                    )
                    for r in real_results:
                        doc = r["document"]
                        score = r["relevance_score"]
                        rerank_cache[(query, doc)] = score
                except Exception as e:
                    print(f"Reranker API call failed during caching: {e}")
                    raise e
                    
            results = []
            for idx, doc in enumerate(documents):
                score = rerank_cache.get((query, doc), 0.0)
                results.append({
                    "index": idx,
                    "relevance_score": score,
                    "document": doc
                })
            
            results.sort(key=lambda x: x["relevance_score"], reverse=True)
            if top_k:
                results = results[:top_k]
            return results
            
        search_engine.reranker.voyage_client.rerank = cached_rerank

    # 4. Temporarily override reranker choice if use_reranker is False
    original_rerank_method = search_engine.reranker.rerank
    if not use_reranker:
        search_engine.reranker.rerank = lambda query, chunks, **kwargs: chunks[:kwargs.get("top_k", len(chunks))]
    elif not use_voyage_rerank:
        # Disable Voyage client temporarily to force local signal-based fallback
        orig_client = search_engine.reranker.voyage_client
        search_engine.reranker.voyage_client = None

    scores = []
    try:
        for record in records:
            query = record["query_text"]
            # Call retrieval
            retrieval = service.retrieve_context_for_query(
                query=query,
                limit_entities=10,
                max_tokens=8000,
                verbosity="minimal",
                include_metadata=True,
            )
            
            # Compute basic metrics using scorer
            score_record = scorer.score_record(golden=record, retrieval_result=retrieval)
            
            # Parse expected entities
            raw_expected = record.get("expected_entities", [])
            expected_entity_ids = set()
            if raw_expected and isinstance(raw_expected[0], dict):
                expected_entity_ids = {scorer.normalize_entity_id(e["entity_id"]) for e in raw_expected}
            else:
                expected_entity_ids = {scorer.normalize_entity_id(e) for e in raw_expected}
                
            # Parse retrieved entities
            retrieved_entities = [
                scorer.normalize_entity_id(e["entity_id"])
                for e in retrieval.get("selected_entities", [])
                if "entity_id" in e
            ]
            
            # Compute recall@50
            rec50 = scorer.recall_at_k(retrieved_entities, expected_entity_ids, k=50)
            # Compute ndcg@10
            ndcg10 = compute_ndcg_at_k(retrieved_entities, expected_entity_ids, k=10)
            
            score_record["recall_at_50"] = rec50
            score_record["ndcg_at_10"] = ndcg10
            
            scores.append(score_record)
    finally:
        # Restore patched methods
        search_engine.embedding_provider.embed_single = original_embed_single
        if original_voyage_rerank and search_engine.reranker.voyage_client:
            search_engine.reranker.voyage_client.rerank = original_voyage_rerank
        if not use_reranker:
            search_engine.reranker.rerank = original_rerank_method
        elif not use_voyage_rerank:
            search_engine.reranker.voyage_client = orig_client

    # 5. Aggregate metrics
    n = len(scores)
    if n == 0:
        return {}
        
    return {
        "mrr": sum(s["mrr"] for s in scores) / n,
        "recall_at_10": sum(s["recall_at_10"] for s in scores) / n,
        "recall_at_50": sum(s["recall_at_50"] for s in scores) / n,
        "precision_at_1": sum(s["precision_at_1"] for s in scores) / n,
        "precision_at_5": sum(s["precision_at_5"] for s in scores) / n,
        "ndcg_at_10": sum(s["ndcg_at_10"] for s in scores) / n,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate hybrid retrieval quality.")
    parser.add_argument("--sweep", action="store_true", help="Perform alpha parameter sweep")
    parser.add_argument("--alpha", type=float, default=0.5, help="Alpha parameter value")
    args = parser.parse_args()

    eval_dir = Path(__file__).resolve().parent
    golden_path = eval_dir / "golden" / "golden_v1.0.json"
    
    if not golden_path.exists():
        print(f"Error: Golden dataset not found at {golden_path}")
        sys.exit(1)
        
    with open(golden_path, "r", encoding="utf-8") as f:
        records = json.load(f)
        
    print(f"Loaded {len(records)} golden records from {golden_path}")
    
    # Initialize service
    service = KnowCodeService()
    
    # Check if index exists
    try:
        service._assert_index_exists()
    except Exception as e:
        print(f"Error: index does not exist. Please run 'knowcode build' or index first. Detail: {e}")
        sys.exit(1)
        
    # Pre-embed queries using batch embedding to save time and API costs
    print("Pre-embedding queries in batch...")
    queries = [r["query_text"] for r in records]
    provider = service.get_indexer().embedding_provider
    embeddings = [provider.embed_single(q) for q in queries]
    
    query_embeddings_cache = {}
    for q, emb in zip(queries, embeddings):
        query_embeddings_cache[q] = emb
        
    rerank_cache = {}

    if args.sweep:
        print("\n=== Alpha Parameter Sweep (0.0 to 1.0) ===")
        print(f"{'Alpha':<6} | {'MRR':<8} | {'Recall@10':<10} | {'Recall@50':<10} | {'P@1':<8} | {'P@5':<8} | {'nDCG@10':<8}")
        print("-" * 75)
        
        alphas = [0.0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0]
        results = []
        for alpha in alphas:
            metrics = evaluate_configuration(
                service=service,
                records=records,
                alpha=alpha,
                use_reranker=False,
                use_voyage_rerank=False,
                query_embeddings_cache=query_embeddings_cache,
                rerank_cache=rerank_cache,
            )
            print(
                f"{alpha:<6.1f} | "
                f"{metrics['mrr']:<8.4f} | "
                f"{metrics['recall_at_10']:<10.4f} | "
                f"{metrics['recall_at_50']:<10.4f} | "
                f"{metrics['precision_at_1']:<8.4f} | "
                f"{metrics['precision_at_5']:<8.4f} | "
                f"{metrics['ndcg_at_10']:<8.4f}"
            )
            results.append((alpha, metrics))
            
        # Select optimal alpha based on nDCG@10
        optimal_alpha, optimal_metrics = max(results, key=lambda x: x[1]["ndcg_at_10"])
        print(f"\nOptimal alpha based on nDCG@10: {optimal_alpha} (nDCG@10 = {optimal_metrics['ndcg_at_10']:.4f})")
    else:
        # Run standard configurations
        print("\n=== Ablation Modes Comparison ===")
        print(f"{'Configuration':<35} | {'MRR':<8} | {'Recall@10':<10} | {'Recall@50':<10} | {'nDCG@10':<8}")
        print("-" * 80)
        
        modes = [
            ("BM25-only (alpha=0.0, no rerank)", 0.0, False, False),
            ("Dense-only (alpha=1.0, no rerank)", 1.0, False, False),
            (f"Hybrid (alpha={args.alpha}, no rerank)", args.alpha, False, False),
            ("Hybrid + signal rerank", args.alpha, True, False),
            ("Hybrid + VoyageAI rerank", args.alpha, True, True),
        ]
        
        for name, alpha, use_reranker, use_voyage_rerank in modes:
            metrics = evaluate_configuration(
                service=service,
                records=records,
                alpha=alpha,
                use_reranker=use_reranker,
                use_voyage_rerank=use_voyage_rerank,
                query_embeddings_cache=query_embeddings_cache,
                rerank_cache=rerank_cache,
            )
            print(
                f"{name:<35} | "
                f"{metrics['mrr']:<8.4f} | "
                f"{metrics['recall_at_10']:<10.4f} | "
                f"{metrics['recall_at_50']:<10.4f} | "
                f"{metrics['ndcg_at_10']:<8.4f}"
            )


if __name__ == "__main__":
    main()
