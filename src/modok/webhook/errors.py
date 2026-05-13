# @spec WH-PUSH-001, WH-PUSH-004, WH-GH-001, WH-TKT-001

from __future__ import annotations


class WebhookAuthError(Exception):
    pass


class WebhookNotFoundError(Exception):
    pass
