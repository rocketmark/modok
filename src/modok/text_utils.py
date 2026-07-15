"""Shared text tokenization — used by the Diagnostic Retrieval Engine's
read-time pre-match (`retrieval/engine.py`) and mechanical feature anchor
linking at ingestion time (`ingestion/anchor_linking.py`), so both stay in
sync on what counts as a token match."""
# @spec SQ-ANCH-008

from __future__ import annotations

import re

_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_WORD_RE = re.compile(r"\b[a-zA-Z_][a-zA-Z0-9_]{2,}\b")

# @spec DRE-TOKEN-004 — common English stopwords excluded from every token set.
# Found live: a multi-word feature slug (e.g. "client-signal-and-output",
# "recording-and-export") can literally contain a stopword as one of its
# hyphen-joined components. Without this filter, "and" alone — present in
# nearly any sentence — made both slugs token-match almost every ticket
# regardless of actual relevance.
_STOPWORDS = {
    "and", "or", "the", "a", "an", "of", "to", "in", "on", "for", "with",
    "but", "not", "is", "are", "was", "were", "be", "been", "being",
    "it", "its", "this", "that", "these", "those", "at", "by", "as",
    "from", "into", "onto", "than", "then", "so", "if", "no", "yes",
}


# @spec DRE-TOKEN-001, DRE-TOKEN-004, SQ-ANCH-008
def tokenize(name: str) -> set[str]:
    """Split a camelCase/snake_case/kebab-case identifier into lowercase
    tokens (length > 2), excluding common English stopwords."""
    parts = re.split(r"[_\-\s]+", name)
    tokens: set[str] = set()
    for part in parts:
        for sub in _CAMEL_SPLIT_RE.split(part):
            lowered = sub.lower()
            if len(sub) > 2 and lowered not in _STOPWORDS:
                tokens.add(lowered)
    return tokens


def extract_text_tokens(text: str) -> set[str]:
    """Extract tokenized words from free-form text (e.g. ticket raw_text)."""
    tokens: set[str] = set()
    for word in _WORD_RE.finditer(text):
        tokens.update(tokenize(word.group(0)))
    return tokens
