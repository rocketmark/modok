"""
Verifier property tests — Layer 2.
Pure-function invariants on verify_proposal; no I/O, no graph.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from hypothesis import given, settings
from hypothesis import strategies as st

from modok.ingestion.verifier import verify_proposal
from modok.llm.models import MetadataProposal


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_slugs = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-",
    min_size=1, max_size=20,
)
_values = st.one_of(
    st.text(min_size=1, max_size=30),
    st.lists(st.text(min_size=1, max_size=15), min_size=1, max_size=5),
)
_evidence = st.text(min_size=0, max_size=100)


@st.composite
def _proposal(draw, fields):
    """Build a MetadataProposal with arbitrary field values."""
    proposed = {f: draw(_values) for f in fields}
    evidence = draw(_evidence)
    return MetadataProposal(
        proposed_fields=proposed,
        confidence=draw(st.floats(0.0, 1.0, allow_nan=False)),
        evidence=evidence,
        raw_response="{}",
    )


def _permissive_registry():
    """Registry that accepts every slug (isolates verifier logic from registry)."""
    reg = MagicMock()
    reg.has_feature.return_value = True
    reg.has_module.return_value = True
    reg.has_error.return_value = True
    reg.feature_slugs.return_value = []
    return reg


# ---------------------------------------------------------------------------
# Property Tests
# ---------------------------------------------------------------------------

# @spec LLM-VER-001, LLM-VER-002, LLM-VER-003, LLM-VER-004, LLM-VER-005,
#       LLM-VER-006, LLM-VER-007, LLM-VER-008
@given(
    fields=st.lists(_slugs, min_size=1, max_size=8, unique=True).flatmap(
        lambda fs: st.tuples(st.just(fs), _proposal(fs))
    ),
)
@settings(max_examples=200, deadline=None)
def test_partition_invariant(fields):
    """Every key proposed inside missing_fields lands in exactly one bucket."""
    missing_fields, proposal = fields
    registry = _permissive_registry()

    result = verify_proposal(proposal, missing_fields, {}, registry)

    proposed_in_scope = set(proposal.proposed_fields.keys()) & set(missing_fields)
    valid_keys = set(result.valid_fields.keys())
    rejected_keys = {r.field for r in result.rejected_fields}

    # No key appears in both buckets
    assert valid_keys.isdisjoint(rejected_keys), (
        f"key(s) in both buckets: {valid_keys & rejected_keys}"
    )
    # Every in-scope proposed key is accounted for
    assert proposed_in_scope == valid_keys | rejected_keys, (
        f"keys missing from both buckets: {proposed_in_scope - valid_keys - rejected_keys}"
    )


# @spec LLM-VER-010, LLM-VER-011
@given(
    fields=st.lists(_slugs, min_size=1, max_size=6, unique=True).flatmap(
        lambda fs: st.tuples(st.just(fs), _proposal(fs))
    ),
)
@settings(max_examples=200, deadline=None)
def test_is_valid_iff_no_rejected_fields(fields):
    """is_valid is True if and only if rejected_fields is empty."""
    missing_fields, proposal = fields
    registry = _permissive_registry()

    result = verify_proposal(proposal, missing_fields, {}, registry)

    assert result.is_valid == (len(result.rejected_fields) == 0), (
        f"is_valid={result.is_valid} but rejected_fields={result.rejected_fields}"
    )


# @spec LLM-VER-008
@given(
    fields=st.lists(_slugs, min_size=1, max_size=6, unique=True),
    evidence=st.one_of(
        st.text(max_size=14),                                   # too short
        st.just("the document is about the pipeline failure"),  # filler prefix
        st.just("this document describes something"),
        st.just("based on the document content here"),
        st.just("the file mentions some details"),
    ),
)
@settings(max_examples=150, deadline=None)
def test_evidence_failure_rejects_all_fields(fields, evidence):
    """When evidence fails the hard or soft check, valid_fields is always empty."""
    proposed = {f: "some-value" for f in fields}
    proposal = MetadataProposal(
        proposed_fields=proposed,
        confidence=0.9,
        evidence=evidence,
        raw_response="{}",
    )
    registry = _permissive_registry()

    result = verify_proposal(proposal, fields, {}, registry)

    assert result.valid_fields == {}, (
        f"expected empty valid_fields on bad evidence {evidence!r}, got {result.valid_fields}"
    )
    assert not result.is_valid
