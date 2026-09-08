"""
NEXUS Trigger Listener — Dependabot source

CORE PRINCIPLE: no real trigger, no action. This script does not invent
work. It only runs when GitHub Actions tells it a real Dependabot event
happened, and it only produces a task record when that event is genuine.

What this does NOT do (by design, for now):
  - It does not decide risk (that's risk_classifier.py's job, run later
    by nexus-gate.yml once a PR exists).
  - It does not call any AI model.
  - It does not open PRs itself — Dependabot already does that natively
    when configured via dependabot.yml. This listener's job is to take
    Dependabot's raw alert/PR event and produce a structured, logged
    "task record" that later pipeline stages (the router, in step 6)
    will consume.

In this first version, the "trigger" is simply: Dependabot opened a PR.
GitHub Actions invokes this script with that PR's data already available
via the standard pull_request event context — no polling, no guessing.
"""

import os
import sys
import json
import datetime


TASK_LOG_PATH = os.environ.get(
    "NEXUS_TASK_LOG",
    os.path.join(os.path.dirname(__file__), "..", "logs", "audit", "tasks.jsonl"),
)

DEPENDABOT_LOGIN = "dependabot[bot]"


class TriggerRejected(Exception):
    """Raised when the event does not qualify as a real trigger."""


def load_event_payload(event_path: str) -> dict:
    """
    GitHub Actions writes the full event JSON to a file and exposes its
    path via GITHUB_EVENT_PATH. We read from there — not from any
    manually-constructed input — so the trigger is always tied to a
    real GitHub event, never a fabricated one.
    """
    with open(event_path, "r") as f:
        return json.load(f)


def validate_dependabot_trigger(payload: dict) -> dict:
    """
    Confirms this is a genuine Dependabot-originated pull_request event.
    Raises TriggerRejected if not — NEXUS must never log or act on a
    trigger it can't verify actually happened.
    """
    pr = payload.get("pull_request")
    if pr is None:
        raise TriggerRejected("Event payload has no pull_request — not a PR event.")

    author = pr.get("user", {}).get("login", "")
    if author != DEPENDABOT_LOGIN:
        raise TriggerRejected(f"PR author is '{author}', not Dependabot — ignoring.")

    action = payload.get("action")
    if action not in ("opened", "reopened", "synchronize"):
        raise TriggerRejected(f"Action '{action}' is not a task-worthy trigger — ignoring.")

    return pr


def build_task_record(pr: dict, repo: str) -> dict:
    """
    Produces the structured record that downstream stages (router,
    agents) will read. Deliberately plain data — no AI-generated
    content at this stage.
    """
    return {
        "trigger_source": "dependabot",
        "trigger_type": "dependency_update",
        "repo": repo,
        "pr_number": pr["number"],
        "pr_title": pr["title"],
        "pr_url": pr["html_url"],
        "base_branch": pr["base"]["ref"],
        "head_branch": pr["head"]["ref"],
        "detected_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        # Downstream stages (nexus-gate.yml) independently determine the
        # actual risk level from the diff. This listener does not guess it.
        "status": "detected",
    }


def append_task_log(record: dict, log_path: str):
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a") as f:
        f.write(json.dumps(record) + "\n")


def main():
    event_path = os.environ["GITHUB_EVENT_PATH"]
    repo = os.environ["GITHUB_REPOSITORY"]

    payload = load_event_payload(event_path)

    try:
        pr = validate_dependabot_trigger(payload)
    except TriggerRejected as e:
        # Not an error — this is the event-driven principle working as
        # intended. Most PR events on a repo are NOT Dependabot events,
        # and the listener should silently no-op for those, not fail.
        print(f"NEXUS trigger listener: no action taken. Reason: {e}")
        return

    record = build_task_record(pr, repo)
    append_task_log(record, TASK_LOG_PATH)

    print(f"NEXUS trigger recorded: {record['trigger_type']} on PR #{record['pr_number']}")
    print(json.dumps(record, indent=2))

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"triggered=true\n")
            f.write(f"pr_number={record['pr_number']}\n")


if __name__ == "__main__":
    main()
