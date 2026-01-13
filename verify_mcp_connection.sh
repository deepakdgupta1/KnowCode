#!/bin/bash

echo "=== MCP Connection Verification Script ==="
echo ""

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 1. Check if MCP server process is running
echo "1. Checking if KnowCode MCP server is running..."
if ps aux | grep -v grep | grep "knowcode mcp-server" > /dev/null; then
    PID=$(ps aux | grep -v grep | grep "knowcode mcp-server" | awk '{print $2}')
    echo -e "   ${GREEN}✓${NC} MCP server is running (PID: $PID)"
    
    # Check if it's running with correct path
    FULL_CMD=$(ps aux | grep -v grep | grep "knowcode mcp-server" | head -1)
    if echo "$FULL_CMD" | grep -q ".venv/bin"; then
        echo -e "   ${GREEN}✓${NC} Using virtual environment binary"
    else
        echo -e "   ${YELLOW}⚠${NC} Not using expected venv path"
    fi
else
    echo -e "   ${RED}✗${NC} MCP server is NOT running"
    echo "   This is expected if Antigravity hasn't been restarted yet."
fi

echo ""

# 2. Verify MCP configuration
echo "2. Verifying MCP configuration..."
MCP_CONFIG="/home/deeog/.gemini/mcp_servers.json"
if [ -f "$MCP_CONFIG" ]; then
    echo -e "   ${GREEN}✓${NC} Config file exists: $MCP_CONFIG"
    
    # Check if it uses full path
    if grep -q "/home/deeog/Desktop/KnowCode/.venv/bin/knowcode" "$MCP_CONFIG"; then
        echo -e "   ${GREEN}✓${NC} Using absolute path to knowcode binary"
    else
        echo -e "   ${RED}✗${NC} Not using absolute path - may fail to start"
    fi
else
    echo -e "   ${RED}✗${NC} Config file not found!"
fi

echo ""

# 3. Check knowledge store
echo "3. Checking knowledge store..."
STORE_PATH="/home/deeog/Desktop/KnowCode/knowcode_knowledge.json"
if [ -f "$STORE_PATH" ]; then
    SIZE=$(du -h "$STORE_PATH" | cut -f1)
    echo -e "   ${GREEN}✓${NC} Knowledge store exists: $SIZE"
    
    # Check if it's recent
    MOD_TIME=$(stat -c %Y "$STORE_PATH")
    CURRENT_TIME=$(date +%s)
    AGE=$((CURRENT_TIME - MOD_TIME))
    
    if [ $AGE -lt 86400 ]; then  # Less than 24 hours
        echo -e "   ${GREEN}✓${NC} Store is recent (modified within 24 hours)"
    else
        DAYS=$((AGE / 86400))
        echo -e "   ${YELLOW}⚠${NC} Store is $DAYS days old - consider re-analyzing"
    fi
else
    echo -e "   ${RED}✗${NC} Knowledge store not found!"
fi

echo ""

# 4. Check semantic index
echo "4. Checking semantic index..."
INDEX_PATH="/home/deeog/Desktop/KnowCode/knowcode_index"
if [ -d "$INDEX_PATH" ]; then
    echo -e "   ${GREEN}✓${NC} Semantic index directory exists"
    
    # Check for key index files (updated to match latest implementation)
    if [ -f "$INDEX_PATH/index_manifest.json" ]; then
        echo -e "   ${GREEN}✓${NC} Index manifest found (index_manifest.json)"
    else
        echo -e "   ${RED}✗${NC} Index manifest missing!"
    fi
    
    if [ -f "$INDEX_PATH/chunks.json" ]; then
        echo -e "   ${GREEN}✓${NC} Chunk metadata found (chunks.json)"
    else
        echo -e "   ${RED}✗${NC} Chunk metadata missing!"
    fi

    if [ -f "$INDEX_PATH/vectors.index" ]; then
        echo -e "   ${GREEN}✓${NC} FAISS vector index found (vectors.index)"
    else
        echo -e "   ${RED}✗${NC} FAISS vector index missing!"
    fi
else
    echo -e "   ${YELLOW}⚠${NC} Semantic index not found (will use lexical search)"
fi

echo ""

# 5. Test knowcode CLI
echo "5. Testing knowcode CLI availability..."
if [ -f "/home/deeog/Desktop/KnowCode/.venv/bin/knowcode" ]; then
    echo -e "   ${GREEN}✓${NC} knowcode binary exists in venv"
    
    # Try to get version
    VERSION=$(source /home/deeog/Desktop/KnowCode/.venv/bin/activate && knowcode --version 2>&1 || echo "unknown")
    echo "   Version: $VERSION"
else
    echo -e "   ${RED}✗${NC} knowcode binary not found in venv"
fi

echo ""
echo "=== Summary ==="
echo ""
echo "KnowCode MCP server provides the following tools:"
echo "  1. search_codebase           - Lexical search for entities"
echo "  2. get_entity_context        - Deep-dive into specific code items"
echo "  3. trace_calls               - Map out dependencies"
echo "  4. retrieve_context_for_query - Primary RAG retrieval (unified task-aware context)"
echo ""
echo "To complete the setup:"
echo "1. ${YELLOW}Stop any manually-started MCP server${NC} (Ctrl+C)"
echo "2. ${YELLOW}Restart Antigravity IDE${NC}"
echo "3. ${YELLOW}Antigravity will auto-start the MCP server using the config${NC}"
echo ""
echo "Expected behavior after restart:"
echo "  • All 4 KnowCode tools will be available in the IDE"
echo "  • Agent rules in .agent/rules/context.md will guide tool usage"
echo ""
