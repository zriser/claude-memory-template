#!/usr/bin/env python3
"""
lint.py — Health checks for the knowledge base.

Static checks (broken links, orphans, stale notes, missing frontmatter) run
with pure Python — no AI required.

The --contradictions flag uses Claude to find semantic inconsistencies:
  Primary:  claude_agent_sdk  (Claude Code credentials, no API key)
  Fallback: anthropic SDK     (requires ANTHROPIC_API_KEY)

Usage:
  python3 lint.py
  python3 lint.py --contradictions
  python3 lint.py --broken-only
  python3 lint.py --stale-only
"""

import asyncio
import argparse
import datetime
import os
import re
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from utils import call_claude_pipe, AGENT_SDK_AVAILABLE as _SDK_AVAIL  # noqa: E402

VAULT = Path(__file__).parent.parent
STALE_DAYS = 90

SEARCH_DIRS = [
    VAULT / "wiki",
    VAULT / "brain",
    VAULT / "patterns",
    VAULT / "mistakes",
    VAULT / "daily",
]

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

# ── Article loading ────────────────────────────────────────────────────────────


def load_all_articles() -> dict[str, str]:
    articles = {}
    for search_dir in SEARCH_DIRS:
        for md_file in search_dir.rglob("*.md"):
            rel = str(md_file.relative_to(VAULT))
            articles[rel] = md_file.read_text(encoding="utf-8")
    return articles


def build_slug_map(articles: dict[str, str]) -> dict[str, str]:
    slug_map = {}
    for path, content in articles.items():
        stem = Path(path).stem
        slug_map[stem] = path
        title_match = re.search(
            r'^title:\s*["\']?(.+?)["\']?\s*$', content, re.MULTILINE
        )
        if title_match:
            title = title_match.group(1).strip()
            slug_map[title] = path
            slug_map[title.lower()] = path
    return slug_map


# ── Static checks (no AI needed) ─────────────────────────────────────────────


def check_broken_links(articles: dict[str, str]) -> list[str]:
    slug_map = build_slug_map(articles)
    issues = []
    for path, content in articles.items():
        for link in re.findall(r"\[\[([^\]]+)\]\]", content):
            link = link.strip()
            if link not in slug_map and link.lower() not in slug_map:
                issues.append(f"BROKEN LINK: `[[{link}]]` in `{path}`")
    return issues


def check_orphaned_notes(articles: dict[str, str]) -> list[str]:
    referenced: set[str] = set()
    for content in articles.values():
        for link in re.findall(r"\[\[([^\]]+)\]\]", content):
            referenced.add(link.strip())
            referenced.add(link.strip().lower())
        for link in re.findall(r"\[.*?\]\(([^)]+)\)", content):
            referenced.add(link.strip())

    issues = []
    for path in articles:
        if Path(path).name == "index.md" or path.startswith("daily/"):
            continue
        stem = Path(path).stem
        if (
            stem not in referenced
            and stem.lower() not in referenced
            and path not in referenced
        ):
            issues.append(f"ORPHAN: `{path}` has no incoming links")
    return issues


def check_stale_notes(articles: dict[str, str]) -> list[str]:
    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=STALE_DAYS)
    issues = []
    for path, content in articles.items():
        if path.startswith("daily/"):
            continue
        updated = re.search(r"^updated:\s*(\d{4}-\d{2}-\d{2})", content, re.MULTILINE)
        created = re.search(r"^created:\s*(\d{4}-\d{2}-\d{2})", content, re.MULTILINE)
        date_str = updated or created
        if date_str:
            try:
                note_date = datetime.date.fromisoformat(date_str.group(1))
                if note_date < cutoff:
                    age = (today - note_date).days
                    issues.append(
                        f"STALE ({age}d): `{path}` — last updated {date_str.group(1)}"
                    )
            except ValueError:
                pass
        else:
            issues.append(f"NO DATE: `{path}` — missing created/updated frontmatter")
    return issues


def check_missing_frontmatter(articles: dict[str, str]) -> list[str]:
    required = {"title", "category", "created", "updated"}
    issues = []
    for path, content in articles.items():
        if path.startswith("daily/") or Path(path).name == "index.md":
            continue
        if not content.startswith("---"):
            issues.append(f"NO FRONTMATTER: `{path}`")
            continue
        fm_match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        if fm_match:
            fm = fm_match.group(1)
            for field in required:
                if f"{field}:" not in fm:
                    issues.append(f"MISSING `{field}:`: `{path}`")
    return issues


# ── Contradiction check — Agent SDK (primary) ─────────────────────────────────

SDK_CONTRADICTION_PROMPT = """\
You are auditing the user's personal knowledge base at {vault} for quality issues.

Use Glob and Read tools to examine all articles in:
  wiki/, brain/, patterns/, mistakes/

Look for:
1. Contradictions — two notes that claim conflicting things
2. Outdated claims — notes that reference something as current but it likely changed
3. Inconsistencies — notes that use different terminology for the same concept

For each issue found, report:
- File A path
- File B path (if applicable)
- The conflicting claims
- Which is likely correct or what needs updating

If you find no significant issues, say "No contradictions detected."

Skip daily/ logs — only check compiled articles.
"""


async def _check_contradictions_with_sdk() -> str:
    result = ""
    async for message in sdk_query(
        prompt=SDK_CONTRADICTION_PROMPT.format(vault=str(VAULT)),
        options=ClaudeAgentOptions(
            cwd=str(VAULT),
            system_prompt={"type": "preset", "preset": "claude_code"},
            allowed_tools=["Read", "Glob", "Grep"],
            permission_mode="acceptEdits",
            max_turns=20,
        ),
    ):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    result += block.text
        elif isinstance(message, ResultMessage):
            cost = message.total_cost_usd or 0.0
            if cost:
                result += f"\n*(Cost: ${cost:.4f})*"
    return result.strip()


# ── Contradiction check — anthropic SDK (fallback) ────────────────────────────

FALLBACK_CONTRADICTION_PROMPT = """\
Review these knowledge base articles for contradictions, inconsistencies, or outdated claims.

For each contradiction, report:
- Article A (path)
- Article B (path)
- The conflicting claims
- Which is likely correct

If none found, say "No contradictions detected."

ARTICLES:
{content}
"""


def _check_contradictions_with_api(articles: dict[str, str]) -> str:
    try:
        import anthropic
    except ImportError:
        return "ERROR: neither claude_agent_sdk nor anthropic package is installed"

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return "ERROR: Fallback requires ANTHROPIC_API_KEY env var"

    relevant = {
        path: content
        for path, content in articles.items()
        if not path.startswith("daily/") and Path(path).name != "index.md"
    }
    if not relevant:
        return "No compiled articles to check."

    combined = "\n\n---\n\n".join(
        f"### {p}\n{c}" for p, c in list(relevant.items())[:20]
    )
    if len(combined) > 80_000:
        combined = combined[:80_000] + "\n...[trimmed]"

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            messages=[
                {
                    "role": "user",
                    "content": FALLBACK_CONTRADICTION_PROMPT.format(content=combined),
                }
            ],
        )
        return response.content[0].text.strip()
    except Exception as e:
        return f"ERROR checking contradictions: {e}"


def check_contradictions(articles: dict[str, str]) -> list[str]:
    result: str | None = None

    # Tier 1: Agent SDK (reads vault directly with tools)
    if AGENT_SDK_AVAILABLE:
        print("Tier 1 — Claude Agent SDK (subscription credentials)", file=sys.stderr)
        try:
            result = asyncio.run(_check_contradictions_with_sdk())
        except Exception as e:
            print(
                f"Tier 1 failed ({type(e).__name__}), trying Tier 2...", file=sys.stderr
            )

    # Tier 2: ANTHROPIC_API_KEY
    if result is None and os.environ.get("ANTHROPIC_API_KEY"):
        print("Tier 2 — ANTHROPIC_API_KEY", file=sys.stderr)
        result = _check_contradictions_with_api(articles)

    # Tier 3: claude -p subprocess
    if result is None:
        print("Tier 3 — claude -p subprocess fallback", file=sys.stderr)
        relevant = {
            path: content
            for path, content in articles.items()
            if not path.startswith("daily/") and Path(path).name != "index.md"
        }
        combined = "\n\n---\n\n".join(
            f"### {p}\n{c}" for p, c in list(relevant.items())[:20]
        )
        if len(combined) > 80_000:
            combined = combined[:80_000] + "\n...[trimmed]"
        result = call_claude_pipe(
            FALLBACK_CONTRADICTION_PROMPT.format(content=combined), timeout=120
        )

    if not result or "No contradictions detected" in result:
        return []
    return [f"CONTRADICTION REPORT:\n{result}"]


# ── Report ─────────────────────────────────────────────────────────────────────


def print_report(
    broken: list[str],
    orphans: list[str],
    stale: list[str],
    missing_fm: list[str],
    contradictions: list[str],
) -> None:
    total = (
        len(broken) + len(orphans) + len(stale) + len(missing_fm) + len(contradictions)
    )
    print(f"# Claude Memory Lint Report — {datetime.date.today().isoformat()}")
    print(f"Total issues: {total}\n")

    for title, issues in [
        ("Broken Wiki Links", broken),
        ("Missing Frontmatter", missing_fm),
        ("Orphaned Notes (no incoming links)", orphans),
        (f"Stale Notes (not updated in {STALE_DAYS}+ days)", stale),
        ("Contradictions", contradictions),
    ]:
        if issues:
            print(f"## {title} ({len(issues)})\n")
            for issue in issues:
                print(f"- {issue}")
            print()

    if total == 0:
        print("No issues found. Knowledge base is healthy.")


# ── Main ───────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Lint the Claude Memory knowledge base"
    )
    parser.add_argument(
        "--contradictions",
        action="store_true",
        help="Check for contradictions via Claude (uses Claude Code credentials or ANTHROPIC_API_KEY)",
    )
    parser.add_argument(
        "--broken-only", action="store_true", help="Only report broken links"
    )
    parser.add_argument(
        "--stale-only", action="store_true", help="Only report stale notes"
    )
    args = parser.parse_args()

    articles = load_all_articles()
    print(f"Checking {len(articles)} articles...\n", file=sys.stderr)

    if args.broken_only:
        for issue in check_broken_links(articles):
            print(issue)
        return

    if args.stale_only:
        for issue in check_stale_notes(articles):
            print(issue)
        return

    broken = check_broken_links(articles)
    orphans = check_orphaned_notes(articles)
    stale = check_stale_notes(articles)
    missing_fm = check_missing_frontmatter(articles)
    contradictions = check_contradictions(articles) if args.contradictions else []

    print_report(broken, orphans, stale, missing_fm, contradictions)


if __name__ == "__main__":
    main()
