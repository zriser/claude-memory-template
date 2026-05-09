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
    VAULT / "work",
    VAULT / "sessions",
    VAULT / "dashboard",
    VAULT / "templates",
]

# Folders whose contents are loaded (so they resolve wikilinks TO them) but
# excluded from orphan + missing-frontmatter + stale checks — they're tooling
# or scaffolding, not curated content:
EXCLUDED_FROM_CHECKS = {"dashboard", "templates", "raw"}

# Root-level meta files — loaded so they count as linkers in orphan checks,
# but never held to frontmatter / stale / orphan content standards themselves:
ROOT_META_FILES = {"CLAUDE.md", "MEMORY.md", "README.md", "TODO.md", "AUDIT.md"}

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
    # Scan search dirs first
    for search_dir in SEARCH_DIRS:
        for md_file in search_dir.rglob("*.md"):
            rel = str(md_file.relative_to(VAULT))
            articles[rel] = md_file.read_text(encoding="utf-8")
    # Also scan root-level curated files (CLAUDE.md, MEMORY.md, README.md, etc.)
    # so they count as linkers for orphan detection.
    for md_file in VAULT.glob("*.md"):
        rel = str(md_file.relative_to(VAULT))
        articles[rel] = md_file.read_text(encoding="utf-8")
    return articles


def build_slug_map(articles: dict[str, str]) -> dict[str, str]:
    """Map wikilink-resolvable identifiers to their canonical path.

    Obsidian resolves [[link]] using any of: bare filename, path-without-ext, or
    frontmatter title. Match all three so path-style wikilinks like
    [[brain/people/kristen-wagner]] aren't reported as broken.
    """
    slug_map = {}
    for path, content in articles.items():
        stem = Path(path).stem
        slug_map[stem] = path
        slug_map[stem.lower()] = path
        # Index by path without extension (supports [[brain/people/foo]] style)
        path_no_ext = path[:-3] if path.endswith(".md") else path
        slug_map[path_no_ext] = path
        slug_map[path_no_ext.lower()] = path
        title_match = re.search(
            r'^title:\s*["\']?(.+?)["\']?\s*$', content, re.MULTILINE
        )
        if title_match:
            title = title_match.group(1).strip()
            slug_map[title] = path
            slug_map[title.lower()] = path
    return slug_map


def _wikilink_target(raw: str) -> str:
    """Strip any |alias and any #heading suffix from a wikilink body."""
    return raw.split("|", 1)[0].split("#", 1)[0].strip()


_REDIRECT_RE = re.compile(
    r"^\s*<!--\s*(Redirect|Duplicate)\b",
    re.IGNORECASE,
)


def _strip_code(content: str) -> str:
    """Remove fenced code blocks and inline code spans so lint doesn't
    mistake `[[literal]]` examples in code for real wikilinks."""
    content = re.sub(r"```[\s\S]*?```", "", content)
    content = re.sub(r"`[^`\n]+`", "", content)
    return content


def _is_redirect_stub(content: str) -> bool:
    """True if the file's first non-empty line is a Redirect/Duplicate comment."""
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        return bool(_REDIRECT_RE.match(stripped))
    return False


# ── Static checks (no AI needed) ─────────────────────────────────────────────


def check_broken_links(articles: dict[str, str]) -> list[str]:
    slug_map = build_slug_map(articles)
    issues = []
    for path, content in articles.items():
        scan = _strip_code(content)
        for link in re.findall(r"\[\[([^\]]+)\]\]", scan):
            target = _wikilink_target(link)
            if target in slug_map or target.lower() in slug_map:
                continue
            issues.append(f"BROKEN LINK: `[[{link}]]` in `{path}`")
    return issues


def check_orphaned_notes(articles: dict[str, str]) -> list[str]:
    """Find notes that no other note references via any wikilink shape.

    Counts a note as referenced if any inbound wikilink resolves to it via
    the slug_map (stem, path-without-ext, title, or lowercase forms).
    """
    slug_map = build_slug_map(articles)
    referenced_paths: set[str] = set()
    for content in articles.values():
        scan = _strip_code(content)
        for link in re.findall(r"\[\[([^\]]+)\]\]", scan):
            target = _wikilink_target(link)
            if target in slug_map:
                referenced_paths.add(slug_map[target])
            elif target.lower() in slug_map:
                referenced_paths.add(slug_map[target.lower()])
        # Markdown-style links like [text](../path/file.md)
        for link in re.findall(r"\[.*?\]\(([^)]+\.md)\)", scan):
            link = link.strip()
            # Normalize ../ prefix to vault-root-relative path
            cleaned = link.lstrip("./")
            # If the cleaned value matches an article key, count it
            if cleaned in articles:
                referenced_paths.add(cleaned)

    issues = []
    for path, content in articles.items():
        if Path(path).name == "index.md" or path.startswith("daily/"):
            continue
        # Root-level meta files (CLAUDE.md, MEMORY.md, etc.) are meta, not content
        if "/" not in path and path in ROOT_META_FILES:
            continue
        # Exclude scaffolding folders
        first_part = path.split("/", 1)[0]
        if first_part in EXCLUDED_FROM_CHECKS:
            continue
        # Skip redirect stubs — they intentionally hold no content
        if _is_redirect_stub(content):
            continue
        if path not in referenced_paths:
            issues.append(f"ORPHAN: `{path}` has no incoming links")
    return issues


def check_stale_notes(articles: dict[str, str]) -> list[str]:
    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=STALE_DAYS)
    issues = []
    for path, content in articles.items():
        if path.startswith("daily/"):
            continue
        # Root-level meta files don't get stale checks
        if "/" not in path and path in ROOT_META_FILES:
            continue
        # Scaffolding folders skipped here too
        first_part = path.split("/", 1)[0]
        if first_part in EXCLUDED_FROM_CHECKS:
            continue
        # Redirect stubs don't need dates
        if _is_redirect_stub(content):
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
    # category is optional now — folder encodes the category
    required = {"title", "created", "updated"}
    issues = []
    for path, content in articles.items():
        if path.startswith("daily/") or Path(path).name == "index.md":
            continue
        # Root-level meta files (CLAUDE.md, MEMORY.md, etc.) don't need frontmatter
        if "/" not in path and path in ROOT_META_FILES:
            continue
        # Skip scaffolding folders
        first_part = path.split("/", 1)[0]
        if first_part in EXCLUDED_FROM_CHECKS:
            continue
        # Redirect stubs intentionally hold no content; they're safe to skip
        if _is_redirect_stub(content):
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
You are auditing Zach's personal knowledge base at {vault} for quality issues.

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
