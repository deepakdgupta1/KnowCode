# Testing & Evaluation

How KnowCode is tested and how retrieval quality is gated. Layout mirrors
`src/knowcode/` so a module's tests are where you expect them.

## Layout

| Directory | What |
|---|---|
| `tests/unit/` | Per-module tests mirroring the package layout (analysis, api, cli, indexing, llm, mcp, models, parsers, retrieval, scripts, service, storage, utils) |
| `tests/integration/` | Full flows: generation hot-swap, incremental indexing, golden queries, rate-limited server, telemetry privacy, preflight e2e |
| `tests/e2e/` | Release gates: pipeline, security, soak, limitations, IDE integration |
| `tests/helpers/` | Shared assertions: adversarial repo, graph gates, parser/vector contract assertions |
| `tests/fixtures/` | Parser fixtures (fixture gates enforce the [parser matrix](parser-matrix.md)) |
| `tests/test_mcp_workflow.md` | Manual MCP test plan with expected sufficiency scores |
| `tests/eval/` | Marker registered; the live harness lives externally (below) |

## Running

```bash
pytest                                  # whole suite (conftest sets KNOWCODE_TESTING=1)
pytest tests/unit/retrieval             # one area
pytest -m eval                          # eval-marked tests
mypy src/                               # strict
ruff check src/ && ruff format src/     # lint/format
```

Golden-query gates (`tests/integration/test_golden_queries.py`): three
natural-language queries must return the right file as the top hit using
deterministic dummy embeddings (BM25 does the work). Baseline handling:
`--save-baseline` / `--allow-drift` control the golden-SHA guard.

Vector-contract suite (`tests/unit/storage/test_vector_contract.py`,
parametrized over FAISS, numpy, LanceDB) is the executable form of the
storage contract on [storage formats](internals/storage-formats.md).

## Retrieval quality evaluation (external)

The effectiveness harness lives in the separate
[`knowcode-evals`](https://github.com/deepakdgupta1/knowcode-evals)
repository — benchmark data, judge orchestration, external downloads,
statistical gates, and generated evidence stay out of the product package.
KnowCode owns only the runtime contract:

1. Local answering is disabled unless a policy artifact is explicitly
   configured.
2. The artifact must use the supported schema and target an exact KnowCode
   version and Git revision.
3. The caller must provide the artifact's trusted SHA-256 separately.
4. KnowCode verifies locked-holdout identity, external dataset identities,
   judge identities, metric floors, canary evidence, and cited source
   hashes.
5. Any missing, malformed, or drifted evidence fails closed to the LLM path.

**Consuming a blessed policy.** From the trusted evaluator CI run for the
exact revision being deployed, obtain `machine-verification.json` and
`machine-verification.sha256`, then:

```bash
export KNOWCODE_ROUTING_POLICY_ARTIFACT=/secure/path/machine-verification.json
export KNOWCODE_ROUTING_POLICY_SHA256=<trusted-sha256>
```

Never compute the trusted digest from an artifact received through an
untrusted channel — the digest is the independent authenticity pin.

**Current status.** The consumer boundary and fail-closed enforcement are
implemented; P1 is not yet complete (the 60-case corpus is
calibration-only; no blessed policy exists), so the default local task
allowlist remains empty — every `ask` escalates to the LLM. Gate criteria
and thresholds live in the [roadmap](../roadmap.md) (P1).

## Release gates

The human checklist on top of the automated gates is
[release.md](release.md).
