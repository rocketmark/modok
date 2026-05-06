"""
Golden scenario tests — Layer 1.
Each test loads a YAML scenario and asserts that real MODOK and the
reference model agree on the debug packet contents.
All tests written before implementation (Phase 5).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.hifi.harness.runner import run_scenario
from tests.hifi.harness.loader import load_scenario
from tests.hifi.harness.compare import assert_packets_equivalent

SCENARIOS = Path(__file__).parent / "scenarios"


# @spec GS-001
@pytest.mark.asyncio
async def test_feature_to_files():
    expected, actual = await run_scenario(load_scenario(SCENARIOS / "feature_to_files.yaml"))
    assert_packets_equivalent(expected, actual)
    assert "agent/src/shtp.c" in actual.relevant_files


# @spec GS-002
@pytest.mark.asyncio
async def test_error_to_known_issue():
    expected, actual = await run_scenario(load_scenario(SCENARIOS / "error_to_known_issue.yaml"))
    assert_packets_equivalent(expected, actual)
    ki_ids = [k.id for k in actual.known_issues]
    assert "KI-001" in ki_ids


# @spec GS-003
@pytest.mark.asyncio
async def test_known_issue_to_fix():
    expected, actual = await run_scenario(load_scenario(SCENARIOS / "known_issue_to_fix.yaml"))
    assert_packets_equivalent(expected, actual)
    fix_ids = [f.id for f in actual.prior_fixes]
    assert "FIX-001" in fix_ids


# @spec GS-004
@pytest.mark.asyncio
async def test_idempotent_reingest():
    scenario = load_scenario(SCENARIOS / "idempotent_reingest.yaml")
    _, actual1 = await run_scenario(scenario)
    _, actual2 = await run_scenario(scenario)
    # The two actual packets must agree with each other
    assert_packets_equivalent(actual1, actual2)
    assert_packets_equivalent(actual2, actual1)


# @spec GS-005
@pytest.mark.asyncio
async def test_cross_project_isolation():
    expected, actual = await run_scenario(load_scenario(SCENARIOS / "cross_project_isolation.yaml"))
    assert_packets_equivalent(expected, actual)
    # Project-B nodes must be absent
    ki_ids = [k.id for k in actual.known_issues]
    assert "KI-PROJECT-B" not in ki_ids
    assert "project_b/src/file.c" not in actual.relevant_files
