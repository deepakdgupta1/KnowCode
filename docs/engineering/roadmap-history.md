# Roadmap History

> **Status:** Living record. Append-only below the header.
>
> This explains **why** KnowCode is where it is. What it is building **next** is
> the [roadmap](../roadmap.md). This document succeeds the v1.1
> operationalization era preserved at
> [`archive/MCP_operationalization.md`](../archive/MCP_operationalization.md).

The roadmap carried its own history until 2026-09-09, when dated shipped-work
narrative had grown to roughly half the document and pushed the open work below
the fold. The narrative moved here. The verbatim pre-split text is not
reproduced: git holds it losslessly at commit `f91fcd2`, and a second copy in
the documentation tree would be indexed alongside the live roadmap and returned
against it in retrieval.

**One owner per fact.** Storage-phase execution detail is not repeated here. It
lives in §17 of the
[Storage Footprint & Optimization Plan](../research/storage_optimization_2026_v4.md),
which is the running execution log for that stream. Defects live in the
[engineering backlog](backlog.md). This document holds only the cross-workstream
narrative that has no other home.

## Where the project stood before Phase B (to 2026-08-29)

Measuring index storage surfaced two defects that changed what "next" meant.
Markdown documents whose H1 slugified to their own filename were dropped from
the index entirely by an entity id collision, which was 32% of this repository's
prose including seven of eight ADRs and most of the user guide. Separately, the
chunk carrying the largest block of most files was tagged with an entity id no
parser emitted, so it was disconnected from the graph. A published generation
was also storing every embedding twice.

All three are fixed. Prose coverage went from 41.6% to 95.7%.

## Completed foundations, with the evidence as it stood

The regression gates these established are still binding and stay in the
[roadmap](../roadmap.md). The dated evidence is here.

| Foundation | Evidence at the time |
| --- | --- |
| MCP operating contract | [MCP Contract](../mcp-contract.md) documents minimal-first retrieval and the escalation ladder. |
| Freshness and coverage safety | The scanner, watcher, and `doctor` validate source coverage and stale artifacts. |
| Local readiness verification | `knowcode doctor --mcp` checks config, artifacts, disk use, agent rules, and an MCP handshake. |
| Local telemetry | JSONL events record retrieval, agent-routing, and MCP tool activity. |
| Derived vector plane | [ADR 9](adr/adr-0009-derived-vector-plane.md): the ANN index is rebuilt from durable chunk rows, not published. One generation fell 113.18 MB to 78.87 MB. |

## P1 — why the 60 records are void (2026-08-30)

Work item 1 says "run the real `Agent.smart_answer` escalation path". Until
2026-08-30 there was none to run: both broadening rungs were gated on the
always-empty local-answer allowlist, so every question was measured on a
one-entity, no-source, 1,500-token bundle ([BL-19](backlog.md), fixed), and
dependency expansion under-filled even that ([BL-18](backlog.md), fixed). Every
record collected before those two landed describes a bundle the product no
longer builds, so none of it is calibration data.

Two measurement defects stood between a fresh run and a usable number. Both are
closed: sufficiency no longer saturates at 1.00 for any complete bundle
([BL-20](backlog.md)), and the local-answer rate is no longer computed against
the wrong gate ([BL-22](backlog.md)). Re-collecting is now worth doing, which is
why the instruction survives in the live roadmap.

**Progress (2026-07-31).** The proof and validation harness moved to the
independent `knowcode-evals` repository. KnowCode now contains only a
checksum-pinned, source-bound schema 1.1 policy consumer and runtime enforcement
tests. The evaluator owns the datasets, judges, benchmark adapters, statistics,
workflow, and artifact issuance.

## P2 — contract conformance (2026-08-11), and why it did not close

The drift P2 exists to catch: the direct LLM agent requested the default minimal
projection while still reading metadata that minimal responses omit.

Shipped 2026-08-11:

- `Agent.answer` and `Agent.smart_answer` request the minimal projection with
  the task metadata they consume.
- `smart_answer` follows the contract escalation ladder — narrow minimal,
  broader minimal, then standard detail before LLM fallback — reusing the final
  retrieval instead of querying a fourth time.
- Integration coverage exercises local and LLM routing against the actual
  `RetrievalOrchestrator` projection.
- The `knowcode doctor --mcp` and [release checklist](release.md) conformance
  audit completed, validating the canonical tool `retrieve_context_for_query`
  and minimal projection response formatting.

That last line is why P2 stayed open. The audit validated a tool surface P3's
consolidation then replaced. `retrieve_context_for_query` became a legacy flat
tool behind `--legacy-tools`, off by default, and the default entry point became
`knowcode_retrieve` with `action="query"`. The runtime followed; three documents
did not, so the single policy source contradicted itself until 2026-09-09.

## P3 — the token diet as it shipped

**Consolidation.** The default surface became three concern-split tools with
`action` enums: `knowcode_retrieve`, `knowcode_lifecycle`, `knowcode_inspect`.
The five flat tools remain behind `mcp-server --legacy-tools` for one release.
The split is by concern rather than a single tool because client permissions are
per-tool, so retrieval can be allowlisted while builds stay confirmed.

**Scope change.** Consolidation shipped together with a capability expansion
(build/index/export/doctor/freshness/stats/history/preflight/telemetry/job
polling), so the recurring schema cost went from ~650 tokens for 5 capabilities
to ~1,110 for 14 rather than down to ~200. Cost per capability improved ~2.3x;
absolute per-turn cost did not fall. A ceiling test now guards it.

**Unblocked by the same change.** `mcp-server` no longer refuses to start
without a store, so an agent can bootstrap a repository through
`knowcode_lifecycle action='build'` without a terminal step.

**Response profiles and payload telemetry (2026-09-09).** The absolute reduction
consolidation could not deliver came from the responses themselves. Profiles are
one definition, `retrieval/response_profiles.py`, that every source-bearing
retrieval consumer answers to. Summary-first is the default on `query` *and*
`context` — the latter used to synthesize raw source on every call. Escalation
rides the existing `verbosity` ladder rather than a new schema enum, because the
schema is paid on every turn and stays at ~1,140 tokens under the 1,200 ceiling.
`debug` and `review` include source even at `minimal`, a floor an explicit
default could not be allowed to cancel since `minimal` *is* the default; that
set is now stated canonically in the
[MCP contract](../mcp-contract.md#source-hungry-task-types) and pinned by test.
`semantic_search` is the explicit source request and stays raw. The `query`
projection stopped claiming an omission it did not make: a source-hungry minimal
response says `source_included` instead of a reduction summary.

`tests/integration/test_mcp_payload_caps.py` builds a real index and pins every
default action payload under ~2x its measured bytes, the 2x absorbing
absolute-path noise that swings ~30% between tmp roots. A profile flip is caught
comparatively — same call, summary versus explicit source, same root — which
path noise cannot fool. Telemetry records `payload_bytes` on every tool call, a
length carrying the same privacy posture as `query_chars` and additive to the
event schema so old records stay valid. Measured on this repository: default
`context` fell from 4,313 to 674 bytes on the probe entity, a 6.4x reduction.

## P7 — storage and prose coverage

Phase-by-phase accounting is **not** duplicated here. §17 of the
[Storage Footprint & Optimization Plan](../research/storage_optimization_2026_v4.md)
is the running execution log and carries the measured ledger. Read its entries
rather than the deltas between them, and read it before §11, whose targets are
sized against the pre-B corpus.

Four measured results lived only in the roadmap and have no entry in §17, so
they are recorded here. Phase C1 made a generation's size independent of the
directory it was built in: two builds of one corpus differed by 10.9 MB, 19%,
and after C1 differ by 0.16 MB. C1 does not make a generation portable — ids
stay absolute above the storage layer and resolve against the recorded root, so
moving a repository still requires a rebuild, as ADR 1 has always said. The
lossless remainder was sized by the simulator at 2.99 MB and landed at 2.80 MB,
of which the `chunks.content` deflate was 2.69 MB; the gap between projection
and measurement is why §17 records measured figures rather than projected ones.

The cross-workstream fact worth keeping outside that plan: **DR-4** was settled
2026-08-30 after three separate phases proposed buying footprint with recall. It
is why Phase F and the int8 ANN cache are closed as *rejected* rather than
deferred, and why a measured recall number is now evidence that there is no
loss rather than a licence to ship one. The [backlog](backlog.md) states the
same rule for defects, deliberately: the two documents state one rule.

## Execution log

Append one dated entry per shipped workstream item. Keep it to what the live
roadmap cannot carry: the decision and its reason. Measured storage numbers
belong in §17, defects in the [backlog](backlog.md).

- **2026-09-09 — Roadmap split.** The forward plan and this record separated at
  `f91fcd2`. P2 was found still open: its 2026-08-11 conformance audit had
  validated a tool surface P3 replaced, and the MCP contract's appendix still
  described tool consolidation and summary-first responses as proposed after
  both had shipped. The appendix was corrected and the source-hungry task-type
  set given one owner in the contract. A documentation link checker
  (`scripts/check_doc_links.py`) now runs in CI, because the split would
  otherwise have been free to break relative links silently.
