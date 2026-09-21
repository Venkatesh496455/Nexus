"""
NEXUS Scout — Issue Finder

Searches ONLY the repos explicitly listed in watchlist.yml, for issues
carrying a label from issue_label_allowlist (e.g. "good first issue",
"help wanted"). This is the "real trigger" for Scout: a maintainer
explicitly signaling they want outside help, not NEXUS guessing at
what might be useful.

This module only reads. It has no ability to comment, label, or
otherwise touch anything on GitHub.
"""

import os
import yaml
import json
import urllib.request
import urllib.parse
import urllib.error


GITHUB_API = "https://api.github.com"


def load_watchlist(path: str) -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)


def _api_get(url: str, token: str = None) -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def find_issues_for_repo(repo: str, labels: list, token: str = None, max_results: int = 5) -> list:
    """
    Uses GitHub's search API to find open issues on `repo` carrying
    at least one of `labels`. Excludes pull requests (search's issue
    index includes PRs unless filtered out).
    """
    label_query = " ".join(f'label:"{l}"' for l in labels)
    query = f"repo:{repo} is:issue is:open {label_query}"
    url = f"{GITHUB_API}/search/issues?q={urllib.parse.quote(query)}&per_page={max_results}"

    try:
        data = _api_get(url, token)
    except urllib.error.HTTPError as e:
        return [{"error": f"HTTP {e.code} searching {repo}: {e.read().decode()}"}]

    results = []
    for item in data.get("items", []):
        results.append({
            "repo": repo,
            "number": item["number"],
            "title": item["title"],
            "url": item["html_url"],
            "labels": [l["name"] for l in item.get("labels", [])],
            "created_at": item["created_at"],
        })
    return results


def find_all_candidate_issues(watchlist_path: str, token: str = None) -> list:
    config = load_watchlist(watchlist_path)
    repos = config.get("watchlist") or []
    labels = config.get("issue_label_allowlist", [])

    if not repos:
        return []

    all_issues = []
    for entry in repos:
        repo = entry["repo"]
        issues = find_issues_for_repo(repo, labels, token)
        all_issues.extend(issues)
    return all_issues


if __name__ == "__main__":
    import sys
    watchlist_path = os.path.join(os.path.dirname(__file__), "watchlist.yml")
    token = os.environ.get("GITHUB_TOKEN")
    issues = find_all_candidate_issues(watchlist_path, token)
    print(json.dumps(issues, indent=2))
