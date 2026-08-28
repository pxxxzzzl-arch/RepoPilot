"""Deterministic tests for provider-independent agent orchestration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

import issue2patch.orchestrator as orchestrator_module
from issue2patch import (
    AgentConfig,
    AgentContext,
    AgentOrchestrator,
    FinishAction,
    InvalidModelActionError,
    PatchAction,
    PatchOperation,
    ReadFileAction,
    RunTestsAction,
    ScriptedModel,
    SearchAction,
    TerminationStatus,
    TestOutcome as Outcome,
    TraceRecorder,
)
from issue2patch.sandbox import SandboxResult


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@pytest.fixture
def broken_repository(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repository"
    (root / "tests").mkdir(parents=True)
    before = "def divide(a, b):\n    return a * b\n"
    (root / "calculator.py").write_text(before, encoding="utf-8")
    (root / "tests" / "test_calculator.py").write_text(
        "from calculator import divide\n\n"
        "def test_divide():\n    assert divide(6, 3) == 2\n",
        encoding="utf-8",
    )
    return root, before


class ContentAwareRunner:
    def __init__(self, forced_success: bool | None = None) -> None:
        self.forced_success = forced_success
        self.workspaces: list[Path] = []

    def run_tests(
        self,
        repo_root: str | Path,
        path: str | Path = "tests",
        *,
        timeout: float = 60.0,
    ) -> SandboxResult:
        del path, timeout
        workspace = Path(repo_root)
        self.workspaces.append(workspace)
        content = (workspace / "calculator.py").read_text(encoding="utf-8")
        success = (
            self.forced_success
            if self.forced_success is not None
            else "return a / b" in content
        )
        return SandboxResult(
            success=success,
            stdout="1 passed\n" if success else "1 failed\n",
            exit_code=0 if success else 1,
            cleanup_status="removed",
            container_name="deterministic-test-container",
        )


class RecordingModel:
    def __init__(self, actions: list[object]) -> None:
        self.scripted = ScriptedModel(actions)  # type: ignore[arg-type]
        self.contexts: list[AgentContext] = []

    def next_action(self, context: AgentContext):
        self.contexts.append(context)
        return self.scripted.next_action(context)


class NeverCalledModel:
    def __init__(self) -> None:
        self.called = False

    def next_action(self, context: AgentContext):
        del context
        self.called = True
        raise AssertionError("model must not be called")


class StaticRunner:
    def __init__(self, result: SandboxResult) -> None:
        self.result = result
        self.calls = 0

    def run_tests(
        self,
        repo_root: str | Path,
        path: str | Path = "tests",
        *,
        timeout: float = 60.0,
    ) -> SandboxResult:
        del repo_root, path, timeout
        self.calls += 1
        return self.result


def _fix_operation(before: str, expected_hash: str | None = None) -> PatchOperation:
    return PatchOperation(
        path="calculator.py",
        old_content="return a * b",
        new_content="return a / b",
        expected_sha256=expected_hash or _sha256(before),
    )


def test_scripted_model_repairs_copy_and_returns_diff_without_changing_original(
    broken_repository: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, before = broken_repository
    runner = ContentAwareRunner()
    trace_path = tmp_path / "agent-trace.jsonl"
    monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-be-recorded")
    model = RecordingModel(
        [
            SearchAction("return a"),
            ReadFileAction("calculator.py"),
            PatchAction((_fix_operation(before),)),
            RunTestsAction(),
            FinishAction("repair completed"),
        ]
    )

    result = AgentOrchestrator(model, test_runner=runner).run(
        repository,
        issue="divide should return a quotient",
        trace_recorder=TraceRecorder(trace_path, run_id="scripted-success"),
    )

    assert result.status == TerminationStatus.SUCCESS
    assert result.success is True
    assert result.last_test_passed is True
    assert result.steps == 5
    assert result.tool_calls == 5
    assert result.baseline_test is not None
    assert result.baseline_test.outcome == Outcome.ASSERTION_FAILED
    assert model.contexts[0].baseline_test == result.baseline_test
    assert model.contexts[0].observations[0].action_type == "BaselineTests"
    assert len(runner.workspaces) == 2
    assert "-    return a * b" in result.diff
    assert "+    return a / b" in result.diff
    assert (repository / "calculator.py").read_text(encoding="utf-8") == before
    assert runner.workspaces[0] != repository
    assert not runner.workspaces[0].exists()

    serialized = trace_path.read_text(encoding="utf-8")
    entries = [json.loads(line) for line in serialized.splitlines()]
    assert [entry["event"] for entry in entries] == [
        "agent_action",
        "agent_action",
        "agent_action",
        "agent_action",
        "agent_action",
        "agent_action",
        "agent_termination",
    ]
    assert entries[0]["action_type"] == "BaselineTests"
    assert entries[2]["action_type"] == "ReadFileAction"
    assert entries[2]["metadata"] == {
        "path": "calculator.py",
        "sha256": _sha256(before),
        "bytes": len(before.encode()),
    }
    assert entries[3]["action_type"] == "PatchAction"
    assert entries[3]["files"][0]["path"] == "calculator.py"
    assert entries[-1]["termination_reason"] == "success"
    assert "return a * b" not in serialized
    assert "return a / b" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "sk-must-not-be-recorded" not in serialized


def test_finish_after_failing_tests_has_structured_status(
    broken_repository: tuple[Path, str]
) -> None:
    repository, before = broken_repository
    model = ScriptedModel([RunTestsAction(), FinishAction("still failing")])

    result = AgentOrchestrator(
        model, test_runner=ContentAwareRunner(forced_success=False)
    ).run(repository)

    assert result.status == TerminationStatus.TESTS_FAILED
    assert result.success is False
    assert result.last_test_passed is False
    assert (repository / "calculator.py").read_text(encoding="utf-8") == before


@dataclass(frozen=True)
class ShellAction:
    command: str


def test_shell_or_other_illegal_action_is_rejected(
    broken_repository: tuple[Path, str]
) -> None:
    repository, _ = broken_repository
    model = ScriptedModel([ShellAction("rm -rf /")])  # type: ignore[list-item]

    result = AgentOrchestrator(model, test_runner=ContentAwareRunner()).run(repository)

    assert result.status == TerminationStatus.INVALID_ACTION
    assert result.tool_calls == 1
    assert "ShellAction" in result.error


class ExplodingModel:
    def next_action(self, context: AgentContext):
        del context
        raise RuntimeError("API key=sk-super-secret-value")


class InvalidOutputModel:
    def next_action(self, context: AgentContext):
        del context
        raise InvalidModelActionError("shell actions are not permitted")


def test_invalid_model_output_is_rejected_as_invalid_action(
    broken_repository: tuple[Path, str]
) -> None:
    repository, _ = broken_repository

    result = AgentOrchestrator(
        InvalidOutputModel(), test_runner=ContentAwareRunner()
    ).run(repository)

    assert result.status == TerminationStatus.INVALID_ACTION
    assert result.tool_calls == 1
    assert "invalid model output" in result.error


def test_model_exception_is_structured_and_audit_redacts_secret(
    broken_repository: tuple[Path, str], tmp_path: Path
) -> None:
    repository, _ = broken_repository
    trace_path = tmp_path / "model-error.jsonl"

    result = AgentOrchestrator(
        ExplodingModel(), test_runner=ContentAwareRunner()
    ).run(
        repository,
        trace_recorder=TraceRecorder(trace_path, run_id="model-error"),
    )

    assert result.status == TerminationStatus.MODEL_ERROR
    assert "model error" in result.error
    serialized = trace_path.read_text(encoding="utf-8")
    assert "sk-super-secret-value" not in serialized
    assert "[REDACTED]" in serialized


def test_tool_exception_is_structured(
    broken_repository: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, _ = broken_repository

    def explode(*_: object, **__: object):
        raise RuntimeError("search backend crashed")

    monkeypatch.setattr(orchestrator_module, "search_code", explode)

    result = AgentOrchestrator(
        ScriptedModel([SearchAction("divide")]),
        test_runner=ContentAwareRunner(),
    ).run(repository)

    assert result.status == TerminationStatus.TOOL_ERROR
    assert result.tool_calls == 2
    assert "search backend crashed" in result.error


def test_step_limit_is_structured(broken_repository: tuple[Path, str]) -> None:
    repository, _ = broken_repository
    model = ScriptedModel(
        [SearchAction("divide"), SearchAction("return"), FinishAction()]
    )

    result = AgentOrchestrator(
        model,
        config=AgentConfig(max_steps=2),
        test_runner=ContentAwareRunner(),
    ).run(repository)

    assert result.status == TerminationStatus.STEP_LIMIT
    assert result.steps == 2
    assert result.tool_calls == 3


def test_tool_call_limit_is_structured(broken_repository: tuple[Path, str]) -> None:
    repository, _ = broken_repository
    model = ScriptedModel([SearchAction("divide"), ReadFileAction("calculator.py")])

    result = AgentOrchestrator(
        model,
        config=AgentConfig(max_tool_calls=1),
        test_runner=ContentAwareRunner(),
    ).run(repository)

    assert result.status == TerminationStatus.TOOL_CALL_LIMIT
    assert result.tool_calls == 1


def test_repeated_action_without_progress_is_detected(
    broken_repository: tuple[Path, str]
) -> None:
    repository, _ = broken_repository
    action = SearchAction("divide")

    result = AgentOrchestrator(
        ScriptedModel([action, action]),
        config=AgentConfig(max_repeated_actions=2),
        test_runner=ContentAwareRunner(),
    ).run(repository)

    assert result.status == TerminationStatus.NO_PROGRESS
    assert result.tool_calls == 2
    assert "repeated action" in result.error


def test_patch_hash_conflict_is_structured_and_original_is_unchanged(
    broken_repository: tuple[Path, str]
) -> None:
    repository, before = broken_repository
    conflict = _fix_operation(before, expected_hash="0" * 64)

    result = AgentOrchestrator(
        ScriptedModel([PatchAction((conflict,))]),
        test_runner=ContentAwareRunner(),
    ).run(repository)

    assert result.status == TerminationStatus.PATCH_CONFLICT
    assert "expected_sha256 conflict" in result.error
    assert (repository / "calculator.py").read_text(encoding="utf-8") == before


class CapturingModel:
    def __init__(self) -> None:
        self.contexts: list[AgentContext] = []

    def next_action(self, context: AgentContext):
        self.contexts.append(context)
        if len(self.contexts) == 1:
            return ReadFileAction("calculator.py")
        return FinishAction()


class MetadataDrivenRepairModel:
    """Build the patch hash only from the preceding read observation."""

    def __init__(self) -> None:
        self.calls = 0
        self.observed_sha256 = ""

    def next_action(self, context: AgentContext):
        self.calls += 1
        if self.calls == 1:
            return ReadFileAction("calculator.py")
        if self.calls == 2:
            read_observation = context.observations[-1]
            self.observed_sha256 = str(read_observation.metadata["sha256"])
            return PatchAction(
                (
                    PatchOperation(
                        path="calculator.py",
                        old_content="return a * b",
                        new_content="return a / b",
                        expected_sha256=self.observed_sha256,
                    ),
                )
            )
        if self.calls == 3:
            return RunTestsAction()
        return FinishAction("metadata-driven repair")


def test_model_can_patch_using_sha256_from_read_metadata(
    broken_repository: tuple[Path, str]
) -> None:
    repository, before = broken_repository
    model = MetadataDrivenRepairModel()

    result = AgentOrchestrator(model, test_runner=ContentAwareRunner()).run(repository)

    assert result.status == TerminationStatus.SUCCESS
    assert model.observed_sha256 == _sha256(before)
    assert "+    return a / b" in result.diff
    assert (repository / "calculator.py").read_text(encoding="utf-8") == before


def test_tool_output_is_truncated_before_returning_to_model(
    broken_repository: tuple[Path, str]
) -> None:
    repository, _ = broken_repository
    model = CapturingModel()

    result = AgentOrchestrator(
        model,
        config=AgentConfig(max_tool_output_chars=16),
        test_runner=ContentAwareRunner(),
    ).run(repository)

    assert result.status == TerminationStatus.TESTS_FAILED
    assert len(model.contexts[1].observations[-1].output) <= 16
    assert model.contexts[1].observations[-1].output.endswith("...[truncated]")
    assert model.contexts[1].observations[-1].metadata == {
        "path": "calculator.py",
        "sha256": _sha256("def divide(a, b):\n    return a * b\n"),
        "bytes": len("def divide(a, b):\n    return a * b\n".encode()),
    }


def test_initially_passing_tests_finish_without_model_call(
    broken_repository: tuple[Path, str]
) -> None:
    repository, _ = broken_repository
    model = NeverCalledModel()

    result = AgentOrchestrator(
        model, test_runner=ContentAwareRunner(forced_success=True)
    ).run(repository)

    assert result.status == TerminationStatus.SUCCESS
    assert result.steps == 0
    assert result.tool_calls == 1
    assert result.baseline_test is not None
    assert result.baseline_test.outcome == Outcome.PASSED
    assert model.called is False


def test_baseline_error_summary_is_bounded_before_model_context(
    broken_repository: tuple[Path, str]
) -> None:
    repository, _ = broken_repository
    model = RecordingModel([FinishAction()])
    runner = StaticRunner(
        SandboxResult(
            success=False,
            stderr="x" * 1_000,
            exit_code=1,
            cleanup_status="removed",
        )
    )

    AgentOrchestrator(
        model,
        config=AgentConfig(max_tool_output_chars=32),
        test_runner=runner,
    ).run(repository)

    summary = model.contexts[0].baseline_test
    assert summary is not None
    assert len(summary.error_summary) <= 32
    assert summary.error_summary.endswith("...[truncated]")


def test_docker_unavailable_is_infrastructure_error_not_tests_failed(
    broken_repository: tuple[Path, str]
) -> None:
    repository, _ = broken_repository
    model = NeverCalledModel()
    runner = StaticRunner(
        SandboxResult(
            success=False,
            stderr="Docker is unavailable",
            exit_code=None,
            cleanup_status="not_created",
        )
    )

    result = AgentOrchestrator(model, test_runner=runner).run(repository)

    assert result.status == TerminationStatus.TEST_INFRASTRUCTURE_ERROR
    assert result.status != TerminationStatus.TESTS_FAILED
    assert result.baseline_test is not None
    assert result.baseline_test.outcome == Outcome.INFRASTRUCTURE_ERROR
    assert model.called is False


def test_baseline_timeout_has_distinct_termination(
    broken_repository: tuple[Path, str]
) -> None:
    repository, _ = broken_repository
    model = NeverCalledModel()
    runner = StaticRunner(
        SandboxResult(
            success=False,
            stderr="container timed out",
            exit_code=None,
            timed_out=True,
            cleanup_status="removed",
        )
    )

    result = AgentOrchestrator(model, test_runner=runner).run(repository)

    assert result.status == TerminationStatus.TEST_TIMEOUT
    assert result.baseline_test is not None
    assert result.baseline_test.outcome == Outcome.TIMED_OUT
    assert model.called is False


def test_container_cleanup_error_has_distinct_termination(
    broken_repository: tuple[Path, str]
) -> None:
    repository, _ = broken_repository
    runner = StaticRunner(
        SandboxResult(
            success=False,
            stderr="container cleanup failed",
            exit_code=1,
            cleanup_status="failed",
        )
    )

    result = AgentOrchestrator(NeverCalledModel(), test_runner=runner).run(repository)

    assert result.status == TerminationStatus.CONTAINER_CLEANUP_ERROR
    assert result.baseline_test is not None
    assert result.baseline_test.outcome == Outcome.CLEANUP_ERROR


def test_oversized_repository_is_rejected_before_copy_or_tests(
    broken_repository: tuple[Path, str]
) -> None:
    repository, _ = broken_repository
    model = NeverCalledModel()
    runner = StaticRunner(SandboxResult(success=True, exit_code=0, cleanup_status="removed"))

    result = AgentOrchestrator(
        model,
        config=AgentConfig(max_repository_files=1),
        test_runner=runner,
    ).run(repository)

    assert result.status == TerminationStatus.REPOSITORY_LIMIT
    assert "file count" in result.error
    assert runner.calls == 0
    assert model.called is False


def test_agent_uses_fixed_string_search_for_malicious_regex_by_default(
    broken_repository: tuple[Path, str]
) -> None:
    repository, _ = broken_repository
    (repository / "long.txt").write_text("a" * 100_000 + "X\n", encoding="utf-8")
    model = ScriptedModel([SearchAction("(a+)+$"), FinishAction()])

    result = AgentOrchestrator(
        model,
        config=AgentConfig(search_timeout=0.5),
        test_runner=ContentAwareRunner(forced_success=False),
    ).run(repository)

    assert result.status == TerminationStatus.TESTS_FAILED
    search_observation = result.observations[1]
    assert search_observation.action_type == "SearchAction"
    assert search_observation.success is True
    assert search_observation.output == ""


def test_regex_search_requires_explicit_agent_configuration(
    broken_repository: tuple[Path, str]
) -> None:
    repository, _ = broken_repository

    result = AgentOrchestrator(
        ScriptedModel([SearchAction("divide.*", regex=True)]),
        test_runner=ContentAwareRunner(forced_success=False),
    ).run(repository)

    assert result.status == TerminationStatus.INVALID_ACTION
    assert "disabled" in result.error
    assert result.tool_calls == 1


def test_regex_search_can_be_explicitly_enabled(
    broken_repository: tuple[Path, str]
) -> None:
    repository, _ = broken_repository

    result = AgentOrchestrator(
        ScriptedModel([SearchAction(r"return\s+a", regex=True), FinishAction()]),
        config=AgentConfig(allow_regex_search=True),
        test_runner=ContentAwareRunner(forced_success=False),
    ).run(repository)

    assert result.observations[1].success is True
    assert "calculator.py:2" in result.observations[1].output
