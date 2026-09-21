"""
NEXUS Scout — Policy Check

MANDATORY FIRST GATE. Per NEXUS's governing principle ("respect
external projects — NEXUS must check and obey each project's
contribution/AI-use policy before contributing anything"), this
runs before Scout is allowed to draft anything for an external repo.

Three possible outcomes:
  - "blocked"            : the project explicitly disallows AI contributions
  - "needs_human_review"  : no clear policy found either way
  - "allowed"             : the project explicitly welcomes AI contributions

Deliberately conservative, matching the pattern used everywhere else
in NEXUS: "needs_human_review" is the default when uncertain, never
"allowed". A missing or silent CONTRIBUTING.md does NOT mean go ahead —
it means a human decides, same as an unclassifiable diff gets held
rather than guessed at by the risk classifier.

This module only READS. It never opens issues, comments, or submits
anything — that boundary belongs entirely to a human, enforced by
draft_presenter.py never having a "submit" function at all.
"""

import re
import sys
import json
import urllib.request
import urllib.error


GITHUB_API = "https://api.github.com"

CANDIDATE_PATHS = [
    "CONTRIBUTING.md",
    "CONTRIBUTING.rst",
    ".github/CONTRIBUTING.md",
    "docs/CONTRIBUTING.md",
]

# Conservative keyword matching. Order matters: block-phrases are
# checked first, so an explicit prohibition always wins even if the
# same doc also contains a vaguer allow-sounding phrase elsewhere.
BLOCK_PHRASES = [
    "no ai-generated",
    "no ai generated",
    "not accept ai",
    "do not accept ai",
    "ai contributions are not",
    "no llm-generated",
    "no llm generated",
    "human-written only",
    "human written only",
    "do not use ai",
    "do not use generative ai",
    "no generative ai",
    "must be written by a human",
]

ALLOW_PHRASES = [
    "ai-generated contributions are welcome",
    "ai contributions are welcome",
    "llm-assisted contributions are welcome",
    "we welcome ai-assisted",
    "ai tools are permitted",
    "use of ai tools is permitted",
]


def _fetch_raw_file(repo: str, path: str, token: str = None) -> str:
    """Fetches a file's raw content from the default branch via the
    GitHub API's contents endpoint. Returns None if not found."""
    url = f"{GITHUB_API}/repos/{repo}/contents/{path}"
    headers = {"Accept": "application/vnd.github.raw+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def find_contributing_doc(repo: str, token: str = None) -> tuple:
    """Returns (path, content) for the first CONTRIBUTING doc found,
    or (None, None) if none of the candidate paths exist."""
    for path in CANDIDATE_PATHS:
        content = _fetch_raw_file(repo, path, token)
        if content:
            return path, content
    return None, None


def classify_ai_policy(content: str) -> tuple:
    """
    Returns (status, matched_phrase_or_None).
    status is one of: "blocked", "allowed", "needs_human_review"
    """
    lowered = content.lower()

    for phrase in BLOCK_PHRASES:
        if phrase in lowered:
            return "blocked", phrase

    for phrase in ALLOW_PHRASES:
        if phrase in lowered:
            return "allowed", phrase

    return "needs_human_review", None


def check_repo_policy(repo: str, token: str = None) -> dict:
    """
    Full policy check for a single repo. Always returns a result —
    a repo with NO CONTRIBUTING doc at all is "needs_human_review",
    not "allowed". Scout must never contribute silently by default.
    """
    path, content = find_contributing_doc(repo, token)

    if content is None:
        return {
            "repo": repo,
            "status": "needs_human_review",
            "reason": "No CONTRIBUTING document found — policy unknown.",
            "source_path": None,
            "matched_phrase": None,
        }

    status, matched_phrase = classify_ai_policy(content)

    reason = {
        "blocked": f"Found explicit prohibition: \"{matched_phrase}\"",
        "allowed": f"Found explicit permission: \"{matched_phrase}\"",
        "needs_human_review": "No explicit AI-contribution policy found in the document.",
    }[status]

    return {
        "repo": repo,
        "status": status,
        "reason": reason,
        "source_path": path,
        "matched_phrase": matched_phrase,
    }


if __name__ == "__main__":
    # Simple CLI for manually checking a repo before adding it to the watchlist.
    if len(sys.argv) != 2:
        print("Usage: python policy_check.py <owner/repo>")
        sys.exit(1)
    result = check_repo_policy(sys.argv[1])
    print(json.dumps(result, indent=2))
