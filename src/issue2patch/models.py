"""Real provider client implementations for Issue2Patch."""

from __future__ import annotations

import copy
import json
import os
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Protocol

from issue2patch.orchestrator import (
    AgentAction,
    AgentContext,
    FinishAction,
    InvalidModelActionError,
    ModelUsage,
    PatchAction,
    ReadFileAction,
    RunTestsAction,
    SearchAction,
)
from issue2patch.patching import PatchOperation

DEFAULT_OPENAI_MODEL = "gpt-5.6-luna"
DEFAULT_OPENAI_TIMEOUT = 30.0
DEFAULT_OPENAI_MAX_RETRIES = 2
DEFAULT_OPENAI_MAX_OUTPUT_TOKENS = 2_000
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
DEFAULT_DEEPSEEK_TIMEOUT = 30.0
DEFAULT_DEEPSEEK_MAX_RETRIES = 2
DEFAULT_DEEPSEEK_MAX_OUTPUT_TOKENS = 2_000
DEEPSEEK_RESPONSES_URL = "https://api.deepseek.com/responses"

# USD per one million tokens: input, cached input, output.
_OPENAI_MODEL_PRICING = {
    "gpt-5.6-luna": (0.20, 0.02, 1.20),
    "gpt-5.6-terra": (2.00, 0.20, 12.00),
    "gpt-5.6-sol": (4.00, 0.40, 20.00),
    "gpt-5.6": (4.00, 0.40, 20.00),
}

# DeepSeek prices verified from the official pricing page on 2026-09-06.
# Each value contains off-peak and peak (input, cached input, output) rates.
_DEEPSEEK_MODEL_PRICING = {
    "deepseek-v4-flash": ((0.22, 0.007, 0.66), (0.44, 0.014, 1.32)),
    "deepseek-v4-pro": ((0.66, 0.022, 1.98), (1.32, 0.044, 3.96)),
    "deepseek-v4-flash-vision-exp": (
        (0.22, 0.007, 0.66),
        (0.44, 0.014, 1.32),
    ),
}


class ModelClientError(RuntimeError):
    """Base class for safe, user-facing model client failures."""


class MissingAPIKeyError(ModelClientError):
    """Raised when the selected provider's API key is absent."""


class InvalidModelOutputError(InvalidModelActionError, ModelClientError):
    """Raised before an invalid model-produced action can be executed."""


class ModelTransportError(ModelClientError):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class ResponsesTransport(Protocol):
    def send(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout: float,
    ) -> Mapping[str, Any]:
        ...


class UrllibResponsesTransport:
    """Small stdlib HTTPS transport, injectable for deterministic tests."""

    def send(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout: float,
    ) -> Mapping[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=dict(headers),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read(1_000).decode("utf-8", errors="replace")
            retryable = error.code in {408, 409, 429} or error.code >= 500
            raise ModelTransportError(
                f"model API HTTP {error.code}: {detail}", retryable=retryable
            ) from error
        except (urllib.error.URLError, TimeoutError, socket.timeout) as error:
            raise ModelTransportError(
                f"model API connection failed: {error}", retryable=True
            ) from error
        try:
            decoded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ModelTransportError(
                "model API returned invalid JSON", retryable=False
            ) from error
        if not isinstance(decoded, dict):
            raise ModelTransportError(
                "model API returned a non-object response", retryable=False
            )
        return decoded


class _ResponsesModelClient:
    """Shared fail-closed client for OpenAI-compatible Responses APIs."""

    def __init__(
        self,
        *,
        provider_name: str,
        api_key_env: str,
        response_url: str,
        model: str,
        timeout: float,
        max_retries: int,
        max_output_tokens: int,
        strict_schema: bool,
        pricing_resolver: Callable[
            [str, datetime], tuple[float, float, float] | None
        ],
        transport: ResponsesTransport | None = None,
        sleeper: Any = time.sleep,
        utc_now: Callable[[], datetime] | None = None,
    ) -> None:
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise MissingAPIKeyError(f"{api_key_env} is not set")
        if not model.strip():
            raise ValueError("model must not be empty")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be greater than zero")
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_output_tokens = max_output_tokens
        self._transport = transport or UrllibResponsesTransport()
        self._sleeper = sleeper
        self._api_key = api_key
        self._provider_name = provider_name
        self._response_url = response_url
        self._strict_schema = strict_schema
        self._pricing_resolver = pricing_resolver
        self._utc_now = utc_now or (lambda: datetime.now(timezone.utc))
        self._usage = ModelUsage(
            estimated_cost_usd=(
                0.0 if pricing_resolver(model, self._utc_now()) is not None else None
            )
        )

    @property
    def usage(self) -> ModelUsage:
        return self._usage

    def next_action(self, context: AgentContext) -> AgentAction:
        text_format: dict[str, object] = {
            "type": "json_schema",
            "name": "issue2patch_action",
            "schema": _action_schema(context.allow_regex_search),
        }
        if self._strict_schema:
            text_format["strict"] = True
        payload = {
            "model": self.model,
            "instructions": _AGENT_INSTRUCTIONS,
            "input": self._context_input(context),
            "text": {"format": text_format},
            "reasoning": {"effort": "low"},
            "max_output_tokens": self.max_output_tokens,
            "store": False,
        }
        started = time.monotonic()
        retries = 0
        for attempt in range(self.max_retries + 1):
            try:
                response = self._transport.send(
                    url=self._response_url,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    payload=payload,
                    timeout=self.timeout,
                )
                break
            except ModelTransportError as error:
                if not error.retryable or attempt >= self.max_retries:
                    self._record_duration(time.monotonic() - started, retries)
                    raise
                retries += 1
                self._sleeper(0.25 * (2**attempt))
        else:  # pragma: no cover - loop always returns or raises
            raise AssertionError("unreachable retry state")

        duration = time.monotonic() - started
        self._record_response_usage(response, duration, retries)
        if response.get("status", "completed") != "completed":
            raise ModelClientError(
                f"{self._provider_name} response did not complete: "
                f"{response.get('status')}"
            )
        output_text = self._extract_output_text(response)
        try:
            decoded = json.loads(output_text)
        except json.JSONDecodeError as error:
            raise InvalidModelOutputError("model output is not valid JSON") from error
        if not isinstance(decoded, dict) or set(decoded) != {"action"}:
            raise InvalidModelOutputError(
                "model output must contain exactly one action field"
            )
        return action_from_mapping(decoded["action"])

    @staticmethod
    def _context_input(context: AgentContext) -> str:
        def encode(value: object) -> object:
            if isinstance(value, Enum):
                return value.value
            raise TypeError(f"unsupported context value: {type(value).__name__}")

        return json.dumps(asdict(context), ensure_ascii=False, default=encode)

    @staticmethod
    def _extract_output_text(response: Mapping[str, Any]) -> str:
        direct = response.get("output_text")
        if isinstance(direct, str):
            return direct
        output = response.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict) or item.get("type") != "message":
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "output_text":
                        text = part.get("text")
                        if isinstance(text, str):
                            return text
                    if isinstance(part, dict) and part.get("type") == "refusal":
                        raise InvalidModelOutputError("model refused to return an action")
        raise InvalidModelOutputError("model response contains no output text")

    def _record_duration(self, duration: float, retries: int) -> None:
        self._usage = ModelUsage(
            request_count=self._usage.request_count,
            retry_count=self._usage.retry_count + retries,
            input_tokens=self._usage.input_tokens,
            cached_input_tokens=self._usage.cached_input_tokens,
            output_tokens=self._usage.output_tokens,
            total_tokens=self._usage.total_tokens,
            duration_seconds=self._usage.duration_seconds + duration,
            estimated_cost_usd=self._usage.estimated_cost_usd,
        )

    def _record_response_usage(
        self, response: Mapping[str, Any], duration: float, retries: int
    ) -> None:
        raw_usage = response.get("usage")
        usage = raw_usage if isinstance(raw_usage, dict) else {}
        input_tokens = _nonnegative_int(usage.get("input_tokens"))
        output_tokens = _nonnegative_int(usage.get("output_tokens"))
        total_tokens = _nonnegative_int(
            usage.get("total_tokens"), default=input_tokens + output_tokens
        )
        details = usage.get("input_tokens_details")
        cached_tokens = _nonnegative_int(
            details.get("cached_tokens") if isinstance(details, dict) else None
        )
        aggregate_input = self._usage.input_tokens + input_tokens
        aggregate_cached = self._usage.cached_input_tokens + cached_tokens
        aggregate_output = self._usage.output_tokens + output_tokens
        request_cost = _estimate_cost(
            self._pricing_resolver(self.model, self._utc_now()),
            input_tokens,
            cached_tokens,
            output_tokens,
        )
        cost = (
            self._usage.estimated_cost_usd + request_cost
            if self._usage.estimated_cost_usd is not None and request_cost is not None
            else None
        )
        self._usage = ModelUsage(
            request_count=self._usage.request_count + 1,
            retry_count=self._usage.retry_count + retries,
            input_tokens=aggregate_input,
            cached_input_tokens=aggregate_cached,
            output_tokens=aggregate_output,
            total_tokens=self._usage.total_tokens + total_tokens,
            duration_seconds=self._usage.duration_seconds + duration,
            estimated_cost_usd=cost,
        )


class OpenAIResponsesModelClient(_ResponsesModelClient):
    """Map strict OpenAI Responses API output into the existing action union."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_OPENAI_MODEL,
        timeout: float = DEFAULT_OPENAI_TIMEOUT,
        max_retries: int = DEFAULT_OPENAI_MAX_RETRIES,
        max_output_tokens: int = DEFAULT_OPENAI_MAX_OUTPUT_TOKENS,
        transport: ResponsesTransport | None = None,
        sleeper: Any = time.sleep,
    ) -> None:
        super().__init__(
            provider_name="OpenAI",
            api_key_env="OPENAI_API_KEY",
            response_url=OPENAI_RESPONSES_URL,
            model=model,
            timeout=timeout,
            max_retries=max_retries,
            max_output_tokens=max_output_tokens,
            strict_schema=True,
            pricing_resolver=_openai_pricing,
            transport=transport,
            sleeper=sleeper,
        )


class DeepSeekResponsesModelClient(_ResponsesModelClient):
    """Map DeepSeek's Responses API JSON output into the safe action union."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_DEEPSEEK_MODEL,
        timeout: float = DEFAULT_DEEPSEEK_TIMEOUT,
        max_retries: int = DEFAULT_DEEPSEEK_MAX_RETRIES,
        max_output_tokens: int = DEFAULT_DEEPSEEK_MAX_OUTPUT_TOKENS,
        transport: ResponsesTransport | None = None,
        sleeper: Any = time.sleep,
        utc_now: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(
            provider_name="DeepSeek",
            api_key_env="DEEPSEEK_API_KEY",
            response_url=DEEPSEEK_RESPONSES_URL,
            model=model,
            timeout=timeout,
            max_retries=max_retries,
            max_output_tokens=max_output_tokens,
            strict_schema=False,
            pricing_resolver=_deepseek_pricing,
            transport=transport,
            sleeper=sleeper,
            utc_now=utc_now,
        )


def action_from_mapping(value: object) -> AgentAction:
    """Strictly validate and construct one permitted action."""
    if not isinstance(value, dict):
        raise InvalidModelOutputError("model action must be a JSON object")
    action_type = value.get("type")
    if action_type == "search":
        _require_keys(value, {"type", "pattern", "path", "regex"})
        return SearchAction(
            pattern=_string(value, "pattern"),
            path=_string(value, "path"),
            regex=_boolean(value, "regex"),
        )
    if action_type == "read_file":
        _require_keys(value, {"type", "path"})
        return ReadFileAction(path=_string(value, "path"))
    if action_type == "patch":
        _require_keys(value, {"type", "operations"})
        raw_operations = value["operations"]
        if not isinstance(raw_operations, list) or not raw_operations:
            raise InvalidModelOutputError("patch operations must be a non-empty array")
        if len(raw_operations) > 10:
            raise InvalidModelOutputError("patch operation count exceeds limit")
        operations: list[PatchOperation] = []
        for raw in raw_operations:
            if not isinstance(raw, dict):
                raise InvalidModelOutputError("patch operation must be an object")
            _require_keys(
                raw,
                {"path", "old_content", "new_content", "expected_sha256"},
            )
            operations.append(
                PatchOperation(
                    path=_string(raw, "path"),
                    old_content=_string(raw, "old_content"),
                    new_content=_string(raw, "new_content", allow_empty=True),
                    expected_sha256=_string(raw, "expected_sha256"),
                )
            )
        return PatchAction(tuple(operations))
    if action_type == "run_tests":
        _require_keys(value, {"type", "path"})
        return RunTestsAction(path=_string(value, "path"))
    if action_type == "finish":
        _require_keys(value, {"type", "summary"})
        return FinishAction(summary=_string(value, "summary", allow_empty=True))
    raise InvalidModelOutputError(f"unsupported model action type: {action_type!r}")


def _require_keys(value: Mapping[str, object], expected: set[str]) -> None:
    if set(value) != expected:
        raise InvalidModelOutputError(
            f"model action fields do not match schema: expected {sorted(expected)}"
        )


def _string(
    value: Mapping[str, object], key: str, *, allow_empty: bool = False
) -> str:
    result = value.get(key)
    if not isinstance(result, str) or (not allow_empty and not result):
        raise InvalidModelOutputError(f"{key} must be a non-empty string")
    return result


def _boolean(value: Mapping[str, object], key: str) -> bool:
    result = value.get(key)
    if not isinstance(result, bool):
        raise InvalidModelOutputError(f"{key} must be a boolean")
    return result


def _nonnegative_int(value: object, *, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default


def _openai_pricing(
    model: str, _: datetime
) -> tuple[float, float, float] | None:
    return _OPENAI_MODEL_PRICING.get(model)


def _deepseek_pricing(
    model: str, when: datetime
) -> tuple[float, float, float] | None:
    rates = _DEEPSEEK_MODEL_PRICING.get(model)
    if rates is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    utc = when.astimezone(timezone.utc)
    is_weekday = utc.weekday() < 5
    is_peak_hour = 1 <= utc.hour < 4 or 6 <= utc.hour < 10
    off_peak, peak = rates
    return peak if is_weekday and is_peak_hour else off_peak


def _estimate_cost(
    pricing: tuple[float, float, float] | None,
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
) -> float | None:
    if pricing is None:
        return None
    input_rate, cached_rate, output_rate = pricing
    uncached_tokens = max(0, input_tokens - cached_tokens)
    return (
        uncached_tokens * input_rate
        + cached_tokens * cached_rate
        + output_tokens * output_rate
    ) / 1_000_000


_AGENT_INSTRUCTIONS = """You are Issue2Patch's action selector. Return exactly one JSON action matching the schema. You cannot run shell commands. Use fixed-string search unless context.allow_regex_search is true; when it is false, SearchAction.regex must be false. Inspect files with read_file. ReadFileAction observations include metadata.sha256; copy that exact value into PatchOperation.expected_sha256 and never calculate or guess a hash. Apply only exact hash-guarded patches, run tests after changes, and finish only after tests pass. Never request secrets or sensitive files. Do not include reasoning or prose outside the action."""

_OPERATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "path": {"type": "string", "minLength": 1},
        "old_content": {"type": "string", "minLength": 1},
        "new_content": {"type": "string"},
        "expected_sha256": {"type": "string", "pattern": "^[0-9a-fA-F]{64}$"},
    },
    "required": ["path", "old_content", "new_content", "expected_sha256"],
}

_ACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {
            "anyOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "type": {"type": "string", "enum": ["search"]},
                        "pattern": {"type": "string", "minLength": 1},
                        "path": {"type": "string", "minLength": 1},
                        "regex": {"type": "boolean"},
                    },
                    "required": ["type", "pattern", "path", "regex"],
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "type": {"type": "string", "enum": ["read_file"]},
                        "path": {"type": "string", "minLength": 1},
                    },
                    "required": ["type", "path"],
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "type": {"type": "string", "enum": ["patch"]},
                        "operations": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 10,
                            "items": _OPERATION_SCHEMA,
                        },
                    },
                    "required": ["type", "operations"],
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "type": {"type": "string", "enum": ["run_tests"]},
                        "path": {"type": "string", "minLength": 1},
                    },
                    "required": ["type", "path"],
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "type": {"type": "string", "enum": ["finish"]},
                        "summary": {"type": "string"},
                    },
                    "required": ["type", "summary"],
                },
            ],
        }
    },
    "required": ["action"],
}


def _action_schema(allow_regex_search: bool) -> dict[str, Any]:
    schema: dict[str, Any] = copy.deepcopy(_ACTION_SCHEMA)
    if not allow_regex_search:
        regex_schema = schema["properties"]["action"]["anyOf"][0]["properties"][
            "regex"
        ]
        regex_schema["enum"] = [False]
    return schema


__all__ = [
    "DEFAULT_DEEPSEEK_MODEL",
    "DEFAULT_OPENAI_MODEL",
    "DEEPSEEK_RESPONSES_URL",
    "DeepSeekResponsesModelClient",
    "InvalidModelOutputError",
    "MissingAPIKeyError",
    "ModelClientError",
    "ModelTransportError",
    "ModelUsage",
    "OpenAIResponsesModelClient",
    "ResponsesTransport",
    "UrllibResponsesTransport",
    "action_from_mapping",
]
