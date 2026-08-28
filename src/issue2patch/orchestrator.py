"""Provider-independent, deterministic agent orchestration."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol, Sequence, runtime_checkable

from issue2patch.patching import FileChange, PatchOperation, apply_patch
from issue2patch.sandbox import DockerSandboxRunner, SandboxResult
from issue2patch.tools import PathSecurityError, _repository_root, read_file, search_code
from issue2patch.trace import TraceRecorder

_IGNORED_COPY_NAMES = {
    ".git", ".issue2patch", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".venv", "__pycache__",
}


class RepositoryLimitError(ValueError):
    """Raised before an oversized repository can be copied or inspected."""


class InvalidModelActionError(ValueError):
    """Raised when model output cannot safely map to an allowed action."""


@dataclass(frozen=True, slots=True)
class SearchAction:
    pattern: str
    path: str = "."
    regex: bool = False


@dataclass(frozen=True, slots=True)
class ReadFileAction:
    path: str


@dataclass(frozen=True, slots=True)
class PatchAction:
    operations: tuple[PatchOperation, ...]


@dataclass(frozen=True, slots=True)
class RunTestsAction:
    path: str = "tests"


@dataclass(frozen=True, slots=True)
class FinishAction:
    summary: str = ""


AgentAction = SearchAction | ReadFileAction | PatchAction | RunTestsAction | FinishAction
_ALLOWED_ACTION_TYPES = (SearchAction, ReadFileAction, PatchAction, RunTestsAction, FinishAction)


class TestOutcome(str, Enum):
    PASSED = "passed"
    ASSERTION_FAILED = "assertion_failed"
    TIMED_OUT = "timed_out"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    CLEANUP_ERROR = "cleanup_error"


@dataclass(frozen=True, slots=True)
class TestRunSummary:
    outcome: TestOutcome
    exit_code: int | None
    timed_out: bool
    cleanup_status: str
    duration_seconds: float
    error_summary: str = ""


@dataclass(frozen=True, slots=True)
class ToolObservation:
    action_type: str
    success: bool
    output: str = ""
    error: str = ""
    duration_seconds: float = 0.0
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentContext:
    run_id: str
    issue: str
    step: int
    remaining_steps: int
    remaining_tool_calls: int
    observations: tuple[ToolObservation, ...] = ()
    last_test_passed: bool | None = None
    baseline_test: TestRunSummary | None = None
    latest_test: TestRunSummary | None = None


@dataclass(frozen=True, slots=True)
class ModelUsage:
    request_count: int = 0
    retry_count: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    duration_seconds: float = 0.0
    estimated_cost_usd: float | None = 0.0


@dataclass(frozen=True, slots=True)
class AgentProgressEvent:
    event_type: str
    step: int
    action_type: str = ""
    success: bool | None = None
    summary: str = ""


@runtime_checkable
class ModelClient(Protocol):
    def next_action(self, context: AgentContext) -> AgentAction:
        """Return exactly one structured action."""
        ...


@runtime_checkable
class TestRunner(Protocol):
    def run_tests(
        self, repo_root: str | Path, path: str | Path = "tests", *, timeout: float = 60.0
    ) -> SandboxResult:
        ...


@dataclass(frozen=True, slots=True)
class AgentConfig:
    max_steps: int = 12
    max_tool_calls: int = 10
    max_tool_output_chars: int = 4_000
    max_repeated_actions: int = 2
    test_timeout: float = 60.0
    baseline_test_path: str = "tests"
    max_repository_files: int = 2_000
    max_repository_file_bytes: int = 2_000_000
    max_repository_total_bytes: int = 20_000_000
    search_timeout: float = 2.0
    max_search_results: int = 100
    allow_regex_search: bool = False

    def __post_init__(self) -> None:
        positive = {
            "max_steps": self.max_steps,
            "max_tool_calls": self.max_tool_calls,
            "max_tool_output_chars": self.max_tool_output_chars,
            "max_repository_files": self.max_repository_files,
            "max_repository_file_bytes": self.max_repository_file_bytes,
            "max_repository_total_bytes": self.max_repository_total_bytes,
            "max_search_results": self.max_search_results,
        }
        invalid = next((name for name, value in positive.items() if value <= 0), None)
        if invalid:
            raise ValueError(f"{invalid} must be greater than zero")
        if self.max_repeated_actions <= 1:
            raise ValueError("max_repeated_actions must be greater than one")
        if self.test_timeout <= 0:
            raise ValueError("test_timeout must be greater than zero")
        if self.search_timeout <= 0:
            raise ValueError("search_timeout must be greater than zero")
        if not self.baseline_test_path:
            raise ValueError("baseline_test_path must not be empty")


class TerminationStatus(str, Enum):
    SUCCESS = "success"
    TESTS_FAILED = "tests_failed"
    TESTS_NOT_RUN = "tests_not_run"
    TEST_TIMEOUT = "test_timeout"
    TEST_INFRASTRUCTURE_ERROR = "test_infrastructure_error"
    CONTAINER_CLEANUP_ERROR = "container_cleanup_error"
    INVALID_ACTION = "invalid_action"
    MODEL_ERROR = "model_error"
    TOOL_ERROR = "tool_error"
    STEP_LIMIT = "step_limit"
    TOOL_CALL_LIMIT = "tool_call_limit"
    NO_PROGRESS = "no_progress"
    PATCH_CONFLICT = "patch_conflict"
    ORIGINAL_CHANGED = "original_changed"
    REPOSITORY_LIMIT = "repository_limit"
    SETUP_ERROR = "setup_error"


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    run_id: str
    status: TerminationStatus
    success: bool
    diff: str = ""
    steps: int = 0
    tool_calls: int = 0
    duration_seconds: float = 0.0
    error: str = ""
    summary: str = ""
    observations: tuple[ToolObservation, ...] = ()
    last_test_passed: bool | None = None
    baseline_test: TestRunSummary | None = None
    latest_test: TestRunSummary | None = None
    model_usage: ModelUsage = ModelUsage()


class ScriptedModel:
    def __init__(self, actions: Sequence[AgentAction]) -> None:
        self._actions = tuple(actions)
        self._index = 0

    def next_action(self, context: AgentContext) -> AgentAction:
        del context
        if self._index >= len(self._actions):
            raise RuntimeError("scripted model ran out of actions")
        action = self._actions[self._index]
        self._index += 1
        return action


class AgentOrchestrator:
    """Execute structured tools against a bounded temporary repository copy."""

    def __init__(
        self, model: ModelClient, *, config: AgentConfig | None = None,
        test_runner: TestRunner | None = None,
        progress_callback: Callable[[AgentProgressEvent], None] | None = None,
    ) -> None:
        self.model = model
        self.config = config or AgentConfig()
        self.test_runner = test_runner or DockerSandboxRunner()
        self.progress_callback = progress_callback

    def run(
        self, repo_root: str | Path, *, issue: str = "",
        trace_recorder: TraceRecorder | None = None,
    ) -> AgentRunResult:
        started = time.monotonic()
        run_id = trace_recorder.run_id if trace_recorder else uuid.uuid4().hex
        observations: list[ToolObservation] = []
        tool_calls = 0
        last_test_passed: bool | None = None
        baseline_test: TestRunSummary | None = None
        latest_test: TestRunSummary | None = None
        workspace_revision = 0
        repeated_actions: dict[tuple[int, str], int] = {}

        try:
            original_root = _repository_root(repo_root)
        except PathSecurityError as error:
            return self._finish_without_workspace(
                run_id, TerminationStatus.SETUP_ERROR, started, trace_recorder, error=str(error)
            )
        if trace_recorder and self._path_is_within(trace_recorder.log_path, original_root):
            return self._finish_without_workspace(
                run_id, TerminationStatus.SETUP_ERROR, started, None,
                error="trace log must be outside the original repository",
            )

        try:
            original_snapshot = self._snapshot(original_root)
            with tempfile.TemporaryDirectory(prefix="issue2patch-agent-") as temporary:
                workspace = Path(temporary) / "repository"
                self._copy_repository(original_root, workspace)
                workspace_baseline = self._snapshot(workspace)

                baseline_started = time.monotonic()
                try:
                    baseline_result = self.test_runner.run_tests(
                        workspace,
                        self.config.baseline_test_path,
                        timeout=self.config.test_timeout,
                    )
                except Exception as error:
                    baseline_result = SandboxResult(
                        success=False,
                        stderr=f"test runner infrastructure error: {error}",
                        cleanup_status="not_created",
                    )
                tool_calls = 1
                baseline_test = self._bounded_test_summary(
                    self._summarize_tests(baseline_result)
                )
                latest_test = baseline_test
                baseline_observation = self._bounded_observation(
                    self._test_observation("BaselineTests", baseline_result, baseline_test),
                    time.monotonic() - baseline_started,
                )
                observations.append(baseline_observation)
                self._record_action(
                    trace_recorder, 0, "BaselineTests", baseline_observation.success,
                    baseline_observation.duration_seconds, output=baseline_observation.output,
                    error=baseline_observation.error,
                )
                self._emit(AgentProgressEvent(
                    event_type="baseline",
                    step=0,
                    action_type="BaselineTests",
                    success=baseline_observation.success,
                    summary=baseline_test.outcome.value,
                ))

                fatal_status = self._fatal_test_status(baseline_test)
                if fatal_status is not None:
                    return self._finish(
                        original_root, original_snapshot, workspace, workspace_baseline,
                        run_id, fatal_status, started, trace_recorder, step=0,
                        tool_calls=tool_calls, observations=observations,
                        last_test_passed=None, baseline_test=baseline_test,
                        latest_test=latest_test,
                        error=baseline_test.error_summary or baseline_test.outcome.value,
                    )
                if baseline_test.outcome == TestOutcome.PASSED:
                    return self._finish(
                        original_root, original_snapshot, workspace, workspace_baseline,
                        run_id, TerminationStatus.SUCCESS, started, trace_recorder, step=0,
                        tool_calls=tool_calls, observations=observations,
                        last_test_passed=True, baseline_test=baseline_test,
                        latest_test=latest_test, summary="target tests already pass",
                    )
                last_test_passed = False

                for step in range(1, self.config.max_steps + 1):
                    context = AgentContext(
                        run_id=run_id, issue=issue, step=step,
                        remaining_steps=self.config.max_steps - step + 1,
                        remaining_tool_calls=max(0, self.config.max_tool_calls - tool_calls),
                        observations=tuple(observations),
                        last_test_passed=last_test_passed,
                        baseline_test=baseline_test, latest_test=latest_test,
                    )
                    self._emit(AgentProgressEvent(
                        event_type="model",
                        step=step,
                        summary="requesting next structured action",
                    ))
                    try:
                        action = self.model.next_action(context)
                    except InvalidModelActionError as error:
                        return self._finish_run(
                            original_root, original_snapshot, workspace, workspace_baseline,
                            run_id, TerminationStatus.INVALID_ACTION, started, trace_recorder,
                            step, tool_calls, observations, last_test_passed,
                            baseline_test, latest_test, f"invalid model output: {error}",
                        )
                    except Exception as error:
                        return self._finish_run(
                            original_root, original_snapshot, workspace, workspace_baseline,
                            run_id, TerminationStatus.MODEL_ERROR, started, trace_recorder,
                            step, tool_calls, observations, last_test_passed,
                            baseline_test, latest_test, f"model error: {error}",
                        )

                    if not isinstance(action, _ALLOWED_ACTION_TYPES):
                        action_type = type(action).__name__
                        self._record_action(
                            trace_recorder, step, action_type, False, 0.0,
                            error="model returned an unsupported action type",
                        )
                        return self._finish_run(
                            original_root, original_snapshot, workspace, workspace_baseline,
                            run_id, TerminationStatus.INVALID_ACTION, started, trace_recorder,
                            step, tool_calls, observations, last_test_passed,
                            baseline_test, latest_test, f"unsupported action: {action_type}",
                        )
                    self._emit(AgentProgressEvent(
                        event_type="action",
                        step=step,
                        action_type=type(action).__name__,
                        summary="action selected",
                    ))
                    if isinstance(action, SearchAction) and action.regex and not self.config.allow_regex_search:
                        error = "regular-expression search is disabled by AgentConfig"
                        self._record_action(trace_recorder, step, "SearchAction", False, 0.0, error=error)
                        return self._finish_run(
                            original_root, original_snapshot, workspace, workspace_baseline,
                            run_id, TerminationStatus.INVALID_ACTION, started, trace_recorder,
                            step, tool_calls, observations, last_test_passed,
                            baseline_test, latest_test, error,
                        )
                    if isinstance(action, FinishAction):
                        status = (
                            TerminationStatus.SUCCESS if last_test_passed is True
                            else TerminationStatus.TESTS_FAILED if last_test_passed is False
                            else TerminationStatus.TESTS_NOT_RUN
                        )
                        error = "" if status == TerminationStatus.SUCCESS else "tests did not pass"
                        self._record_action(
                            trace_recorder, step, "FinishAction",
                            status == TerminationStatus.SUCCESS, 0.0, error=error,
                        )
                        return self._finish(
                            original_root, original_snapshot, workspace, workspace_baseline,
                            run_id, status, started, trace_recorder, step=step,
                            tool_calls=tool_calls, observations=observations,
                            last_test_passed=last_test_passed,
                            baseline_test=baseline_test, latest_test=latest_test,
                            summary=action.summary, error=error,
                        )
                    if tool_calls >= self.config.max_tool_calls:
                        return self._finish_run(
                            original_root, original_snapshot, workspace, workspace_baseline,
                            run_id, TerminationStatus.TOOL_CALL_LIMIT, started, trace_recorder,
                            step, tool_calls, observations, last_test_passed,
                            baseline_test, latest_test, "maximum tool call count reached",
                        )

                    fingerprint = self._action_fingerprint(action)
                    repeat_key = (workspace_revision, fingerprint)
                    repeated_actions[repeat_key] = repeated_actions.get(repeat_key, 0) + 1
                    if repeated_actions[repeat_key] >= self.config.max_repeated_actions:
                        return self._finish_run(
                            original_root, original_snapshot, workspace, workspace_baseline,
                            run_id, TerminationStatus.NO_PROGRESS, started, trace_recorder,
                            step, tool_calls, observations, last_test_passed,
                            baseline_test, latest_test,
                            "repeated action without workspace progress",
                        )

                    tool_calls += 1
                    action_started = time.monotonic()
                    try:
                        observation, files, test_summary = self._execute_action(action, workspace)
                    except Exception as error:
                        duration = time.monotonic() - action_started
                        self._record_action(
                            trace_recorder, step, type(action).__name__, False, duration,
                            error=f"tool error: {error}",
                        )
                        return self._finish_run(
                            original_root, original_snapshot, workspace, workspace_baseline,
                            run_id, TerminationStatus.TOOL_ERROR, started, trace_recorder,
                            step, tool_calls, observations, last_test_passed,
                            baseline_test, latest_test, f"tool error: {error}",
                        )
                    observation = self._bounded_observation(
                        observation, time.monotonic() - action_started
                    )
                    observations.append(observation)
                    self._record_action(
                        trace_recorder, step, observation.action_type, observation.success,
                        observation.duration_seconds, output=observation.output,
                        error=observation.error, files=files,
                        metadata=observation.metadata,
                    )
                    self._emit(AgentProgressEvent(
                        event_type="tool",
                        step=step,
                        action_type=observation.action_type,
                        success=observation.success,
                        summary=(
                            observation.error[:160]
                            if observation.error
                            else f"{len(observation.output)} output characters"
                        ),
                    ))

                    if isinstance(action, PatchAction):
                        if not observation.success:
                            return self._finish_run(
                                original_root, original_snapshot, workspace, workspace_baseline,
                                run_id, TerminationStatus.PATCH_CONFLICT, started, trace_recorder,
                                step, tool_calls, observations, last_test_passed,
                                baseline_test, latest_test, observation.error,
                            )
                        workspace_revision += 1
                        last_test_passed = None
                        latest_test = None
                    elif isinstance(action, RunTestsAction):
                        assert test_summary is not None
                        latest_test = test_summary
                        fatal_status = self._fatal_test_status(test_summary)
                        if fatal_status is not None:
                            return self._finish_run(
                                original_root, original_snapshot, workspace, workspace_baseline,
                                run_id, fatal_status, started, trace_recorder,
                                step, tool_calls, observations, None,
                                baseline_test, latest_test,
                                test_summary.error_summary or test_summary.outcome.value,
                            )
                        last_test_passed = test_summary.outcome == TestOutcome.PASSED

                return self._finish_run(
                    original_root, original_snapshot, workspace, workspace_baseline,
                    run_id, TerminationStatus.STEP_LIMIT, started, trace_recorder,
                    self.config.max_steps, tool_calls, observations, last_test_passed,
                    baseline_test, latest_test, "maximum step count reached",
                )
        except RepositoryLimitError as error:
            return self._finish_without_workspace(
                run_id, TerminationStatus.REPOSITORY_LIMIT, started, trace_recorder,
                error=str(error),
            )
        except OSError as error:
            return self._finish_without_workspace(
                run_id, TerminationStatus.SETUP_ERROR, started, trace_recorder,
                error=f"unable to prepare temporary workspace: {error}",
            )

    def _execute_action(
        self, action: AgentAction, workspace: Path
    ) -> tuple[ToolObservation, list[dict[str, object]], TestRunSummary | None]:
        if isinstance(action, SearchAction):
            result = search_code(
                workspace, action.pattern, action.path, regex=action.regex,
                timeout=self.config.search_timeout,
                max_results=self.config.max_search_results,
                max_files=self.config.max_repository_files,
                max_file_bytes=self.config.max_repository_file_bytes,
                max_total_bytes=self.config.max_repository_total_bytes,
            )
            return ToolObservation("SearchAction", result.success, result.stdout, result.stderr), [], None
        if isinstance(action, ReadFileAction):
            result = read_file(workspace, action.path)
            return ToolObservation(
                "ReadFileAction",
                result.success,
                result.stdout,
                result.stderr,
                metadata=dict(result.metadata),
            ), [], None
        if isinstance(action, PatchAction):
            result = apply_patch(workspace, action.operations)
            files = [self._file_change_summary(change) for change in result.changes]
            return ToolObservation("PatchAction", result.success, result.diff, result.error), files, None
        if isinstance(action, RunTestsAction):
            result = self.test_runner.run_tests(workspace, action.path, timeout=self.config.test_timeout)
            summary = self._bounded_test_summary(self._summarize_tests(result))
            return self._test_observation("RunTestsAction", result, summary), [], summary
        raise TypeError(f"unsupported action: {type(action).__name__}")

    @staticmethod
    def _summarize_tests(result: SandboxResult) -> TestRunSummary:
        if result.cleanup_status == "failed":
            outcome = TestOutcome.CLEANUP_ERROR
        elif result.timed_out:
            outcome = TestOutcome.TIMED_OUT
        elif result.cleanup_status != "removed" or result.exit_code is None:
            outcome = TestOutcome.INFRASTRUCTURE_ERROR
        elif result.exit_code == 0 and result.success:
            outcome = TestOutcome.PASSED
        elif result.exit_code == 1:
            outcome = TestOutcome.ASSERTION_FAILED
        else:
            outcome = TestOutcome.INFRASTRUCTURE_ERROR
        error = result.stderr.strip()
        if not error and outcome not in (TestOutcome.PASSED, TestOutcome.ASSERTION_FAILED):
            error = outcome.value
        return TestRunSummary(
            outcome, result.exit_code, result.timed_out, result.cleanup_status,
            result.duration_seconds, error,
        )

    @staticmethod
    def _test_observation(
        action_type: str, result: SandboxResult, summary: TestRunSummary
    ) -> ToolObservation:
        error = result.stderr
        if summary.outcome not in (TestOutcome.PASSED, TestOutcome.ASSERTION_FAILED) and not error:
            error = summary.outcome.value
        return ToolObservation(
            action_type, summary.outcome == TestOutcome.PASSED,
            result.stdout, error, result.duration_seconds,
        )

    @staticmethod
    def _fatal_test_status(summary: TestRunSummary) -> TerminationStatus | None:
        return {
            TestOutcome.TIMED_OUT: TerminationStatus.TEST_TIMEOUT,
            TestOutcome.INFRASTRUCTURE_ERROR: TerminationStatus.TEST_INFRASTRUCTURE_ERROR,
            TestOutcome.CLEANUP_ERROR: TerminationStatus.CONTAINER_CLEANUP_ERROR,
        }.get(summary.outcome)

    def _bounded_observation(self, observation: ToolObservation, duration: float) -> ToolObservation:
        return ToolObservation(
            observation.action_type, observation.success,
            self._truncate(observation.output), self._truncate(observation.error), duration,
            dict(observation.metadata),
        )

    def _bounded_test_summary(self, summary: TestRunSummary) -> TestRunSummary:
        return TestRunSummary(
            outcome=summary.outcome,
            exit_code=summary.exit_code,
            timed_out=summary.timed_out,
            cleanup_status=summary.cleanup_status,
            duration_seconds=summary.duration_seconds,
            error_summary=self._truncate(summary.error_summary),
        )

    def _finish_run(
        self, original_root: Path, original_snapshot: dict[str, bytes], workspace: Path,
        workspace_baseline: dict[str, bytes], run_id: str, status: TerminationStatus,
        started: float, trace_recorder: TraceRecorder | None, step: int,
        tool_calls: int, observations: Sequence[ToolObservation],
        last_test_passed: bool | None, baseline_test: TestRunSummary | None,
        latest_test: TestRunSummary | None, error: str,
    ) -> AgentRunResult:
        return self._finish(
            original_root, original_snapshot, workspace, workspace_baseline,
            run_id, status, started, trace_recorder, step=step,
            tool_calls=tool_calls, observations=observations,
            last_test_passed=last_test_passed, baseline_test=baseline_test,
            latest_test=latest_test, error=error,
        )

    def _finish(
        self, original_root: Path, original_snapshot: dict[str, bytes], workspace: Path,
        workspace_baseline: dict[str, bytes], run_id: str, status: TerminationStatus,
        started: float, trace_recorder: TraceRecorder | None, *, step: int,
        tool_calls: int, observations: Sequence[ToolObservation],
        last_test_passed: bool | None, baseline_test: TestRunSummary | None,
        latest_test: TestRunSummary | None, error: str = "", summary: str = "",
    ) -> AgentRunResult:
        try:
            if self._snapshot(original_root) != original_snapshot:
                status = TerminationStatus.ORIGINAL_CHANGED
                error = "original repository changed during agent run"
            diff = self._diff(workspace, workspace_baseline)
        except RepositoryLimitError as limit_error:
            status = TerminationStatus.REPOSITORY_LIMIT
            error = str(limit_error)
            diff = ""
        result = AgentRunResult(
            run_id, status, status == TerminationStatus.SUCCESS, diff, step, tool_calls,
            time.monotonic() - started, error, summary, tuple(observations),
            last_test_passed, baseline_test, latest_test, self._model_usage(),
        )
        if trace_recorder:
            trace_recorder.record_agent_termination(
                reason=result.status.value, success=result.success,
                duration_seconds=result.duration_seconds, steps=result.steps,
                tool_calls=result.tool_calls, error=result.error,
                model_usage=asdict(result.model_usage),
            )
        return result

    @staticmethod
    def _finish_without_workspace(
        run_id: str, status: TerminationStatus, started: float,
        trace_recorder: TraceRecorder | None, *, error: str,
    ) -> AgentRunResult:
        result = AgentRunResult(
            run_id=run_id, status=status, success=False,
            duration_seconds=time.monotonic() - started, error=error,
        )
        if trace_recorder:
            trace_recorder.record_agent_termination(
                reason=status.value, success=False,
                duration_seconds=result.duration_seconds, steps=0, tool_calls=0, error=error,
            )
        return result

    def _truncate(self, value: str) -> str:
        limit = self.config.max_tool_output_chars
        if len(value) <= limit:
            return value
        marker = "...[truncated]"
        return value[: max(0, limit - len(marker))] + marker

    def _model_usage(self) -> ModelUsage:
        usage = getattr(self.model, "usage", None)
        return usage if isinstance(usage, ModelUsage) else ModelUsage()

    def _emit(self, event: AgentProgressEvent) -> None:
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(event)
        except Exception:
            return

    @staticmethod
    def _record_action(
        recorder: TraceRecorder | None, step: int, action_type: str,
        success: bool, duration: float, *, output: str = "", error: str = "",
        files: list[dict[str, object]] | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        if recorder:
            recorder.record_agent_action(
                step=step, action_type=action_type, success=success,
                duration_seconds=duration, output=output, error=error, files=files,
                metadata=metadata,
            )

    @staticmethod
    def _file_change_summary(change: FileChange) -> dict[str, object]:
        return {
            "path": change.path, "before_sha256": change.before_sha256,
            "after_sha256": change.after_sha256, "applied": change.applied,
        }

    @staticmethod
    def _action_fingerprint(action: AgentAction) -> str:
        encoded = json.dumps(asdict(action), ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(type(action).__name__.encode("utf-8") + encoded).hexdigest()

    def _copy_repository(self, source: Path, destination: Path) -> None:
        self._snapshot(source)
        ignored = shutil.ignore_patterns(*_IGNORED_COPY_NAMES, "*.pyc")
        shutil.copytree(source, destination, symlinks=True, ignore=ignored)
        self._snapshot(destination)

    def _snapshot(self, root: Path) -> dict[str, bytes]:
        snapshot: dict[str, bytes] = {}
        file_count = 0
        total_bytes = 0

        def add(path: Path, content: bytes) -> None:
            nonlocal file_count, total_bytes
            relative = path.relative_to(root).as_posix()
            file_count += 1
            total_bytes += len(content)
            if file_count > self.config.max_repository_files:
                raise RepositoryLimitError(
                    f"repository file count exceeds limit ({self.config.max_repository_files})"
                )
            if len(content) > self.config.max_repository_file_bytes:
                raise RepositoryLimitError(
                    f"repository file exceeds byte limit ({self.config.max_repository_file_bytes}): {relative}"
                )
            if total_bytes > self.config.max_repository_total_bytes:
                raise RepositoryLimitError(
                    f"repository total bytes exceed limit ({self.config.max_repository_total_bytes})"
                )
            snapshot[relative] = content

        for directory, directory_names, file_names in os.walk(root, followlinks=False):
            directory_path = Path(directory)
            retained: list[str] = []
            for name in sorted(directory_names):
                if name in _IGNORED_COPY_NAMES:
                    continue
                path = directory_path / name
                if path.is_symlink():
                    add(path, f"SYMLINK:{os.readlink(path)}".encode("utf-8"))
                else:
                    retained.append(name)
            directory_names[:] = retained
            for name in sorted(file_names):
                if name.endswith(".pyc"):
                    continue
                path = directory_path / name
                if path.is_symlink():
                    content = f"SYMLINK:{os.readlink(path)}".encode("utf-8")
                elif path.is_file():
                    size = path.stat().st_size
                    if size > self.config.max_repository_file_bytes:
                        raise RepositoryLimitError(
                            f"repository file exceeds byte limit ({self.config.max_repository_file_bytes}): "
                            f"{path.relative_to(root).as_posix()}"
                        )
                    content = path.read_bytes()
                else:
                    continue
                add(path, content)
        return snapshot

    def _diff(self, workspace: Path, baseline: dict[str, bytes]) -> str:
        current = self._snapshot(workspace)
        chunks: list[str] = []
        for relative in sorted(set(baseline) | set(current)):
            before = baseline.get(relative, b"")
            after = current.get(relative, b"")
            if before == after:
                continue
            try:
                before_text, after_text = before.decode("utf-8"), after.decode("utf-8")
            except UnicodeDecodeError:
                continue
            chunks.extend(difflib.unified_diff(
                before_text.splitlines(keepends=True), after_text.splitlines(keepends=True),
                fromfile=f"a/{relative}", tofile=f"b/{relative}",
            ))
        return "".join(chunks)

    @staticmethod
    def _path_is_within(path: Path, root: Path) -> bool:
        try:
            return path.resolve(strict=False).is_relative_to(root)
        except (OSError, RuntimeError):
            return False


__all__ = [
    "AgentAction", "AgentConfig", "AgentContext", "AgentOrchestrator",
    "AgentProgressEvent", "AgentRunResult", "FinishAction", "ModelClient",
    "InvalidModelActionError", "ModelUsage", "PatchAction",
    "ReadFileAction", "RepositoryLimitError", "RunTestsAction", "ScriptedModel",
    "SearchAction", "TerminationStatus", "TestOutcome", "TestRunSummary",
    "ToolObservation",
]
