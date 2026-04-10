#!/usr/bin/env bash
# setup-new-vault.sh — First-time interactive setup for a new claude-memory vault.
# Safe to re-run (idempotent). Works on WSL2/Ubuntu, Linux, macOS.
set -euo pipefail

SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ok()   { echo "  ✓ $*"; }
skip() { echo "  · $* (already done)"; }
warn() { echo "  ⚠  $*"; }
die()  { echo "  ✗ $*" >&2; exit 1; }
step() { echo ""; echo "── $*"; }
guard() { [[ -f "$1" ]] && { skip "$2"; return 1; } || return 0; }

# ── Interactive prompts ────────────────────────────────────────────────────────
echo "╔═══════════════════════════════════════════╗"
echo "║  claude-memory  ·  new vault setup        ║"
echo "╚═══════════════════════════════════════════╝"
echo ""
read -rp "Your name (for CLAUDE.md) [User]: "                   _NAME
read -rp "GitHub username (optional, for repo setup): "         _GH
echo "  1) subscription  Claude Code credentials (no API key)"
echo "  2) api-key       ANTHROPIC_API_KEY env var"
read -rp "Model tier [1]: "                                     _TIER
read -rp "Vault location [~/claude-memory]: "                   _LOC

VAULT_OWNER="${_NAME:-User}"
GH_USER="${_GH:-}"
[[ "${_TIER:-1}" =~ ^2|api ]] && TIER="api-key" || TIER="subscription"
VAULT="${_LOC:-$HOME/claude-memory}"; VAULT="${VAULT/#\~/$HOME}"
VENV="$VAULT/.venv"

echo ""; echo "  Vault : $VAULT  |  Owner : $VAULT_OWNER  |  Tier : $TIER"
read -rp "Continue? [Y/n]: " _C
[[ "${_C:-Y}" =~ ^[Nn] ]] && echo "Aborted." && exit 0

# ── Platform ──────────────────────────────────────────────────────────────────
step "Platform"
PLATFORM="linux"
[[ "$(uname -s)" == "Darwin" ]] && PLATFORM="macos"
grep -qi "microsoft" /proc/version 2>/dev/null && PLATFORM="wsl2"
ok "$PLATFORM"

# ── Python 3.10+ ─────────────────────────────────────────────────────────────
step "Python"
PYTHON=""
for _c in python3.12 python3.11 python3.10 python3; do
  if command -v "$_c" &>/dev/null; then
    _v=$("$_c" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    [[ "${_v%%.*}" -ge 3 && "${_v##*.}" -ge 10 ]] && { PYTHON="$_c"; break; }
  fi
done
[[ -z "$PYTHON" ]] && die "Python 3.10+ required (sudo apt install python3.11 / brew install python)"
ok "$PYTHON ($_v)"

# ── Directory structure ────────────────────────────────────────────────────────
step "Directories"
mkdir -p "$VAULT"
for _d in raw wiki daily sessions scripts hooks patterns mistakes \
           brain/{architecture,decisions,concepts,people,mcp-servers,skills} \
           work/{active,archive}; do
  mkdir -p "$VAULT/$_d"
done
ok "All directories ready"

# ── Copy scripts from source ──────────────────────────────────────────────────
step "Scripts"
if [[ "$VAULT" != "$SOURCE" ]]; then
  for _f in flush.py compile.py query.py lint.py session-start.py utils.py; do
    if [[ ! -f "$VAULT/scripts/$_f" ]]; then
      [[ -f "$SOURCE/scripts/$_f" ]] \
        && cp "$SOURCE/scripts/$_f" "$VAULT/scripts/$_f" && ok "$_f" \
        || warn "scripts/$_f missing from source — copy manually"
    else skip "scripts/$_f"; fi
  done
  [[ -f "$SOURCE/requirements.txt" && ! -f "$VAULT/requirements.txt" ]] \
    && cp "$SOURCE/requirements.txt" "$VAULT/requirements.txt" && ok "requirements.txt"
else
  ok "Vault is source repo — no copy needed"
fi
find "$VAULT/scripts" -name "*.py" -exec chmod +x {} \; 2>/dev/null || true

# ── Python venv ───────────────────────────────────────────────────────────────
step "Python venv"
[[ ! -d "$VENV" ]] && "$PYTHON" -m venv "$VENV" && ok "Created $VENV" || skip "venv"
"$VENV/bin/pip" install --quiet --upgrade pip
if [[ -f "$VAULT/requirements.txt" ]]; then
  "$VENV/bin/pip" install --quiet -r "$VAULT/requirements.txt" && ok "Dependencies installed"
else
  "$VENV/bin/pip" install --quiet anthropic && ok "anthropic SDK installed (no requirements.txt)"
fi

# ── CLAUDE.md ─────────────────────────────────────────────────────────────────
# Copy from source (which is the canonical generic rules file), personalize with name.
# Falls back to a minimal embedded template if source CLAUDE.md isn't available.
step "CLAUDE.md"
if [[ ! -f "$VAULT/CLAUDE.md" ]]; then
  if [[ -f "$SOURCE/CLAUDE.md" && "$VAULT" != "$SOURCE" ]]; then
    sed "s/^# Claude Memory Vault/# $VAULT_OWNER's Claude Memory Vault/" \
      "$SOURCE/CLAUDE.md" > "$VAULT/CLAUDE.md"
    ok "Created from source (personalized for $VAULT_OWNER)"
  else
    cat > "$VAULT/CLAUDE.md" << TMPL
# $VAULT_OWNER's Claude Memory Vault — Interaction Rules

## What This Is
Persistent knowledge base for Claude Code sessions. Stores decisions, patterns,
mistakes, and concepts across sessions.

## Rules
- **MEMORY.md is an index only** — one-line pointers, never content, ≤50 lines
- Before creating a note: grep for existing ones, update rather than duplicate
- Every note needs frontmatter: title, tags, created, updated, category
- Use [[wiki-link]] syntax for cross-references
- MCP server configured → create/update brain/mcp-servers/
- Skill/command created → update brain/skills/index.md
- flush.py tags: [mcp-setup] for MCP sessions, [skill-created] for skill sessions
- At session end: update MEMORY.md if new notes were created

## Categories: decision | pattern | mistake | concept | person | project-update

## File Organization
\`\`\`
brain/  wiki/  daily/  work/{active,archive}/  patterns/  mistakes/  raw/  sessions/
\`\`\`
TMPL
    ok "Created (minimal template)"
  fi
else
  skip "CLAUDE.md"
fi

# ── MEMORY.md ─────────────────────────────────────────────────────────────────
step "MEMORY.md"
if [[ ! -f "$VAULT/MEMORY.md" || ! -s "$VAULT/MEMORY.md" ]]; then
  cat > "$VAULT/MEMORY.md" << 'TMPL'
# Memory Index

> Index only — no content here. Keep under 50 lines. Updated by Claude at session end.

## Active Projects
## Recent Decisions
## Key Patterns
## Key Mistakes
## Concepts

## Infrastructure & Tooling
- [MCP Servers Catalog](brain/mcp-servers/index.md) — all configured MCP servers
- [Skills Catalog](brain/skills/index.md) — custom slash commands and skills

## People

---
*Last updated: (not yet updated — run a session to populate)*
TMPL
  ok "Initialized"
else skip "MEMORY.md"; fi

# ── Starter catalog notes + wiki index ───────────────────────────────────────
step "Starter notes"
TODAY=$(date +%Y-%m-%d)
if guard "$VAULT/wiki/index.md" "wiki/index.md"; then
  printf -- "---\ntitle: \"Wiki Index\"\nupdated: %s\n---\n\n## Architecture\n## Concepts\n## Decisions\n## Patterns\n## Mistakes\n" "$TODAY" > "$VAULT/wiki/index.md"
  ok "wiki/index.md"
fi
if guard "$VAULT/brain/mcp-servers/index.md" "brain/mcp-servers/index.md"; then
  cat > "$VAULT/brain/mcp-servers/index.md" << TMPL
---
title: "MCP Servers Catalog"
tags: [mcp, tools, catalog]
category: concept
created: $TODAY
updated: $TODAY
---

See [[Skills Catalog]] for skills that depend on these.

## Configured Servers
<!-- name | what it does | scope | auth | environments | link to detail note -->

## Quick Config
\`\`\`bash
claude mcp add --scope user <name> <command> [args...]  # user scope
claude mcp list                                          # verify
\`\`\`
TMPL
  ok "brain/mcp-servers/index.md"
fi
if guard "$VAULT/brain/skills/index.md" "brain/skills/index.md"; then
  cat > "$VAULT/brain/skills/index.md" << TMPL
---
title: "Skills Catalog"
tags: [skills, slash-commands, catalog]
category: concept
created: $TODAY
updated: $TODAY
---

See [[MCP Servers Catalog]] for MCP dependencies.

| Type | Location | Invoked |
|------|----------|---------|
| Slash command | \`.claude/commands/<name>.md\` | \`/<name>\` |
| Global skill  | \`~/.claude/commands/<name>.md\` | \`/<name>\` everywhere |

## Custom Skills
<!-- name | what it does | scope | file path | MCP deps -->
TMPL
  ok "brain/skills/index.md"
fi

# ── .gitignore ────────────────────────────────────────────────────────────────
step ".gitignore"
if guard "$VAULT/.gitignore" ".gitignore"; then
  cat > "$VAULT/.gitignore" << 'TMPL'
sessions/*.jsonl
sessions/backup-*
.venv/
__pycache__/
*.pyc
.env
.env.*
.DS_Store
Thumbs.db
.vscode/
*.swp
TMPL
  ok "Created"
fi

# ── hooks/settings-snippet.json ───────────────────────────────────────────────
step "hooks/settings-snippet.json"
PYBIN="$VENV/bin/python"
cat > "$VAULT/hooks/settings-snippet.json" << TMPL
{
  "_comment": "Merge the 'hooks' object into ~/.claude/settings.json",
  "hooks": {
    "SessionStart": [{"matcher": "", "hooks": [
      {"type": "command", "command": "$PYBIN $VAULT/scripts/session-start.py", "timeout": 8000}
    ]}],
    "PreCompact": [{"matcher": "", "hooks": [
      {"type": "command", "command": "bash -c 'if [ -n \"\$CLAUDE_TRANSCRIPT_PATH\" ]; then cp \"\$CLAUDE_TRANSCRIPT_PATH\" $VAULT/sessions/backup-\$(date +%Y%m%d-%H%M%S).jsonl 2>/dev/null || true; fi'", "timeout": 5000}
    ]}],
    "Stop": [{"matcher": "", "hooks": [
      {"type": "command", "command": "bash -c '$PYBIN $VAULT/scripts/flush.py \"\$CLAUDE_TRANSCRIPT_PATH\" >> $VAULT/sessions/flush.log 2>&1 &'", "timeout": 3000}
    ]}]
  }
}
TMPL
ok "Written with resolved paths"

# ── Git init ──────────────────────────────────────────────────────────────────
step "Git"
if [[ ! -d "$VAULT/.git" ]]; then
  git -C "$VAULT" init -b main -q && ok "Initialized (branch: main)"
else skip "already a git repo"; fi

# ── Auth check ────────────────────────────────────────────────────────────────
step "Auth"
HAVE_AUTH=false
if command -v claude &>/dev/null && claude --version &>/dev/null 2>&1; then
  ok "Tier 1: Claude Code SDK (subscription)"; HAVE_AUTH=true
else warn "Tier 1 unavailable — install claude at https://claude.ai/code"; fi
if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
  ok "Tier 2: ANTHROPIC_API_KEY ✓"; HAVE_AUTH=true
else warn "Tier 2 unavailable — export ANTHROPIC_API_KEY=sk-ant-..."; fi
command -v claude &>/dev/null && { ok "Tier 3: claude -p subprocess ✓"; HAVE_AUTH=true; } || true
[[ "$HAVE_AUTH" == "false" ]] && { echo ""; echo "  No auth available — set up Tier 1 or 2 above before using the vault"; }

# ── GitHub repo setup ─────────────────────────────────────────────────────────
if [[ -n "$GH_USER" ]]; then
  step "GitHub"
  REPO_NAME="$(basename "$VAULT")"
  if git -C "$VAULT" remote get-url origin &>/dev/null 2>&1; then
    skip "remote origin already set"
  else
    read -rp "  Create private repo '$GH_USER/$REPO_NAME'? [y/N]: " _CR
    if [[ "${_CR:-N}" =~ ^[Yy] ]]; then
      if command -v gh &>/dev/null; then
        gh repo create "$GH_USER/$REPO_NAME" --private --source "$VAULT" --remote origin \
          && ok "Repo created + remote set" \
          || warn "gh failed — add remote manually: git remote add origin git@github.com:$GH_USER/$REPO_NAME.git"
      else
        echo "  gh CLI not installed. Add remote manually:"
        echo "    git -C $VAULT remote add origin git@github.com:$GH_USER/$REPO_NAME.git"
      fi
    fi
  fi
fi

# ── Merge hooks into settings.json ────────────────────────────────────────────
SETTINGS="$HOME/.claude/settings.json"
SNIPPET="$VAULT/hooks/settings-snippet.json"
step "Claude Code settings.json"
if [[ -f "$SETTINGS" ]]; then
  HAS=$(python3 -c "import json; d=json.load(open('$SETTINGS')); print('yes' if 'hooks' in d else 'no')" 2>/dev/null || echo "unknown")
  if [[ "$HAS" == "no" ]]; then
    read -rp "  No hooks in settings.json — auto-merge? [y/N]: " _M
    if [[ "${_M:-N}" =~ ^[Yy] ]]; then
      python3 - << PYEOF
import json
s = json.load(open('$SETTINGS'))
s['hooks'] = json.load(open('$SNIPPET'))['hooks']
json.dump(s, open('$SETTINGS', 'w'), indent=2)
print("  ✓ Hooks merged")
PYEOF
    else ok "Skipped — see $SNIPPET"; fi
  else warn "Hooks already present — merge manually if needed: $SNIPPET"; fi
else warn "~/.claude/settings.json not found — is Claude Code installed?"; fi

# ── Summary ────────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Setup complete                                              ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  Vault    : $VAULT"
echo "  Platform : $PLATFORM  |  Python: $PYBIN"
echo ""
echo "  Add to ~/.bashrc or ~/.zshrc:"
echo "    alias mem-save='cd $VAULT && git add -A && git commit -m \"session \$(date +%Y-%m-%d)\" && git push'"
echo ""
echo "  Start a Claude Code session → work normally → exit."
echo "  Check $VAULT/daily/ for your first extraction."
echo "  Check $VAULT/sessions/flush.log if nothing appears."
echo ""
