import hashlib


def idFrom(*parts: str) -> int:
    # @spec QC-ID-001, QC-ID-002, QC-ID-003
    # Null-byte separator prevents ('a', 'bc') colliding with ('ab', 'c').
    # Node type name must always be the first element (enforced by callers).
    digest = hashlib.sha256("\x00".join(parts).encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)
