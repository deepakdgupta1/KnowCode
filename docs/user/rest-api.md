# REST API

KnowCode's local FastAPI server — the preferred surface for locally hosted
AI agents. Start it with `knowcode server` (requires the `server` extra;
`--watch` additionally requires `watch`):

```bash
knowcode server [--host 127.0.0.1] [--port 8000] [--store <path>] [--watch]
```

**Local-only by policy.** The server binds `127.0.0.1` by default and has no
authentication — expose it beyond loopback at your own risk. Proxy-header
processing is disabled (`proxy_headers=False`), so `X-Forwarded-For` cannot
be used to rotate rate-limit buckets; proxied deployment is not supported.

## Endpoints

### Standard tier — 60 requests/minute

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/health` | Liveness check |
| `GET /api/v1/stats` | Entity/relationship counts by kind |
| `GET /api/v1/search?q=<pattern>` | Lexical entity search |
| `GET /api/v1/context?target=<name>&task_type=<type>` | Context bundle for a named entity |
| `GET /api/v1/entities/{entity_id}` | Raw entity detail |
| `GET /api/v1/callers/{entity_id}` | Direct callers |
| `GET /api/v1/callees/{entity_id}` | Direct callees |
| `POST /api/v1/context/query` | Semantic query with full retrieval orchestration (same pipeline as `knowcode ask`) |
| `POST /api/v1/reload` | Reload the knowledge store from disk |
| `GET /api/v1/freshness` | Whether store/index are stale relative to sources |

### Expensive tier — 10 requests/minute

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/trace_calls/{entity_id}?direction=callers\|callees&depth=1-5` | Multi-hop BFS call-graph traversal |
| `GET /api/v1/impact/{entity_id}?max_depth=<n>` | Transitive deletion-impact analysis with risk score |

The machine-readable schema is served at `/openapi.json`.

## Semantics worth knowing

**Rate limiting** is IP-keyed (slowapi). In direct local mode every client
is `127.0.0.1` and shares one bucket — the tiers cap a runaway local agent,
which is the intended boundary.

**Watch mode.** With `--watch`, modified or created files are queued for
incremental re-indexing and deleted/renamed files have their chunks
invalidated and removed. Without it, re-run `knowcode build .` after code
changes.

**Freshness.** `GET /api/v1/freshness` compares artifact state against the
newest source change and reports `is_stale` with reasons
(`store_stale_source_changed`, `index_stale_source_changed`, …). Retrieval
responses carry the same block: stale results are flagged, not blocked.
Recovery: `knowcode build .`, or `POST /api/v1/reload` after rebuilding
artifacts out-of-band.

**Read-only by default.** Endpoints never trigger analysis or builds; they
serve what the current generation contains.

## Examples

```bash
# Semantic query through the orchestrated retrieval pipeline
curl -X POST http://127.0.0.1:8000/api/v1/context/query \
  -H "Content-Type: application/json" \
  -d '{"query": "Where is retrieval fusion implemented?", "task_type": "locate"}'

# Multi-hop call graph
curl "http://127.0.0.1:8000/api/v1/trace_calls/GraphBuilder.build_from_directory?direction=callers&depth=3"

# Deletion impact with risk score
curl http://127.0.0.1:8000/api/v1/impact/KnowledgeStore
```

For agent-facing usage patterns (which endpoints to expose as tools), see
[IDE & Agent Integration](ide-integration.md).
