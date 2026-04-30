from __future__ import annotations


class LLMGatewayError(Exception):
    pass


class LLMConfigError(LLMGatewayError):
    pass


class LLMUnavailableError(LLMGatewayError):
    pass


class LLMResponseError(LLMGatewayError):
    pass
