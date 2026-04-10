# claude-memory

Personal persistent knowledge base for Claude Code sessions. Based on Karpathy's LLM Wiki pattern — plain markdown files, git-tracked, with hooks that auto-extract insights using the Claude API.

## What This Is

Every Claude Code session produces knowledge: decisions made, patterns discovered, mistakes to avoid. Without a memory system, that knowledge evaporates when the session ends. This repo captures it automatically.

**The cycle:**
1. Work in a Claude Code session as normal
2. At session end, `flush.py` extracts insights from the transcript using Claude API
3. Insights land in `daily/YYYY-MM-DD.md`
4. After 6 PM, `compile.py` synthesizes daily logs into structured wiki articles
5. Next session starts with that context injected automatically via `session-start.py`

## Relationship to claude-context

| Repo | Contains | Loaded via |
|------|----------|------------|
| `~/.claude-context/` | WHO I AM — preferences, stack, working style | Symlinked CLAUDE.md |
| `~/claude-memory/` | WHAT I KNOW — decisions, patterns, lessons | Hooks + on-demand reads |
| `<repo>/CLAUDE.md` | PROJECT-SPECIFIC — goals, constraints, architecture | Committed in each repo |

## Authentication — Three Tiers

The scripts try three auth methods in order and use the first that works. You don't need to configure anything — the fallback chain is automatic.

| Tier | Method | Requires | Speed |
|------|--------|----------|-------|
| **1** | `claude_agent_sdk` — reads `~/.claude/.credentials.json` | Claude Code installed and authenticated | Fastest |
| **2** | `anthropic` SDK — direct API call | `ANTHROPIC_API_KEY` env var | Fast, pay-per-token |
| **3** | `claude -p` subprocess — shells out to the Claude Code CLI | `claude` in PATH, any Claude subscription | Slowest, most reliable |

**Tier 1** is the primary path. It uses the same credentials as your Claude Code CLI session — no separate API key needed. This is the same approach used by [claude-memory-compiler](https://github.com/coleam00/claude-memory-compiler).

> **Note on Tier 1 stability:** Anthropic's official position on using subscription credentials via the Agent SDK for scripted/background use is still evolving. Tier 1 works reliably today for personal use, but if it stops working (auth changes, rate limits, policy updates), Tiers 2 and 3 catch it automatically. You'll see a clear printed message about which tier is active.

**Tier 2** requires a separate API key from [console.anthropic.com](https://console.anthropic.com). Pay-per-token, separate from your Pro subscription. Optional but useful as a stable fallback:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
# Add to ~/.bashrc or ~/.zshrc for persistence
```

**Tier 3** shells out to `claude -p` (Claude Code headless/print mode). Works with any Claude subscription — Pro, Max, or Teams. Slower because it spawns a subprocess and waits for the full response, but has zero auth ambiguity. No configuration needed if `claude` is in your PATH.

```bash
# Check which tiers are available in your environment:
python3 scripts/utils.py  # not a runnable script, but imports work for testing
# Or just run any script — it prints the active tier at startup
```

## Setup

### Your own vault (first time)

```bash
git clone https://github.com/zriser/claude-memory-template.git ~/claude-memory
cd ~/claude-memory
bash setup-new-vault.sh
```

`setup-new-vault.sh` runs interactive prompts (name, GitHub username, model tier, vault
location), creates the directory structure, generates a personalized `CLAUDE.md`, sets up
the Python venv, and offers to auto-merge the Claude Code hooks into `~/.claude/settings.json`.

### Re-bootstrap on a new machine (existing vault)

```bash
git clone git@github.com:<you>/claude-memory.git ~/claude-memory
cd ~/claude-memory
bash bootstrap-memory.sh
```

`bootstrap-memory.sh` skips prompts — it assumes you already have a vault and just need
the venv, directory structure, and hook instructions re-created on the new machine.

### Adding hooks to Claude Code

After either script, edit `~/.claude/settings.json` and merge in the hooks from
`hooks/settings-snippet.json` (the file is generated with absolute paths resolved to
your vault and venv).

The three hooks are:
- **SessionStart** — injects recent context (memory index, last 3 days, active projects)
- **PreCompact** — backs up transcript before context compaction
- **Stop** — runs `flush.py` as a background process when you end the session

## Directory Structure

```
claude-memory/
├── CLAUDE.md           # Rules for how Claude interacts with this vault
├── MEMORY.md           # Auto-maintained index (pointers, ~50 lines max)
├── raw/                # Raw ingested sources (articles, transcripts, etc.)
├── wiki/               # LLM-compiled concept articles
│   └── index.md        # Master article index
├── daily/              # Daily session logs (YYYY-MM-DD.md)
├── brain/
│   ├── architecture/   # Architecture decisions
│   ├── decisions/      # Key decisions with context
│   ├── concepts/       # Concept articles
│   ├── people/         # People notes (1:1s, contacts)
│   ├── mcp-servers/    # MCP server catalog (index.md + per-server notes)
│   └── skills/         # Custom skills and slash commands catalog
├── work/
│   ├── active/         # Current projects (1-3 at a time)
│   └── archive/        # Completed work by year
├── patterns/           # Reusable approaches that worked
├── mistakes/           # What went wrong and why
├── sessions/           # Transcript backups (gitignored)
└── scripts/
    ├── flush.py        # Session end extractor
    ├── compile.py      # Daily → wiki compiler
    ├── query.py        # Knowledge base query tool
    ├── lint.py         # Health checks
    └── session-start.py # Session start context injector
```

## Usage

### Query the knowledge base

```bash
# Ask a question
.venv/bin/python scripts/query.py "What patterns do I use for Flask APIs?"

# Save the answer back to the wiki
.venv/bin/python scripts/query.py "What mistakes have I made with Docker?" --file-back

# Interactive mode
.venv/bin/python scripts/query.py --interactive

# List all articles
.venv/bin/python scripts/query.py --list
```

### Manual flush (if hook didn't run)

```bash
.venv/bin/python scripts/flush.py /path/to/transcript.jsonl
```

### Compile daily logs into wiki articles

```bash
# Dry run (see what would be written)
.venv/bin/python scripts/compile.py --dry-run

# Run for real
.venv/bin/python scripts/compile.py
```

### Lint / health check

```bash
# Basic checks (broken links, orphans, stale notes)
.venv/bin/python scripts/lint.py

# Include contradiction detection (uses API, slower)
.venv/bin/python scripts/lint.py --contradictions
```

## Multiple Environments

Because this is a plain git repo:

```bash
# On a new machine / LXC / code-server instance
git clone git@github.com:<you>/claude-memory.git ~/claude-memory
bash ~/claude-memory/bootstrap-memory.sh
```

Sync across machines:
```bash
cd ~/claude-memory
git pull  # get knowledge from other sessions
git add -A && git commit -m "session $(date +%Y-%m-%d)" && git push
```

Consider a daily cron or alias for the sync:
```bash
alias mem-save='cd ~/claude-memory && git add -A && git commit -m "session $(date +%Y-%m-%d)" && git push'
```

## Share with a Colleague

The vault structure and scripts are generic — only the content (daily logs, brain notes,
wiki articles) is personal. To give a colleague their own independent vault:

1. **They fork this repo** on GitHub (or you share the URL so they can clone it)
2. **They run the setup script:**
   ```bash
   git clone https://github.com/zriser/claude-memory-template.git ~/claude-memory
   cd ~/claude-memory
   bash setup-new-vault.sh
   ```
3. **Their vault is independent** — their own git repo, their own `daily/` logs, their
   own `brain/` notes. The scripts (`flush.py`, `compile.py`, etc.) and directory
   structure are shared from the fork; the knowledge is entirely theirs.
4. **They fill in their own context** — after setup, they should populate `CLAUDE.md`
   with their personal preferences (stack, working style) or link their own
   `~/.claude-context/` if they use that pattern.

The setup script handles:
- Personalizing `CLAUDE.md` with their name
- Generating empty starter catalog notes (`brain/mcp-servers/`, `brain/skills/`)
- Creating a fresh `MEMORY.md` (no content from your vault is copied)
- Setting up the Python venv and hooks with their paths

## Design Principles

- **Plain markdown** — no database, no proprietary format, grep-able forever
- **Git-tracked** — full history, works offline, deployable anywhere
- **Index-not-content** — MEMORY.md stays tiny (≤50 lines) so it fits in context every session
- **Background extraction** — flush.py never blocks your session exit
- **Graceful degradation** — if the API is unavailable, the session continues normally
- **Prune-friendly** — `lint.py` finds stale/orphaned notes so cleanup is easy

## Troubleshooting

**flush.py isn't running**
- Check `sessions/flush.log` for errors
- Verify `CLAUDE_TRANSCRIPT_PATH` env var is being set by the hook
- The script prints which auth tier it's using — check for Tier 1/2/3 messages in the log
- If all tiers fail: ensure `claude` is in PATH and you're logged in (`claude --version`)

**session-start.py output not appearing**
- Check that your hook output format matches Claude Code's expected JSON (`{"additionalContext": "..."}`)
- Test manually: `python3 scripts/session-start.py` — should print valid JSON

**compile.py not grouping notes well**
- It uses Claude to synthesize; the quality depends on how much content is in the daily logs
- Run `--dry-run` to preview without writing files

**Too much noise in daily logs**
- Edit the `EXTRACTION_PROMPT` in `flush.py` to tighten the criteria
- The default skips "routine code generation" and "basic Q&A" but you can tune it
