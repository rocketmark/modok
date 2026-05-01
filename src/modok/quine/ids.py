"""Test-harness-only ID helper.

This module is used exclusively by the HiFi test harness (DummyQuine,
ReferenceModok, scenario loader). It is NOT used by production MODOK code.

Production MODOK addresses Quine nodes using Quine's built-in idFrom() Cypher
function, embedded directly in Cypher query strings. Python never computes
real Quine node addresses.
"""
import hashlib


def idFrom(*parts: str) -> int:
    # Null-byte separator prevents ('a', 'bc') colliding with ('ab', 'c').
    # Node type name must always be the first element (enforced by callers).
    digest = hashlib.sha256("\x00".join(parts).encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)
