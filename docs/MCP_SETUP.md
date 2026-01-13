# KnowCode MCP Server Setup Guide

## Overview

This guide documents how to set up and configure the KnowCode MCP (Model Context Protocol) server for integration with Antigravity IDE.

## What is MCP?

MCP (Model Context Protocol) allows IDE agents to retrieve context from your codebase **before** hitting external LLMs, reducing token consumption and improving response quality.

## Architecture

```
User Query
    ↓
Antigravity Agent
    ↓
retrieve_context_for_query (MCP Tool)
    ↓
KnowCode MCP Server
    ↓
Knowledge Store + Semantic Index
    ↓
Context Bundle (with sufficiency_score)
    ↓
Agent Decision:
    • If sufficiency_score >= 0.88 → Answer from context only
    • If sufficiency_score < 0.88 → Use external LLM
```

## Prerequisites

1. **KnowCode installed** in a virtual environment
2. **Knowledge store generated** (`knowcode_knowledge.json`)
3. **Semantic index built** (optional, but recommended)
4. **Antigravity IDE** with MCP support

## Configuration

### 1. MCP Server Configuration File

**Location:** `/home/deeog/.gemini/mcp_servers.json`

**Content:**
```json
{
  "mcpServers": {
    "knowcode": {
      "command": "/home/deeog/Desktop/KnowCode/.venv/bin/knowcode",
      "args": ["mcp-server", "--store", "/home/deeog/Desktop/KnowCode/"]
    }
  }
}
```

**⚠️ Important Notes:**
- Use **absolute path** to the `knowcode` binary in your virtual environment
- Do NOT use just `"knowcode"` - it won't be in the system PATH
- The `--store` argument should point to the directory containing `knowcode_knowledge.json`

### 2. Agent Rules Configuration

**Location:** `/home/deeog/Desktop/KnowCode/.agent/context.md`

**Content:**
```markdown
Always call the tool retrieve_context_for_query before answering.
Use task_type=auto, max_tokens=3000, limit_entities=3, expand_deps=true.
If sufficiency_score >= 0.88 and context_text is non-empty, answer ONLY from context_text.
Do not call other tools or request more context.
If sufficiency_score < 0.88, then proceed with a full LLM answer.
```

This ensures the agent follows the local-first workflow.

## Setup Steps

### Step 1: Analyze Codebase (Includes Indexing)

```bash
cd /home/deeog/Desktop/KnowCode
source .venv/bin/activate
knowcode analyze . -o .
```

This **automatically**:
- ✅ Parses the codebase and builds the knowledge graph
- ✅ Creates `knowcode_knowledge.json` with all entities and relationships
- ✅ **Builds the semantic index** at `knowcode_index/` with vector embeddings
- ✅ Reports statistics including indexed chunk count

**Note:** You do NOT need to run a separate `knowcode index` command - it's built-in!

### Step 3: Configure MCP Server

Create or update `/home/deeog/.gemini/mcp_servers.json` with the configuration shown above.

### Step 4: Restart Antigravity IDE

The IDE needs to be restarted to:
1. Read the updated MCP configuration
2. Establish connection to the MCP server
3. Make the `retrieve_context_for_query` tool available

### Step 5: Verify Connection

Run the verification script:

```bash
./verify_mcp_connection.sh
```

## Testing the Setup

### Test 1: Check MCP Server is Running

```bash
ps aux | grep "knowcode mcp-server"
```

You should see a process running with the full path to your venv.

### Test 2: Ask a Question

In Antigravity, ask a question about your codebase:

```
How does search work in KnowCode?
```

**Expected behavior:**
1. Agent calls `retrieve_context_for_query`
2. Returns context with `sufficiency_score`
3. If score >= 0.88, answers from context only
4. If score < 0.88, uses external LLM

### Test 3: Verify All Tools

The MCP server provides 4 specialized tools. You can verify they are all available in the Antigravity tool list:
1.  **`retrieve_context_for_query`**: Primary tool for general questions (RAG).
2.  **`search_codebase`**: Best for finding specific symbols by name.
3.  **`get_entity_context`**: Best for deep-diving into the source of a specific class or method.
4.  **`trace_calls`**: Best for understanding the call graph and dependencies.

### Test 4: Check Agent Logs

The agent should show:
```
Calling retrieve_context_for_query...
Sufficiency score: 0.92
Answering from local context only.
```

## Troubleshooting

### Issue 1: MCP Server Not Starting

**Symptoms:**
- `retrieve_context_for_query` tool not available
- No MCP server process running

**Solutions:**
1. Check the command path in `mcp_servers.json` is absolute
2. Verify the knowcode binary exists: `ls -la /path/to/.venv/bin/knowcode`
3. Check file permissions: `chmod +x /path/to/.venv/bin/knowcode`
4. Restart the IDE

### Issue 2: Knowledge Store Not Found

**Symptoms:**
- MCP server starts but returns empty context
- Error: "Knowledge store not found"

**Solutions:**
1. Verify `knowcode_knowledge.json` exists in the store path
2. Check the `--store` argument in `mcp_servers.json`
3. Re-run `knowcode analyze`

### Issue 3: Low Sufficiency Scores

**Symptoms:**
- `sufficiency_score` always < 0.88
- Agent always uses external LLM

**Solutions:**
1. Build semantic index: `knowcode index .`
2. Increase `max_tokens` parameter (default: 3000)
3. Increase `limit_entities` parameter (default: 3)
4. Check if the query matches your codebase domain

### Issue 4: Semantic Search Not Working

**Symptoms:**
- Falls back to lexical search
- Warning: "Semantic retrieval failed"

**Solutions:**
1. Verify `knowcode_index/` directory exists
2. Check `knowcode_index/manifest.json` exists
3. Verify embedding model is configured in `aimodels.yaml`
4. Check API keys for embedding provider (VoyageAI, OpenAI, etc.)

## MCP Server Commands

### Start Manually (for testing)

```bash
cd /home/deeog/Desktop/KnowCode
source .venv/bin/activate
knowcode mcp-server --store .
```

### Check Status

```bash
./check_mcp_server.sh
```

### Stop Server

If running manually, press `Ctrl+C`.

If started by Antigravity, it will be managed automatically.

## Configuration Files Reference

### `aimodels.yaml` (Embedding Configuration)

```yaml
embedding_models:
  - name: voyage-code-3
    provider: voyageai
    api_key_env: VOYAGE_API_KEY_1
    dimensions: 1024

reranking_models:
  - name: rerank-2.5
    provider: voyageai
    api_key_env: VOYAGE_API_KEY_1
```

### Environment Variables

Create a `.env` file in your project root:

```bash
VOYAGE_API_KEY_1=your_api_key_here
OPENAI_API_KEY=your_openai_key_here
```

## Performance Tuning

### Optimize for Speed

```markdown
# In .agent/context.md
Use task_type=auto, max_tokens=1500, limit_entities=2, expand_deps=false.
```

### Optimize for Quality

```markdown
# In .agent/context.md
Use task_type=auto, max_tokens=6000, limit_entities=5, expand_deps=true.
```

### Balance (Recommended)

```markdown
# In .agent/context.md
Use task_type=auto, max_tokens=3000, limit_entities=3, expand_deps=true.
```

## Monitoring

### Check Token Savings

The MCP server logs show:
- Queries answered locally (sufficiency >= 0.88)
- Queries sent to external LLM (sufficiency < 0.88)
- Token counts for each response

### Metrics to Track

1. **Sufficiency Score Distribution**: Aim for >70% of queries with score >= 0.88
2. **Token Consumption**: Compare before/after MCP integration
3. **Response Quality**: Verify local answers are accurate

## Best Practices

1. **Keep Knowledge Store Updated**: Re-analyze after significant code changes
2. **Rebuild Index Periodically**: Especially after adding new files
3. **Monitor Sufficiency Scores**: Low scores indicate missing context
4. **Use Semantic Search**: Much better than lexical-only
5. **Configure Appropriate Limits**: Balance speed vs. quality

## Advanced Configuration

### Custom Sufficiency Threshold

You can adjust the threshold in `.agent/context.md`:

```markdown
If sufficiency_score >= 0.75 and context_text is non-empty, answer ONLY from context_text.
```

Lower threshold = more local answers, but potentially lower quality.

### Multi-Hop Dependency Expansion

For complex queries, enable deeper dependency traversal:

```markdown
Use expand_deps=true, max_depth=2
```

### Task-Specific Configuration

```markdown
For code_explanation queries: max_tokens=4000, limit_entities=5
For debugging queries: max_tokens=6000, limit_entities=3, expand_deps=true
For general queries: max_tokens=2000, limit_entities=2
```

## Security Considerations

1. **API Keys**: Store in `.env`, never commit to git
2. **MCP Server**: Runs locally, no external data transmission
3. **Knowledge Store**: Contains your code, keep secure
4. **Embedding Vectors**: May be sent to external providers (VoyageAI, OpenAI)

## Support

For issues or questions:
1. Check this documentation
2. Run `./verify_mcp_connection.sh`
3. Check `./check_mcp_server.sh`
4. Review conversation history for similar issues

## Changelog

- **2026-01-13**: Initial MCP setup documentation
- **2026-01-13**: Fixed command path issue (absolute path required)
- **2026-01-13**: Added verification script

## References

- [KnowCode Documentation](../README.md)
- [MCP Protocol Specification](https://modelcontextprotocol.io/)
- [Antigravity IDE Documentation](https://antigravity.dev/)
