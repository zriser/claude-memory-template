#!/usr/bin/env python3
"""
query.py — Ask questions against the knowledge base.

Primary:  claude_agent_sdk  — agent searches the vault using Glob/Grep/Read
          tools and answers directly (no API key required).
Fallback: anthropic SDK     — keyword search + direct synthesis (requires ANTHROPIC_API_KEY).

Usage:
  python3 query.py "What patterns do I use for Flask APIs?"
  python3 query.py "What mistakes have I made with Docker?" --file-back
  python3 query.py --interactive
  python3 query.py --list
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
WIKI_DIR = VAULT / "wiki"
BRAIN_DIR = VAULT / "brain"
PATTERNS_DIR = VAULT / "patterns"
MISTAKES_DIR = VAULT / "mistakes"
MAX_CONTEXT_CHARS = 60_000

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

# ── Agent SDK path (primary) ──────────────────────────────────────────────────

SDK_QUERY_PROMPT = """\
You are querying Zach's personal knowledge base stored in {vault}.

The knowledge base is organized as:
  wiki/          — compiled concept articles
  brain/         — durable topic notes (architecture, decisions, concepts, people)
  patterns/      — reusable approaches
  mistakes/      — anti-patterns and postmortems
  daily/         — raw session logs (less useful for answering questions)

Your job:
1. Use Glob and Grep to find articles relevant to the question
2. Read the most relevant ones
3. Synthesize a clear, concise answer citing specific article paths
4. If the knowledge base doesn't have a clear answer, say so rather than guessing
5. At the end, suggest related topics in the vault worth exploring
{file_back_instruction}

QUESTION: {question}
"""

FILE_BACK_INSTRUCTION = """
After answering, save the answer as a Q&A note by writing a new file:
  {wiki}/qa-{today}-<slug>.md

Use this frontmatter:
---
title: "Q&A: <question>"
tags: [qa, auto-generated]
category: concept
created: {today}
updated: {today}
---
"""

async def _query_with_sdk(question: str, file_back: bool) -> str:
    """Agent searches the vault with tools and synthesizes an answer."""
    today = datetime.date.today().isoformat()

    file_back_instr = ""
    if file_back:
        file_back_instr = FILE_BACK_INSTRUCTION.format(wiki=str(WIKI_DIR), today=today)

    prompt = SDK_QUERY_PROMPT.format(
        vault=str(VAULT),
        question=question,
        file_back_instruction=file_back_instr,
    )

    tools = ["Read", "Glob", "Grep"]
    if file_back:
        tools.append("Write")

    answer = ""
    total_cost = 0.0

    async for message in sdk_query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            cwd=str(VAULT),
            system_prompt={"type": "preset", "preset": "claude_code"},
            allowed_tools=tools,
            permission_mode="acceptEdits",
            max_turns=15,
        ),
    ):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    answer += block.text
        elif isinstance(message, ResultMessage):
            total_cost = message.total_cost_usd or 0.0

    if total_cost:
        answer += f"\n\n*(Cost: ${total_cost:.4f})*"

    return answer.strip()

# ── Fallback: keyword search + anthropic SDK ──────────────────────────────────

FALLBACK_QUERY_PROMPT = """\
You are querying Zach's personal knowledge base. Use the provided articles to answer \
his question accurately and concisely.

Rules:
- Cite specific articles by path when relevant
- If the knowledge base doesn't have a clear answer, say so
- Highlight connections between related notes
- At the end, suggest related topics worth exploring

KNOWLEDGE BASE CONTEXT:
{context}

QUESTION: {question}
"""

def _load_all_articles() -> dict[str, str]:
    articles = {}
    for search_dir in [WIKI_DIR, BRAIN_DIR, PATTERNS_DIR, MISTAKES_DIR]:
        for md_file in search_dir.rglob("*.md"):
            if md_file.name == "index.md":
                continue
            rel = str(md_file.relative_to(VAULT))
            articles[rel] = md_file.read_text(encoding="utf-8")
    return articles

def _find_relevant(question: str, articles: dict[str, str]) -> list[tuple[str, str]]:
    keywords = re.findall(r'\b\w{4,}\b', question.lower())
    scored = []
    for path, content in articles.items():
        score = sum(content.lower().count(kw) for kw in keywords)
        if score > 0:
            scored.append((score, path, content))
    scored.sort(reverse=True)

    result, total_chars = [], 0
    for _, path, content in scored[:20]:
        if total_chars + len(content) > MAX_CONTEXT_CHARS:
            break
        result.append((path, content))
        total_chars += len(content)
    return result

def _save_answer_to_wiki(question: str, answer: str) -> None:
    today = datetime.date.today().isoformat()
    slug = re.sub(r'[^\w\s-]', '', question.lower())
    slug = re.sub(r'[\s_]+', '-', slug)[:50]
    content = f"""\
---
title: "Q&A: {question[:60]}"
tags: [qa, auto-generated]
category: concept
created: {today}
updated: {today}
---

# Q: {question}

{answer}

---
*Auto-generated by query.py on {today}*
"""
    out_path = WIKI_DIR / f"qa-{today}-{slug}.md"
    out_path.write_text(content, encoding="utf-8")
    print(f"\nSaved to: {out_path}")

def _query_with_api(question: str, file_back: bool) -> str | None:
    """Fallback: keyword search + direct Anthropic API synthesis."""
    try:
        import anthropic
    except ImportError:
        return "ERROR: neither claude_agent_sdk nor anthropic package is installed"

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return "ERROR: Fallback requires ANTHROPIC_API_KEY env var"

    articles = _load_all_articles()
    if not articles:
        return "Knowledge base is empty. Run a session and let flush.py populate it first."

    relevant = _find_relevant(question, articles)
    if not relevant:
        return "No relevant articles found for this question."

    context = "\n\n---\n\n".join(f"### {p}\n{c}" for p, c in relevant)
    prompt = FALLBACK_QUERY_PROMPT.format(context=context, question=question)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        answer = response.content[0].text.strip()
        if file_back:
            _save_answer_to_wiki(question, answer)
        return answer
    except Exception as e:
        return f"ERROR: API call failed: {e}"

# ── Dispatcher ─────────────────────────────────────────────────────────────────

def run_query(question: str, file_back: bool) -> str | None:
    # Tier 1: Agent SDK (searches vault with file tools)
    if AGENT_SDK_AVAILABLE:
        print("Tier 1 — Claude Agent SDK (subscription credentials)\n", flush=True)
        try:
            return asyncio.run(_query_with_sdk(question, file_back))
        except Exception as e:
            print(f"Tier 1 failed ({type(e).__name__}), trying Tier 2...", flush=True)

    # Tier 2: ANTHROPIC_API_KEY (keyword search + API synthesis)
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("Tier 2 — ANTHROPIC_API_KEY\n", flush=True)
        result = _query_with_api(question, file_back)
        if result and not result.startswith("ERROR"):
            return result
        print("Tier 2 failed, trying Tier 3...", flush=True)

    # Tier 3: claude -p (keyword search + subprocess synthesis)
    print("Tier 3 — claude -p subprocess fallback\n", flush=True)
    articles = _load_all_articles()
    relevant = _find_relevant(question, articles)
    if not relevant:
        return "No relevant articles found. The knowledge base may be empty."
    context = "\n\n---\n\n".join(f"### {p}\n{c}" for p, c in relevant)
    prompt = FALLBACK_QUERY_PROMPT.format(context=context, question=question)
    result = call_claude_pipe(prompt, timeout=90)
    if result and file_back:
        _save_answer_to_wiki(question, result)
    return result or "All tiers failed — no response from claude -p."

# ── Interactive mode ───────────────────────────────────────────────────────────

def interactive_mode() -> None:
    print("Claude Memory Query — interactive mode")
    print("Commands: 'quit' to exit, 'save' to save last answer to wiki\n")
    last_question = last_answer = ""

    while True:
        try:
            question = input("\nQuery> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break

        if not question:
            continue
        if question.lower() in ("quit", "exit", "q"):
            break
        if question.lower() == "save" and last_answer:
            _save_answer_to_wiki(last_question, last_answer)
            continue

        answer = run_query(question, file_back=False)
        if answer:
            print(f"\n{answer}")
            last_question = question
            last_answer = answer

# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Query the Claude Memory knowledge base")
    parser.add_argument("question", nargs="?", help="Question to ask")
    parser.add_argument("--file-back", action="store_true", help="Save answer to wiki")
    parser.add_argument("--interactive", "-i", action="store_true", help="Interactive mode")
    parser.add_argument("--list", action="store_true", help="List all articles in the vault")
    args = parser.parse_args()

    if args.interactive:
        interactive_mode()
        return

    if args.list:
        articles = _load_all_articles()
        print(f"Knowledge base: {len(articles)} article(s)\n")
        for path in sorted(articles):
            print(f"  {path}")
        return

    if not args.question:
        parser.print_help()
        sys.exit(1)

    answer = run_query(args.question, file_back=args.file_back)
    if answer:
        print(answer)


if __name__ == "__main__":
    main()
