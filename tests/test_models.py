"""Mocked API tests for the real Responses API model client."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

import pytest

from issue2patch import AgentContext, PatchAction, SearchAction
from issue2patch.models import (
    DEEPSEEK_RESPONSES_URL,
    DeepSeekResponsesModelClient,
    InvalidModelOutputError,
    MissingAPIKeyError,
    ModelTransportError,
    OpenAIResponsesModelClient,
    action_from_mapping,
)


class MockTransport:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def send(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout: float,
    ) -> Mapping[str, Any]:
        self.calls.append(
            {"url": url, "headers": dict(headers), "payload": payload, "timeout": timeout}
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        assert isinstance(response, dict)
        return response


def _context() -> AgentContext:
    return AgentContext(
        run_id="mock-run",
        issue="divide should return quotient",
        step=1,
        remaining_steps=5,
        remaining_tool_calls=4,
    )


def _response(action: dict[str, object]) -> dict[str, object]:
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": json.dumps({"action": action})}
                ],
            }
        ],
        "usage": {
            "input_tokens": 1_000,
            "input_tokens_details": {"cached_tokens": 200},
            "output_tokens": 100,
            "total_tokens": 1_100,
        },
    }


def test_api_key_is_required_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(MissingAPIKeyError):
        OpenAIResponsesModelClient(transport=MockTransport([]))


def test_deepseek_requires_its_own_environment_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-reused")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(MissingAPIKeyError, match="DEEPSEEK_API_KEY"):
        DeepSeekResponsesModelClient(transport=MockTransport([]))


def test_mock_api_maps_strict_structured_output_and_sends_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only")
    transport = MockTransport(
        [_response({"type": "search", "pattern": "divide", "path": ".", "regex": False})]
    )
    client = OpenAIResponsesModelClient(transport=transport, timeout=7.5)

    action = client.next_action(_context())

    assert action == SearchAction("divide")
    request = transport.calls[0]
    assert request["timeout"] == 7.5
    payload = request["payload"]
    assert isinstance(payload, dict)
    assert payload["store"] is False
    assert payload["text"]["format"]["type"] == "json_schema"  # type: ignore[index]
    assert payload["text"]["format"]["strict"] is True  # type: ignore[index]
    schema = payload["text"]["format"]["schema"]  # type: ignore[index]
    assert schema["type"] == "object"
    assert "oneOf" not in schema
    assert "anyOf" in schema["properties"]["action"]


def test_deepseek_uses_its_endpoint_key_and_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-test-only")
    transport = MockTransport(
        [_response({"type": "read_file", "path": "calculator.py"})]
    )
    client = DeepSeekResponsesModelClient(transport=transport, timeout=8.0)

    action = client.next_action(_context())

    assert action.path == "calculator.py"
    request = transport.calls[0]
    assert request["url"] == DEEPSEEK_RESPONSES_URL
    assert request["timeout"] == 8.0
    assert request["headers"] == {
        "Authorization": "Bearer sk-deepseek-test-only",
        "Content-Type": "application/json",
    }
    payload = request["payload"]
    assert isinstance(payload, dict)
    assert payload["model"] == "deepseek-v4-flash"
    text_format = payload["text"]["format"]  # type: ignore[index]
    assert text_format["type"] == "json_schema"
    assert "strict" not in text_format
    assert text_format["schema"]["type"] == "object"


@pytest.mark.parametrize(
    ("hour", "expected_cost"),
    [
        (2, 0.0004868),
        (5, 0.0002434),
    ],
)
def test_deepseek_cost_uses_current_peak_or_off_peak_rates(
    monkeypatch: pytest.MonkeyPatch, hour: int, expected_cost: float
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-test-only")
    transport = MockTransport([_response({"type": "finish", "summary": "done"})])
    client = DeepSeekResponsesModelClient(
        transport=transport,
        utc_now=lambda: datetime(2026, 9, 7, hour, tzinfo=timezone.utc),
    )

    client.next_action(_context())

    assert client.usage.estimated_cost_usd == pytest.approx(expected_cost)


def test_deepseek_invalid_output_is_rejected_before_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-test-only")
    transport = MockTransport(
        [{"status": "completed", "output_text": '{"action":{"type":"shell"}}'}]
    )
    client = DeepSeekResponsesModelClient(transport=transport)

    with pytest.raises(InvalidModelOutputError, match="unsupported model action"):
        client.next_action(_context())


def test_usage_duration_and_luna_cost_are_accumulated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only")
    transport = MockTransport(
        [_response({"type": "finish", "summary": "done"})]
    )
    client = OpenAIResponsesModelClient(transport=transport)

    client.next_action(_context())

    assert client.usage.request_count == 1
    assert client.usage.input_tokens == 1_000
    assert client.usage.cached_input_tokens == 200
    assert client.usage.output_tokens == 100
    assert client.usage.total_tokens == 1_100
    assert client.usage.duration_seconds >= 0
    assert client.usage.estimated_cost_usd == pytest.approx(0.000284)


def test_retryable_api_error_has_finite_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only")
    waits: list[float] = []
    transport = MockTransport(
        [
            ModelTransportError("rate limited", retryable=True),
            _response({"type": "finish", "summary": "done"}),
        ]
    )
    client = OpenAIResponsesModelClient(
        transport=transport, max_retries=2, sleeper=waits.append
    )

    client.next_action(_context())

    assert len(transport.calls) == 2
    assert waits == [0.25]
    assert client.usage.retry_count == 1


def test_nonretryable_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only")
    transport = MockTransport(
        [ModelTransportError("bad request", retryable=False)]
    )
    client = OpenAIResponsesModelClient(transport=transport)

    with pytest.raises(ModelTransportError):
        client.next_action(_context())

    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "invalid",
    [
        {"type": "shell", "command": "rm -rf /"},
        {"type": "read_file", "path": "x.py", "extra": "ignored?"},
        {"type": "search", "pattern": "x", "path": ".", "regex": "false"},
    ],
)
def test_illegal_model_output_is_rejected_before_action_construction(
    invalid: dict[str, object],
) -> None:
    with pytest.raises(InvalidModelOutputError):
        action_from_mapping(invalid)


def test_patch_output_maps_to_existing_patch_action() -> None:
    action = action_from_mapping(
        {
            "type": "patch",
            "operations": [
                {
                    "path": "calculator.py",
                    "old_content": "return a * b",
                    "new_content": "return a / b",
                    "expected_sha256": "a" * 64,
                }
            ],
        }
    )

    assert isinstance(action, PatchAction)
    assert action.operations[0].path == "calculator.py"


def test_mock_api_invalid_json_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only")
    transport = MockTransport(
        [{"status": "completed", "output_text": "not json", "usage": {}}]
    )
    client = OpenAIResponsesModelClient(transport=transport)

    with pytest.raises(InvalidModelOutputError):
        client.next_action(_context())
