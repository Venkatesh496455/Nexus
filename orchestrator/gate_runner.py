"""
NEXUS Gate Runner

Runs inside GitHub Actions on every PR event. It:
  1. Fetches the list of changed files for the PR from GitHub's API
  2. Builds a DiffMetadata object from that data
  3. Runs it through the deterministic risk_classifier
  4. Posts the result as a PR comment
  5. Sets a GitHub Actions output so later workflow steps (or a separate
     auto-merge workflow) can react to the risk level

No AI calls happen here. This script is as deterministic as the classifier
it calls — same PR contents, same posted result, every time.
"""

import os
import re
import sys
import json
import urllib.request
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from risk_classifier import Policy, DiffMetadata, FileChange, classify


GITHUB_API = "https://api.github.com"


def _api_request(url: str, token: str, method: str = "GET", body: dict = None):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, headers=headers, data=data, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"GitHub API error {e.code} calling {url}: {e.read().decode()}", file=sys.stderr)
        raise


def fetch_pr_files(repo: str, pr_number: str, token: str) -> list:
    """Returns raw file-change dicts from the GitHub API (paginated)."""
    files = []
    page = 1
    while True:
        url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/files?per_page=100&page={page}"
        batch = _api_request(url, token)
        if not batch:
            break
        files.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return files


def fetch_pr_meta(repo: str, pr_number: str, token: str) -> dict:
    url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}"
    return _api_request(url, token)


DEPENDABOT_LOGIN = "dependabot[bot]"
BUMP_PATTERN = re.compile(
    r"bump\s+\S+\s+from\s+(\d+)\.(\d+)\.(\d+)\s+to\s+(\d+)\.(\d+)\.(\d+)", re.IGNORECASE
)


def detect_dependency_bump(pr_meta: dict) -> tuple:
    """
    Returns (opened_by_dependabot: bool, bump_type: str|None).
    Parses Dependabot's standard PR title format to infer patch/minor/major.
    Conservative: if we can't confidently parse it, bump_type is None,
    which the classifier treats as untrusted -> not routine-eligible.
    """
    author = pr_meta.get("user", {}).get("login", "")
    if author != DEPENDABOT_LOGIN:
        return False, None

    title = pr_meta.get("title", "")
    match = BUMP_PATTERN.search(title)
    if not match:
        return True, None

    old_major, old_minor, old_patch, new_major, new_minor, new_patch = map(int, match.groups())
    if new_major != old_major:
        return True, "major"
    if new_minor != old_minor:
        return True, "minor"
    return True, "patch"


def build_diff_metadata(raw_files: list, pr_meta: dict) -> DiffMetadata:
    opened_by_dependabot, bump_type = detect_dependency_bump(pr_meta)

    file_changes = []
    for f in raw_files:
        file_changes.append(FileChange(
            path=f["filename"],
            lines_added=f.get("additions", 0),
            lines_removed=f.get("deletions", 0),
            is_binary=(f.get("patch") is None and f.get("status") != "renamed"),
            is_symlink=False,  # GitHub's Files API doesn't expose this directly;
                                # a stricter check could fetch blob mode via the
                                # git trees API if this becomes a real risk vector.
        ))

    return DiffMetadata(
        files=file_changes,
        opened_by_dependabot=opened_by_dependabot,
        dependency_bump_type=bump_type,
    )


EMOJI = {"routine": "🟢", "normal": "🟡", "high_risk": "🔴"}


def format_comment(result, diff: DiffMetadata) -> str:
    emoji = EMOJI[str(result.risk_level)]
    lines = [
        f"## {emoji} NEXUS Risk Assessment: `{str(result.risk_level).upper()}`",
        "",
        f"**Files changed:** {len(diff.files)}",
        f"**Lines changed:** {diff.total_lines_changed}",
        f"**Operation type(s) detected:** {', '.join(result.operation_types_found) or 'none'}",
        f"**Allowlisted for auto-merge:** {'yes' if result.is_allowlisted else 'no'}",
        "",
        "**Reasoning:**",
    ]
    for r in result.reasons:
        lines.append(f"- {r}")

    if str(result.risk_level) == "routine" and result.is_allowlisted:
        lines.append("")
        lines.append("✅ Eligible for auto-merge once required checks pass.")
    else:
        lines.append("")
        lines.append("🔒 Manual approval required before merge.")

    return "\n".join(lines)


def post_comment(repo: str, pr_number: str, token: str, body: str):
    url = f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments"
    _api_request(url, token, method="POST", body={"body": body})


def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    pr_number = os.environ["PR_NUMBER"]
    token = os.environ["GITHUB_TOKEN"]
    policy_path = os.environ.get(
        "NEXUS_POLICY_PATH",
        os.path.join(os.path.dirname(__file__), "policy_config.yml"),
    )

    policy = Policy(policy_path)
    pr_meta = fetch_pr_meta(repo, pr_number, token)
    raw_files = fetch_pr_files(repo, pr_number, token)
    diff = build_diff_metadata(raw_files, pr_meta)
    result = classify(diff, policy)

    comment = format_comment(result, diff)
    post_comment(repo, pr_number, token, comment)

    # Surface the result to later steps/workflows (e.g. auto-merge workflow).
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"risk_level={str(result.risk_level)}\n")
            f.write(f"is_allowlisted={'true' if result.is_allowlisted else 'false'}\n")

    print(comment)

    # Fail the check only if something is HIGH_RISK and slipped in unexpectedly
    # without protected-path detection working — belt-and-suspenders, not a
    # duplicate gate. The real gate is the required-approval branch protection.


if __name__ == "__main__":
    main()
