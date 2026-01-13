# MCP Workflow Test Plan

## After IDE Restart - Test These Questions

Once you've restarted Antigravity IDE, test the MCP workflow with these questions:

### Test 1: Simple Code Query (Expected: High Sufficiency)
```
How does search work in KnowCode?
```

**Expected Result:**
- `sufficiency_score >= 0.88`
- Agent answers from context only
- No external LLM call
- Response includes details about SearchEngine, HybridIndex, etc.

---

### Test 2: Specific Function Query (Expected: High Sufficiency)
```
What does the retrieve_context_for_query method do?
```

**Expected Result:**
- `sufficiency_score >= 0.88`
- Agent answers from context only
- Response includes method signature, parameters, and logic

---

### Test 3: Architecture Query (Expected: Medium Sufficiency)
```
Explain the overall architecture of KnowCode
```

**Expected Result:**
- `sufficiency_score` may be 0.70-0.85
- Agent may use external LLM for synthesis
- But context should be retrieved first

---

### Test 4: Implementation Query (Expected: High Sufficiency)
```
Show me how semantic search is implemented
```

**Expected Result:**
- `sufficiency_score >= 0.88`
- Agent answers from context only
- Response includes HybridIndex, VectorStore, embeddings

---

### Test 5: Out-of-Scope Query (Expected: Low Sufficiency)
```
What are the best practices for Python async programming?
```

**Expected Result:**
- `sufficiency_score < 0.50`
- Agent uses external LLM
- This is correct behavior (not in codebase)

---

## How to Verify MCP is Working

### Check 1: Tool Availability
The agent should have access to `retrieve_context_for_query` tool.

### Check 2: Agent Behavior
Look for these indicators in the agent's response:
- "Retrieving context from codebase..."
- "Sufficiency score: X.XX"
- "Answering from local context" (when score >= 0.88)

### Check 3: MCP Server Process
```bash
ps aux | grep "knowcode mcp-server"
```

Should show the server running with the venv path.

### Check 4: No Manual Server Needed
You should NOT need to manually run `knowcode mcp-server` - Antigravity starts it automatically.

---

## Troubleshooting

### If MCP Tool Not Available

1. Check MCP server is running:
   ```bash
   ./verify_mcp_connection.sh
   ```

2. Verify configuration:
   ```bash
   cat ~/.gemini/mcp_servers.json
   ```

3. Check IDE logs (if available)

4. Restart IDE again

### If Sufficiency Scores Always Low

1. Verify semantic index exists:
   ```bash
   ls -la knowcode_index/
   ```

2. Rebuild index if needed:
   ```bash
   source .venv/bin/activate
   knowcode index . --output knowcode_index
   ```

3. Check embedding configuration in `aimodels.yaml`

### If Agent Doesn't Follow Rules

1. Verify `.agent/context.md` exists and contains the rules
2. Check the rules are properly formatted
3. Try rephrasing your query to be more specific

---

## Success Criteria

✅ MCP server starts automatically when IDE starts
✅ `retrieve_context_for_query` tool is available
✅ Agent retrieves context before answering
✅ High sufficiency scores (>0.88) for codebase questions
✅ Agent answers from context only when sufficient
✅ Agent uses external LLM only when needed (score < 0.88)

---

## Next Steps After Successful Test

1. **Monitor token usage** - Track how much you save
2. **Adjust thresholds** - Fine-tune sufficiency_score threshold
3. **Update knowledge store** - Re-analyze after code changes
4. **Expand to other projects** - Set up MCP for other codebases
5. **Document learnings** - Note what works best for your workflow

---

## Performance Benchmarks

Track these metrics over time:

| Metric | Target | Current |
|--------|--------|---------|
| % Queries with score >= 0.88 | >70% | TBD |
| Avg response time (local) | <2s | TBD |
| Avg response time (external) | <10s | TBD |
| Token savings per day | >50% | TBD |
| Answer accuracy (local) | >95% | TBD |

---

## Questions to Test Different Features

### Dependency Expansion
```
What functions does SearchEngine.search call?
```

### Multi-Entity Context
```
How do SearchEngine, HybridIndex, and Reranker work together?
```

### Temporal Queries (if coverage data available)
```
Which functions were recently modified?
```

### Impact Analysis
```
What would break if I change the HybridIndex.search method?
```

---

## Remember

The goal is **local-first retrieval** to:
- ✅ Reduce external LLM token consumption
- ✅ Get faster responses for codebase questions
- ✅ Maintain privacy (code stays local)
- ✅ Improve answer quality (direct from source)

Good luck with your testing! 🚀
