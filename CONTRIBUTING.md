# Contributing to KnowCode

Setup, verification commands, and where the maintainer documentation lives.
User-facing docs start at [docs/user/getting-started.md](docs/user/getting-started.md);
the product view at [docs/product/overview.md](docs/product/overview.md).

## Development environment

Python **3.10–3.12** (tree-sitter-languages wheels end at cp312), managed
with [uv](https://docs.astral.sh/uv/):

```bash
uv venv
source .venv/bin/activate            # On Windows: .venv\Scripts\activate
uv sync --dev --extra all --extra mcp --extra voyageai
```

## Verification

Run before opening a PR. CI (`.github/workflows/ci-cd.yml`) runs:
`uv run ruff check .`, `uv run mypy src` (strict), `uv run pytest`,
`uv run mkdocs build` (deliberately **not** `--strict` — docs link into
`src/`), and `uv run python scripts/check_doc_links.py` (every relative
docs link must resolve). There is no `ruff format` gate; match the
existing style instead.

Conventions:

- Docstrings are Google-style; CI auto-generates missing ones for changed
  files (`.github/workflows/ai-docs-enforcer.yml`), but writing them
  yourself produces better results.
- Conventional commits — `CHANGELOG.md` is intentionally kept out of
  version control; generate a local entry draft with
  `uv run python scripts/generate_changelog.py` and curate it by hand.
- New persisted artifacts or protocol changes must respect the fail-closed
  versioning policy ([ADR 7](docs/engineering/adr/adr-0007-protocol-and-artifact-evolution-inventory.md)).

## Where things are documented

Docs are organized by audience; each topic has exactly one canonical home —
link to it rather than restating it:

| Topic | Canonical home |
|---|---|
| Architecture (current state) | [docs/engineering/architecture.md](docs/engineering/architecture.md) |
| Decisions | [docs/engineering/adr/](docs/engineering/adr/index.md) |
| Subsystem internals | [docs/engineering/internals/](docs/engineering/internals/indexing-generations.md) |
| Parser construct coverage | [docs/engineering/parser-matrix.md](docs/engineering/parser-matrix.md) |
| Testing & evaluation | [docs/engineering/testing.md](docs/engineering/testing.md) |
| CLI surface (commands/flags/defaults) | [docs/user/cli-reference.md](docs/user/cli-reference.md) |
| Configuration & env vars | [docs/user/configuration.md](docs/user/configuration.md) |
| Agent retrieval policy | [docs/mcp-contract.md](docs/mcp-contract.md) |
| Forward plan | [docs/roadmap.md](docs/roadmap.md) |

Superseded docs move to `docs/archive/` (excluded from the site build) —
never edit archived documents.

## Release process

Automated e2e gates plus the human checklist at
[docs/engineering/release.md](docs/engineering/release.md).
