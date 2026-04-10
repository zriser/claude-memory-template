#!/usr/bin/env bash
# bootstrap-memory.sh — Re-bootstrap an EXISTING claude-memory vault on a new machine.
# For first-time setup of a NEW vault, run setup-new-vault.sh instead.
# Safe to run multiple times (idempotent).
set -euo pipefail

VAULT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$VAULT/.venv"

echo "=== Claude Memory Bootstrap (existing vault) ==="
echo "Vault: $VAULT"
echo ""
echo "First time? Run setup-new-vault.sh instead for interactive setup."
echo ""

# ── 1. Check Python 3.10+ ──────────────────────────────────────────────────────
PYTHON=""
for candidate in python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" &>/dev/null; then
        version=$("$candidate" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
            PYTHON="$candidate"
            echo "✓ Python $version found at $(which $candidate)"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "✗ Python 3.10+ not found. Install it first:"
    echo "  Ubuntu/Debian: sudo apt install python3.11"
    echo "  Or use pyenv: https://github.com/pyenv/pyenv"
    exit 1
fi

# ── 2. Create directory structure ─────────────────────────────────────────────
echo ""
echo "Creating directory structure..."
dirs=(
    raw
    wiki
    daily
    brain/architecture
    brain/decisions
    brain/concepts
    brain/people
    brain/mcp-servers
    brain/skills
    work/active
    work/archive
    patterns
    mistakes
    sessions
    scripts
    hooks
)

for dir in "${dirs[@]}"; do
    mkdir -p "$VAULT/$dir"
done
echo "✓ Directory structure ready"

# ── 3. Create or activate venv ────────────────────────────────────────────────
echo ""
if [ ! -d "$VENV" ]; then
    echo "Creating virtual environment..."
    "$PYTHON" -m venv "$VENV"
    echo "✓ Virtual environment created at $VENV"
else
    echo "✓ Virtual environment already exists"
fi

echo "Installing dependencies..."
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$VAULT/requirements.txt"
echo "✓ Dependencies installed (anthropic SDK)"

# ── 4. Initialize git repo ────────────────────────────────────────────────────
echo ""
if [ ! -d "$VAULT/.git" ]; then
    git -C "$VAULT" init -b main
    echo "✓ Git repository initialized"
else
    echo "✓ Git repository already initialized"
fi

# ── 5. Initialize MEMORY.md if missing ───────────────────────────────────────
if [ ! -f "$VAULT/MEMORY.md" ] || [ ! -s "$VAULT/MEMORY.md" ]; then
    cat > "$VAULT/MEMORY.md" << 'EOF'
# Memory Index

> Index only — no content here. Keep under 50 lines. Updated by Claude at session end.

## Active Projects
<!-- work/active/ entries go here -->

## Recent Decisions
<!-- brain/decisions/ entries go here -->

## Key Patterns
<!-- patterns/ entries go here -->

## Key Mistakes
<!-- mistakes/ entries go here -->

## Concepts
<!-- brain/concepts/ and wiki/ entries go here -->

## People
<!-- brain/people/ entries go here -->

---
*Last updated: (not yet updated — run a session to populate)*
EOF
    echo "✓ MEMORY.md initialized"
fi

# ── 6. Initialize wiki/index.md if missing ────────────────────────────────────
if [ ! -f "$VAULT/wiki/index.md" ] || [ ! -s "$VAULT/wiki/index.md" ]; then
    cat > "$VAULT/wiki/index.md" << EOF
---
title: "Wiki Index"
updated: $(date +%Y-%m-%d)
---

# Knowledge Wiki Index

> Master index of compiled concept articles. Sized to fit in a context window.
> Auto-maintained by compile.py. Do not edit manually.

## Architecture
<!-- Entries added by compile.py -->

## Concepts
<!-- Entries added by compile.py -->

## Decisions
<!-- Entries added by compile.py -->

## Patterns
<!-- Entries added by compile.py -->

## Mistakes
<!-- Entries added by compile.py -->

---
*Compiled from daily logs. Source of truth for cross-session knowledge.*
EOF
    echo "✓ wiki/index.md initialized"
fi

# ── 7. Make scripts executable ────────────────────────────────────────────────
chmod +x "$VAULT/scripts/"*.py
echo "✓ Scripts made executable"

# ── 8. Verify imports ─────────────────────────────────────────────────────────
echo ""
echo "Verifying script imports..."
if "$VENV/bin/python" -c "import anthropic; print(f'  ✓ anthropic {anthropic.__version__}')"; then
    :
else
    echo "  ✗ anthropic import failed"
fi

# ── 9. Check for ANTHROPIC_API_KEY ────────────────────────────────────────────
echo ""
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    echo "✓ ANTHROPIC_API_KEY is set"
else
    echo "⚠  ANTHROPIC_API_KEY is not set"
    echo "   Add to your shell profile: export ANTHROPIC_API_KEY=sk-ant-..."
fi

# ── 10. Print hooks instructions ──────────────────────────────────────────────
SNIPPET="$VAULT/hooks/settings-snippet.json"
SETTINGS="$HOME/.claude/settings.json"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "NEXT STEP: Add hooks to Claude Code settings"
echo "═══════════════════════════════════════════════════════════"
echo ""
echo "Edit: $SETTINGS"
echo ""
echo "Add the hooks from: $SNIPPET"
echo ""
echo "Replace VAULT_PATH in the snippet with:"
echo "  $VAULT"
echo ""
echo "Quick check — current hooks in settings.json:"
if [ -f "$SETTINGS" ]; then
    if command -v jq &>/dev/null; then
        jq '.hooks // "No hooks configured"' "$SETTINGS" 2>/dev/null || echo "  (could not parse settings.json)"
    else
        grep -A5 '"hooks"' "$SETTINGS" 2>/dev/null | head -10 || echo "  No hooks found"
    fi
else
    echo "  $SETTINGS not found — Claude Code may not be installed"
fi

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "MANUAL QUERY USAGE"
echo "═══════════════════════════════════════════════════════════"
echo ""
echo "  $VENV/bin/python $VAULT/scripts/query.py 'What patterns do I use?'"
echo "  $VENV/bin/python $VAULT/scripts/lint.py"
echo "  $VENV/bin/python $VAULT/scripts/compile.py --dry-run"
echo ""
echo "=== Bootstrap complete ==="
