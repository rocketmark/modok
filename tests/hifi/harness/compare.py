"""
Comparison utilities for HiFi differential testing.
"""
from __future__ import annotations

from modok.retrieval.models import DebugPacket


# @spec HFI-CMP-001
def assert_packets_equivalent(expected: DebugPacket, actual: DebugPacket) -> None:
    """
    Assert that actual contains at least everything expected specifies.
    extra items in actual are allowed (superset semantics).
    """
    expected_ki = {k.id for k in expected.known_issues}
    actual_ki = {k.id for k in actual.known_issues}
    missing_ki = expected_ki - actual_ki
    assert not missing_ki, (
        f"known_issues missing from actual: {sorted(missing_ki)}\n"
        f"  expected: {sorted(expected_ki)}\n"
        f"  actual:   {sorted(actual_ki)}"
    )

    expected_fix = {f.id for f in expected.prior_fixes}
    actual_fix = {f.id for f in actual.prior_fixes}
    missing_fix = expected_fix - actual_fix
    assert not missing_fix, (
        f"prior_fixes missing from actual: {sorted(missing_fix)}\n"
        f"  expected: {sorted(expected_fix)}\n"
        f"  actual:   {sorted(actual_fix)}"
    )

    expected_files = set(expected.relevant_files)
    actual_files = set(actual.relevant_files)
    missing_files = expected_files - actual_files
    assert not missing_files, (
        f"relevant_files missing from actual: {sorted(missing_files)}\n"
        f"  expected: {sorted(expected_files)}\n"
        f"  actual:   {sorted(actual_files)}"
    )
