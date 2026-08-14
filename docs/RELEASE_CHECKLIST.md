# KnowCode Release Checklist

**Goal:** Every release parses real code correctly, keeps one coherent
knowledge/chunk/vector generation under failures and concurrency, and holds its
security boundaries against hostile input — not only the MCP contract.

This checklist is the human-facing gate that sits on top of the automated
release gate (`tests/e2e/test_release_gate_*.py`) and the per-step contracts in
[`architecture/hardening-contracts.md`](architecture/hardening-contracts.md).

## 1. Gates are green

- [ ] `uv run pytest -q` — the full suite passes, including
      `tests/e2e/test_release_gate_pipeline.py`,
      `tests/e2e/test_release_gate_security.py`,
      `tests/e2e/test_release_gate_soak.py`, and
      `tests/e2e/test_release_gate_limitations.py` for **both** vector backends.
- [ ] `uv run ruff check .`, `uv run mypy src/`, and `uv run mkdocs build --strict` pass.
- [ ] The seeded soak is stable: run
      `uv run pytest tests/e2e/test_release_gate_soak.py` a few times (a red run
      prints its seed and op-log — reproduce with `KNOWCODE_GATE_SEED=<seed>`).
- [ ] No real embedding or LLM network call runs in the suite (the deterministic
      `DummyEmbeddingProvider` is the test-time default).

## 2. Local readiness (doctor)

- [ ] `uv run knowcode doctor` passes on a freshly built repository: **Index
      generation**, **Knowledge store**, and **Semantic index** are `pass`.
- [ ] `uv run knowcode doctor --mcp` verifies the MCP server exposes
      `retrieve_context_for_query` and honors `verbosity="minimal"`,
      `max_tokens=1500`, `limit_entities=1`.
- [ ] After a **watched** edit, doctor's **Freshness** check warns
      `store_stale_source_changed` (expected — see Known limitations) and a full
      `knowcode build` clears it.

## 3. Generation integrity and migration

- [ ] `uv run knowcode build .` publishes one generation under
      `knowcode_index/generations/<id>/` selected by `current.json`; its manifest
      counts (`entities`/`chunks`/`vectors`) and checksums agree.
- [ ] **Migration:** legacy v1/v2 artifacts (`chunks.db` without the embedding
      column; `vectors.json` at an old schema) fail closed with a `knowcode build`
      instruction rather than being adopted. The committed `knowcode_index/` is a
      legacy layout and must be rebuilt. Confirm doctor reports the rebuild need
      rather than a silent in-memory migration.
- [ ] A failed rebuild (e.g. no embedding provider) preserves the previous
      generation and exits non-zero; it never publishes a graph beside a stale or
      absent index.

## 4. MCP contract conformance

- [ ] **First tool:** `retrieve_context_for_query` is the default natural-language entry point.
- [ ] **Defaults:** the default payload does not exceed `max_tokens=1500` or `limit_entities=1`.
- [ ] **Minimal projection:** `verbosity="minimal"` strips evidence arrays and internal metadata.
- [ ] **Local answer gate:** `sufficiency_threshold` (default 0.8) is respected and
      `context_text`/`sufficiency_score` are populated for agent routing.

## 5. Security boundaries

- [ ] **Prompt hierarchy:** retrieved repository content (including a hostile
      comment) travels only in the untrusted-data channel; KnowCode's task
      instructions occupy the provider system channel. Pinned by the security gate.
- [ ] **Telemetry privacy:** raw queries, prompt bodies, entity ids, and
      secret-shaped strings are not persisted; telemetry is local, `0600`, bounded,
      and removed by `knowcode telemetry clear`. Raw capture is opt-in via
      `KNOWCODE_TELEMETRY_RAW=1`.
- [ ] **Rate limiting / proxy trust:** the direct server sets `proxy_headers=False`
      and `forwarded_allow_ips=[]`; a spoofed `X-Forwarded-For`/`Forwarded` cannot
      rotate the bucket. Pinned by `tests/integration/test_rate_limit_server.py`.

## 6. Known limitations (must remain documented, not silently changed)

- [ ] Directory-level watch events are not expanded into their files; a rebuild
      re-keys the subtree.
- [ ] A watched edit refreshes retrieval (chunks/vectors) but not the knowledge
      graph until a rebuild; doctor warns.
- [ ] An empty LanceDB index is never written as a loadable artifact (the caller
      guard keeps the backend residual unreachable).

These are pinned by `tests/e2e/test_release_gate_limitations.py`. If a release
*fixes* one, update that test and this list rather than deleting the assertion.

## 7. Documentation & examples

- [ ] `docs/mcp-contract.md` reflects any schema changes.
- [ ] `docs/architecture/hardening-contracts.md` and this checklist match the
      implemented behavior.
- [ ] Configuration examples (`aimodels.yaml`) reflect any new environment
      variables or supported model families.
