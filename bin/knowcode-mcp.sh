#!/bin/bash
# Wrapper script for KnowCode MCP server.
# Works around macOS Sequoia com.apple.provenance restrictions that prevent
# Claude Code from directly spawning the venv Python binary.
#
# This script is a plain shell script (no provenance attribute), so macOS
# allows Claude Code to execute it. It then activates the venv and runs
# knowcode with all passed arguments.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Activate the virtual environment
source "${SCRIPT_DIR}/.venv/bin/activate"

# Execute knowcode with all arguments passed through
exec knowcode "$@"
