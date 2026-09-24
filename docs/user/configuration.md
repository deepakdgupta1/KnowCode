# Configuration

Every knob a KnowCode user can turn: model selection, thresholds, and
environment variables. Malformed configuration **raises** rather than
falling back to silent defaults — if the server or MCP server starts, the
config it loaded is the config you wrote.

## `aimodels.yaml`

KnowCode loads configuration in this order:

1. Path passed via `--config`
2. `aimodels.yaml` in the current directory
3. `~/.aimodels.yaml`
4. Built-in defaults

### Model sections

```yaml
# Chat/completion models for `knowcode ask` (tried in order with failover)
natural_language_models:
  - name: glm-5
    provider: z-ai            # or: google, openrouter, mistralai, openai
    api_key_env: LITELLM_MASTER_KEY  # env var holding the key its requests send
    rpm_free_tier_limit: 99999   # client-side rate limiter, requests/min
    rpd_free_tier_limit: 99999   # client-side rate limiter, requests/day

# Embedding models for the semantic index
embedding_models:
  - name: voyage-code-3
    provider: voyageai
    api_key_env: LITELLM_MASTER_KEY
    tokens_free_tier_limit: 200000000

# Reranking calls VoyageAI directly, so it names the provider's own key
reranking_models:
  - name: rerank-2.5
    provider: voyageai
    api_key_env: VOYAGE_API_KEY_1

# Evaluation models use the same shape: eval_models: [...]
```

`z-ai` chat and `voyageai` embedding traffic goes to the LiteLLM proxy, which
holds the provider keys itself. Those entries therefore name the proxy's key,
not the provider's, and the proxy rejects a provider key with
`400 No connected db`. Reranking still calls VoyageAI directly
([BL-44](../engineering/backlog.md)), so its entries name `VOYAGE_API_KEY_1`.

An entry that names no key reads the one its route accepts. Chat and embedding
entries for `z-ai`, `glm` and `voyageai` read `LITELLM_MASTER_KEY`, `openai`
reads `OPENAI_API_KEY`, `openrouter` and `mistralai` read `OPENROUTER_API_KEY`,
and `google` reads `GOOGLE_API_KEY`. Reranking entries read `VOYAGE_API_KEY_1`.
Only entries whose traffic goes to the proxy default to the proxy's key, so it
is never sent to another host.

The rate-limiter fields keep `ask` inside your provider's free tier; when a
provider reports resource exhaustion, KnowCode fails over to the next
configured model. `prose_embedding_models` is also a recognized top-level
section and uses the same model-entry shape as `embedding_models`. The legacy
`models:` top-level key is still accepted as a synonym for
`natural_language_models`.

### `config` section — retrieval behavior

| Key | Default | Meaning |
|---|---|---|
| `sufficiency_threshold` | `0.8` | Retrieved context with a sufficiency score at or above this is considered complete enough to answer from locally. Lower → more local (cheaper) answers; higher → more LLM escalation. |
| `local_answer_task_types` | `[]` | Task types allowed to be answered without an LLM. **Force-cleared on every load** — it can only be repopulated from a checksum-pinned, machine-verified policy artifact (`KNOWCODE_ROUTING_POLICY_ARTIFACT` + `KNOWCODE_ROUTING_POLICY_SHA256`). Any mismatch keeps local answering off. |
| `routing_quality_floor` | `0.9` | Hard lower bound on the effective local-answer threshold: `max(sufficiency_threshold, routing_quality_floor)`. |
| `hybrid_alpha` | `0.2` | Sparse/dense blend in hybrid search. `0.2` = 80% BM25 lexical / 20% vector similarity. Raise toward `1.0` to favor semantic similarity over exact-identifier matching. |
| `reranker_top_k_multiplier` | `5` | The cross-encoder reranks the top `limit × multiplier` fused candidates. |
| `vector_backend` | `lancedb` | Vector store backend: `lancedb` or `faiss`. |
| `entity_source` | `disk` | Where `entities.source_code` is served from. `disk` reads it from the working tree, verifying it against the stored content hash and failing closed on drift; `stored` serves the persisted copy, which may be stale. |

Unknown keys in `config` warn (or raise in strict server/MCP mode). Values
are validated at load: `sufficiency_threshold`, `routing_quality_floor`, and
`hybrid_alpha` must lie in [0, 1], and `reranker_top_k_multiplier` must lie in
[1, 100]. Unknown top-level keys are treated exactly the same way.

### `preflight` section

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Run the preflight report during `build`. |
| `min_score` | `0.0` | Optional gate: `knowcode preflight` exits non-zero below this score. `0.0` = no gate. |
| `weights` | built-in | Override the 10 dimension weights (must sum to 1.0); only the known dimension keys are accepted. |

## Environment variables

Model entries name the variable they read via `api_key_env`; the shipped
defaults use:

| Variable | Used for |
|---|---|
| `LITELLM_MASTER_KEY` | Key the LiteLLM proxy authenticates callers with, sent by the default chat model (`z-ai`) for `ask` and by voyage-code-3 embeddings |
| `GLM_BASE_URL` | Optional address override for the LiteLLM proxy that GLM/z-ai chat traffic targets (default `http://127.0.0.1:4000`). The entry's key is sent there, so it must name a LiteLLM proxy. |
| `VOYAGE_API_KEY_1` | rerank-2.5 cross-encoder, which still calls VoyageAI directly ([BL-44](../engineering/backlog.md)) |
| `VOYAGE_BASE_URL` | Optional address override for the LiteLLM proxy that voyage embeddings target (default `http://127.0.0.1:4000`). The entry's key is sent there, so it must name a LiteLLM proxy. |
| `GOOGLE_API_KEY` | Optional Gemini chat models |
| `KNOWCODE_ROUTING_POLICY_ARTIFACT` / `KNOWCODE_ROUTING_POLICY_SHA256` | Path + expected SHA-256 of the machine-verified routing policy artifact (see [retrieval evals](../engineering/testing.md)) |
| `KNOWCODE_TELEMETRY_RAW` | `1` enables opt-in raw query capture — see [telemetry](telemetry.md#opt-in-raw-query-capture) |
| `KNOWCODE_TESTING` | Test-only: set by the test suite so telemetry writes run synchronously instead of on the background pool. Never set it in normal use. |

`.env.example` documents the API keys and base URLs and mentions
`KNOWCODE_TELEMETRY_RAW` and `KNOWCODE_TESTING` in comments; this page is the
canonical documentation of the `KNOWCODE_*` behavioral variables. It is a safe
template to copy to `.env` (gitignored).

## Optional extras

The core install is lightweight; commands that need more fail fast with an
install hint:

| Extra | Unlocks |
|---|---|
| `knowcode[server]` | `knowcode server` |
| `knowcode[search]` | `knowcode index`, `knowcode semantic-search` |
| `knowcode[llm]` | `knowcode ask` |
| `knowcode[watch]` | `knowcode server --watch` |
| `knowcode[mcp]` | `knowcode mcp-server` |
| `knowcode[voyageai]` | VoyageAI embeddings + reranking |
| `knowcode[all]` | Union of all of the above |

`knowcode install` applies the full set at once.

## Tuning guidance

The evidence-based way to tune `sufficiency_threshold` is the telemetry
feedback loop: `knowcode telemetry show` reports the local routing rate,
average sufficiency, and misses. If the local rate is low but local answers
are good, lower the threshold; if local answers miss, raise it. Details in
[Telemetry & Privacy](telemetry.md#threshold-tuning).
