#!/usr/bin/env python3
"""
session-start.py — Called by SessionStart hook.

Injects relevant context into the Claude session:
- Recent daily log entries (last 3 days)
- Active project summaries
- Urgent/pinned items from MEMORY.md
- Git status summary

Outputs JSON with additionalContext field for Claude Code hooks.

Usage: python3 session-start.py
"""

import json
import datetime
import subprocess
import sys
from pathlib import Path

VAULT = Path(__file__).parent.parent
DAILY_DIR = VAULT / "daily"
ACTIVE_DIR = VAULT / "work" / "active"
MEMORY_FILE = VAULT / "MEMORY.md"
MAX_DAILY_CHARS = 2000
MAX_PROJECT_CHARS = 500


def get_recent_daily_logs(days: int = 3) -> str:
    """Return content from the last N daily logs."""
    today = datetime.date.today()
    parts = []

    for i in range(days):
        date = today - datetime.timedelta(days=i)
        log_file = DAILY_DIR / f"{date.isoformat()}.md"
        if log_file.exists():
            content = log_file.read_text(encoding="utf-8")
            # Strip frontmatter
            if content.startswith("---"):
                sections = content.split("---", 2)
                if len(sections) >= 3:
                    content = sections[2]
            parts.append(f"**{date.isoformat()}:**\n{content.strip()}")

    if not parts:
        return ""

    combined = "\n\n".join(parts)
    if len(combined) > MAX_DAILY_CHARS:
        combined = combined[-MAX_DAILY_CHARS:]
        combined = "...[trimmed]\n\n" + combined

    return combined


def get_active_projects() -> str:
    """Return summaries of active projects."""
    if not ACTIVE_DIR.exists():
        return ""

    parts = []
    for project_file in sorted(ACTIVE_DIR.glob("*.md")):
        content = project_file.read_text(encoding="utf-8")
        # Strip frontmatter
        if content.startswith("---"):
            sections = content.split("---", 2)
            if len(sections) >= 3:
                content = sections[2]
        # Take first MAX_PROJECT_CHARS chars
        summary = content.strip()[:MAX_PROJECT_CHARS]
        parts.append(f"**{project_file.stem}:**\n{summary}")

    return "\n\n".join(parts)


def get_urgent_items() -> str:
    """Extract items tagged 'urgent' from MEMORY.md."""
    if not MEMORY_FILE.exists():
        return ""

    content = MEMORY_FILE.read_text(encoding="utf-8")
    urgent_lines = [
        line.strip()
        for line in content.splitlines()
        if "urgent" in line.lower() and line.strip().startswith("-")
    ]

    return "\n".join(urgent_lines)


def get_git_status() -> str:
    """Return a brief git status of the memory vault."""
    try:
        result = subprocess.run(
            ["git", "-C", str(VAULT), "status", "--short"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            lines = result.stdout.strip().splitlines()
            return f"{len(lines)} uncommitted change(s) in memory vault"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return ""


def get_memory_index_preview(max_chars: int = 4000) -> str:
    """Return the MEMORY.md index, capped at max_chars to prevent context bloat."""
    if not MEMORY_FILE.exists():
        return ""
    content = MEMORY_FILE.read_text(encoding="utf-8")
    import re

    content = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL)
    content = content.strip()
    if len(content) > max_chars:
        content = content[:max_chars] + "\n...[truncated]"
    return content


def rotate_session_backups(max_age_days: int = 30) -> None:
    """Delete sessions/backup-*.jsonl files older than max_age_days."""
    sessions_dir = VAULT / "sessions"
    if not sessions_dir.exists():
        return
    cutoff = datetime.datetime.now().timestamp() - max_age_days * 86400
    for backup in sessions_dir.glob("backup-*.jsonl"):
        try:
            if backup.stat().st_mtime < cutoff:
                backup.unlink()
        except OSError:
            pass


def prune_compiled_daily_logs(max_age_days: int = 60) -> None:
    """Delete daily/*.md files older than max_age_days that compile.py has marked done.

    Daily logs are a pipeline intermediate — once compiled, their content lives in
    wiki/brain/patterns/mistakes notes. The raw JSONL in sessions/backup-*.jsonl
    (and git history) remain as audit trail.
    """
    if not DAILY_DIR.exists():
        return
    cutoff = datetime.datetime.now().timestamp() - max_age_days * 86400
    for log in DAILY_DIR.glob("*.md"):
        try:
            if log.stat().st_mtime >= cutoff:
                continue
            if "compiled: true" not in log.read_text(encoding="utf-8"):
                continue
            log.unlink()
        except OSError:
            pass


def build_context() -> str:
    """Assemble the full context string."""
    sections = []

    memory_index = get_memory_index_preview()
    if memory_index:
        sections.append(f"## Memory Index\n{memory_index}")

    recent_logs = get_recent_daily_logs()
    if recent_logs:
        sections.append(f"## Recent Session Logs (last 3 days)\n{recent_logs}")

    active_projects = get_active_projects()
    if active_projects:
        sections.append(f"## Active Projects\n{active_projects}")

    urgent = get_urgent_items()
    if urgent:
        sections.append(f"## Urgent Items\n{urgent}")

    git_status = get_git_status()
    if git_status:
        sections.append(f"## Vault Status\n{git_status}")

    if not sections:
        return "Memory vault is empty. No prior session context available."

    return "\n\n---\n\n".join(sections)


def main():
    rotate_session_backups()
    prune_compiled_daily_logs()
    try:
        context = build_context()

        # Output as Claude Code hook JSON format
        output = {"additionalContext": f"# Claude Memory Context\n\n{context}"}
        print(json.dumps(output))

    except Exception as e:
        # On any failure, output empty context rather than breaking the session
        output = {"additionalContext": f"(Memory vault context unavailable: {e})"}
        print(json.dumps(output))
        sys.exit(0)  # Never block a session start


if __name__ == "__main__":
    main()
