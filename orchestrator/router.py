"""
NEXUS Model Router

Given a task (identified by its trigger_type, e.g. "dependency_update"),
decides:
  1. Which roles this task needs (implementation, qa, review, ui_design)
  2. Which specific agent fills each role, respecting fallback order
     and current availability (quota-aware, model-agnostic)

This is deterministic, same as the risk classifier — same task type +
same agent availability = same routing decision, every time. No AI
judgment involved in the routing decision itself.

If a REQUIRED role (see router_config.yml) has no available agent,
the task is marked "held" rather than dropped or force-assigned —
NEXUS surfaces this so a human can intervene (add quota, fix an
agent, etc.) instead of silently failing or picking a bad substitute.
"""

import os
import yaml
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class RoutingResult:
    task_trigger_type: str
    assignments: Dict[str, str]          # role -> agent_name
    unfilled_roles: List[str]            # roles that needed an agent but got none
    status: str                          # "routed" or "held"
    reasons: List[str] = field(default_factory=list)


class RouterConfig:
    def __init__(self, router_config_path: str, agent_status_path: str):
        with open(router_config_path, "r") as f:
            raw = yaml.safe_load(f)
        self.task_role_map = raw["task_role_map"]
        self.role_agent_chain = raw["role_agent_chain"]
        self.required_roles = set(raw["required_roles"])

        with open(agent_status_path, "r") as f:
            status_raw = yaml.safe_load(f)
        self.agent_availability = {
            name: info.get("available", False)
            for name, info in status_raw["agents"].items()
        }


def route_task(trigger_type: str, config: RouterConfig) -> RoutingResult:
    reasons = []

    roles_needed = config.task_role_map.get(trigger_type)
    if roles_needed is None:
        # Unknown task type -> nothing NEXUS recognizes how to route.
        # Held, not guessed. Consistent with "uncertain -> escalate/hold"
        # rather than inventing a routing decision.
        return RoutingResult(
            task_trigger_type=trigger_type,
            assignments={},
            unfilled_roles=[],
            status="held",
            reasons=[f"Unknown task type '{trigger_type}' — no role mapping exists."],
        )

    assignments = {}
    unfilled_roles = []

    for role in roles_needed:
        chain = config.role_agent_chain.get(role, [])
        chosen_agent = None
        for agent in chain:
            if config.agent_availability.get(agent, False):
                chosen_agent = agent
                break
        if chosen_agent:
            assignments[role] = chosen_agent
            reasons.append(f"Role '{role}' -> assigned to '{chosen_agent}'")
        else:
            unfilled_roles.append(role)
            reasons.append(f"Role '{role}' -> NO available agent in chain {chain}")

    unmet_required = [r for r in unfilled_roles if r in config.required_roles]
    status = "held" if unmet_required else "routed"

    if unmet_required:
        reasons.append(
            f"Task HELD: required role(s) {unmet_required} have no available agent."
        )

    return RoutingResult(
        task_trigger_type=trigger_type,
        assignments=assignments,
        unfilled_roles=unfilled_roles,
        status=status,
        reasons=reasons,
    )


def load_config(
    router_config_path: Optional[str] = None,
    agent_status_path: Optional[str] = None,
) -> RouterConfig:
    base = os.path.dirname(__file__)
    router_config_path = router_config_path or os.path.join(base, "router_config.yml")
    agent_status_path = agent_status_path or os.path.join(base, "agent_status.yml")
    return RouterConfig(router_config_path, agent_status_path)
