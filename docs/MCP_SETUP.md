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

Run the local readiness check before wiring the client:

```bash
uv run knowcode analyze . --output .
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
uv run knowcode analyze . --output .
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

### Context Still Too Small

Follow the ladder in [mcp-contract.md](mcp-contract.md): increase breadth while
staying in `minimal`, then use `standard` for implementation detail, then
`verbose` for evidence. Avoid making `diagnostic` the default.

## Security Notes

- The MCP server runs locally over stdio.
- `knowcode_knowledge.json` and `knowcode_index/` contain repository-derived data.
- Embedding providers may receive text during indexing, depending on your
  configured provider.
- Store API keys in environment variables, not committed files.

## References

- [MCP retrieval contract](mcp-contract.md)
- [Documentation MCP section](index.md#mcp-server)
- [MCP token overhead notes](MCP_TOKEN_OVERHEAD_REDUCTION.md)
