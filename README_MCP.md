# MCP Setup - Quick Reference

## ✅ What We Fixed

1. **MCP Configuration Path Issue**
   - **Problem**: Used `"command": "knowcode"` (not in PATH)
   - **Solution**: Changed to absolute path to knowcode binary in local `.venv` (e.g. `"/Users/deepg/Desktop/KnowCode/.venv/bin/knowcode"` or `"<project_root>/.venv/bin/knowcode"`)
   - **File**: `~/.gemini/antigravity/mcp_config.json` (or `~/.gemini/antigravity-ide/mcp_config.json`)

## 📋 Current Status

Verify the setup status dynamically using the `knowcode doctor` command:
```bash
uv run knowcode doctor --store .
```

### ✅ Ready

- [x] MCP configuration file updated with absolute path
- [x] Knowledge store exists
- [x] KnowCode CLI working
- [x] Virtual environment configured
- [x] Agent rules defined in [.agent/rules/context.md](file:///.agent/rules/context.md)

### 🔄 Next Actions Required

1. **Stop the manual MCP server** (Ctrl+C in terminal)
2. **Restart Antigravity IDE**
3. **Test the workflow** (see [tests/test_mcp_workflow.md](file:///Users/deepg/Desktop/KnowCode/tests/test_mcp_workflow.md))

## 🎯 Expected Workflow After Restart

```
User asks: "How does search work in KnowCode?"
    ↓
Agent calls: retrieve_context_for_query(
    query="How does search work in KnowCode?",
    task_type="auto",
    max_tokens=1500,
    limit_entities=1,
    expand_deps=false
)
    ↓
KnowCode MCP Server returns:
{
    "context_text": "...",
    "sufficiency_score": 0.92,
    "evidence": [...],
    ...
}
    ↓
Agent checks: sufficiency_score >= sufficiency_threshold (0.8 per aimodels.yaml)?
    ↓
YES → Answer directly from local context_text
NO  → Escalate local context (raise max_tokens/limit_entities, or verbosity to standard/verbose)
      If local context still insufficient → Fallback to external LLM (as defined in docs/mcp-contract.md)
```

## 📁 Files Created

1. **[verify_mcp_connection.sh](file:///Users/deepg/Desktop/KnowCode/verify_mcp_connection.sh)** - Check MCP setup status
2. **[tests/test_mcp_workflow.md](file:///Users/deepg/Desktop/KnowCode/tests/test_mcp_workflow.md)** - Test questions after restart
3. **[docs/MCP_SETUP.md](file:///Users/deepg/Desktop/KnowCode/docs/MCP_SETUP.md)** - Complete setup documentation
4. **[README_MCP.md](file:///Users/deepg/Desktop/KnowCode/README_MCP.md)** - This quick reference (you are here)

## 🚀 Quick Commands

### Check MCP Status

```bash
./verify_mcp_connection.sh
```

### Check MCP Server Process

```bash
ps aux | grep "knowcode mcp-server"
```

### View MCP Configuration

```bash
cat ~/.gemini/antigravity/mcp_config.json
```

### Rebuild Knowledge Store (if needed)

```bash
source .venv/bin/activate
knowcode analyze . -o .
```

**Note:** This rebuilds the knowledge store and attempts semantic indexing. If indexing is skipped, run `knowcode index .` after configuring embeddings.

## 🐛 Troubleshooting

### Check Setup with Doctor Command
The absolute best way to check the status of your KnowCode configuration, knowledge store, and semantic index is using `knowcode doctor`:
```bash
uv run knowcode doctor --store . --mcp
```
This command checks configuration files, verifies the presence of required index files (`index_manifest.json`, `chunks.json`, `vectors.index`), and tests the local MCP server handshake.

### MCP Tool Not Available After Restart?

1. Check if the server process is running:
   ```bash
   ps aux | grep "knowcode mcp-server"
   ```

2. Check configuration file paths:
   ```bash
   cat ~/.gemini/antigravity/mcp_config.json
   ```

3. Restart the IDE/client to reload the configuration.

### Low Sufficiency Scores?

1. Verify the semantic index directory contains the three required files:
   - `index_manifest.json`
   - `chunks.json`
   - `vectors.index`
   ```bash
   ls -la knowcode_index/
   ```

2. If any files are missing, rebuild the index:
   ```bash
   uv run knowcode index . --output knowcode_index
   ```

3. Adjust token budget parameters in [.agent/rules/context.md](file:///.agent/rules/context.md) following the verbosity ladder in [docs/mcp-contract.md](file:///Users/deepg/Desktop/KnowCode/docs/mcp-contract.md).

## 📊 Success Metrics

After setup, you should see:

- ✅ 70%+ queries with `sufficiency_score >= 0.8`
- ✅ Faster responses for codebase questions
- ✅ 50%+ reduction in external LLM token usage
- ✅ Accurate answers from local context

## 📚 Documentation

- **Full Setup Guide**: [docs/MCP_SETUP.md](file:///Users/deepg/Desktop/KnowCode/docs/MCP_SETUP.md)
- **Test Plan**: [tests/test_mcp_workflow.md](file:///Users/deepg/Desktop/KnowCode/tests/test_mcp_workflow.md)
- **KnowCode Docs**: [README.md](file:///Users/deepg/Desktop/KnowCode/README.md)
- **MCP Contract**: [docs/mcp-contract.md](file:///Users/deepg/Desktop/KnowCode/docs/mcp-contract.md)

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

## 🔒 Security Notes

- MCP server runs **locally** (no external data transmission)
- Knowledge store contains your **source code** (keep secure)
- Embeddings may be sent to **external providers** (VoyageAI, OpenAI)
- Store API keys in `.env` (never commit)

## 🎉 You're Almost There!

Just need to:

1. Stop the manual MCP server (Ctrl+C)
2. Restart Antigravity IDE
3. Ask a test question

Good luck! 🚀

---

_Last updated: 2026-06-06_
