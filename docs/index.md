# KnowCode

[![CI/CD Pipeline](https://github.com/deepakdgupta1/KnowCode/actions/workflows/ci-cd.yml/badge.svg)](https://github.com/deepakdgupta1/KnowCode/actions/workflows/ci-cd.yml)

KnowCode is a local-first codebase intelligence tool. It parses a codebase
into a semantic knowledge graph of entities (functions, classes, modules) and
relationships (calls, imports, dependencies), indexes the chunks with hybrid
BM25 + vector search, and serves token-efficient context bundles to AI coding
agents through a CLI, a local REST server, and an MCP server.

The problem it solves is the LLM context window: instead of stuffing whole
files into a prompt, an agent asks KnowCode for the smallest useful context
first, and only escalates to an expensive LLM when local context is provably
insufficient. Retrieval is 100% local and deterministic; LLMs are only needed
for optional features (embeddings, reranking, Q&A).

## Where to start

| I am a… | Start here |
|---|---|
| **User** — installing and using KnowCode on a codebase | [Getting started](user/getting-started.md) · [CLI reference](user/cli-reference.md) |
| **Product manager** — use-cases, personas, business logic | [Product overview](product/overview.md) · [Personas & use-cases](product/personas-use-cases.md) · [Business logic](product/business-logic.md) |
| **Engineer** — maintaining or extending KnowCode itself | [Architecture](engineering/architecture.md) · [Hardening contracts (ADRs)](engineering/adr/index.md) · [Parser construct matrix](engineering/parser-matrix.md) |

## Quick start

```bash
knowcode build .                  # knowledge base + semantic index, one atomic step
knowcode doctor --store . --mcp   # verify readiness, including an MCP handshake
knowcode query search "MyClass"
knowcode ask "How does the graph builder work?"
```

The full command surface (16 commands, flags, and defaults) is the
[CLI reference](user/cli-reference.md). The single forward plan is the
[Roadmap](roadmap.md); the canonical agent retrieval policy is the
[MCP contract](mcp-contract.md).

## How these docs are organized

- **User documentation** — command reference, configuration, IDE and REST
  integration, telemetry and privacy.
- **Product documentation** — overview, personas and use-cases, and the
  business logic behind every user-visible heuristic, with trade-offs.
- **Engineering documentation** — current-state architecture, architecture
  decision records, subsystem internals, testing and release guides.
- **Research** — exploratory designs and evaluation studies feeding the
  roadmap.
