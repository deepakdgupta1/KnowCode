# KnowCode Observability and Telemetry

KnowCode implements a local, non-blocking telemetry system to monitor retrieval quality, detect stale states, and enable data-driven threshold tuning.

## Telemetry Log Format

All telemetry events are logged to an append-only JSON Lines (JSONL) file named `knowcode_telemetry.jsonl` located in the **store root directory** (the directory you pass to `--store`, typically the root of your project). For example, if you run `knowcode build .` from `/path/to/project`, the file is written to `/path/to/project/knowcode_telemetry.jsonl`.

Each log entry is a single line containing a JSON object with at least the following standard fields:
- `timestamp`: Epoch timestamp (integer seconds)
- `event_type`: The type of event (e.g. `"query"`, `"retrieval_decision"`, `"tool_call"`, `"agent_decision"`)

### Event Types & Schemas

#### 1. Retrieval Decision (`event_type: "retrieval_decision"`)
Logged by the `RetrievalOrchestrator` when context is retrieved for a query:
- `query`: The query string.
- `task_type`: Resolved/detected query task type.
- `retrieval_mode`: `"semantic"`, `"lexical"`, or `"none"`.
- `sufficiency_score`: Average context sufficiency score.
- `total_tokens`: Total tokens in retrieved context.
- `max_tokens`: Configured max tokens.
- `selected_entities`: List of retrieved entity IDs.

#### 2. Agent Decision (`event_type: "agent_decision"`)
Logged by `Agent.smart_answer` during local-first routing decisions:
- `query`: The query string.
- `source`: `"local"` (answered locally) or `"llm"` (escalated to LLM).
- `sufficiency_score`: Sufficiency score of the local context.
- `threshold`: Configured sufficiency threshold.
- `force_llm`: Boolean indicating if LLM was forced.
- `task_type`: Resolved query task type.
- `llm_tokens_saved`: Estimate of LLM tokens saved by answering locally.

#### 3. MCP Tool Call (`event_type: "tool_call"`)
Logged by `KnowCodeMCPServer` on incoming tool calls from the IDE/client:
- `tool_name`: Name of the tool called.
- `arguments`: The arguments payload passed to the tool.

---

## Retention Policy

By default, telemetry logs are retained indefinitely on the local disk. Because the log file is simple append-only JSONL, its storage footprint is extremely small (~150 bytes per query). For a typical repository with 10,000 queries, the log file occupies less than 2 MB.

If you wish to clear the logs, you can safely delete the `knowcode_telemetry.jsonl` file in your store directory at any time.

---

## Privacy Tradeoffs

- **100% Local**: The telemetry system operates completely locally. Log entries are written to your local disk and are **never** transmitted to external servers by KnowCode.
- **Sensitive Data**: Because queries and tool arguments (which may contain code snippets) are recorded, the telemetry file contains sensitive information. Avoid sharing the `knowcode_telemetry.jsonl` file publicly if your codebase contains proprietary information.

---

## Threshold Tuning

The sufficiency threshold (`sufficiency_threshold` in `aimodels.yaml`) controls the balance between:
- **Local Routing Rate**: Answering locally saves API costs and reduces latency.
- **Answer Accuracy**: Escalating to an LLM provides higher accuracy for complex queries.

To tune the threshold for your repository:
1. Inspect `knowcode_telemetry.jsonl` directly to review `source` ("local" vs "llm"), `sufficiency_score`, and `llm_tokens_saved` across recent queries. You can use any JSONL reader or a simple `jq` command:
   ```bash
   cat knowcode_telemetry.jsonl | jq 'select(.event_type == "agent_decision") | {source, sufficiency_score, query}'
   ```
2. If the `local` routing rate is too low but local answers are accurate, lower `sufficiency_threshold` (e.g. to `0.7`).
3. If users frequently mark local answers as misses, raise `sufficiency_threshold` (e.g. to `0.85` or `0.9`).

---

## Future Spend-Metric Extension Path

> **Planned — not yet implemented.** The fields below are proposed additions to the telemetry schema for a future release.

To enable cost optimization, the telemetry schema is fully extensible. Future versions of KnowCode will include:
- `estimated_token_usage`: Exact input/output token counts for LLM calls.
- `estimated_spend_usd`: API cost calculated using model pricing matrices.
- `savings_usd`: Cost saved by routing to local context instead of the LLM.
