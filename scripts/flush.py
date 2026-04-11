#!/usr/bin/env python3
"""
flush.py — Session-end knowledge extractor.

Called by the Stop hook. Reads the session transcript, uses the Claude Agent
SDK (via Claude Code's built-in credentials) to extract decisions, patterns,
mistakes, and concepts, then appends them to the daily log.

Must run as a background process — does not block session exit.

Primary:  claude_agent_sdk  (uses ~/.claude/.credentials.json — no API key needed)
Fallback: anthropic SDK     (requires ANTHROPIC_API_KEY env var)

Usage: python3 flush.py [transcript_path]
       Or set CLAUDE_TRANSCRIPT_PATH env var.
"""

import asyncio
import datetime
import json
import logging
import logging.handlers
import os
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
MAX_TRANSCRIPT_CHARS = 80_000

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
        backupCount=2,     # keeps flush.log, flush.log.1, flush.log.2
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)

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
                role = entry.get("role", "")
                content = entry.get("content", "")
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

async def _extract_with_sdk(transcript: str) -> str:
    """Run extraction via the Claude Agent SDK (no API key required)."""
    result = ""
    async for message in sdk_query(
        prompt=EXTRACTION_PROMPT + transcript,
        options=ClaudeAgentOptions(
            cwd=str(VAULT),
            allowed_tools=[],
            max_turns=2,
        ),
    ):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    result += block.text
        elif isinstance(message, ResultMessage):
            cost = message.total_cost_usd or 0.0
            if cost:
                logging.info(f"SDK cost: ${cost:.4f}")
    return result.strip()

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

def extract_knowledge(transcript: str) -> dict | None:
    """Extract knowledge, preferring Agent SDK, falling back to direct API."""
    raw: str | None = None

    # Tier 1: Agent SDK
    if AGENT_SDK_AVAILABLE:
        logging.info("Tier 1: Claude Agent SDK")
        print("flush.py: Tier 1 — Claude Agent SDK (subscription credentials)", flush=True)
        try:
            raw = asyncio.run(_extract_with_sdk(transcript))
        except Exception as e:
            logging.error(f"Tier 1 failed: {e}\n{traceback.format_exc()}")
            print(f"flush.py: Tier 1 failed ({type(e).__name__}), trying Tier 2", flush=True)

    # Tier 2: ANTHROPIC_API_KEY
    if raw is None and os.environ.get("ANTHROPIC_API_KEY"):
        logging.info("Tier 2: ANTHROPIC_API_KEY")
        print("flush.py: Tier 2 — ANTHROPIC_API_KEY", flush=True)
        try:
            raw = _extract_with_api(transcript)
        except Exception as e:
            logging.error(f"Tier 2 failed: {e}")
            print(f"flush.py: Tier 2 failed ({type(e).__name__}), trying Tier 3", flush=True)

    # Tier 3: claude -p subprocess
    if raw is None:
        logging.info("Tier 3: claude -p subprocess")
        print("flush.py: Tier 3 — claude -p subprocess fallback", flush=True)
        raw = call_claude_pipe(EXTRACTION_PROMPT + transcript, timeout=180)

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
        logging.error(f"Failed to parse extraction JSON: {e}\nRaw response:\n{raw[:500]}")
        return None

# ── Daily log writer ───────────────────────────────────────────────────────────

def write_daily_log(knowledge: dict, date: datetime.date) -> Path:
    """Append extracted knowledge to today's daily log."""
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

    sections.append(f"\n## Session extracted at {now}\n")

    for item in knowledge.get("decisions", []):
        sections.append(f"""\
### [DECISION] {item['title']}
**Summary:** {item['summary']}
**Rationale:** {item['rationale']}
**Tags:** {', '.join(item.get('tags', []))}
""")

    for item in knowledge.get("patterns", []):
        sections.append(f"""\
### [PATTERN] {item['title']}
**Summary:** {item['summary']}
**When to use:** {item['when_to_use']}
**Tags:** {', '.join(item.get('tags', []))}
""")

    for item in knowledge.get("mistakes", []):
        sections.append(f"""\
### [MISTAKE] {item['title']}
**Summary:** {item['summary']}
**Root cause:** {item['root_cause']}
**Fix:** {item['fix']}
**Tags:** {', '.join(item.get('tags', []))}
""")

    for item in knowledge.get("concepts", []):
        sections.append(f"""\
### [CONCEPT] {item['title']}
**Summary:** {item['summary']}
**Tags:** {', '.join(item.get('tags', []))}
""")

    for item in knowledge.get("project_updates", []):
        sections.append(f"""\
### [PROJECT] {item['project']}
**Update:** {item['update']}
**Tags:** {', '.join(item.get('tags', []))}
""")

    total = sum(
        len(knowledge.get(k, []))
        for k in ("decisions", "patterns", "mistakes", "concepts", "project_updates")
    )
    if total == 0:
        sections.append("*No significant knowledge extracted from this session.*\n")

    with open(log_path, "a", encoding="utf-8") as f:
        f.write("\n".join(sections))

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

# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    setup_logging()

    transcript_path = (
        sys.argv[1] if len(sys.argv) > 1
        else os.environ.get("CLAUDE_TRANSCRIPT_PATH", "")
    )

    if not transcript_path:
        logging.warning("No transcript path provided (CLAUDE_TRANSCRIPT_PATH not set)")
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
    log_path = write_daily_log(knowledge, today)

    total = sum(len(v) for v in knowledge.values() if isinstance(v, list))
    logging.info(f"Wrote {total} items to {log_path}")

    maybe_trigger_compile()
    logging.info("Flush complete")


if __name__ == "__main__":
    main()
