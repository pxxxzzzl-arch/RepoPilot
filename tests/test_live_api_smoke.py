"""Opt-in smoke test for the real OpenAI Responses API."""

from __future__ import annotations

import os

import pytest

from issue2patch import AgentContext
from issue2patch.models import OpenAIResponsesModelClient


@pytest.mark.skipif(
    os.environ.get("ISSUE2PATCH_RUN_LIVE_API") != "1",
    reason="set ISSUE2PATCH_RUN_LIVE_API=1 to run the paid real API smoke test",
)
def test_real_openai_api_returns_one_structured_action() -> None:
    client = OpenAIResponsesModelClient(timeout=30, max_retries=1)
    action = client.next_action(
        AgentContext(
            run_id="live-smoke",
            issue="Inspect the repository; choose a fixed-string search for divide.",
            step=1,
            remaining_steps=1,
            remaining_tool_calls=1,
        )
    )

    assert action is not None
    assert client.usage.request_count == 1
