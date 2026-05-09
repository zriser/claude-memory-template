#!/bin/bash
# Windows Claude Code → WSL delegation for PreCompact hook.
# Backs up the most recent Windows-side transcript to claude-memory/sessions/
# before Claude Code compacts context.
#
# Configurable:
#   WIN_USER           — Windows username (default: $USER from WSL — works when
#                        Windows and WSL usernames match)
#   CLAUDE_MEMORY_DIR  — vault path (default: ~/claude-memory)

set -u

WIN_USER="${WIN_USER:-$USER}"
CLAUDE_MEMORY_DIR="${CLAUDE_MEMORY_DIR:-$HOME/claude-memory}"

# Windows Claude Code stores transcripts at C:\Users\<user>\.claude\projects\
# which is /mnt/c/Users/<user>/.claude/projects/ from WSL.
WIN_PROJECTS="/mnt/c/Users/${WIN_USER}/.claude/projects"

TRANSCRIPT=$(find "$WIN_PROJECTS" -maxdepth 2 -name "*.jsonl" ! -path "*/subagents/*" -printf "%T@ %p\n" 2>/dev/null | sort -n | tail -1 | cut -d" " -f2-)

if [ -n "$TRANSCRIPT" ]; then
    DEST="$CLAUDE_MEMORY_DIR/sessions/backup-$(date +%Y%m%d-%H%M%S).jsonl"
    cp "$TRANSCRIPT" "$DEST" 2>/dev/null || true
fi
