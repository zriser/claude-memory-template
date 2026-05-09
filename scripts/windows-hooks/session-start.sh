#!/bin/bash
# Windows Claude Code → WSL delegation for SessionStart hook.
# Invoked via: wsl.exe -e bash -ic 'bash ~/claude-memory/scripts/windows-hooks/session-start.sh'
#
# Prints the memory-context JSON that Claude Code injects at session start.
# Interactive shell (-ic) already sourced ~/.bashrc, so ANTHROPIC_API_KEY is in env.
#
# Configurable:
#   CLAUDE_MEMORY_DIR  — vault path (default: ~/claude-memory)
#   CLAUDE_MEMORY_VENV — venv path  (default: ~/.venvs/claude-memory)

CLAUDE_MEMORY_DIR="${CLAUDE_MEMORY_DIR:-$HOME/claude-memory}"
CLAUDE_MEMORY_VENV="${CLAUDE_MEMORY_VENV:-$HOME/.venvs/claude-memory}"

exec "$CLAUDE_MEMORY_VENV/bin/python" "$CLAUDE_MEMORY_DIR/scripts/session-start.py"
