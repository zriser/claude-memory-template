# Windows Claude Code → WSL Hook Bridge

Claude Code has two surfaces on Windows:

1. **WSL CLI** (`claude` invoked from a WSL shell) — hooks fire reliably; nothing here is needed.
2. **Windows-native Claude Code** (`claude.exe`, or the Claude Desktop app's embedded Code surface) — runs from `C:\Users\<user>\.claude\` with its own `settings.json` and writes transcripts to a different folder than WSL Claude Code.

The vault's automation (`flush.py`, `compile.py`, `session-start.py`) lives in WSL. Without a bridge, Windows-side sessions skip all of it — daily logs never get written and session-start context never injects.

These scripts are the bridge. Each one is a thin shell wrapper that runs in WSL (via `wsl.exe`), translates paths if needed, and calls the same Python entrypoints the WSL hooks use.

## What's in this directory

| File | Purpose |
|---|---|
| `session-start.sh` | Pure pass-through to `session-start.py`; emits the context JSON Claude Code injects at session start. |
| `pre-compact.sh` | Snapshots the most recent Windows-side transcript to `sessions/backup-*.jsonl` before Claude Code compacts context. |
| `session-end.sh` | Translates `$CLAUDE_TRANSCRIPT_PATH` from `C:\...` to `/mnt/c/...` with `wslpath`, then fires `flush.py` in the background (`nohup ... &` + `disown`) so the hook returns instantly. |

A companion script — `../capture-latest.sh` — is the manual fallback for when hooks don't fire (see the Desktop-app caveat below).

## Wiring it up

### 1. Install the vault in WSL first

This bridge assumes you've already followed the main `README.md` to set up the Python venv and clone the vault. The defaults below assume:

- Vault at `~/claude-memory/`
- Venv at `~/.venvs/claude-memory/`
- WSL username matches your Windows username

If any of those are different, set `CLAUDE_MEMORY_DIR`, `CLAUDE_MEMORY_VENV`, or `WIN_USER` in the WSL shell environment (`~/.bashrc`) — every script in this directory respects them.

### 2. Edit `C:\Users\<you>\.claude\settings.json`

Add a `hooks` block:

```json
{
  "hooks": {
    "SessionStart": [{"matcher": "", "hooks": [{
      "type": "command",
      "command": "wsl.exe -e bash -ic \"bash ~/claude-memory/scripts/windows-hooks/session-start.sh\"",
      "timeout": 15000
    }]}],
    "PreCompact": [{"matcher": "", "hooks": [{
      "type": "command",
      "command": "wsl.exe -e bash -ic \"bash ~/claude-memory/scripts/windows-hooks/pre-compact.sh\"",
      "timeout": 10000
    }]}],
    "SessionEnd": [{"matcher": "", "hooks": [{
      "type": "command",
      "command": "wsl.exe -e bash -ic \"bash ~/claude-memory/scripts/windows-hooks/session-end.sh\"",
      "timeout": 5000
    }]}]
  }
}
```

The bumped timeouts (15s / 10s / 5s) account for `wsl.exe` startup overhead (~0.5–1.5s per invocation).

### 3. Verify

Open a Windows Claude Code session, exit it, then in WSL:

```bash
tail -20 ~/claude-memory/sessions/flush.log
ls -lt ~/claude-memory/daily/ | head
```

If today's daily log materialized, the bridge is operational.

## Why these particular shell incantations

### `bash -ic` (interactive)

Non-interactive bash does not source `~/.bashrc`. That file is where `ANTHROPIC_API_KEY` lives (when used as a fallback), and `flush.py` / `compile.py` need it for their Tier 2 auth path. Interactive mode (`-i`) sources `.bashrc` reliably; `-c` runs the command and exits.

### `nohup ... &` + `disown` in `session-end.sh`

`flush.py` typically takes 5–30s but can hit 2–3 minutes on dense transcripts. The 5-second hook timeout would kill it if run synchronously. We background and detach so the hook returns instantly, and the WSL init system keeps the orphaned Python alive until completion.

### `wslpath -a` in `session-end.sh`

Windows Claude Code passes a Windows-format path (`C:\Users\...`) in `$CLAUDE_TRANSCRIPT_PATH`; WSL Python expects POSIX (`/mnt/c/Users/...`). `wslpath -a` converts; if the path is already POSIX it's a safe no-op.

### `unset ANTHROPIC_API_KEY` in `session-end.sh`

Hooks run after the session exits — no concurrent-session conflict — so subscription OAuth (free, included with Claude Pro/Max) works. We unset the API key so flush.py uses the free path instead of billing pay-per-token API credits. Manual `capture-latest.sh` does the same thing, for the same reason.

## Caveats

- **Claude Desktop app's embedded Code surface fires hooks unreliably.** As of early 2026, closing the chat tab/window often produces no `SessionEnd` invocation. The hook wiring here is harmless if unused and ready if Anthropic ships hook reliability later — but for now, **Desktop-app sessions need manual capture** via `../capture-latest.sh` (run from any WSL terminal after closing the chat).
- **WSL distro must be running** for hooks to fire. If stopped, `wsl.exe` cold-boots it (~2–4s one-time). For heavy use, run any `wsl.exe echo` in a Windows terminal at startup to keep it warm.
- **Both surfaces write to the same `~/claude-memory/`.** Lockfiles in `compile.py` and `flush.py` handle concurrent access — no special coordination needed.
- **Username assumption.** The default `WIN_USER=$USER` only works when your Windows and WSL usernames match. They usually do for solo machines but not always — set `WIN_USER` explicitly in `~/.bashrc` if they differ.
