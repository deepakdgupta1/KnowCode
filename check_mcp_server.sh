#!/bin/bash
# Check if KnowCode MCP server is running

echo "=== Checking for KnowCode MCP Server ==="
echo ""

# Method 1: Check for knowcode mcp-server process
echo "1. Checking for 'knowcode mcp-server' process:"
ps aux | grep -E "knowcode.*mcp-server" | grep -v grep
if [ $? -eq 0 ]; then
    echo "   ✓ KnowCode MCP server is RUNNING"
else
    echo "   ✗ KnowCode MCP server is NOT running"
fi

echo ""

# Method 2: Check for any knowcode process
echo "2. Checking for any 'knowcode' process:"
ps aux | grep "knowcode" | grep -v grep
if [ $? -eq 0 ]; then
    echo "   ✓ Found knowcode process(es)"
else
    echo "   ✗ No knowcode processes found"
fi

echo ""

# Method 3: Check for Python processes that might be running knowcode
echo "3. Checking for Python processes with 'knowcode' or 'mcp':"
ps aux | grep -E "python.*knowcode|python.*mcp" | grep -v grep

echo ""
echo "=== MCP Server Configuration Check ==="

# Check if MCP config exists
if [ -f "$HOME/.gemini/mcp_servers.json" ]; then
    echo "✓ Found MCP config: $HOME/.gemini/mcp_servers.json"
    echo "Content:"
    cat "$HOME/.gemini/mcp_servers.json" | jq '.' 2>/dev/null || cat "$HOME/.gemini/mcp_servers.json"
else
    echo "✗ MCP config not found at: $HOME/.gemini/mcp_servers.json"
fi

echo ""
echo "=== Knowledge Store Check ==="
if [ -f "knowcode_knowledge.json" ]; then
    echo "✓ Knowledge store exists: $(pwd)/knowcode_knowledge.json"
    echo "   Size: $(du -h knowcode_knowledge.json | cut -f1)"
else
    echo "✗ Knowledge store not found: $(pwd)/knowcode_knowledge.json"
    echo "   Run: knowcode analyze src/"
fi
