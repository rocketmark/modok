"""
Tests for modok.text_utils — shared tokenization used by mechanical feature
anchor linking (SQ-ANCH-008) and the DRE's read-time pre-match
(_pre_match_modules). All tests are written before implementation
(Phase 5). Every test cites the EARS spec it verifies via @spec annotation.

Specs verified: DRE-TOKEN-001, DRE-TOKEN-004.
"""

from __future__ import annotations

from modok.text_utils import extract_text_tokens, tokenize


# @spec DRE-TOKEN-001
def test_tokenize_splits_kebab_case():
    assert tokenize("wifi-provisioning") == {"wifi", "provisioning"}


# @spec DRE-TOKEN-001
def test_tokenize_splits_camel_case():
    assert tokenize("trackerLostLogged") == {"tracker", "lost", "logged"}


# @spec DRE-TOKEN-001
def test_tokenize_excludes_short_tokens():
    assert "to" not in tokenize("client-to-server")


# @spec DRE-TOKEN-004
def test_tokenize_excludes_stopwords():
    assert "and" not in tokenize("client-signal-and-output")
    assert "and" not in tokenize("recording-and-export")
    assert tokenize("client-signal-and-output") == {"client", "signal", "output"}
    assert tokenize("recording-and-export") == {"recording", "export"}


# @spec DRE-TOKEN-004
def test_extract_text_tokens_excludes_stopwords():
    text = "It asks for wifi config, and I add info, but it's not ever connecting to wifi."
    tokens = extract_text_tokens(text)
    assert "and" not in tokens
    assert "for" not in tokens
    assert "but" not in tokens
    assert "not" not in tokens
    assert "wifi" in tokens
    assert "config" in tokens
    assert "connecting" in tokens


# @spec DRE-TOKEN-004
def test_wifi_ticket_does_not_token_match_unrelated_hyphenated_features():
    """Regression test for the exact false-match found live: a ticket about
    wifi connectivity, with no mention of "signal", "output", "recording", or
    "export", must not token-match client-signal-and-output or
    recording-and-export — both slugs only shared the stopword "and" with
    the ticket text before this fix."""
    text = "It asks for wifi config, and I add info, but it's not ever connecting to wifi. I don't know what state it's in."
    ticket_tokens = extract_text_tokens(text)

    assert not (ticket_tokens & tokenize("client-signal-and-output"))
    assert not (ticket_tokens & tokenize("recording-and-export"))
    assert ticket_tokens & tokenize("wifi-provisioning")
