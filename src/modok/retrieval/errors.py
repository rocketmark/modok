from __future__ import annotations


class DREError(Exception):
    pass


class DRENotFoundError(DREError):
    pass  # CustomerIssue not found, or project_slug mismatch


class DREAnchorError(DREError):
    pass  # no graph anchors and LLM fallback failed or returned none


class DREGraphUnavailableError(DREError):
    pass  # Quine unreachable


class DRELLMUnavailableError(DREError):
    pass  # LLM gateway unreachable (fallback path only)
