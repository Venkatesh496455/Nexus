"""
NEXUS Pipeline — Stage 2 (auto-merge decision)

Runs AFTER nexus-gate.yml's risk classification step. Given the risk
level and allowlist status from that step, this decides the final
outcome for a PR:

  - Not routine, or not allowlisted -> stop here. Nothing more to do;
    the PR sits and waits for Venkatesh's manual approval, as designed.
  - Routine AND allowlisted -> run QA (Gemini). QA verdict decides:
      - QA passes -> enable GitHub's native auto-merge, so GitHub
        itself waits for required status checks to go green before
        actually merging. NEXUS never bypasses required checks —
        it only tells GitHub "merge this once your own checks pass."
      - QA fails -> do NOT enable auto-merge. Post a comment
        explaining why, and stop. Escalate, don't guess.

HARD RULE this enforces in code: a required gate is never skipped to
save time/quota. If QA can't run (no key, network error), the PR is
treated as NOT cleared for auto-merge — same "uncertain -> hold"
principle used throughout NEXUS.
"""

import os
import sys
import json
import urllib.request
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agents"))

from gate_runner import fetch_pr_files, fetch_pr_meta, _api_request, GITHUB_API
from gemini_qa_adapter import run_qa


AUDIT_LOG_PATH = os.environ.get(
    "NEXUS_PIPELINE_LOG",
    os.path.join(os.path.dirname(__file__), "..", "logs", "audit", "pipeline.jsonl"),
)


def build_diff_summary(raw_files: list) -> str:
    lines = []
    for f in raw_files:
        lines.append(f"- {f['filename']} (+{f.get('additions', 0)}/-{f.get('deletions', 0)})")
    return "\n".join(lines) if lines else "No files changed."


def enable_auto_merge(repo: str, pr_node_id: str, token: str) -> dict:
    """
    Enables GitHub's native auto-merge via GraphQL. This does NOT merge
    immediately — GitHub itself will merge once its own required status
    checks pass. This is deliberate: NEXUS never substitutes its own
    judgment for the repo's configured required checks.
    """
    query = """
    mutation($pullRequestId: ID!) {
      enablePullRequestAutoMerge(input: {pullRequestId: $pullRequestId, mergeMethod: SQUASH}) {
        pullRequest { autoMergeRequest { enabledAt } }
      }
    }
    """
    body = {"query": query, "variables": {"pullRequestId": pr_node_id}}
    req = urllib.request.Request(
        f"{GITHUB_API}/graphql",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def post_comment(repo: str, pr_number: str, token: str, body: str):
    url = f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments"
    _api_request(url, token, method="POST", body={"body": body})


def append_audit_log(record: dict, log_path: str):
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a") as f:
        f.write(json.dumps(record) + "\n")


def run_pipeline(repo: str, pr_number: str, token: str, risk_level: str, is_allowlisted: bool):
    """
    Pure-ish orchestration function, deliberately taking risk_level and
    is_allowlisted as explicit inputs (from the prior workflow step's
    outputs) rather than re-deriving them here — stage 1 (risk
    classification) and stage 2 (this file) each do exactly one job.
    """
    record = {
        "repo": repo,
        "pr_number": pr_number,
        "risk_level": risk_level,
        "is_allowlisted": is_allowlisted,
    }

    if not (risk_level == "routine" and is_allowlisted):
        record["outcome"] = "no_action"
        record["reason"] = "Not routine + allowlisted — awaiting manual approval, as designed."
        append_audit_log(record, AUDIT_LOG_PATH)
        print(record["reason"])
        return record

    pr_meta = fetch_pr_meta(repo, pr_number, token)
    raw_files = fetch_pr_files(repo, pr_number, token)
    diff_summary = build_diff_summary(raw_files)

    task_record = {
        "pr_title": pr_meta.get("title", ""),
        "trigger_source": "dependabot" if pr_meta.get("user", {}).get("login") == "dependabot[bot]" else "unknown",
    }

    qa_result = run_qa(task_record, diff_summary, api_key=os.environ.get("GEMINI_API_KEY"))
    record["qa_success"] = qa_result.success
    record["qa_summary"] = qa_result.summary

    if not qa_result.success:
        record["outcome"] = "held_qa_failed"
        append_audit_log(record, AUDIT_LOG_PATH)
        post_comment(
            repo, pr_number, token,
            f"## 🔒 NEXUS: Auto-merge withheld\n\nQA check did not pass.\n\n"
            f"**Summary:** {qa_result.summary}\n\n**Details:**\n{qa_result.details or qa_result.error}\n\n"
            f"This PR requires manual review before merge."
        )
        print(f"QA failed — auto-merge withheld: {qa_result.summary}")
        return record

    try:
        merge_response = enable_auto_merge(repo, pr_meta["node_id"], token)
        if "errors" in merge_response:
            raise RuntimeError(json.dumps(merge_response["errors"]))
        record["outcome"] = "auto_merge_enabled"
        append_audit_log(record, AUDIT_LOG_PATH)
        post_comment(
            repo, pr_number, token,
            f"## ✅ NEXUS: Auto-merge enabled\n\nQA passed: {qa_result.summary}\n\n"
            f"This PR will merge automatically once required checks pass."
        )
        print("Auto-merge enabled.")
    except (urllib.error.HTTPError, RuntimeError) as e:
        record["outcome"] = "auto_merge_failed"
        record["error"] = str(e)
        append_audit_log(record, AUDIT_LOG_PATH)
        post_comment(
            repo, pr_number, token,
            f"## ⚠️ NEXUS: Could not enable auto-merge\n\n"
            f"QA passed, but enabling auto-merge failed: {str(e)}\n\n"
            f"This usually means auto-merge isn't enabled for this repository "
            f"(Settings → General → Pull Requests → 'Allow auto-merge'), or "
            f"branch protection with required status checks isn't configured. "
            f"Manual merge required."
        )
        print(f"Failed to enable auto-merge: {e}")

    return record


def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    pr_number = os.environ["PR_NUMBER"]
    token = os.environ["GITHUB_TOKEN"]
    risk_level = os.environ["NEXUS_RISK_LEVEL"]
    is_allowlisted = os.environ.get("NEXUS_IS_ALLOWLISTED", "false") == "true"

    run_pipeline(repo, pr_number, token, risk_level, is_allowlisted)


if __name__ == "__main__":
    main()
