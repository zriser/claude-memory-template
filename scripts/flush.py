#!/usr/bin/env python3
"""
flush.py — Session-end knowledge extractor.

Reads the session transcript, uses the Claude Agent SDK (via Claude Code's
built-in credentials) to extract decisions, patterns, mistakes, and concepts,
then appends them to the daily log.

Must run as a background process — does not block session exit.

WARNING: Do NOT wire this to the Stop hook. Stop fires on every assistant
response, not just at session end, causing O(N) concurrent process accumulation
per session in tmux and other multiplexed environments. Use SessionEnd only.

Primary:  claude_agent_sdk  (uses ~/.claude/.credentials.json — no API key needed)
Fallback: anthropic SDK     (requires ANTHROPIC_API_KEY env var)

Usage: python3 flush.py [transcript_path]
       Or set CLAUDE_TRANSCRIPT_PATH env var.
"""

import asyncio
import datetime
import fcntl
import json
import logging
import logging.handlers
import os
import signal
import sys
import traceback
from pathlib import Path

# Shared auth utilities (Tier 3: claude -p fallback)
sys.path.insert(0, str(Path(__file__).parent))
from utils import call_claude_pipe, AGENT_SDK_AVAILABLE as _SDK_AVAIL  # noqa: E402

# ── Config ────────────────────────────────────────────────────────────────────

VAULT = Path(__file__).parent.parent
DAILY_DIR = VAULT / "daily"
LOG_FILE = VAULT / "sessions" / "flush.log"
COST_LOG_FILE = VAULT / "sessions" / "cost.log"
FLUSH_LOCK_FILE = Path("/tmp/claude-memory-flush.lock")
MAX_TRANSCRIPT_CHARS = 80_000
COST_WARN_USD = 0.50
# NOTE: This is a write-gate, not a spend-gate. The API call completes before
# this threshold is checked; only the result write is aborted.
COST_ABORT_USD = 2.00
WALL_CLOCK_LIMIT = 120  # seconds — SIGALRM kills the process if exceeded

EXTRACTION_PROMPT = """\
You are a knowledge extractor for a personal memory system. Analyze this Claude \
Code session transcript and extract durable knowledge worth preserving.

Extract ONLY items that are:
- Non-obvious decisions with clear rationale
- Patterns or approaches that worked and are reusable
- Mistakes, bugs, or dead-ends worth remembering to avoid
- New concepts or mental models that were established
- Project status changes (completed milestones, blockers, pivots)

SKIP:
- Routine code generation without a lesson
- Basic Q&A that is easily re-discoverable
- Anything highly specific to a one-off task with no generalizable insight
- Error messages that were immediately fixed with no insight

Output a JSON object with this exact structure (no markdown fences):
{
  "decisions": [
    {"title": "...", "summary": "...", "rationale": "...", "tags": [...]}
  ],
  "patterns": [
    {"title": "...", "summary": "...", "when_to_use": "...", "tags": [...]}
  ],
  "mistakes": [
    {"title": "...", "summary": "...", "root_cause": "...", "fix": "...", "tags": [...]}
  ],
  "concepts": [
    {"title": "...", "summary": "...", "tags": [...]}
  ],
  "project_updates": [
    {"project": "...", "update": "...", "tags": [...]}
  ]
}

For project_updates: capture any meaningful progress, blockers, or pivots on a named project.
Examples: "made progress on X", "deployed Y", "blocked on Z", "paused after finishing X".
The "project" field should match a project name (matching a file in work/active/).
These are the signal compile.py uses to keep work/active/ files current.

Return an empty array for any category with nothing worth capturing.
Keep each item concise — 2-4 sentences max per field.

TRANSCRIPT:
"""

# ── SDK availability ───────────────────────────────────────────────────────────

AGENT_SDK_AVAILABLE = _SDK_AVAIL
if AGENT_SDK_AVAILABLE:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query as sdk_query,
    )

# ── Logging ───────────────────────────────────────────────────────────────────


def setup_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        str(LOG_FILE),
        maxBytes=500_000,  # 500 KB per file
        backupCount=2,  # keeps flush.log, flush.log.1, flush.log.2
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)


# ── Wall-clock timeout ────────────────────────────────────────────────────────


def _timeout_handler(signum: int, frame) -> None:
    logging.error(f"flush.py exceeded {WALL_CLOCK_LIMIT}s wall-clock limit — aborting")
    sys.exit(1)


# ── Cost logging ──────────────────────────────────────────────────────────────


def write_cost_log(tier: str, cost_usd: float) -> None:
    """Append one cost record to sessions/cost.log."""
    try:
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        COST_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(COST_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{ts} flush.py {tier} ${cost_usd:.4f}\n")
    except Exception:
        pass  # cost log is best-effort


# ── Transcript loading ─────────────────────────────────────────────────────────


def load_transcript(path: str) -> str | None:
    """Load and trim a JSONL transcript file into readable turn text."""
    try:
        p = Path(path)
        if not p.exists():
            logging.warning(f"Transcript not found: {path}")
            return None

        lines = p.read_text(encoding="utf-8").strip().splitlines()
        if not lines:
            logging.warning(f"Transcript is empty: {path}")
            return None

        turns = []
        for line in lines:
            try:
                entry = json.loads(line)
                # Claude Code format: {type: "user"|"assistant", message: {role, content}}
                # Fallback: flat {role, content}
                msg = (
                    entry.get("message")
                    if isinstance(entry.get("message"), dict)
                    else entry
                )
                role = msg.get("role", "")
                content = msg.get("content", "")
                if isinstance(content, list):
                    text_parts = [
                        block.get("text", "")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                    content = " ".join(text_parts)
                if role and content:
                    turns.append(f"[{role.upper()}]: {content}")
            except json.JSONDecodeError:
                continue

        text = "\n\n".join(turns)
        if len(text) > MAX_TRANSCRIPT_CHARS:
            text = "...[trimmed]...\n\n" + text[-MAX_TRANSCRIPT_CHARS:]
        return text

    except Exception as e:
        logging.error(f"Failed to load transcript: {e}")
        return None


# ── Extraction — Agent SDK (primary) ──────────────────────────────────────────


async def _extract_with_sdk(transcript: str) -> tuple[str, float]:
    """Run extraction via the Claude Agent SDK (no API key required).

    Returns (raw_text, cost_usd).
    """
    result = ""
    cost_usd = 0.0
    stderr_lines: list[str] = []

    async for message in sdk_query(
        prompt=EXTRACTION_PROMPT + transcript,
        options=ClaudeAgentOptions(
            cwd=str(VAULT),
            allowed_tools=[],
            max_turns=2,
            permission_mode="acceptEdits",
            stderr=lambda line: stderr_lines.append(line),
        ),
    ):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    result += block.text
        elif isinstance(message, ResultMessage):
            cost_usd = message.total_cost_usd or 0.0
            if cost_usd:
                logging.info(f"SDK cost: ${cost_usd:.4f}")

    if stderr_lines:
        logging.warning(f"SDK stderr: {''.join(stderr_lines[:20])}")

    return result.strip(), cost_usd


# ── Extraction — anthropic SDK (fallback) ─────────────────────────────────────


def _extract_with_api(transcript: str) -> str | None:
    """Fallback extraction via direct Anthropic API (requires ANTHROPIC_API_KEY)."""
    try:
        import anthropic
    except ImportError:
        logging.error("Neither claude_agent_sdk nor anthropic package is installed")
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logging.error("Fallback failed: ANTHROPIC_API_KEY not set")
        return None

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": EXTRACTION_PROMPT + transcript}],
        )
        return response.content[0].text.strip()
    except anthropic.AuthenticationError:
        logging.error("Fallback failed: ANTHROPIC_API_KEY is invalid")
        return None
    except Exception as e:
        logging.error(f"Fallback API call failed: {e}")
        return None


# ── Dispatcher ─────────────────────────────────────────────────────────────────


def _check_cost(cost_usd: float, tier: str) -> bool:
    """Log warning or abort based on cost thresholds. Returns False if aborted.

    NOTE: This is a write-gate. The API call has already completed and the cost
    is already incurred. Aborting here only prevents writing the result to disk.
    """
    write_cost_log(tier, cost_usd)
    if cost_usd >= COST_ABORT_USD:
        logging.error(
            f"Cost guard triggered: ${cost_usd:.4f} exceeds abort threshold "
            f"(${COST_ABORT_USD:.2f}) — aborting flush"
        )
        return False
    if cost_usd >= COST_WARN_USD:
        logging.warning(
            f"Cost guard: ${cost_usd:.4f} exceeds warning threshold (${COST_WARN_USD:.2f})"
        )
    return True


def extract_knowledge(transcript: str) -> dict | None:
    """Extract knowledge, preferring Agent SDK, falling back to direct API.

    Attempts each auth tier exactly once. No retry loop.
    """
    raw: str | None = None

    # Tier 1: Agent SDK
    if AGENT_SDK_AVAILABLE:
        logging.info("Tier 1: Claude Agent SDK")
        print(
            "flush.py: Tier 1 — Claude Agent SDK (subscription credentials)", flush=True
        )
        try:
            raw, cost = asyncio.run(_extract_with_sdk(transcript))
            if not _check_cost(cost, "tier1-sdk"):
                return None
        except Exception as e:
            logging.error(f"Tier 1 failed: {e}\n{traceback.format_exc()}")
            print(
                f"flush.py: Tier 1 failed ({type(e).__name__}), trying Tier 2",
                flush=True,
            )

    # Tier 2: ANTHROPIC_API_KEY
    if raw is None and os.environ.get("ANTHROPIC_API_KEY"):
        logging.info("Tier 2: ANTHROPIC_API_KEY")
        print("flush.py: Tier 2 — ANTHROPIC_API_KEY", flush=True)
        try:
            raw = _extract_with_api(transcript)
            if raw is not None:
                write_cost_log("tier2-api", 0.0)  # direct API cost not tracked here
        except Exception as e:
            logging.error(f"Tier 2 failed: {e}")
            print(
                f"flush.py: Tier 2 failed ({type(e).__name__}), trying Tier 3",
                flush=True,
            )

    # Tier 3: claude -p subprocess
    if raw is None:
        logging.info("Tier 3: claude -p subprocess")
        print("flush.py: Tier 3 — claude -p subprocess fallback", flush=True)
        raw = call_claude_pipe(EXTRACTION_PROMPT + transcript, timeout=180)
        if raw is not None:
            write_cost_log("tier3-pipe", 0.0)

    if not raw:
        return None

    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0]

    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError as e:
        logging.error(
            f"Failed to parse extraction JSON: {e}\nRaw response:\n{raw[:500]}"
        )
        return None


# ── Daily log writer ───────────────────────────────────────────────────────────


def write_daily_log(
    knowledge: dict, date: datetime.date, transcript_path: str = ""
) -> Path:
    """Append extracted knowledge to today's daily log.

    Uses fcntl.flock (non-blocking) to serialize concurrent writes from
    multiple flush.py instances. Skips write if lock cannot be acquired.
    """
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    log_path = DAILY_DIR / f"{date.isoformat()}.md"
    now = datetime.datetime.now().strftime("%H:%M")
    sections = []

    if not log_path.exists():
        sections.append(f"""\
---
title: "Daily Log {date.isoformat()}"
date: {date.isoformat()}
compiled: false
---

# Session Log — {date.strftime("%A, %B %d, %Y")}
""")

    sections.append(
        f"\n## Session extracted at {now}\n**source:** {Path(transcript_path).name}\n"
    )

    for item in knowledge.get("decisions", []):
        sections.append(f"""\
### [DECISION] {item["title"]}
**Summary:** {item["summary"]}
**Rationale:** {item["rationale"]}
**Tags:** {", ".join(item.get("tags", []))}
""")

    for item in knowledge.get("patterns", []):
        sections.append(f"""\
### [PATTERN] {item["title"]}
**Summary:** {item["summary"]}
**When to use:** {item["when_to_use"]}
**Tags:** {", ".join(item.get("tags", []))}
""")

    for item in knowledge.get("mistakes", []):
        sections.append(f"""\
### [MISTAKE] {item["title"]}
**Summary:** {item["summary"]}
**Root cause:** {item["root_cause"]}
**Fix:** {item["fix"]}
**Tags:** {", ".join(item.get("tags", []))}
""")

    for item in knowledge.get("concepts", []):
        sections.append(f"""\
### [CONCEPT] {item["title"]}
**Summary:** {item["summary"]}
**Tags:** {", ".join(item.get("tags", []))}
""")

    for item in knowledge.get("project_updates", []):
        sections.append(f"""\
### [PROJECT] {item["project"]}
**Update:** {item["update"]}
**Tags:** {", ".join(item.get("tags", []))}
""")

    total = sum(
        len(knowledge.get(k, []))
        for k in ("decisions", "patterns", "mistakes", "concepts", "project_updates")
    )
    if total == 0:
        sections.append("*No significant knowledge extracted from this session.*\n")

    try:
        with open(log_path, "a", encoding="utf-8") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                logging.warning("Daily log locked by concurrent flush — skipping write")
                return log_path
            f.write("\n".join(sections))
            # Lock releases automatically when file closes
    except Exception as e:
        logging.error(f"write_daily_log failed: {e}")

    return log_path


# ── After-6pm compile trigger ─────────────────────────────────────────────────


def maybe_trigger_compile() -> None:
    """If it's after 6 PM, kick off compile.py as a background process."""
    if datetime.datetime.now().hour >= 18:
        import subprocess

        compile_script = Path(__file__).parent / "compile.py"
        subprocess.Popen(
            [sys.executable, str(compile_script)],
            stdout=open(LOG_FILE.parent / "compile.log", "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        logging.info("Triggered compile.py (post-6pm)")


# ── Transcript discovery ───────────────────────────────────────────────────────

# SessionEnd does not reliably pass CLAUDE_TRANSCRIPT_PATH in all environments
# (e.g. some hook runners leave it empty). When that happens we fall back to
# finding the single transcript that was just active, identified by a recency
# window. We use 60 seconds — tight enough to avoid picking up stale files from
# earlier sessions, wide enough to survive slow hook dispatch.
FALLBACK_RECENCY_SECS = 60


def find_latest_transcript() -> str | None:
    """Find the single most-recently-modified transcript touched in the last 60s.

    Returns None (with a logged warning) if no file qualifies, so we never
    accidentally process a stale transcript from an older session.
    """
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        logging.warning("Transcript fallback: ~/.claude/projects/ does not exist")
        return None

    import time

    cutoff = time.time() - FALLBACK_RECENCY_SECS

    recent = [
        p
        for p in projects_dir.glob("*/*.jsonl")
        if "subagents" not in p.parts and p.stat().st_mtime >= cutoff
    ]

    if not recent:
        logging.warning(
            f"Transcript fallback: no .jsonl file modified in the last "
            f"{FALLBACK_RECENCY_SECS}s — assuming no active session to process"
        )
        return None

    if len(recent) > 1:
        logging.warning(
            f"Transcript fallback: {len(recent)} files modified in the last "
            f"{FALLBACK_RECENCY_SECS}s — picking the newest one"
        )

    latest = max(recent, key=lambda p: p.stat().st_mtime)
    logging.info(f"Transcript fallback: selected {latest}")
    return str(latest)


# ── Dedup guard ───────────────────────────────────────────────────────────────


def already_processed_today(transcript_path: str) -> bool:
    """Return True if this transcript's filename appears in today's daily log."""
    filename = Path(transcript_path).name
    log_path = DAILY_DIR / f"{datetime.date.today().isoformat()}.md"
    if not log_path.exists():
        return False
    return f"source: {filename}" in log_path.read_text(encoding="utf-8")


# ── Main ───────────────────────────────────────────────────────────────────────


def main() -> None:
    setup_logging()

    # ── Exclusive process lock — bail if another flush is already running ──
    try:
        lock_fd = open(FLUSH_LOCK_FILE, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        logging.info("Lock acquired")
    except BlockingIOError:
        logging.info("Lock exists, exiting")
        sys.exit(0)

    # ── Wall-clock timeout via SIGALRM ────────────────────────────────────
    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(WALL_CLOCK_LIMIT)

    try:
        transcript_path = (
            sys.argv[1]
            if len(sys.argv) > 1
            else os.environ.get("CLAUDE_TRANSCRIPT_PATH", "")
        )

        if not transcript_path:
            logging.warning(
                "No transcript path provided via argv or CLAUDE_TRANSCRIPT_PATH — "
                "attempting recency-based fallback"
            )
            transcript_path = find_latest_transcript() or ""

        if not transcript_path:
            logging.warning(
                "Transcript fallback found nothing — exiting without processing"
            )
            sys.exit(0)

        if "claude-memory" in transcript_path:
            logging.info("Skipping own project transcript")
            sys.exit(0)

        if already_processed_today(transcript_path):
            logging.info("Transcript already processed today — skipping")
            sys.exit(0)

        logging.info(f"Starting flush for: {transcript_path}")

        transcript = load_transcript(transcript_path)
        if not transcript:
            logging.warning("Empty or unreadable transcript — skipping")
            sys.exit(0)

        logging.info(f"Loaded transcript ({len(transcript)} chars)")

        knowledge = extract_knowledge(transcript)
        if not knowledge:
            logging.error("Extraction failed — skipping daily log write")
            sys.exit(1)

        today = datetime.date.today()
        log_path = write_daily_log(knowledge, today, transcript_path)

        total = sum(len(v) for v in knowledge.values() if isinstance(v, list))
        logging.info(f"Wrote {total} items to {log_path}")

        maybe_trigger_compile()
        logging.info("Flush complete")

    finally:
        signal.alarm(0)  # cancel the alarm
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == "__main__":
    main()
