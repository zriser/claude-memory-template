#!/bin/bash
# Manual capture of the most recent Claude Code transcript.
#
# Use this when Windows Claude Desktop app doesn't fire SessionEnd hooks,
# or any time you want to force-capture before a hook would naturally run.
#
# Usage:
#   bash ~/claude-memory/scripts/capture-latest.sh           (auto: newest across Win + WSL + Cowork)
#   bash ~/claude-memory/scripts/capture-latest.sh --windows (force Windows Claude Code transcript dir)
#   bash ~/claude-memory/scripts/capture-latest.sh --wsl     (force WSL Claude Code transcript dir)
#   bash ~/claude-memory/scripts/capture-latest.sh --cowork  (force Cowork session transcript dir)
#
# Fully close your Claude Code session before running, or subscription auth
# will 401 with concurrent-session rejection. This script unsets
# ANTHROPIC_API_KEY so a lingering key in bashrc doesn't burn pay-per-token
# dollars by accident.
#
# Configurable:
#   WIN_USER           — Windows username (default: $USER from WSL)
#   CLAUDE_MEMORY_DIR  — vault path (default: ~/claude-memory)
#   CLAUDE_MEMORY_VENV — venv path  (default: ~/.venvs/claude-memory)

set -u

MODE="${1:---auto}"

WIN_USER="${WIN_USER:-$USER}"
CLAUDE_MEMORY_DIR="${CLAUDE_MEMORY_DIR:-$HOME/claude-memory}"
CLAUDE_MEMORY_VENV="${CLAUDE_MEMORY_VENV:-$HOME/.venvs/claude-memory}"

WIN_PROJECTS="/mnt/c/Users/${WIN_USER}/.claude/projects"
WSL_PROJECTS="$HOME/.claude/projects"
# Cowork (Claude Desktop's Cowork mode) stores .jsonl transcripts under the
# MSIX-redirected AppData path, nested ~6 levels deep inside per-session dirs.
# The Claude_pzs8sxrjxfjjc package ID is the app's identity and is stable
# across installs.
COWORK_PROJECTS_ROOT="/mnt/c/Users/${WIN_USER}/AppData/Local/Packages/Claude_pzs8sxrjxfjjc/LocalCache/Roaming/Claude/local-agent-mode-sessions"

# Claude Code projects keep transcripts at fixed depth — shallow search avoids
# walking unrelated trees.
find_latest_shallow() {
    find "$@" -maxdepth 2 -name "*.jsonl" ! -path "*/subagents/*" \
        -printf "%T@ %p\n" 2>/dev/null
}
# Cowork nests transcripts deeper; let find walk the full tree.
find_latest_deep() {
    find "$@" -name "*.jsonl" ! -path "*/subagents/*" \
        -printf "%T@ %p\n" 2>/dev/null
}

case "$MODE" in
    --windows)
        TRANSCRIPT=$(find_latest_shallow "$WIN_PROJECTS" | sort -n | tail -1 | cut -d" " -f2-)
        ;;
    --wsl)
        TRANSCRIPT=$(find_latest_shallow "$WSL_PROJECTS" | sort -n | tail -1 | cut -d" " -f2-)
        ;;
    --cowork)
        TRANSCRIPT=$(find_latest_deep "$COWORK_PROJECTS_ROOT" | sort -n | tail -1 | cut -d" " -f2-)
        ;;
    --auto)
        TRANSCRIPT=$( { find_latest_shallow "$WIN_PROJECTS" "$WSL_PROJECTS"; find_latest_deep "$COWORK_PROJECTS_ROOT"; } | sort -n | tail -1 | cut -d" " -f2-)
        ;;
    *)
        echo "Usage: $0 [--windows|--wsl|--cowork|--auto]"
        exit 1
        ;;
esac

if [ -z "$TRANSCRIPT" ]; then
    echo "No transcripts found."
    exit 1
fi

SIZE=$(du -h "$TRANSCRIPT" | cut -f1)
MTIME=$(stat -c %y "$TRANSCRIPT" | cut -d. -f1)
NAME=$(basename "$TRANSCRIPT")

echo "Processing: $NAME"
echo "Location:   $TRANSCRIPT"
echo "Size:       $SIZE"
echo "Modified:   $MTIME"
echo ""

# Back up the transcript (PreCompact equivalent — capture raw before flushing).
BACKUP="$CLAUDE_MEMORY_DIR/sessions/backup-$(date +%Y%m%d-%H%M%S).jsonl"
cp "$TRANSCRIPT" "$BACKUP" 2>/dev/null && echo "Backup:     $(basename "$BACKUP")"
echo ""

# Prefer subscription auth (free) — safe because caller has closed the session.
unset ANTHROPIC_API_KEY

echo "Running flush.py (subscription auth, \$0)..."
echo ""
"$CLAUDE_MEMORY_VENV/bin/python" "$CLAUDE_MEMORY_DIR/scripts/flush.py" "$TRANSCRIPT"
