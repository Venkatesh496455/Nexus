"""
NEXUS Agent Adapter — shared interface

Every agent adapter (Claude Code, OpenCode, Gemini CLI, Pencil.dev)
returns the same shape of result, so the rest of NEXUS (router,
workflows, audit logging) can treat them interchangeably. This is
what "model-agnostic orchestration" means in practice: NEXUS's core
logic never needs to know which specific tool did the work.
"""

from dataclasses import dataclass, field
from typing import List


@dataclass
class AgentResult:
    agent_name: str
    role: str                  # "implementation", "qa", "review", "ui_design"
    success: bool
    summary: str                # short human-readable outcome
    details: str = ""           # longer output (logs, test results, etc.)
    files_changed: List[str] = field(default_factory=list)
    error: str = ""             # populated only when success is False
