#!/usr/bin/env python3
"""
utils.py — Shared utilities for claude-memory scripts.

Auth tier summary:
  Tier 1: claude_agent_sdk  — uses ~/.claude/.credentials.json (Claude Code / Pro subscription)
  Tier 2: anthropic SDK     — requires ANTHROPIC_API_KEY env var (pay-per-token)
  Tier 3: claude -p         — shells out to the Claude Code CLI (any Claude subscription)

Each script tries Tier 1 first, then falls through automatically.
This module provides the Tier 3 implementation and shared detection helpers.
"""

import os
import subprocess
import sys
from pathlib import Path


# ── Tier 1 detection ──────────────────────────────────────────────────────────

try:
    from claude_agent_sdk import (  # noqa: F401 — imported for availability check
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query as sdk_query,
    )
    AGENT_SDK_AVAILABLE = True
except ImportError:
    AGENT_SDK_AVAILABLE = False


# ── Tier 2 detection ──────────────────────────────────────────────────────────

def api_key_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


# ── Tier 3: claude -p subprocess ─────────────────────────────────────────────

def call_claude_pipe(prompt: str, timeout: int = 120) -> str | None:
    """
    Tier 3 fallback: shell out to `claude -p` (Claude Code headless/pipe mode).

    Works with any Claude subscription — Pro, Max, or Teams.
    No API key or SDK auth required; uses whatever credentials the `claude`
    CLI is already authenticated with.

    Slower than Tiers 1 and 2 (spawns a subprocess, waits for full response)
    but has zero auth ambiguity.

    Args:
        prompt:  The prompt text to send to Claude.
        timeout: Seconds to wait before giving up (default 120).

    Returns:
        Response text, or None if the subprocess fails or times out.
    """
    try:
        # Pass prompt as a CLI argument — subprocess handles quoting, no shell=True
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            _warn(f"claude -p exited {result.returncode}: {result.stderr.strip()[:200]}")
            return None
        if not output:
            _warn("claude -p returned empty output")
            return None
        return output

    except FileNotFoundError:
        _warn("`claude` not found in PATH — is Claude Code installed?")
        return None
    except subprocess.TimeoutExpired:
        _warn(f"claude -p timed out after {timeout}s")
        return None
    except Exception as e:
        _warn(f"claude -p subprocess error: {e}")
        return None


def _warn(msg: str) -> None:
    print(f"  [Tier 3] {msg}", file=sys.stderr, flush=True)


# ── Convenience: print active tier ───────────────────────────────────────────

def print_auth_status() -> None:
    """Print which auth tiers are available (useful for debugging)."""
    print(f"  Tier 1 (Agent SDK):    {'✓ available' if AGENT_SDK_AVAILABLE else '✗ not installed'}")
    print(f"  Tier 2 (API key):      {'✓ set' if api_key_available() else '✗ ANTHROPIC_API_KEY not set'}")
    # Check if claude CLI exists
    try:
        subprocess.run(["claude", "--version"], capture_output=True, timeout=5)
        cli_ok = True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        cli_ok = False
    print(f"  Tier 3 (claude -p):    {'✓ available' if cli_ok else '✗ claude not in PATH'}")


if __name__ == "__main__":
    print("Claude Memory — auth tier status:")
    print_auth_status()
