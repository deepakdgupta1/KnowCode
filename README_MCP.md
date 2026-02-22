# MCP Setup - Quick Reference

## ✅ What We Fixed

1. **MCP Configuration Path Issue**
   - **Problem**: Used `"command": "knowcode"` (not in PATH)
   - **Solution**: Changed to `"/home/deeog/Desktop/KnowCode/.venv/bin/knowcode"`
   - **File**: `/home/deeog/.gemini/mcp_servers.json`

## 📋 Current Status

### ✅ Ready
- [x] MCP configuration file updated with absolute path
- [x] Knowledge store exists (1.1M, 1 day old)
- [x] KnowCode CLI working (v0.2.1)
- [x] Virtual environment configured
- [x] Agent rules defined in `.agent/context.md`

### ⚠️ Needs Attention
- [ ] **Semantic index missing** - Will use lexical search only
- [ ] **Knowledge store is 1 day old** - Consider re-analyzing

### 🔄 Next Actions Required
1. **Stop the manual MCP server** (Ctrl+C in terminal)
2. **Restart Antigravity IDE**
3. **Test the workflow** (see test_mcp_workflow.md)

## 🎯 Expected Workflow After Restart

```
User asks: "How does search work in KnowCode?"
    ↓
Agent calls: retrieve_context_for_query(
    query="How does search work in KnowCode?",
    task_type="auto",
    max_tokens=3000,
    limit_entities=3,
    expand_deps=true
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
Agent checks: sufficiency_score >= 0.8?
    ↓
YES → Answer from context_text only (no external LLM)
NO  → Use external LLM (Claude Sonnet 4.5)
```

## 📁 Files Created

1. **`verify_mcp_connection.sh`** - Check MCP setup status
2. **`test_mcp_workflow.md`** - Test questions after restart
3. **`docs/MCP_SETUP.md`** - Complete setup documentation
4. **`README_MCP.md`** - This quick reference (you are here)

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
cat ~/.gemini/mcp_servers.json
```

### Rebuild Knowledge Store (if needed)
```bash
source .venv/bin/activate
knowcode analyze . -o .
```

**Note:** This rebuilds the knowledge store and attempts semantic indexing. If indexing is skipped, run `knowcode index .` after configuring embeddings.

## 🐛 Troubleshooting

### MCP Tool Not Available After Restart?

1. Check server is running:
   ```bash
   ps aux | grep "knowcode mcp-server"
   ```

2. Check configuration:
   ```bash
   cat ~/.gemini/mcp_servers.json
   ```

3. Restart IDE again

### Low Sufficiency Scores?

1. Verify index exists (should be created by analyze):
   ```bash
   ls -la knowcode_index/
   ```

2. If missing, run a dedicated index build:
   ```bash
   knowcode index . --output knowcode_index
   ```

3. Increase token budget in `.agent/context.md`:
   ```markdown
   Use max_tokens=6000, limit_entities=5
   ```

## 📊 Success Metrics

After setup, you should see:
- ✅ 70%+ queries with `sufficiency_score >= 0.8`
- ✅ Faster responses for codebase questions
- ✅ 50%+ reduction in external LLM token usage
- ✅ Accurate answers from local context

## 📚 Documentation

- **Full Setup Guide**: `docs/MCP_SETUP.md`
- **Test Plan**: `test_mcp_workflow.md`
- **KnowCode Docs**: `README.md`

## 🎓 Key Concepts

**Sufficiency Score**: Confidence that retrieved context is enough to answer the query
- `>= 0.8` → Answer locally
- `< 0.8` → Use external LLM

**Retrieval Modes**:
- **Semantic**: Uses embeddings + vector search (better)
- **Lexical**: Uses keyword matching (fallback)

**Dependency Expansion**: Includes related code (callees, callers) for complete context

## ⚡ Performance Tips

1. **Build semantic index** - Much better than lexical
2. **Keep knowledge store updated** - Re-analyze after major changes
3. **Tune parameters** - Adjust max_tokens and limit_entities
4. **Monitor scores** - Track sufficiency_score distribution

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

*Last updated: 2026-01-13*
