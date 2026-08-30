# Telemetry & Privacy

KnowCode writes a small, local, aggregate-only telemetry log so you can tune
retrieval thresholds with evidence instead of guesses. It is not analytics: it
is never transmitted anywhere, it does not contain your questions or your code,
and one command deletes it.

## What is recorded

Telemetry is an append-only JSON Lines file, `knowcode_telemetry.jsonl`, in the
**store root** — the directory you pass to `--store`, normally your project
root. It is never written inside `knowcode_index/`, because index generations
are immutable and are retired on a schedule you do not control.

Every record carries `telemetry_schema_version`, `timestamp`, and `event_type`.
Each event type has a fixed field allowlist defined in
`src/knowcode/telemetry_policy.py`; a field outside it is dropped before the
record is written, and an event type outside it is not written at all.

### `query` — one per question

This is the only event type counted as a query. One question produces exactly
one, no matter how many retrieval attempts it takes or whether it arrived
through the CLI, the API, or MCP.

| Field | Meaning |
| --- | --- |
| `query_id` | Keyed correlation id (see below) — not the question |
| `query_chars`, `query_length_bucket` | Length of the question |
| `entry_point` | `agent`, `service`, `mcp`, or `orchestrator` |
| `task_type`, `task_confidence` | Classification of the question |
| `retrieval_mode` | `semantic`, `lexical`, `exact`, or `none` |
| `verbosity`, `max_tokens`, `total_tokens`, `truncated` | Requested and produced context size |
| `sufficiency_score`, `sufficiency_bucket` | Context quality |
| `local_or_escalated`, `is_stale` | Routing outcome and index freshness |
| `selected_entity_count`, `evidence_count`, `error_count` | Result shape — counts, never ids |
| `retrievals`, `duration_ms`, `outcome` | Attempts, latency, and `ok`/`error` |
| `user_marked_miss` | Operator flag marking the answer as a miss; `telemetry show` totals these as "User-marked misses" |

### `agent_decision`, `tool_call`, `reranker_latency`

These describe a query that has *already* been counted, so they are excluded
from `total_queries`. `agent_decision` records local-versus-LLM routing and the
thresholds that produced it. `tool_call` records which MCP tool ran, how many
arguments it received, and whether it succeeded — never the arguments
themselves, which are client-supplied and routinely contain pasted code.
`reranker_latency` records reranking method and latency.

## What is deliberately not recorded

Query text, retrieved code, prompt bodies, MCP tool arguments, and entity ids
(which are absolute source paths). None of these is representable in the
schema, so no future call site can add one without editing the allowlist.

As defense in depth, every value that *does* survive the allowlist is passed
through recursive secret redaction — API-key formats, bearer tokens, JWTs,
private-key markers, credentials in URLs, `password=`-style assignments, and
long opaque tokens — and bounded in length, depth, and width. Redaction is a
second control, not the first one: the schema is what keeps sensitive data out.

## Correlation ids

`query_id` is `HMAC-SHA256(key, question)` truncated to 16 hex characters. The
key is 32 random bytes in `.knowcode_telemetry_key` beside the log, created on
first use with mode `0600`. It makes repeated questions comparable across runs
without storing what was asked, it cannot be joined across repositories, and
deleting telemetry deletes the key — so deletion starts a fresh identifier
space rather than merely truncating history.

## File protection, rotation, and retention

- Files are created with mode `0600`, and an existing looser file is tightened
  on the next write.
- `knowcode_telemetry.jsonl` rotates at 5 MiB to `knowcode_telemetry.jsonl.1`,
  keeping at most 3 rotations — roughly 20 MiB total.
- Rotations older than 30 days are deleted on the next write. The *active*
  file is bounded by size rather than by age, so on a rarely-used store it can
  still hold records older than the window until it rotates; `knowcode
  telemetry clear` removes them immediately.
- Writes are queued on a single background thread, bounded at 512 pending
  events. Beyond that, telemetry drops events rather than slowing a query; the
  drop count is available through `knowcode.telemetry.dropped_event_count()`.

## Inspecting and deleting

```bash
knowcode telemetry show --store .
```

```bash
knowcode telemetry clear --store . --yes
```

`clear` removes the log, every rotation, the opt-in raw file, and the
correlation key. Deleting the files by hand is equally safe.

## Opt-in raw query capture

Debugging a retrieval problem sometimes requires the actual question. Set
`KNOWCODE_TELEMETRY_RAW=1` to enable it:

```bash
KNOWCODE_TELEMETRY_RAW=1 knowcode ask "why does the order validator reject empty items?"
```

Raw capture is off by default and is not a redaction-only compromise:

- it writes to a **separate** file, `knowcode_telemetry_raw.jsonl`, which is
  never mixed into the default log;
- it logs a warning the first time it writes in a process;
- it applies the same secret redaction, so opting into your question text is
  not opting into storing a credential you pasted into it;
- it is bounded at 1 MiB with one rotation and a 7-day retention window, rather
  than the default file's 30 days.

**Threat model.** The default log is safe to attach to a bug report: it holds
counts, buckets, and enum labels. The raw file is not — it holds what you
asked, which for a private repository is itself sensitive, and redaction only
covers credential formats KnowCode knows. Enable it while reproducing a
problem, then run `knowcode telemetry clear`.

## Threshold tuning

The sufficiency threshold (`sufficiency_threshold` in `aimodels.yaml`) balances
local routing (cheap and fast) against escalation to an LLM (accurate for
complex questions). `knowcode telemetry show` reports the local routing rate
and average sufficiency across all retained records:

- if the local rate is low but local answers are good, lower the threshold;
- if users mark local answers as misses, raise it.

For per-record analysis, the file is ordinary JSONL:

```bash
jq 'select(.event_type == "query") | {local_or_escalated, sufficiency_bucket, duration_ms}' knowcode_telemetry.jsonl
```

## Upgrading from the pre-1 schema

Logs written before the versioned schema contain raw query text and have no
`telemetry_schema_version`. `knowcode telemetry show` still counts them
correctly, so your history is preserved — but those records are exactly what
this schema exists to stop writing. Deleting them with `knowcode telemetry
clear` is recommended.

## Future spend-metric extension path

> **Planned — not yet implemented.** Any addition below is an edit to the
> allowlist in `telemetry_policy.py`, reviewable on its own.

- `estimated_token_usage`: input/output token counts for LLM calls.
- `estimated_spend_usd`: cost from a model pricing matrix.
- `savings_usd`: cost avoided by answering locally.
