"""
Tests for the NEXUS risk classifier — including deliberate attempts
to trick it into under-classifying risk.

Run with: pytest test_risk_classifier.py -v
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from risk_classifier import (
    Policy, DiffMetadata, FileChange, RiskLevel, classify
)

POLICY_PATH = os.path.join(os.path.dirname(__file__), "..", "policy_config.yml")


@pytest.fixture
def policy():
    return Policy(POLICY_PATH)


def diff(*files, **kwargs):
    return DiffMetadata(files=list(files), **kwargs)


# ---------- BASIC HAPPY PATH ----------

def test_docs_only_is_routine(policy):
    d = diff(FileChange("docs/README.md", 10, 2))
    result = classify(d, policy)
    assert result.risk_level == RiskLevel.ROUTINE


def test_tests_only_is_routine(policy):
    d = diff(FileChange("tests/test_app.py", 20, 0))
    result = classify(d, policy)
    assert result.risk_level == RiskLevel.ROUTINE


def test_new_feature_is_normal(policy):
    d = diff(FileChange("src/app.py", 40, 5))
    result = classify(d, policy)
    assert result.risk_level == RiskLevel.NORMAL


def test_dependabot_patch_is_routine(policy):
    d = diff(
        FileChange("package.json", 2, 2),
        opened_by_dependabot=True,
        dependency_bump_type="patch",
    )
    result = classify(d, policy)
    assert result.risk_level == RiskLevel.ROUTINE


def test_dependabot_major_is_normal_minimum(policy):
    d = diff(
        FileChange("package.json", 5, 5),
        opened_by_dependabot=True,
        dependency_bump_type="major",
    )
    result = classify(d, policy)
    assert result.risk_level >= RiskLevel.NORMAL


# ---------- PROTECTED PATHS ----------

def test_workflow_file_is_high_risk(policy):
    d = diff(FileChange(".github/workflows/deploy.yml", 1, 0))
    result = classify(d, policy)
    assert result.risk_level == RiskLevel.HIGH_RISK


def test_auth_folder_is_high_risk_even_if_tiny(policy):
    d = diff(FileChange("auth/login.py", 1, 1))
    result = classify(d, policy)
    assert result.risk_level == RiskLevel.HIGH_RISK


def test_protected_path_beats_docs_classification(policy):
    # A file that LOOKS like docs but lives under a protected dir.
    d = diff(FileChange("infra/README.md", 5, 0))
    result = classify(d, policy)
    assert result.risk_level == RiskLevel.HIGH_RISK


# ---------- ADVERSARIAL: PATH TRICKS ----------

def test_case_sensitive_path_variation_still_caught(policy):
    d = diff(FileChange("AUTH/Login.py", 1, 1))
    result = classify(d, policy)
    assert result.risk_level == RiskLevel.HIGH_RISK


def test_path_traversal_attempt_still_caught(policy):
    d = diff(FileChange("src/../auth/login.py", 1, 1))
    result = classify(d, policy)
    assert result.risk_level == RiskLevel.HIGH_RISK


def test_redundant_separators_still_caught(policy):
    d = diff(FileChange("auth//./login.py", 1, 1))
    result = classify(d, policy)
    assert result.risk_level == RiskLevel.HIGH_RISK


def test_symlink_never_auto_routine(policy):
    d = diff(FileChange("docs/sneaky.md", 1, 0, is_symlink=True))
    result = classify(d, policy)
    # symlink -> "unknown" op type -> not allowlisted -> promoted off routine
    assert result.risk_level != RiskLevel.ROUTINE


def test_binary_file_never_auto_routine(policy):
    d = diff(FileChange("docs/image.png", 0, 0, is_binary=True))
    result = classify(d, policy)
    assert result.risk_level != RiskLevel.ROUTINE


# ---------- ADVERSARIAL: MIXED / HIDDEN CHANGES ----------

def test_docs_plus_production_code_is_not_routine(policy):
    d = diff(
        FileChange("README.md", 5, 0),
        FileChange("src/app.py", 30, 2),
    )
    result = classify(d, policy)
    assert result.risk_level != RiskLevel.ROUTINE
    assert result.risk_level >= RiskLevel.NORMAL


def test_test_file_that_also_touches_production_code(policy):
    d = diff(
        FileChange("tests/test_app.py", 10, 0),
        FileChange("src/app.py", 15, 3),
    )
    result = classify(d, policy)
    assert result.risk_level != RiskLevel.ROUTINE


def test_dependency_patch_mixed_with_app_code_not_routine(policy):
    d = diff(
        FileChange("package.json", 1, 1),
        FileChange("src/app.py", 20, 0),
        opened_by_dependabot=True,
        dependency_bump_type="patch",
    )
    result = classify(d, policy)
    assert result.risk_level != RiskLevel.ROUTINE


def test_generated_file_hiding_production_change(policy):
    # An unrecognized/generated file alongside docs should not let the
    # whole PR ride on the docs classification.
    d = diff(
        FileChange("README.md", 2, 0),
        FileChange("build/generated_output.bin", 500, 0, is_binary=True),
    )
    result = classify(d, policy)
    assert result.risk_level != RiskLevel.ROUTINE


# ---------- ADVERSARIAL: SIZE ----------

def test_huge_formatting_change_gets_promoted(policy):
    d = diff(FileChange("docs/README.md", 200, 100))
    result = classify(d, policy)
    assert result.risk_level != RiskLevel.ROUTINE
    assert any("threshold" in r for r in result.reasons)


def test_small_change_under_threshold_not_promoted_for_size(policy):
    d = diff(FileChange("docs/README.md", 10, 5))
    result = classify(d, policy)
    assert result.risk_level == RiskLevel.ROUTINE


# ---------- ESCALATION DIRECTION ----------

def test_unknown_operation_type_never_routine(policy):
    d = diff(FileChange("src/weird_file.xyz", 5, 0))
    result = classify(d, policy)
    assert result.risk_level != RiskLevel.ROUTINE


def test_schema_change_is_high_risk(policy):
    # Simulated: an "unknown" extension representing a schema migration
    # would in production be tagged by CI metadata as schema_change.
    # Here we confirm the map itself enforces high_risk for that type.
    assert policy.operation_risk_map["schema_change"] == RiskLevel.HIGH_RISK


def test_determinism_same_input_same_output(policy):
    d1 = diff(FileChange("src/app.py", 40, 5))
    d2 = diff(FileChange("src/app.py", 40, 5))
    r1 = classify(d1, policy)
    r2 = classify(d2, policy)
    assert r1.risk_level == r2.risk_level
    assert r1.operation_types_found == r2.operation_types_found
