"""
NEXUS Risk Classifier

This is a PURE, DETERMINISTIC function. No AI/LLM calls happen in this file.
Same diff + same policy = same answer, every single time. That property is
what makes NEXUS's autonomy trustworthy and auditable.

Golden rules (see policy_config.yml for full explanation):
  1. Uncertain or mixed changes escalate UP in risk, never down.
  2. A PR is classified by its RISKIEST file/change, never its safest.
  3. Only operation types explicitly on the allowlist can ever be "routine".
"""

import os
import re
import yaml
from dataclasses import dataclass, field
from enum import IntEnum
from typing import List


class RiskLevel(IntEnum):
    ROUTINE = 0
    NORMAL = 1
    HIGH_RISK = 2

    def __str__(self):
        return self.name.lower()


LEVEL_FROM_STRING = {
    "routine": RiskLevel.ROUTINE,
    "normal": RiskLevel.NORMAL,
    "high_risk": RiskLevel.HIGH_RISK,
}


@dataclass
class FileChange:
    path: str
    lines_added: int = 0
    lines_removed: int = 0
    is_binary: bool = False
    is_symlink: bool = False


@dataclass
class DiffMetadata:
    files: List[FileChange] = field(default_factory=list)
    opened_by_dependabot: bool = False
    dependency_bump_type: str = None  # "patch", "minor", "major", or None

    @property
    def total_lines_changed(self) -> int:
        return sum(f.lines_added + f.lines_removed for f in self.files)


@dataclass
class ClassificationResult:
    risk_level: RiskLevel
    reasons: List[str]
    operation_types_found: List[str]
    is_allowlisted: bool


class Policy:
    def __init__(self, config_path: str):
        with open(config_path, "r") as f:
            raw = yaml.safe_load(f)

        self.protected_paths = [p.lower() for p in raw["protected_paths"]]
        self.operation_risk_map = {
            k: LEVEL_FROM_STRING[v] for k, v in raw["operation_risk_map"].items()
        }
        self.allowlist = set(raw["allowlist"])
        self.size_threshold_lines = raw["size_threshold_lines"]
        self.tier_order = [LEVEL_FROM_STRING[t] for t in raw["risk_tier_order"]]


def _normalize_path(path: str) -> str:
    """Defends against path tricks: case, traversal, redundant separators."""
    normalized = os.path.normpath(path).replace("\\", "/")
    return normalized.lower()


def is_protected(path: str, policy: Policy) -> bool:
    normalized = _normalize_path(path)
    return any(protected in normalized for protected in policy.protected_paths)


def detect_file_operation_type(f: FileChange, diff: DiffMetadata) -> str:
    """
    Rule-based (regex/extension) classification of a SINGLE file's change.
    No AI involved. Deliberately conservative: anything not clearly
    recognized falls through to 'unknown', which the policy maps to
    'normal' minimum risk (see policy_config.yml).
    """
    path = _normalize_path(f.path)

    # Symlinks and binaries are never auto-classified as safe.
    if f.is_symlink or f.is_binary:
        return "unknown"

    # Dependency manifests, only when NEXUS actually knows the bump type
    # (e.g. from Dependabot metadata) - never guessed from the diff alone.
    dep_manifest_patterns = [
        "package.json", "package-lock.json", "requirements.txt",
        "pyproject.toml", "poetry.lock", "cargo.toml", "cargo.lock",
        "go.mod", "go.sum",
    ]
    if any(path.endswith(p) for p in dep_manifest_patterns):
        if diff.opened_by_dependabot and diff.dependency_bump_type in ("patch", "minor"):
            return "dependency_patch_or_minor"
        if diff.opened_by_dependabot and diff.dependency_bump_type == "major":
            return "dependency_major"
        return "unknown"  # manifest changed but we don't trust the source/type

    # Docs
    if re.search(r"\.(md|rst|txt)$", path) or path.startswith("docs/"):
        return "docs_only"

    # Tests
    if re.search(r"(^|/)(test_|.*_test\.|.*\.test\.|.*\.spec\.)", path) or path.startswith("tests/"):
        return "tests_only"

    # Formatting-only requires more than a filename match — see
    # detect_operation_type() for the diff-content check. At the
    # per-file level we can only say "this is a source file".
    return "unknown"


def _looks_formatting_only(f: FileChange) -> bool:
    """
    Placeholder for real diff-content inspection (whitespace/lint-only
    changes). In production this should inspect the actual patch hunks,
    not just line counts. Conservative default: unless we can PROVE
    it's formatting-only, we do NOT call it formatting-only.
    """
    return False


def detect_operation_type(diff: DiffMetadata) -> List[str]:
    """
    Returns the set of distinct operation types present across ALL
    files in the diff. A mixed PR will return multiple types, e.g.
    ["docs_only", "unknown"] for a README + src/app.py change.
    """
    types = set()
    for f in diff.files:
        if _looks_formatting_only(f):
            types.add("formatting_only")
        else:
            types.add(detect_file_operation_type(f, diff))
    return sorted(types)


def _bump_up(level: RiskLevel, policy: Policy) -> RiskLevel:
    idx = policy.tier_order.index(level)
    if idx + 1 < len(policy.tier_order):
        return policy.tier_order[idx + 1]
    return level  # already at the top


def classify(diff: DiffMetadata, policy: Policy) -> ClassificationResult:
    reasons = []

    # Rule 1: protected paths always win, immediately.
    protected_files = [f.path for f in diff.files if is_protected(f.path, policy)]
    if protected_files:
        return ClassificationResult(
            risk_level=RiskLevel.HIGH_RISK,
            reasons=[f"Touches protected path(s): {protected_files}"],
            operation_types_found=[],
            is_allowlisted=False,
        )

    # Rule 2: determine operation type(s) present in the diff.
    op_types = detect_operation_type(diff)
    reasons.append(f"Detected operation types: {op_types}")

    # Rule 3 (mixed-file promotion): classify by the RISKIEST type present,
    # never the safest. A doc file + a code file is NOT routine.
    base_level = RiskLevel.ROUTINE
    for op_type in op_types:
        op_level = policy.operation_risk_map.get(op_type, RiskLevel.NORMAL)
        if op_level > base_level:
            base_level = op_level

    if len(op_types) > 1:
        reasons.append("Mixed change types detected -> classified by riskiest type present.")

    # Rule 4: only allowlisted, single-type, routine-eligible changes can
    # ever end up routine. A single "unknown" or non-allowlisted type
    # anywhere in the diff blocks routine status.
    is_allowlisted = len(op_types) == 1 and op_types[0] in policy.allowlist
    if base_level == RiskLevel.ROUTINE and not is_allowlisted:
        base_level = RiskLevel.NORMAL
        reasons.append("Not on allowlist (or mixed types) -> promoted to normal.")

    # Rule 5: size threshold can bump risk up a tier regardless of type.
    if diff.total_lines_changed > policy.size_threshold_lines:
        pre_bump = base_level
        base_level = _bump_up(base_level, policy)
        if base_level != pre_bump:
            reasons.append(
                f"Diff size {diff.total_lines_changed} lines exceeds "
                f"threshold {policy.size_threshold_lines} -> promoted."
            )

    return ClassificationResult(
        risk_level=base_level,
        reasons=reasons,
        operation_types_found=op_types,
        is_allowlisted=is_allowlisted,
    )
