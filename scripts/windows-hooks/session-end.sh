#!/bin/bash
# Windows Claude Code → WSL delegation for SessionEnd hook.
# Runs flush.py in the background so the hook returns quickly.
#
# Claude Code passes the transcript path via $CLAUDE_TRANSCRIPT_PATH.
# Windows Claude Code passes a Windows path (e.g., C:\Users\<user>\...).
# We convert it to a WSL path with wslpath before handing to flush.py.
#
# Configurable:
#   CLAUDE_MEMORY_DIR  — vault path (default: ~/claude-memory)
#   CLAUDE_MEMORY_VENV — venv path  (default: ~/.venvs/claude-memory)

set -u

CLAUDE_MEMORY_DIR="${CLAUDE_MEMORY_DIR:-$HOME/claude-memory}"
CLAUDE_MEMORY_VENV="${CLAUDE_MEMORY_VENV:-$HOME/.venvs/claude-memory}"

# Prefer subscription auth (free) over API key (pay-per-token) for hook runs.
# SessionEnd fires after the session exits, so no concurrent-session conflict.
# API key stays available for manual invocations from inside active sessions
# (where subscription auth would 401 on concurrent-session rejection).
unset ANTHROPIC_API_KEY

P="${CLAUDE_TRANSCRIPT_PATH:-}"

# Translate Windows-style paths to WSL format; leave WSL paths alone.
if [ -n "$P" ]; then
    case "$P" in
        [A-Za-z]:\\* | [A-Za-z]:/*)
            P=$(wslpath -a "$P" 2>/dev/null || echo "$P")
            ;;
        /mnt/*|/home/*|/tmp/*|/root/*)
            # Already a POSIX path
            ;;
        /c/*|/[a-z]/*)
            # Git-Bash-style path — convert the leading /c/ → /mnt/c/
            P="/mnt$P"
            ;;
    esac
fi

# Background flush.py; disown so it survives the wsl.exe parent exiting.
nohup "$CLAUDE_MEMORY_VENV/bin/python" "$CLAUDE_MEMORY_DIR/scripts/flush.py" "$P" \
    >> "$CLAUDE_MEMORY_DIR/sessions/flush.log" 2>&1 &
disown
