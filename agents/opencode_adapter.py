"""
NEXUS Agent Adapter — OpenCode (implementation role)

Fills the "implementation" role — this is the agent that actually
writes code changes, as a free/open-source replacement for Claude
Code (which requires a paid plan NEXUS doesn't currently have).

OpenCode is a general-purpose CLI that works with many model
providers. NEXUS points it at Gemini (free tier) via a small
opencode.json config that reads the key from the GEMINI_API_KEY
environment variable — never hardcoded, never committed.

This adapter:
  1. Ensures OpenCode is configured to use Gemini
  2. Builds a task prompt from the task record
  3. Runs `opencode run` inside the target repo directory (headless,
     non-interactive — safe for CI)
  4. Diffs the repo before/after to see what actually changed
  5. Returns an AgentResult in the same shape every other agent uses

NEXUS never trusts the agent's own claim that it succeeded — success
here means "the command exited cleanly AND files actually changed."
Anything else is reported as a failure so a human isn't misled.
"""

import os
import sys
import json
import subprocess

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from base_adapter import AgentResult


MODEL = "google/gemini-2.5-pro"
RUN_TIMEOUT_SECONDS = 600  # 10 minutes — generous but bounded; a hung
                            # agent should never block a pipeline forever


def ensure_opencode_config(config_dir: str = None):
    """
    Writes the OpenCode config that points it at Gemini via env var
    reference (not a literal key). Safe to call every run — it just
    overwrites with the same content.
    """
    config_dir = config_dir or os.path.expanduser("~/.config/opencode")
    os.makedirs(config_dir, exist_ok=True)
    config = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "google": {
                "options": {
                    "apiKey": "{env:GEMINI_API_KEY}"
                }
            }
        }
    }
    with open(os.path.join(config_dir, "opencode.json"), "w") as f:
        json.dump(config, f, indent=2)


def build_implementation_prompt(task_record: dict) -> str:
    """
    Builds the instruction OpenCode receives. Deliberately scoped and
    literal — this is a coding agent, not a planner, and the task's
    boundaries (what it's allowed to touch) are set by NEXUS's policy
    layer, not by anything the agent decides for itself.
    """
    title = task_record.get("pr_title") or task_record.get("title", "Untitled task")
    source = task_record.get("trigger_source", "unknown")

    return (
        f"Task: {title}\n"
        f"Source: {source}\n\n"
        "Implement this change in the current repository. Make the "
        "minimal, correct change needed. Do not modify files outside "
        "what the task requires. Do not touch anything under "
        ".github/workflows/, auth/, secrets/, payments/, infra/, or "
        "migrations/ — those are protected paths requiring human review. "
        "After making changes, do not commit or push; leave the working "
        "tree modified so the caller can inspect and commit the diff."
    )


def _git(args: list, cwd: str) -> str:
    result = subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=30
    )
    # rstrip only (trailing newline) — NOT strip(), which would eat the
    # leading status-code space of the first porcelain line and corrupt
    # its column alignment.
    return result.stdout.rstrip("\n")


def get_changed_files(repo_dir: str) -> list:
    """Returns files with uncommitted changes (staged or unstaged)."""
    output = _git(["status", "--porcelain"], repo_dir)
    if not output:
        return []
    files = []
    for line in output.split("\n"):
        if not line:
            continue
        # porcelain format: "XY path" — 2 status chars, 1 space, path at column 4
        files.append(line[3:])
    return files


def run_implementation(task_record: dict, repo_dir: str, api_key: str = None) -> AgentResult:
    api_key = api_key or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return AgentResult(
            agent_name="opencode",
            role="implementation",
            success=False,
            summary="No Gemini API key available.",
            error="GEMINI_API_KEY not set — OpenCode cannot run.",
        )

    if not os.path.isdir(repo_dir):
        return AgentResult(
            agent_name="opencode",
            role="implementation",
            success=False,
            summary="Target repo directory does not exist.",
            error=f"Path not found: {repo_dir}",
        )

    ensure_opencode_config()
    prompt = build_implementation_prompt(task_record)

    env = os.environ.copy()
    env["GEMINI_API_KEY"] = api_key

    try:
        result = subprocess.run(
            ["opencode", "run", "--model", MODEL, prompt],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_SECONDS,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return AgentResult(
            agent_name="opencode",
            role="implementation",
            success=False,
            summary="OpenCode timed out.",
            error=f"Exceeded {RUN_TIMEOUT_SECONDS}s without completing.",
        )
    except FileNotFoundError:
        return AgentResult(
            agent_name="opencode",
            role="implementation",
            success=False,
            summary="OpenCode CLI is not installed on this runner.",
            error="`opencode` command not found — check the install step in the workflow.",
        )

    changed_files = get_changed_files(repo_dir)

    if result.returncode != 0:
        return AgentResult(
            agent_name="opencode",
            role="implementation",
            success=False,
            summary="OpenCode exited with an error.",
            details=result.stdout,
            error=result.stderr,
            files_changed=changed_files,
        )

    if not changed_files:
        # The command "succeeded" but nothing actually changed — do not
        # report this as success. An empty result is not a completed task.
        return AgentResult(
            agent_name="opencode",
            role="implementation",
            success=False,
            summary="OpenCode ran but made no file changes.",
            details=result.stdout,
            error="No changes detected in working tree after run.",
        )

    return AgentResult(
        agent_name="opencode",
        role="implementation",
        success=True,
        summary=f"Implemented change across {len(changed_files)} file(s).",
        details=result.stdout,
        files_changed=changed_files,
    )
