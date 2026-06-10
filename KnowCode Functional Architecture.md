# Documentation

## KnowCode — What It Does

**KnowCode** is a **local-first code intelligence tool** that builds a queryable semantic knowledge graph from a codebase and exposes it to AI coding agents via CLI, REST API, and MCP protocol — enabling token-efficient context retrieval without calling an LLM.

### Context

KnowCode solves the "context window" problem for AI coding assistants. Instead of naively stuffing entire files into an LLM prompt, KnowCode pre-indexes the codebase into a structured graph and a semantic search index, then synthesizes only the relevant context bundle on demand — typically reducing token usage by ~10×.

It runs **entirely locally** (no cloud dependency for retrieval) and integrates with IDE agents (Antigravity, Claude Desktop, VS Code) via the **Model Context Protocol (MCP)**.

### Technical Pipeline

The system follows a **6-stage layered architecture**:

| Stage | Component | Method |
|---|---|---|
| **1. Scan** | [Scanner](file:///Users/deepg/Desktop/KnowCode/src/knowcode/indexing/scanner.py) | Discovers files with `.gitignore` support |
| **2. Parse** | [Parsers](file:///Users/deepg/Desktop/KnowCode/src/knowcode/parsers) | Python AST (`ast` module), Tree-sitter (JS/TS/Java/Rust/Vue), custom parsers (Markdown, YAML) |
| **3. Graph Build** | [GraphBuilder](file:///Users/deepg/Desktop/KnowCode/src/knowcode/indexing/graph_builder.py) | Constructs a semantic graph: entities (classes, functions, methods, modules) + relationships (calls, imports, contains, inherits) |
| **4. Knowledge Store** | [KnowledgeStore](file:///Users/deepg/Desktop/KnowCode/src/knowcode/storage/knowledge_store.py) | In-memory graph with JSON persistence; supports lexical search, caller/callee tracing, dependency expansion |
| **5. Indexing** | [Indexer](file:///Users/deepg/Desktop/KnowCode/src/knowcode/indexing/indexer.py) | Chunker → embedding (VoyageAI `voyage-3-lite` or OpenAI) → FAISS dense vector store + BM25 sparse token index |
| **6. Retrieval** | [SearchEngine](file:///Users/deepg/Desktop/KnowCode/src/knowcode/retrieval/search_engine.py) / [RetrievalOrchestrator](file:///Users/deepg/Desktop/KnowCode/src/knowcode/retrieval/orchestrator.py) | **Hybrid search** (BM25 + FAISS fused via **Reciprocal Rank Fusion** with k=60, α=0.5) → **cross-encoder reranking** (VoyageAI `rerank-2.5`) → **dependency expansion** from graph → **context synthesis** with token budget + priority ranking |

### Key Retrieval Mechanics

- **HybridIndex** ([hybrid_index.py](file:///Users/deepg/Desktop/KnowCode/src/knowcode/retrieval/hybrid_index.py)): Runs BM25 sparse and FAISS dense searches in parallel, merges with **Reciprocal Rank Fusion** (RRF). α=0.5 gives equal weight to sparse/dense.
- **Reranker** ([reranker.py](file:///Users/deepg/Desktop/KnowCode/src/knowcode/retrieval/reranker.py)): VoyageAI cross-encoder reranking on top-k fusion results.
- **Dependency expansion** ([completeness.py](file:///Users/deepg/Desktop/KnowCode/src/knowcode/retrieval/completeness.py)): Walks the knowledge graph 1 hop to attach callers/callees/imports to retrieved chunks.
- **Query classification** ([query_classifier.py](file:///Users/deepg/Desktop/KnowCode/src/knowcode/llm/query_classifier.py)): Classifies queries into task types (explain, debug, review, etc.) to select task-specific context templates.
- **Sufficiency scoring**: Each context bundle carries a `sufficiency_score` (0.0–1.0). If ≥ threshold (default 0.8), the agent answers locally without escalating to a larger LLM.

### Outputs

| Output | Format | Description |
|---|---|---|
| **Knowledge store** | `knowcode_knowledge.json` (~4.6 MB for this repo) | Serialized entity graph: entities + relationships + source locations |
| **Semantic index** | `knowcode_index/` dir (vectors, chunks.json, index_manifest.json) | FAISS vectors + chunk metadata + BM25 tokens + schema manifest |
| **Context bundle** | JSON dict | Markdown-formatted context with entity signature, source code, callers/callees, token count, truncation flag, sufficiency score |
| **Telemetry** | `knowcode_telemetry.jsonl` | Append-only JSONL with query performance, routing decisions, retrieval mode |
| **Exported docs** | Multi-level Markdown | Architecture overview, per-module pages, entity index, freshness manifest |