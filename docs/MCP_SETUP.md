# KnowCode MCP Server Setup Guide

**Last Updated:** 2026-05-23

This guide shows how to connect an MCP-capable IDE or agent to KnowCode. The
agent operating policy is defined in [mcp-contract.md](mcp-contract.md); keep
thresholds, verbosity rules, and token budgets there so all agents behave the
same way.

## Overview

KnowCode MCP lets agents retrieve focused repository context before building a
large prompt. The low-token workflow is:

1. Agent receives a user query.
2. Agent calls `retrieve_context_for_query` with `verbosity="minimal"`.
3. KnowCode returns compact `context_text`, `sufficiency_score`, and token count.
4. Agent answers locally when the configured threshold is met.
5. Agent escalates budget or verbosity only when the minimal context is not enough.

## Prerequisites

- KnowCode installed in the repository environment.
- A generated knowledge store: `knowcode_knowledge.json`.
- A semantic index: `knowcode_index/`.
- An MCP-capable client such as Antigravity, Claude Desktop, VS Code, or another agent host.

Build the knowledge base and semantic index, then run the readiness check:

```bash
uv run knowcode build .
uv run knowcode doctor --store . --mcp
```


## MCP Client Configuration

Use an absolute repository path for `--store`. In a development checkout, using
`uv run` keeps the MCP server on the same environment as the project.

Generic MCP config:

```json
{
  "mcpServers": {
    "knowcode": {
      "command": "uv",
      "args": [
        "run",
        "knowcode",
        "mcp-server",
        "--store",
        "/absolute/path/to/repository"
      ]
    }
  }
}
```

If your client cannot run through `uv`, point `command` at the absolute
`knowcode` executable in the virtual environment:

```json
{
  "mcpServers": {
    "knowcode": {
      "command": "/absolute/path/to/repository/.venv/bin/knowcode",
      "args": ["mcp-server", "--store", "/absolute/path/to/repository"]
    }
  }
}
```

Restart the IDE or agent host after changing its MCP config.

## Agent Rule

Put only a pointer plus the compact rule in agent-specific config files:

```md
When repository context is needed, follow docs/mcp-contract.md.
Start with retrieve_context_for_query using verbosity=minimal and the smallest
budget that fits the task. Escalate to standard or verbose only when the minimal
context is insufficient. Use the configured sufficiency_threshold from
aimodels.yaml to decide whether to answer from local context.
```

The repo-level rule is `.agent/rules/context.md`.

## Tool Use

The MCP server exposes four tools:

- `retrieve_context_for_query`: primary natural-language retrieval path.
- `search_codebase`: find entities by known name or pattern.
- `get_entity_context`: fetch context for a specific known entity.
- `trace_calls`: inspect callers or callees for a specific entity.

Agents should call `retrieve_context_for_query` first for ordinary repository
questions. Use the other tools only for focused follow-up after retrieval has
identified the relevant entity or missing information.

## Verification

Run:

```bash
uv run knowcode doctor --store . --mcp
```

Then ask the agent a repository question such as:

```text
Where is the retrieval orchestration implemented?
```

Expected behavior:

1. The agent calls `retrieve_context_for_query`.
2. The first call uses `verbosity="minimal"`.
3. The agent answers from local context when `sufficiency_score` meets the configured threshold.
4. The agent escalates to `standard` or `verbose` only if the minimal context is insufficient.


## 📊 Success Metrics

After setup, you should see:

- ✅ 70%+ queries with `sufficiency_score >= 0.8`
- ✅ Faster responses for codebase questions
- ✅ 50%+ reduction in external LLM token usage
- ✅ Accurate answers from local context

## Troubleshooting

### MCP Server Not Available

Check that the MCP command path is valid, the client config uses an absolute
store path, and the IDE was restarted after editing the config.

```bash
uv run knowcode mcp-server --store .
```

### Knowledge Store Not Found

Run:

```bash
uv run knowcode build .
```


Then verify the MCP config `--store` points to the directory containing
`knowcode_knowledge.json`.

### Semantic Retrieval Fallback

If responses mention semantic retrieval fallback, rebuild the index and rerun
doctor:

```bash
uv run knowcode index . --output knowcode_index
uv run knowcode doctor --store .
```

Also confirm embedding API keys from `aimodels.yaml` are present in the agent
environment.

### Freshness Safety & Stale State Recovery

If retrieval results include a `freshness` block stating that `is_stale` is true, the local knowledge store or index does not match the current state of the source tree.

To recover:
1. Re-run analysis to update the knowledge store:
   ```bash
   uv run knowcode analyze . --output .
   ```
2. Re-index the codebase to update the vector index:
   ```bash
   uv run knowcode index . --output knowcode_index
   ```
3. If running the FastAPI intelligence server, trigger reload:
   ```bash
   curl -X POST http://127.0.0.1:8000/api/v1/reload
   ```

### Context Still Too Small


Follow the ladder in [mcp-contract.md](mcp-contract.md): increase breadth while
staying in `minimal`, then use `standard` for implementation detail, then
`verbose` for evidence. Avoid making `diagnostic` the default.

## Supported File Extensions

The MCP server and indexer discover and parse files with the following extensions:
`.py`, `.js`, `.ts`, `.jsx`, `.tsx`, `.java`, `.rs`, `.vue`, `.md`, `.yaml`, `.yml`.

Files with other extensions are ignored during scans. If your project relies heavily on unsupported languages, those files will not be present in the semantic index or knowledge store.

## Security Notes


- The MCP server runs locally over stdio.
- `knowcode_knowledge.json` and `knowcode_index/` contain repository-derived data.
- Embedding providers may receive text during indexing, depending on your
  configured provider.
- Store API keys in environment variables, not committed files.


## 🎓 Key Concepts

**Sufficiency Score**: Confidence that retrieved context is enough to answer the query
- `>= sufficiency_threshold` (default 0.8) → Answer locally
- `< sufficiency_threshold` → Escalate or use external LLM

**Retrieval Modes**:
- **Semantic**: Uses embeddings + vector search (better)
- **Lexical**: Uses keyword matching (fallback)

**Dependency Expansion**: Includes related code (callees, callers) for complete context

## ⚡ Performance Tips

1. **Build semantic index** - Much better than lexical
2. **Keep knowledge store updated** - Re-analyze after major changes
3. **Tune parameters** - Adjust `max_tokens` and `limit_entities` following the verbosity ladder
4. **Monitor scores** - Track `sufficiency_score` distribution


## References

- [MCP retrieval contract](mcp-contract.md)
- [Documentation MCP section](index.md#mcp-server)
- [MCP token overhead notes](mcp-contract.md#appendix-token-overhead-reduction-strategies)
