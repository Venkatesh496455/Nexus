"""
NEXUS Scout — Draft Presenter

Combines a found issue + its mandatory policy check into a single
package for Venkatesh to review. This module deliberately has NO
function that opens an issue, posts a comment, forks a repo, or
opens a PR on an external project. That capability does not exist
in this codebase at all — it's not "disabled," it was never written,
so there's nothing to accidentally trigger.

The only output this produces is a human-readable summary and a
structured record for the audit log. Nothing here is "sent" anywhere.
"""

import os
import json
import datetime


def build_draft_package(issue: dict, policy_result: dict) -> dict:
    """
    Combines one issue with its policy check result into a single
    reviewable record. If the policy check didn't come back "allowed",
    the package is explicitly marked as NOT ready to act on — Scout
    surfaces it anyway (transparency), but flags it clearly rather
    than silently dropping it or silently proceeding.
    """
    policy_status = policy_result.get("status", "needs_human_review")

    return {
        "repo": issue.get("repo"),
        "issue_number": issue.get("number"),
        "issue_title": issue.get("title"),
        "issue_url": issue.get("url"),
        "issue_labels": issue.get("labels", []),
        "policy_status": policy_status,
        "policy_reason": policy_result.get("reason"),
        "policy_source": policy_result.get("source_path"),
        "ready_for_contribution": policy_status == "allowed",
        "packaged_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "action_required": (
            "None — policy explicitly allows AI contributions. "
            "Draft implementation can proceed for your review."
            if policy_status == "allowed" else
            "Manually review this project's contribution norms before proceeding. "
            "NEXUS will not draft a fix until you confirm."
        ),
    }


def format_summary(package: dict) -> str:
    status_emoji = {
        "allowed": "🟢",
        "needs_human_review": "🟡",
        "blocked": "🔴",
    }.get(package["policy_status"], "🟡")

    lines = [
        f"{status_emoji} **{package['repo']}#{package['issue_number']}** — {package['issue_title']}",
        f"   Labels: {', '.join(package['issue_labels']) or 'none'}",
        f"   URL: {package['issue_url']}",
        f"   AI-use policy: {package['policy_status']} — {package['policy_reason']}",
        f"   Action required: {package['action_required']}",
    ]
    return "\n".join(lines)


def append_scout_log(packages: list, log_path: str):
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a") as f:
        for package in packages:
            f.write(json.dumps(package) + "\n")


if __name__ == "__main__":
    # Manual demo / dry run with fabricated example data — for
    # verifying formatting only, never used to submit anything.
    example_issue = {
        "repo": "example/project",
        "number": 42,
        "title": "Fix typo in README",
        "url": "https://github.com/example/project/issues/42",
        "labels": ["good first issue", "documentation"],
    }
    example_policy = {
        "status": "needs_human_review",
        "reason": "No explicit AI-contribution policy found in the document.",
        "source_path": "CONTRIBUTING.md",
    }
    pkg = build_draft_package(example_issue, example_policy)
    print(format_summary(pkg))
