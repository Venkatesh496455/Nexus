"""
NEXUS Scout — Automatic Repo Discovery

Replaces the manual watchlist with a live GitHub search: finds issues
carrying a genuine help-wanted-style label, across (a) languages you
actually work in, and (b) other repos generally, gated by a minimum
star count as a basic legitimacy signal.

This does NOT bypass policy_check.py. Discovery only finds candidates
— every one still needs an explicit "allowed" policy result before
Scout treats it as ready to draft. Discovery makes the search
automatic; it does not make the safety gate automatic.

Bounded by design: a hard max-results cap and a freshness window
(max_age_days) keep each run's output small and reviewable, rather
than flooding you with hundreds of low-quality matches.
"""

import os
import sys
import yaml
import json
import datetime
import urllib.request
import urllib.parse
import urllib.error


GITHUB_API = "https://api.github.com"


def load_discovery_config(path: str) -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)


def _api_get(url: str, token: str = None) -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def _build_label_clause(labels: list) -> str:
    return " ".join(f'label:"{l}"' for l in labels)


def build_stack_query(languages: list, labels: list, since_date: str) -> str:
    lang_clause = " ".join(f"language:{lang}" for lang in languages)
    # GitHub search ORs same-key qualifiers only via separate calls, not
    # within one query reliably — so this queries one language at a time
    # from find_candidate_issues() rather than combining them here.
    return f"is:issue is:open {_build_label_clause(labels)} created:>={since_date}"


def build_general_query(min_stars: int, labels: list, since_date: str) -> str:
    return (
        f"is:issue is:open {_build_label_clause(labels)} "
        f"created:>={since_date}"
    )


def search_issues(query: str, token: str, max_results: int) -> list:
    url = f"{GITHUB_API}/search/issues?q={urllib.parse.quote(query)}&per_page={max_results}&sort=created&order=desc"
    try:
        data = _api_get(url, token)
    except urllib.error.HTTPError as e:
        return [{"error": f"HTTP {e.code}: {e.read().decode()}"}]

    results = []
    for item in data.get("items", []):
        repo_url = item["repository_url"]
        repo = "/".join(repo_url.split("/")[-2:])
        results.append({
            "repo": repo,
            "number": item["number"],
            "title": item["title"],
            "url": item["html_url"],
            "labels": [l["name"] for l in item.get("labels", [])],
            "created_at": item["created_at"],
        })
    return results


def _repo_star_count(repo: str, token: str) -> int:
    data = _api_get(f"{GITHUB_API}/repos/{repo}", token)
    return data.get("stargazers_count", 0)


def discover_candidate_issues(config_path: str, token: str = None) -> list:
    config = load_discovery_config(config_path)
    labels = config["issue_label_allowlist"]
    max_results = config["max_results_per_search"]
    since_date = (
        datetime.date.today() - datetime.timedelta(days=config["max_age_days"])
    ).isoformat()

    all_results = []
    seen = set()

    # Stack-specific searches — one language at a time, since GitHub's
    # search syntax doesn't reliably OR the `language:` qualifier.
    for lang in config.get("stack_languages", []):
        query = f"is:issue is:open language:{lang} {_build_label_clause(labels)} created:>={since_date}"
        for issue in search_issues(query, token, max_results):
            key = (issue.get("repo"), issue.get("number"))
            if "error" not in issue and key not in seen:
                seen.add(key)
                issue["found_via"] = f"stack:{lang}"
                all_results.append(issue)

    # General search, filtered afterward by star count — GitHub search
    # doesn't support a `stars:>N` filter combined cleanly with our
    # other qualifiers in every case, so we filter client-side.
    general_cfg = config.get("general_search", {})
    if general_cfg.get("enabled"):
        query = f"is:issue is:open {_build_label_clause(labels)} created:>={since_date}"
        candidates = search_issues(query, token, max_results)
        min_stars = general_cfg.get("min_stars", 0)
        for issue in candidates:
            if "error" in issue:
                continue
            key = (issue.get("repo"), issue.get("number"))
            if key in seen:
                continue
            try:
                stars = _repo_star_count(issue["repo"], token)
            except Exception:
                continue
            if stars >= min_stars:
                seen.add(key)
                issue["found_via"] = "general"
                issue["repo_stars"] = stars
                all_results.append(issue)

    return all_results


if __name__ == "__main__":
    config_path = os.path.join(os.path.dirname(__file__), "discovery_config.yml")
    token = os.environ.get("GITHUB_TOKEN")
    issues = discover_candidate_issues(config_path, token)
    print(json.dumps(issues, indent=2))
