# Claude Memory System — Security & Cost Audit

**Audited by:** Claude (paranoid mode)
**Date:** 2026-04-11
**Scope:** All scripts in `~/claude-memory/scripts/`, hooks in `~/.claude/settings.json`, and `~/claude-memory/hooks/settings-snippet.json`
**Method:** Read-only analysis

---

## Executive Summary

The system has **no infinite loops** and **no true recursion** by design, but it carries several **high-severity** risks around unbounded cost accumulation (no session-level cap), orphaned background processes, feedback-loop-adjacent behavior via the compile trigger, and a missing hook (Stop/flush.py) that the settings-snippet documents but the live settings.json does not wire. The most dangerous single scenario is a long session flushed through Tier 1 (Agent SDK) with `compile.py` also spawned in the background — two concurrent AI invocations with no shared cost ceiling.

---

## File Inventory

| File | Hook Event | AI Call? | Runs |
|------|-----------|----------|------|
| `session-start.py` | SessionStart | No | Every session open |
| `flush.py` | Stop (hook not wired) | Yes — 1 call, 3 tiers | Every session close |
| `compile.py` | Spawned by flush.py after 6 PM | Yes — 1 call, 3 tiers | Nightly (conditionally) |
| `query.py` | Manual only | Yes — 1 call, 3 tiers | On demand |
| `lint.py` | Manual only | Optional — 1 call | On demand |
| `utils.py` | Library | No | — |
| PreCompact hook (inline bash) | PreCompact | No | Every compaction |

---

## Issue Register

---

### ISSUE-01 — Stop Hook Not Wired in Live settings.json

**Risk Level: HIGH**
**File:** `~/.claude/settings.json`

The live `settings.json` contains only `SessionStart` and `PreCompact` hooks. The `settings-snippet.json` documents a `Stop` hook that runs `flush.py`, but it is absent from the live file. This means `flush.py` never runs automatically.

**Immediate consequence:** No knowledge extraction happens. This is a functional failure, not a cost risk.

**Latent risk:** If the Stop hook is added later without understanding its cost profile, it will immediately begin making AI calls on every session exit — with no prior testing baseline. The operator may not notice the change for days.

**Worst-case cost if wired:** See ISSUE-03.

**Recommendation:** Wire the Stop hook deliberately, test with a short session first, review flush.log.

---

### ISSUE-02 — flush.py Spawns compile.py as an Orphaned Background Process

**Risk Level: HIGH**
**File:** `scripts/flush.py`, `maybe_trigger_compile()`

```python
subprocess.Popen(
    [sys.executable, str(compile_script)],
    stdout=open(LOG_FILE.parent / "compile.log", "a"),
    stderr=subprocess.STDOUT,
    start_new_session=True,   # ← detaches from parent
)
```

`flush.py` itself runs as a background process (`&` in the hook command). It then spawns `compile.py` via `Popen` with `start_new_session=True`. This means:

1. `flush.py` background process → spawns → `compile.py` background process (truly orphaned, no parent to kill it)
2. No timeout on `compile.py`. The Agent SDK call in `_compile_with_sdk` uses `max_turns=30` with no wall-clock timeout.
3. No cost guard in `compile.py` (unlike `flush.py` which has `COST_ABORT_USD = 2.00`).
4. The compile log (`sessions/compile.log`) is opened with `open(...)` inside the Popen call — no error handling. If the sessions directory doesn't exist, this raises an exception before Popen runs, silently failing.

**Worst-case scenario:** compile.py runs 30 Agent SDK turns against a large vault at ~$0.01–0.05/turn = **up to $1.50 per nightly compile** with no abort threshold. If triggered every session after 6 PM, and sessions are frequent, this accumulates without any cap.

**Concurrency:** If two sessions end after 6 PM within seconds of each other, two `compile.py` instances are spawned simultaneously. Both will attempt to read the same uncompiled logs, write to the same files, and both will mark logs as compiled — race condition on file writes.

---

### ISSUE-03 — No Session-Level or Daily Cost Ceiling

**Risk Level: HIGH**
**Files:** `scripts/flush.py`, `scripts/compile.py`

`flush.py` has per-invocation cost thresholds:
- `COST_WARN_USD = 0.50` (logs warning, continues)
- `COST_ABORT_USD = 2.00` (logs error, returns None)

These thresholds are checked **after** the API call returns. They do not prevent a single expensive call — they only prevent writing results to disk. The API cost is already incurred.

`compile.py` has **no cost guard at all**. `_compile_with_sdk()` returns a `total_cost` float but nothing checks it against a threshold.

There is no daily cap, no monthly cap, no rate limiter across sessions.

**Worst-case cost per day (Stop hook wired, active day):**
- 10 sessions × flush.py Tier 1 cost: 10 × ~$0.10–0.30 = **$1–3**
- 1 compile.py trigger (post-6pm, 30 turns): ~$0.50–1.50
- Total: **$1.50–$4.50/day** with zero alerting beyond a log file nobody monitors in real-time

---

### ISSUE-04 — Tier Cascade Triples API Cost on Failure

**Risk Level: MEDIUM**
**Files:** `scripts/flush.py`, `scripts/compile.py`

Both scripts implement a 3-tier fallback: Agent SDK → ANTHROPIC_API_KEY → `claude -p`. If Tier 1 raises an exception mid-stream, Tier 2 is invoked with the same transcript/prompt. If Tier 2 fails, Tier 3 runs.

In a degraded environment (flaky Agent SDK), all three tiers fire sequentially on every session exit. Each is a full AI inference call with the same ~80,000 character input.

**Worst-case:** 3 full API calls per session exit at ~$0.30 each = **~$0.90/session** instead of $0.10.

---

### ISSUE-05 — PreCompact Hook: Unbound Transcript File Growth

**Risk Level: MEDIUM**
**File:** `~/.claude/settings.json`, PreCompact hook

This hook copies the most recent transcript to `sessions/backup-YYYYMMDD-HHMMSS.jsonl` on every context compaction. There is no:
- Rotation or max file count
- Size limit per backup
- Cleanup script

**Worst-case:** 100 compactions × 50 MB transcript = **5 GB** in `sessions/` with no automated cleanup. (The `.gitignore` correctly excludes these, so no git bloat — but disk fills up.)

---

### ISSUE-06 — compile.py Agent SDK: max_turns=30 with No Wall-Clock Timeout

**Risk Level: MEDIUM**
**File:** `scripts/compile.py`

`_compile_with_sdk` is called from `asyncio.run()` with no `asyncio.wait_for()` timeout wrapper. If the Agent SDK hangs, the coroutine runs indefinitely. Since `compile.py` is spawned with `start_new_session=True`, there is no parent process timeout to enforce termination.

**Worst-case:** compile.py hangs overnight holding subscription quota.

---

### ISSUE-07 — Recursion Risk: compile.py Runs Inside ~/claude-memory/

**Risk Level: MEDIUM**
**File:** `scripts/compile.py`

`_compile_with_sdk` launches an Agent SDK session with `cwd=str(VAULT)` and the Claude Code preset. This means the sub-agent loads `~/claude-memory/CLAUDE.md` as project context and runs with full Claude Code context.

If the sub-agent's session produces a transcript in a monitored path, `flush.py` could be invoked for the compile sub-agent's own transcript — a 2-level chain. Not infinite (the second sub-session would not trigger compile again), but it doubles extraction cost unpredictably.

**Current mitigation:** PreCompact hook excludes `*/subagents/*`. But compile.py is not a standard subagent invocation — behavior is uncertain.

---

### ISSUE-08 — session-start.py: MEMORY.md Read with No Size Limit

**Risk Level: LOW-MEDIUM**
**File:** `scripts/session-start.py`

`get_memory_index_preview()` reads MEMORY.md with **no character cap**. The 50-line rule is informal, enforced only by Claude's judgment. A bloated MEMORY.md is injected as `additionalContext` at every SessionStart.

**Worst-case:** A 100KB MEMORY.md adds ~25,000 tokens to every session's context, inflating costs by ~$0.075/session at Sonnet pricing.

---

### ISSUE-09 — compile.py: _update_memory() Line Count Logic Error

**Risk Level: LOW-MEDIUM**
**File:** `scripts/compile.py`

The `len(lines) > 45` guard strips comment lines and headings before counting. A MEMORY.md that is 200 lines long but 160 are `#` headings will pass the guard and have content appended. MEMORY.md can grow without bound across compile cycles.

---

### ISSUE-10 — query.py: --file-back Write Tool Not Scoped to wiki/

**Risk Level: LOW**
**File:** `scripts/query.py`

When `--file-back` is used, `"Write"` is added to `allowed_tools` with no path restriction in the SDK options. The prompt instructs writing to `wiki/qa-*` but a manipulated prompt could direct writes elsewhere in the vault.

---

### ISSUE-11 — flush.py: COST_ABORT_USD Check Is Post-Hoc (Write-Gate, Not Spend-Gate)

**Risk Level: LOW**
**File:** `scripts/flush.py`

`_check_cost()` is called after `_extract_with_sdk()` returns. The API call already completed — the money is already spent. The "abort" only prevents writing the result to disk, not incurring the cost.

---

### ISSUE-12 — Tier 3 (claude -p) Has 180s Timeout, No Kill on Parent Exit

**Risk Level: LOW**
**File:** `scripts/flush.py`, `scripts/utils.py`

`call_claude_pipe` uses `subprocess.run(..., timeout=180)` — properly handled. But behavior on SIGHUP (parent shell exits in bare terminal) is environment-dependent. Safe in tmux/WSL2 generally, but not guaranteed.

---

### ISSUE-13 — No Input Sanitization on Transcript Path (Latent Shell Injection)

**Risk Level: LOW** (current wiring is correctly quoted)
**File:** `~/.claude/settings.json` Stop hook (when re-wired)

The Stop hook passes `$CLAUDE_TRANSCRIPT_PATH` as a shell variable. Current double-quoting is correct. A copy-paste error removing quotes would create a shell injection vector. `flush.py`'s `load_transcript()` does no path sanitization beyond `Path(path).exists()`.

---

## Concurrency Analysis

| Scenario | Outcome |
|----------|---------|
| Two sessions start simultaneously | Pure reads, no conflict. Safe. |
| Two sessions end within seconds of each other (post-6pm) | Two flush.py + two compile.py spawn. Race on daily log append and `_mark_compiled()`. **HIGH RISK.** |
| flush.py runs while compile.py is reading | compile.py may process a partial log entry. No file locking anywhere. |
| compile.py runs twice on same logs | Second instance likely finds nothing to compile (already marked). Probably safe in practice, but racy. |

---

## Recursion / Feedback Loop Analysis

```
Claude Code session
  └─► Stop hook fires → flush.py (background)
        └─► Tier 1: Agent SDK session (cwd=~/claude-memory)
              → Loads ~/claude-memory/CLAUDE.md as project context
              → Does NOT trigger Stop hook (not a full Claude Code CLI session)
              → Writes to daily log
        └─► maybe_trigger_compile() → compile.py (background, orphaned)
              └─► Tier 1: Agent SDK session (cwd=~/claude-memory)
                    → Reads uncompiled logs, writes wiki articles
                    → Does NOT trigger Stop hook
                    → Terminates
```

**Verdict:** Two levels deep, not infinite. Agent SDK sub-sessions do not fire Claude Code hooks in current implementation. **Latent risk:** if `claude -p` (Tier 3) *does* trigger hooks for its own session, the Stop hook fires for the flush sub-invocation — true recursive loop. This is a future-behavior risk, not current.

---

## Worst-Case Cost Estimates

| Scenario | Cost Estimate |
|----------|--------------|
| Single session flush (Tier 1, 80K char transcript) | $0.08–0.25 |
| Single session flush, all 3 tiers fire (degraded) | $0.25–0.90 |
| Nightly compile via Agent SDK (30 turns, large vault) | $0.50–1.50 |
| flush.py cost check fires AFTER abort threshold exceeded | Up to $2.00 per invocation before abort |
| 10 sessions/day with flush+compile | **$1.50–$4.50/day** |
| MEMORY.md grows to 100KB, injected every session | +$0.075/session context inflation |
| Two concurrent flush + compile on session exit | 2× cost of above with file race conditions |
| **Monthly worst case (active usage, no monitoring)** | **$45–$135/month** |

---

## Priority Remediation Checklist

1. **[HIGH]** ~~Add a cost guard in `compile.py` equivalent to flush.py's `COST_ABORT_USD`. Cap at $1.00.~~ **RESOLVED 2026-04-11** — Added `COST_WARN_USD = 0.50`, `COST_ABORT_USD = 1.00`, and `_check_cost()` to compile.py. Applied after Tier 1 SDK call.
2. **[HIGH]** ~~Wrap `_compile_with_sdk` in `asyncio.wait_for(..., timeout=300)` to prevent indefinite hangs.~~ **RESOLVED 2026-04-11** — Refactored into `_compile_with_sdk_inner` + `_compile_with_sdk` wrapper using `asyncio.wait_for(timeout=300)`. `asyncio.TimeoutError` is caught and falls through to Tier 2.
3. **[HIGH]** ~~Add a lockfile (`/tmp/claude-memory-compile.lock`) in `maybe_trigger_compile()` to prevent duplicate compile spawns from concurrent session exits.~~ **RESOLVED 2026-04-11** — `COMPILE_LOCK_FILE = /tmp/claude-memory-compile.lock` acquired with `fcntl.flock LOCK_EX | LOCK_NB` at top of `compile.py main()`. Also added `FLUSH_LOCK_FILE = /tmp/claude-memory-flush.lock` to flush.py.
4. **[HIGH]** ~~Add file locking (`fcntl.flock`) in `write_daily_log()` and `compile.py`'s `_mark_compiled()` to prevent concurrent write races.~~ **RESOLVED 2026-04-11** — `write_daily_log()` in flush.py uses `LOCK_EX | LOCK_NB` on the open file handle; skips write with a warning if lock unavailable. `_mark_compiled()` in compile.py uses `r+` mode with `LOCK_EX | LOCK_NB`; skips file with warning if locked.
5. **[MEDIUM]** ~~Add a character cap (~4,000 chars) to `get_memory_index_preview()` in `session-start.py`.~~ **RESOLVED 2026-04-11** — Added `max_chars=4000` cap with `...[truncated]` suffix.
6. **[MEDIUM]** ~~Add backup rotation to the PreCompact hook: prune `sessions/backup-*` files older than 30 days.~~ **RESOLVED 2026-04-11** — Added `rotate_session_backups(max_age_days=30)` to `session-start.py`, called at startup. Uses `st_mtime` comparison, deletes `backup-*.jsonl` files older than 30 days.
7. **[MEDIUM]** ~~Fix `_update_memory()` line count logic to count ALL non-blank lines, not just non-heading/non-comment lines.~~ **RESOLVED 2026-04-11** — Changed from `[l for l in ... if l and not l.startswith("#") and not l.startswith("<!--")]` to `[l for l in ... if l.strip()]`.
8. **[LOW]** ~~Add a daily cost accumulator log so total spend per day is visible without parsing individual entries.~~ **RESOLVED 2026-04-11** — Added `write_cost_log(tier, cost_usd)` to both flush.py and compile.py. Appends `{iso_timestamp} {script} {tier} ${cost:.4f}` lines to `sessions/cost.log` after each run.
9. **[LOW]** ~~Add a comment to `COST_ABORT_USD` clarifying it is a write-gate (post-hoc), not a spend-gate.~~ **RESOLVED 2026-04-11** — Added inline comment above both constants in flush.py and compile.py.
10. **[LOW]** `query.py --file-back` Write tool not scoped to `wiki/` — open, not addressed in this pass.

**Additional fix (from tmux analysis):**
- **Stop hook removed from `hooks/settings-snippet.json`** — **RESOLVED 2026-04-11**. Template now documents only SessionStart and PreCompact. Inline comment explains Stop hook is dangerous in tmux (fires per-response, not per-session). Live `~/.claude/settings.json` already had Stop hook removed.
- **Wall-clock SIGALRM timeouts** — **RESOLVED 2026-04-11**. flush.py: 120s limit. compile.py: 300s limit. Both use `signal.SIGALRM` + `_timeout_handler` to log and `sys.exit(1)` if exceeded.

---

## Files Audited

- `/home/zach/.claude/settings.json`
- `/home/zach/claude-memory/hooks/settings-snippet.json`
- `/home/zach/claude-memory/scripts/session-start.py`
- `/home/zach/claude-memory/scripts/flush.py`
- `/home/zach/claude-memory/scripts/compile.py`
- `/home/zach/claude-memory/scripts/query.py`
- `/home/zach/claude-memory/scripts/lint.py`
- `/home/zach/claude-memory/scripts/utils.py`
- `/home/zach/claude-memory/setup-new-vault.sh`
- `/home/zach/claude-memory/bootstrap-memory.sh`
- `/home/zach/claude-memory/.gitignore`
- `/home/zach/claude-memory/CLAUDE.md`

---

## tmux Interaction Analysis

### 1. `start_new_session=True` and tmux Process Group Semantics

**What `start_new_session=True` does at the kernel level:**

When Python's `subprocess.Popen` is called with `start_new_session=True`, it instructs the child process to call `setsid(2)` immediately after `fork(2)` and before `exec(2)`. The `setsid` syscall does three things atomically:
1. Creates a new POSIX session with the child as session leader.
2. Creates a new process group containing only the child, with the child as process group leader.
3. Detaches the child from any controlling terminal.

**Does this detach compile.py from the tmux pane's process group?**

Yes, completely. After `setsid`, compile.py is in its own new session with its own process group — it has no parent session and no controlling terminal. From the kernel's perspective, it is structurally equivalent to a daemon. The tmux pane's PTY master being closed sends `SIGHUP` to the controlling process group of that PTY — but compile.py, having no controlling terminal after `setsid`, does not receive `SIGHUP` from PTY closure.

**Critical caveat:** This applies to compile.py spawned by flush.py. flush.py itself is spawned by the Stop hook via `bash -c '... &'`. That `&` backgrounds flush.py within bash's process group, which is still in the tmux pane's session. Whether flush.py is fully detached from the pane's session depends on whether Claude Code's hook runner closes its file descriptors and disowns the subprocess — this is implementation-dependent and not guaranteed. flush.py does not call `setsid` itself; only its child compile.py does.

---

### 2. tmux Detach vs Exit: Stop Hook and In-Flight Processes

**Does the Stop hook fire on tmux detach (Ctrl-B D)?**

No. `Ctrl-B D` sends a detach command to the tmux server, which suspends the client's connection to the tmux session. The tmux session and all its pane processes continue running unaffected. Claude Code is not signaled, does not receive `SIGHUP`, and is not aware the user detached. The Stop hook does not fire.

**Does `SessionEnd` fire on detach?**

No. Claude Code hooks fire in response to Claude Code's own lifecycle events, not in response to the terminal multiplexer state. tmux detach is invisible to processes inside the pane.

**What happens to in-flight background processes on detach?**

Nothing changes. Any flush.py or compile.py processes that were already spawned continue running. Their stdin/stdout are already redirected to log files. The pane's PTY master remains open because the tmux server holds it; the PTY is only closed when the tmux pane itself is destroyed.

---

### 3. Stop Hook Firing on Every Response: Process Accumulation in tmux

**Note:** The active `~/.claude/settings.json` does **not** wire a Stop hook. The analysis below assumes the snippet's Stop hook were added.

**Accumulation math for a 50-turn session:**

The Stop hook fires on every assistant response that Claude Code considers a stop event — up to once per turn. In a 50-turn session that is up to 50 flush.py invocations. The hook runner respects the 3000ms timeout, then returns regardless — the `&` ensures bash exits before flush.py completes. So each invocation leaves a flush.py running in the background.

In a fast-paced session where turns come faster than flush.py's LLM call completes (seconds to minutes per call), processes stack up. A 50-turn session could accumulate O(10–30) concurrent flush.py processes. If it's after 6pm, each one that completes also spawns a compile.py.

**Does tmux change accumulation behavior?**

Not materially. The key difference is orphan reaping: orphaned processes (parent has exited) are reparented to PID 1 (WSL2 init) and reaped when they exit. The `&` in the bash hook means bash exits immediately, so flush.py is reparented to init promptly. This behavior is identical in tmux and a bare terminal — the PTY layer does not affect zombie reaping.

---

### 4. compile.py Orphan Survival Under Termination Scenarios

compile.py is spawned with `start_new_session=True`, stdout redirected to `compile.log` via `open()` in the parent, stderr to `STDOUT`. It has no controlling terminal. Survival by scenario:

| Termination event | compile.py survives? | Reason |
|---|---|---|
| Claude Code exits | **Yes** | compile.py is not in Claude Code's process group |
| tmux pane closure (`kill-pane`) | **Yes** | PTY SIGHUP goes to foreground pgrp; compile.py has no controlling terminal post-setsid |
| tmux session detach | **Yes** | Detach doesn't close PTY; no signals sent |
| `tmux kill-server` | **Yes** | tmux server death closes PTY masters, but compile.py has no controlling terminal; kernel doesn't SIGHUP it |
| `wsl --shutdown` | **No** | Entire WSL2 VM is terminated |
| System reboot | **No** | |
| Explicit `kill -9 <pid>` | **No** | |

**Conclusion:** compile.py is effectively a daemon for its operational lifetime. Under normal WSL2 usage, a hung or runaway compile.py requires manual intervention (`pkill -f compile.py`) to terminate.

---

### 5. Multi-Pane Concurrent Exit: Lockfile Race and Log Append Race

**Setup:** Two Claude Code sessions in separate tmux panes (F1, F2) both exit simultaneously, each spawning a flush.py.

**Without a lockfile — compile.py spawn race:**

```
F1: hour >= 18 → True → spawns C1
F2: hour >= 18 → True → spawns C2   (milliseconds later)
```

C1 and C2 run concurrently. Both read the same uncompiled daily logs, both invoke the LLM, and both attempt to write wiki articles and mark logs `compiled: true`. Results:
- **Double LLM cost** — two full compile runs.
- **`_write_article()` collision** — if both check file existence before either writes, both do a fresh write; the second overwrites the first silently.
- **`_update_wiki_index()` duplicates** — both read the index before either writes; the `if entry not in existing` check is not atomic, producing duplicate entries.
- **`_update_memory()` collision** — both attempt MEMORY.md updates concurrently.

Vulnerability window: the entire duration of compile.py execution (potentially minutes).

**With a lockfile at `/tmp/claude-memory-compile.lock` (using `fcntl.flock LOCK_EX | LOCK_NB`):**

- C1 acquires the lock, proceeds.
- C2 fails to acquire lock (non-blocking), exits immediately.
- Result: single compile run, no double-cost, no write collisions.

**What the lockfile does NOT fix — `write_daily_log()` append race:**

The lockfile governs compile.py spawning only. The two flush.py processes (F1 and F2) each independently call `write_daily_log()`:

```python
with open(log_path, "a", encoding="utf-8") as f:
    f.write("\n".join(sections))
```

`O_APPEND` gives atomic seek-to-end-then-write semantics for writes ≤ `PIPE_BUF` (4096 bytes on Linux). For larger writes, POSIX does not guarantee atomicity — two concurrent writes can interleave on the byte level. On ext4/WSL2 DrvFs, VFS-layer serialization makes interleaving unlikely in practice, but it is not guaranteed. **The compile lockfile does not protect this race at all.** A separate `fcntl.flock` on the daily log file is needed to make concurrent `write_daily_log()` calls safe.

**Race coverage summary:**

| Race condition | Protected by compile lockfile? |
|---|---|
| Double compile.py spawn | Yes |
| Double LLM API cost from compile | Yes |
| `_write_article()` write collision | Yes |
| `_update_wiki_index()` duplicate entries | Yes |
| `write_daily_log()` concurrent append (two flush.py) | **No** |
| `_update_memory()` concurrent write | Yes (only one compile runs) |
