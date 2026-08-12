# KnowCode Release Checklist

**Goal:** Ensure every release of KnowCode complies with the canonical MCP contract and guarantees safe, consistent retrieval behavior for IDE agents.

Before cutting a release, verify the following conformance criteria:

## 1. Local Readiness (Doctor)
- [ ] Run `uv run knowcode doctor --mcp` and verify all checks pass.
- [ ] `doctor --mcp` correctly verifies that the MCP server exposes the `retrieve_context_for_query` tool.
- [ ] `doctor --mcp` tests the fallback/default behavior using `verbosity="minimal"`, `max_tokens=1500`, and `limit_entities=1`.

## 2. MCP Contract Conformance
- [ ] **First Tool:** `retrieve_context_for_query` is the default natural-language entry point.
- [ ] **Defaults:** The default payload does not exceed `max_tokens=1500` or `limit_entities=1`.
- [ ] **Minimal Projection:** `verbosity="minimal"` strips out evidence arrays and internal metadata to preserve context window.
- [ ] **Local Answer Gate:** The `sufficiency_threshold` (default 0.8) is respected, and responses correctly populate `context_text` and `sufficiency_score` fields for agent routing.

## 3. Freshness and Safety
- [ ] Run the test suite: `uv run pytest`. All tests must pass, especially the agent retrieval integration contract tests.
- [ ] Run the indexer: `uv run knowcode build .` on a sample repository.
- [ ] Run the file watcher/scanner to ensure it captures new files and modifications.

## 4. Documentation & Examples
- [ ] `docs/mcp-contract.md` is up to date with any schema changes.
- [ ] Configuration examples (`aimodels.yaml`) correctly reflect any new environment variables or supported model families.
