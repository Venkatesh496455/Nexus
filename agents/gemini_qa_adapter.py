"""
NEXUS Agent Adapter — Gemini QA

Fills the "qa" role. Given a task record and the code changes it
produced, asks Gemini to review the diff for obvious bugs, missing
edge cases, and test coverage gaps, and returns a pass/fail-style
result.

Authentication: reads the Gemini API key from the GEMINI_API_KEY
environment variable. In GitHub Actions, this is injected from a
repository Secret — it is never hardcoded or committed to the repo.

This adapter makes ONE real network call (to Gemini's API). It does
not modify any files itself — QA reviews, it doesn't fix.
"""

import os
import sys
import json
import urllib.request
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from base_adapter import AgentResult


GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.0-flash:generateContent"
)


def _call_gemini(prompt: str, api_key: str) -> str:
    url = f"{GEMINI_API_URL}?key={api_key}"
    body = {
        "contents": [{"parts": [{"text": prompt}]}]
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read().decode())
    return result["candidates"][0]["content"]["parts"][0]["text"]


def build_qa_prompt(task_record: dict, diff_summary: str) -> str:
    return f"""You are a QA reviewer. Review this code change for bugs,
missing edge cases, and test coverage gaps. Be concise.

Task: {task_record.get('pr_title', 'unknown')}
Trigger source: {task_record.get('trigger_source', 'unknown')}

Diff summary:
{diff_summary}

Respond in this exact format:
VERDICT: PASS or FAIL
REASON: one or two sentences
"""


def parse_verdict(response_text: str) -> bool:
    """Conservative parsing: only a clear PASS counts as success.
    Anything ambiguous or malformed is treated as FAIL, consistent
    with the 'uncertain -> escalate' principle used elsewhere in NEXUS."""
    first_line = response_text.strip().splitlines()[0] if response_text.strip() else ""
    return "PASS" in first_line.upper() and "FAIL" not in first_line.upper()


def run_qa(task_record: dict, diff_summary: str, api_key: str = None) -> AgentResult:
    api_key = api_key or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return AgentResult(
            agent_name="gemini_cli",
            role="qa",
            success=False,
            summary="No Gemini API key available.",
            error="GEMINI_API_KEY not set — QA could not run.",
        )

    prompt = build_qa_prompt(task_record, diff_summary)

    try:
        response_text = _call_gemini(prompt, api_key)
    except urllib.error.HTTPError as e:
        return AgentResult(
            agent_name="gemini_cli",
            role="qa",
            success=False,
            summary="Gemini API call failed.",
            error=f"HTTP {e.code}: {e.read().decode()}",
        )
    except Exception as e:
        return AgentResult(
            agent_name="gemini_cli",
            role="qa",
            success=False,
            summary="Gemini API call failed.",
            error=str(e),
        )

    passed = parse_verdict(response_text)

    return AgentResult(
        agent_name="gemini_cli",
        role="qa",
        success=passed,
        summary="QA passed." if passed else "QA flagged issues — see details.",
        details=response_text,
    )
