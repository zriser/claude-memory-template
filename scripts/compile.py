#!/usr/bin/env python3
"""
compile.py — Turns daily session logs into structured wiki articles.

Primary:  claude_agent_sdk  — agent reads logs and writes articles directly
          using file tools (no JSON round-trip, no API key required).
Fallback: anthropic SDK     — JSON-based article generation (requires ANTHROPIC_API_KEY).

Usage: python3 compile.py [--dry-run]
"""

import asyncio
import datetime
import fcntl
import json
import logging
import os
import re
import signal
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from utils import call_claude_pipe, AGENT_SDK_AVAILABLE as _SDK_AVAIL  # noqa: E402

VAULT = Path(__file__).parent.parent
DAILY_DIR = VAULT / "daily"
WIKI_DIR = VAULT / "wiki"
BRAIN_DIR = VAULT / "brain"
PATTERNS_DIR = VAULT / "patterns"
MISTAKES_DIR = VAULT / "mistakes"
WORK_ACTIVE_DIR = VAULT / "work" / "active"
LOG_FILE = VAULT / "sessions" / "compile.log"
COST_LOG_FILE = VAULT / "sessions" / "cost.log"
COMPILE_LOCK_FILE = Path("/tmp/claude-memory-compile.lock")
COST_WARN_USD = 0.50
# NOTE: This is a write-gate, not a spend-gate. Cost is already incurred before
# this threshold is checked; only the result write is aborted.
COST_ABORT_USD = 1.00
WALL_CLOCK_LIMIT = 300  # seconds — SIGALRM kills the process if exceeded
SDK_TIMEOUT = 300  # asyncio.wait_for timeout for _compile_with_sdk

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
    logging.basicConfig(
        filename=str(LOG_FILE),
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


# ── Wall-clock timeout ────────────────────────────────────────────────────────


def _timeout_handler(signum: int, frame) -> None:
    logging.error(
        f"compile.py exceeded {WALL_CLOCK_LIMIT}s wall-clock limit — aborting"
    )
    sys.exit(1)


# ── Cost guard ────────────────────────────────────────────────────────────────


def write_cost_log(tier: str, cost_usd: float) -> None:
    """Append one cost record to sessions/cost.log."""
    try:
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        COST_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(COST_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{ts} compile.py {tier} ${cost_usd:.4f}\n")
    except Exception:
        pass  # cost log is best-effort


def _check_cost(cost_usd: float, tier: str) -> bool:
    """Log warning or abort based on cost thresholds. Returns False if aborted.

    NOTE: This is a write-gate. The API call has already completed and the cost
    is already incurred. Aborting here only prevents writing results to disk.
    """
    write_cost_log(tier, cost_usd)
    if cost_usd >= COST_ABORT_USD:
        logging.error(
            f"Cost guard triggered: ${cost_usd:.4f} exceeds abort threshold "
            f"(${COST_ABORT_USD:.2f}) — aborting compile"
        )
        return False
    if cost_usd >= COST_WARN_USD:
        logging.warning(
            f"Cost guard: ${cost_usd:.4f} exceeds warning threshold (${COST_WARN_USD:.2f})"
        )
    return True


# ── Uncompiled log detection ───────────────────────────────────────────────────


def get_uncompiled_logs() -> list[Path]:
    """Return daily log files that have content but haven't been compiled."""
    logs = []
    for log_file in sorted(DAILY_DIR.glob("*.md")):
        content = log_file.read_text(encoding="utf-8")
        has_content = any(
            tag in content
            for tag in (
                "[DECISION]",
                "[PATTERN]",
                "[MISTAKE]",
                "[CONCEPT]",
                "[PROJECT]",
            )
        )
        is_uncompiled = "compiled: false" in content or "compiled:" not in content
        if has_content and is_uncompiled:
            logs.append(log_file)
    return logs


def extract_log_content(log_file: Path) -> str:
    """Strip frontmatter and return the body of a daily log."""
    content = log_file.read_text(encoding="utf-8")
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            content = parts[2]
    return f"=== {log_file.stem} ===\n{content}"


# ── Agent SDK path (primary) ──────────────────────────────────────────────────

SDK_COMPILE_PROMPT = """\
You are compiling Zach's personal knowledge base from raw session logs.

Your job:
1. Use Glob to find all files in {daily_dir} that match *.md and contain "compiled: false"
2. Read each uncompiled log file
3. Synthesize the entries into well-structured wiki articles:
   - Group related [DECISION], [PATTERN], [MISTAKE], [CONCEPT], [PROJECT] entries by topic
   - Each article needs frontmatter: title, tags, category, created, updated
   - category is one of: decision, pattern, mistake, concept, project-update
   - Include a concise TL;DR first paragraph
   - Use [[wiki-links]] for cross-references
   - Write articles to appropriate locations:
     * decisions → {brain_decisions}/
     * patterns  → {patterns}/
     * mistakes  → {mistakes}/
     * concepts  → {brain_concepts}/
     * project updates → {wiki}/
4. For any [PROJECT] entries in the logs, check if a matching file exists in {work_active}/:
   - If it does: update it — refresh "Current Focus", add/remove open issues, update status line.
     Keep the file under 20 lines. Replace stale content; do not append forever.
   - If no matching file exists and the project had significant activity: create one following
     the format of existing files in {work_active}/.
   - Match project names loosely (e.g. "claude-memory" matches claude-memory.md).
5. Update {wiki}/index.md — append new article links under a "### Compiled {today}" heading
6. Update {memory} — add 1-line pointers to new articles (keep file under 50 lines total)
7. Mark each compiled log as compiled by changing "compiled: false" to "compiled: true"

Frontmatter format:
---
title: "Title Here"
tags: [tag1, tag2]
category: decision
created: {today}
updated: {today}
---

Rules:
- Do NOT overwrite existing articles — append new content with a separator if the file exists
- Keep MEMORY.md under 50 lines
- Skip log entries that are too minor to warrant their own article — fold them into related ones
- If a daily log has no significant entries, still mark it compiled
"""


async def _compile_with_sdk_inner(logs: list[Path], dry_run: bool) -> float:
    """Inner coroutine — wrapped by _compile_with_sdk with a timeout."""
    today = datetime.date.today().isoformat()
    log_names = ", ".join(l.name for l in logs)

    prompt = SDK_COMPILE_PROMPT.format(
        daily_dir=str(DAILY_DIR),
        brain_decisions=str(BRAIN_DIR / "decisions"),
        brain_concepts=str(BRAIN_DIR / "concepts"),
        patterns=str(PATTERNS_DIR),
        mistakes=str(MISTAKES_DIR),
        wiki=str(WIKI_DIR),
        work_active=str(WORK_ACTIVE_DIR),
        memory=str(VAULT / "MEMORY.md"),
        today=today,
    )
    prompt += f"\n\nUncompiled logs to process: {log_names}"

    if dry_run:
        prompt += "\n\nDRY RUN: Describe what you WOULD write but do not actually create or edit any files."

    total_cost = 0.0
    async for message in sdk_query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            cwd=str(VAULT),
            system_prompt={"type": "preset", "preset": "claude_code"},
            allowed_tools=["Read", "Write", "Edit", "Glob", "Grep"],
            permission_mode="acceptEdits",
            max_turns=30,
        ),
    ):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    print(f"  {block.text[:120].rstrip()}")
        elif isinstance(message, ResultMessage):
            total_cost = message.total_cost_usd or 0.0

    return total_cost


async def _compile_with_sdk(logs: list[Path], dry_run: bool) -> float:
    """Agent reads logs and writes articles directly using file tools.

    Wrapped with asyncio.wait_for to enforce a hard wall-clock timeout.
    """
    return await asyncio.wait_for(
        _compile_with_sdk_inner(logs, dry_run),
        timeout=SDK_TIMEOUT,
    )


# ── Fallback: anthropic SDK (JSON-based) ──────────────────────────────────────

FALLBACK_COMPILE_PROMPT = """\
You are compiling a personal knowledge base. Synthesize these raw session log entries \
into structured wiki articles.

For each logical topic cluster produce an article with:
- Frontmatter: title, tags, category (decision/pattern/mistake/concept/project-update), created, updated
- A concise TL;DR paragraph
- Sections with key details
- [[wiki-links]] to related concepts

Output a JSON array (no markdown fences):
[
  {
    "filename": "slug-for-article.md",
    "category": "decision|pattern|mistake|concept|project-update",
    "target_dir": "wiki|brain/decisions|brain/concepts|patterns|mistakes",
    "title": "...",
    "tags": [...],
    "content": "full markdown including frontmatter"
  }
]

RAW LOG ENTRIES:
"""


def _write_article(article: dict) -> Path | None:
    target_map = {
        "wiki": WIKI_DIR,
        "brain/decisions": BRAIN_DIR / "decisions",
        "brain/concepts": BRAIN_DIR / "concepts",
        "patterns": PATTERNS_DIR,
        "mistakes": MISTAKES_DIR,
    }
    target_dir = target_map.get(article.get("target_dir", "wiki"), WIKI_DIR)
    target_dir.mkdir(parents=True, exist_ok=True)

    filename = article.get("filename", "untitled.md")
    if not filename.endswith(".md"):
        filename += ".md"
    target_path = target_dir / filename

    today = datetime.date.today().isoformat()
    if target_path.exists():
        existing = target_path.read_text(encoding="utf-8")
        target_path.write_text(
            existing.rstrip()
            + f"\n\n---\n*Updated {today} by compile.py*\n\n"
            + article.get("content", ""),
            encoding="utf-8",
        )
    else:
        target_path.write_text(article.get("content", ""), encoding="utf-8")

    logging.info(f"Wrote: {target_path}")
    return target_path


def _update_wiki_index(written: list[Path]) -> None:
    index = WIKI_DIR / "index.md"
    existing = index.read_text(encoding="utf-8") if index.exists() else ""
    today = datetime.date.today().isoformat()
    new_entries = []
    for path in written:
        rel = path.relative_to(VAULT)
        name = path.stem.replace("-", " ").title()
        entry = f"- [{name}]({rel})"
        if entry not in existing:
            new_entries.append(entry)
    if new_entries:
        index.write_text(
            existing.rstrip()
            + f"\n\n### Compiled {today}\n"
            + "\n".join(new_entries)
            + "\n",
            encoding="utf-8",
        )


def _mark_compiled(logs: list[Path]) -> None:
    """Mark daily log files as compiled. Uses fcntl.flock to serialize concurrent writes."""
    for lf in logs:
        try:
            with open(lf, "r+", encoding="utf-8") as f:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    logging.warning(
                        f"_mark_compiled: {lf.name} locked by concurrent process — skipping"
                    )
                    continue
                content = f.read()
                content = content.replace("compiled: false", "compiled: true")
                if "compiled:" not in content:
                    content = content.replace("---\n", "---\ncompiled: true\n", 1)
                f.seek(0)
                f.write(content)
                f.truncate()
                # Lock releases on f.close() via with block
        except Exception as e:
            logging.error(f"_mark_compiled: failed for {lf.name}: {e}")


def _update_memory(written: list[Path]) -> None:
    memory = VAULT / "MEMORY.md"
    if not memory.exists():
        return
    content = memory.read_text(encoding="utf-8")
    # Count ALL non-blank lines (not just non-heading, non-comment lines)
    lines = [l for l in content.splitlines() if l.strip()]
    if len(lines) > 45:
        logging.warning("MEMORY.md near 50-line limit — skipping auto-update")
        return
    today = datetime.date.today().isoformat()
    new_lines = []
    for path in written[:3]:
        rel = path.relative_to(VAULT)
        name = path.stem.replace("-", " ").title()
        line = f"- [{name}]({rel}) — compiled {today}"
        if line not in content:
            new_lines.append(line)
    if new_lines:
        content = content.replace(
            "*Last updated: (not yet updated — run a session to populate)*",
            f"*Last updated: {today}*",
        )
        memory.write_text(
            content.rstrip() + "\n" + "\n".join(new_lines) + "\n", encoding="utf-8"
        )


def _compile_with_api(logs: list[Path], dry_run: bool) -> int:
    """Fallback: use direct Anthropic API to return JSON, then write files."""
    try:
        import anthropic
    except ImportError:
        print(
            "ERROR: neither claude_agent_sdk nor anthropic package is installed",
            file=sys.stderr,
        )
        return 0

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: Fallback requires ANTHROPIC_API_KEY env var", file=sys.stderr)
        return 0

    combined = "\n\n".join(extract_log_content(lf) for lf in logs)
    if len(combined) > 100_000:
        combined = combined[-100_000:]

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8192,
            messages=[{"role": "user", "content": FALLBACK_COMPILE_PROMPT + combined}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```", 2)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.rsplit("```", 1)[0]
        articles = json.loads(raw.strip())
    except Exception as e:
        logging.error(f"Fallback API call failed: {e}")
        return 0

    if dry_run:
        for a in articles:
            print(f"[DRY RUN] Would write: {a.get('target_dir')}/{a.get('filename')}")
            print(a.get("content", "")[:200])
            print("---")
        return len(articles)

    written = []
    for a in articles:
        path = _write_article(a)
        if path:
            written.append(path)

    _update_wiki_index(written)
    _mark_compiled(logs)
    _update_memory(written)

    return len(articles)


# ── Main ───────────────────────────────────────────────────────────────────────


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    setup_logging()

    if dry_run:
        print("[DRY RUN] compile.py — describing changes without writing")

    # ── Exclusive process lock — bail if another compile is already running ──
    try:
        lock_fd = open(COMPILE_LOCK_FILE, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logging.info("Another compile.py is already running — exiting")
        sys.exit(0)

    # ── Wall-clock timeout via SIGALRM ────────────────────────────────────
    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(WALL_CLOCK_LIMIT)

    try:
        logs = get_uncompiled_logs()
        if not logs:
            logging.info("No uncompiled logs found")
            print("No uncompiled daily logs found.")
            return

        log_names = [l.name for l in logs]
        logging.info(f"Found {len(logs)} uncompiled log(s): {log_names}")
        print(f"Compiling {len(logs)} log(s): {', '.join(log_names)}")

        success = False

        # Tier 1: Agent SDK (agent writes files directly via tools)
        if AGENT_SDK_AVAILABLE:
            print("Tier 1 — Claude Agent SDK (subscription credentials)")
            logging.info("Compile path: Tier 1 (claude_agent_sdk)")
            try:
                cost = asyncio.run(_compile_with_sdk(logs, dry_run))
                if not _check_cost(cost, "tier1-sdk"):
                    logging.error("Aborting compile due to cost guard")
                    sys.exit(1)
                logging.info(f"Tier 1 compile complete. Cost: ${cost:.4f}")
                print(f"\nCompile complete. Cost: ${cost:.4f}")
                success = True
            except asyncio.TimeoutError:
                logging.error(f"Tier 1 timed out after {SDK_TIMEOUT}s — trying Tier 2")
                print(f"Tier 1 timed out after {SDK_TIMEOUT}s, trying Tier 2...")
            except Exception as e:
                logging.error(f"Tier 1 failed: {e}\n{traceback.format_exc()}")
                print(f"Tier 1 failed ({type(e).__name__}), trying Tier 2...")

        # Tier 2: ANTHROPIC_API_KEY (JSON-based)
        if not success and os.environ.get("ANTHROPIC_API_KEY"):
            print("Tier 2 — ANTHROPIC_API_KEY")
            logging.info("Compile path: Tier 2 (anthropic SDK)")
            count = _compile_with_api(logs, dry_run)
            if count:
                write_cost_log("tier2-api", 0.0)
                print(f"Compiled {count} article(s).")
                success = True
            else:
                print("Tier 2 failed, trying Tier 3...")

        # Tier 3: claude -p subprocess (JSON-based, same as Tier 2 but different transport)
        if not success:
            print("Tier 3 — claude -p subprocess fallback")
            logging.info("Compile path: Tier 3 (claude -p)")
            combined = "\n\n".join(extract_log_content(lf) for lf in logs)
            if len(combined) > 100_000:
                combined = combined[-100_000:]
            raw = call_claude_pipe(FALLBACK_COMPILE_PROMPT + combined, timeout=240)
            if raw:
                try:
                    if raw.startswith("```"):
                        raw = raw.split("```", 2)[1]
                        if raw.startswith("json"):
                            raw = raw[4:]
                        raw = raw.rsplit("```", 1)[0]
                    articles = json.loads(raw.strip())
                    written = []
                    if not dry_run:
                        for a in articles:
                            path = _write_article(a)
                            if path:
                                written.append(path)
                        _update_wiki_index(written)
                        _mark_compiled(logs)
                        _update_memory(written)
                    write_cost_log("tier3-pipe", 0.0)
                    print(f"Tier 3 compiled {len(articles)} article(s).")
                    success = True
                except Exception as e:
                    logging.error(f"Tier 3 JSON parse failed: {e}")
                    print(f"All tiers failed. Check {LOG_FILE} for details.")
            else:
                print("All tiers failed — no output from claude -p.")

    finally:
        signal.alarm(0)  # cancel the alarm
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


if __name__ == "__main__":
    main()
