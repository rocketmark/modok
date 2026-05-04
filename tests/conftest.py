from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch


@pytest.fixture(autouse=True)
def _mock_llm_gateway(request):
    """Prevent any test from hitting real LLM endpoints.

    Tests that explicitly patch 'modok.retrieval.engine.gateway' via their own
    `with patch(...)` block override this fixture's mock for the duration of that
    block, so behaviour is unchanged for tests that already manage gateway mocks.
    """
    with patch("modok.retrieval.engine.gateway") as mock_gw:
        mock_gw.summarise_packet = AsyncMock(return_value="")
        mock_gw.parse_ticket = AsyncMock(return_value=None)
        yield mock_gw
