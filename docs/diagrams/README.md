# Diagrams

Draw.io sources for the system and workflow diagrams, with rendered SVG
exports committed alongside. Open a `.drawio` file with
[draw.io](https://app.diagrams.net/) (or the draw.io VS Code extension) to
edit; view the `.svg` directly for reading. Each caption states what the
diagram covers and where the canonical written documentation lives — the
narration that used to live in this file was redistributed into the
audience-organized docs, which are the sources of truth; the diagrams
illustrate them.

| Diagram | Shows | Canonical documentation |
|---|---|---|
| [`architecture_overview`](architecture_overview.svg) | The five-layer system map: user interfaces (CLI, REST API, MCP server), service layer (`KnowCodeService`), core processing pipelines (parsing, indexing, retrieval, synthesis), LLM agent, storage, and infrastructure/plugins — plus the separately deployed Agent Gateway | [engineering/architecture.md](../engineering/architecture.md) |
| [`seq_indexing`](seq_indexing.svg) | Indexing & analysis sequence: scan → parse → graph build → chunking → embeddings → staged generation publication; the optional file-watch loop | [internals/indexing-generations.md](../engineering/internals/indexing-generations.md) |
| [`seq_query_retrieval`](seq_query_retrieval.svg) | Query & retrieval sequence: entry point → service → classification → hybrid search → rerank → dependency expansion → context synthesis → response with sufficiency and freshness | [internals/retrieval-synthesis.md](../engineering/internals/retrieval-synthesis.md) |
| [`seq_mcp`](seq_mcp.svg) | MCP server interaction: agent → `retrieve_context_for_query` (minimal first) → sufficiency-gated escalation → focused follow-up tools | [mcp-contract.md](../mcp-contract.md) · [user/ide-integration.md](../user/ide-integration.md) |
| [`seq_file_watch`](seq_file_watch.svg) | Watch mode: filesystem event → monitor → watch queue → prepare/commit file-update transaction → background indexer → generation publication → server hot-swap | [internals/indexing-generations.md](../engineering/internals/indexing-generations.md#watch-mode) |
| [`seq_agent_gateway`](seq_agent_gateway.svg) | The Agent Gateway microservice (extracted to a separate repository) proxying the REST API behind an LLM tool-use loop | [user/rest-api.md](../user/rest-api.md) |

## Maintaining these diagrams

The `.drawio` XML is the editable source. When changing a component,
boundary, or sequence, update the diagram *and* the linked canonical doc in
the same change, then re-export the SVG:

```bash
drawio -x -f svg -o <name>.svg <name>.drawio   # draw.io CLI (brew install --cask drawio)
```

The release checklist's docs-drift audit covers this.
