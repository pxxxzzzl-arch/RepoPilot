"""Repeatable evaluation suites and content-free aggregate reports."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Callable, Iterable, Sequence

from issue2patch.orchestrator import (
    AgentConfig,
    AgentOrchestrator,
    AgentProgressEvent,
    ModelClient,
    TerminationStatus,
    TestRunner,
)
from issue2patch.sandbox import DockerSandboxRunner
from issue2patch.tools import PathSecurityError, _repository_root, _safe_path

DEFAULT_EVAL_RUNS = 3
_TASK_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_IGNORED_SNAPSHOT_NAMES = {
    ".git", ".issue2patch", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".venv", "__pycache__",
}


class EvalValidationError(ValueError):
    """Raised when a suite or report target is unsafe or malformed."""


@dataclass(frozen=True, slots=True)
class EvalTask:
    task_id: str
    repository: Path
    issue: str
    allowed_files: tuple[str, ...]
    test_path: str = "tests"


@dataclass(frozen=True, slots=True)
class EvalRunRecord:
    task_id: str
    run_number: int
    run_id: str
    status: str
    agent_success: bool
    tests_passed: bool
    repair_success: bool
    original_unchanged: bool
    changed_files: tuple[str, ...]
    unrelated_files: tuple[str, ...]
    tool_calls: int
    steps: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    total_tokens: int
    duration_seconds: float
    model_duration_seconds: float
    estimated_cost_usd: float | None
    timed_out: bool
    patch_conflict: bool
    security_blocked: bool
    error_summary: str = ""


@dataclass(frozen=True, slots=True)
class EvalTaskSummary:
    task_id: str
    runs: int
    repair_success_rate: float
    test_pass_rate: float
    unrelated_file_changes: int
    average_tool_calls: float
    average_total_tokens: float
    average_duration_seconds: float
    total_cost_usd: float | None
    timeout_count: int
    patch_conflict_count: int
    security_block_count: int


@dataclass(frozen=True, slots=True)
class EvalAggregate:
    task_count: int
    total_runs: int
    repair_success_rate: float
    test_pass_rate: float
    unrelated_file_changes: int
    average_tool_calls: float
    total_input_tokens: int
    total_cached_input_tokens: int
    total_output_tokens: int
    total_tokens: int
    average_duration_seconds: float
    total_duration_seconds: float
    total_model_duration_seconds: float
    total_cost_usd: float | None
    timeout_count: int
    patch_conflict_count: int
    security_block_count: int
    termination_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class EvalReport:
    generated_at: str
    suite_path: str
    runs_per_task: int
    records: tuple[EvalRunRecord, ...]
    tasks: tuple[EvalTaskSummary, ...]
    aggregate: EvalAggregate


EvalModelFactory = Callable[[EvalTask, int], ModelClient]
EvalProgressCallback = Callable[[EvalTask, int, AgentProgressEvent], None]


def load_eval_suite(path: str | Path) -> tuple[EvalTask, ...]:
    """Load a suite manifest while containing all task repositories."""
    manifest = Path(path)
    if manifest.is_symlink():
        raise EvalValidationError("suite manifest must not be a symbolic link")
    try:
        suite_root = _repository_root(manifest.parent)
        _, safe_manifest = _safe_path(suite_root, manifest.name)
        decoded = json.loads(safe_manifest.read_text(encoding="utf-8"))
    except (PathSecurityError, OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EvalValidationError(f"unable to load eval suite: {error}") from error
    if not isinstance(decoded, dict) or set(decoded) != {"version", "tasks"}:
        raise EvalValidationError("suite must contain exactly version and tasks")
    if decoded["version"] != 1 or not isinstance(decoded["tasks"], list):
        raise EvalValidationError("unsupported eval suite version or tasks value")
    if not 1 <= len(decoded["tasks"]) <= 100:
        raise EvalValidationError("suite must define between 1 and 100 tasks")

    tasks: list[EvalTask] = []
    identifiers: set[str] = set()
    for raw in decoded["tasks"]:
        if not isinstance(raw, dict) or set(raw) != {
            "id", "repository", "issue", "allowed_files", "test_path"
        }:
            raise EvalValidationError("each task must use the complete task schema")
        task_id = raw["id"]
        issue = raw["issue"]
        repository_name = raw["repository"]
        test_path = raw["test_path"]
        allowed_files = raw["allowed_files"]
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            raise EvalValidationError("invalid task id")
        if task_id in identifiers:
            raise EvalValidationError(f"duplicate task id: {task_id}")
        identifiers.add(task_id)
        if not isinstance(issue, str) or not issue.strip():
            raise EvalValidationError(f"task issue must not be empty: {task_id}")
        if not isinstance(repository_name, str):
            raise EvalValidationError(f"invalid repository path: {task_id}")
        try:
            _, repository = _safe_path(suite_root, repository_name)
        except PathSecurityError as error:
            raise EvalValidationError(f"unsafe task repository {task_id}: {error}") from error
        if not repository.is_dir():
            raise EvalValidationError(f"task repository is not a directory: {task_id}")
        if not isinstance(test_path, str) or not test_path:
            raise EvalValidationError(f"invalid test path: {task_id}")
        _validate_relative_path(test_path, f"test path for {task_id}")
        if not isinstance(allowed_files, list) or not allowed_files:
            raise EvalValidationError(f"allowed_files must be non-empty: {task_id}")
        normalized_allowed: list[str] = []
        for allowed in allowed_files:
            if not isinstance(allowed, str) or not allowed:
                raise EvalValidationError(f"invalid allowed file: {task_id}")
            _validate_relative_path(allowed, f"allowed file for {task_id}")
            normalized_allowed.append(Path(allowed).as_posix())
        tasks.append(EvalTask(
            task_id=task_id,
            repository=repository,
            issue=issue,
            allowed_files=tuple(sorted(set(normalized_allowed))),
            test_path=test_path,
        ))
    return tuple(tasks)


class EvalRunner:
    """Run every fixed task repeatedly through the production orchestrator."""

    def __init__(
        self,
        model_factory: EvalModelFactory,
        *,
        agent_config: AgentConfig | None = None,
        test_runner_factory: Callable[[], TestRunner] | None = None,
        progress_callback: EvalProgressCallback | None = None,
    ) -> None:
        self.model_factory = model_factory
        self.agent_config = agent_config or AgentConfig()
        self.test_runner_factory = test_runner_factory or DockerSandboxRunner
        self.progress_callback = progress_callback

    def run(
        self,
        tasks: Sequence[EvalTask],
        *,
        runs_per_task: int = DEFAULT_EVAL_RUNS,
        suite_path: str | Path = "",
    ) -> EvalReport:
        if not tasks:
            raise ValueError("at least one eval task is required")
        if runs_per_task <= 0 or runs_per_task > 20:
            raise ValueError("runs_per_task must be between 1 and 20")
        records: list[EvalRunRecord] = []
        for task in tasks:
            baseline_digest = _tree_digest(task.repository)
            for run_number in range(1, runs_per_task + 1):
                model = self.model_factory(task, run_number)
                callback = None
                if self.progress_callback:
                    callback = lambda event, task=task, run_number=run_number: (
                        self.progress_callback(task, run_number, event)
                    )
                config = replace(
                    self.agent_config, baseline_test_path=task.test_path
                )
                result = AgentOrchestrator(
                    model,
                    config=config,
                    test_runner=self.test_runner_factory(),
                    progress_callback=callback,
                ).run(task.repository, issue=task.issue)
                current_digest = _tree_digest(task.repository)
                original_unchanged = current_digest == baseline_digest
                changed_files = _changed_files(result.diff)
                unrelated = tuple(
                    sorted(set(changed_files) - set(task.allowed_files))
                )
                tests_passed = result.last_test_passed is True
                agent_success = result.status == TerminationStatus.SUCCESS
                security_blocked = _security_blocked(result.status, result.error)
                usage = result.model_usage
                records.append(EvalRunRecord(
                    task_id=task.task_id,
                    run_number=run_number,
                    run_id=result.run_id,
                    status=result.status.value,
                    agent_success=agent_success,
                    tests_passed=tests_passed,
                    repair_success=(
                        agent_success
                        and tests_passed
                        and original_unchanged
                        and not unrelated
                    ),
                    original_unchanged=original_unchanged,
                    changed_files=changed_files,
                    unrelated_files=unrelated,
                    tool_calls=result.tool_calls,
                    steps=result.steps,
                    input_tokens=usage.input_tokens,
                    cached_input_tokens=usage.cached_input_tokens,
                    output_tokens=usage.output_tokens,
                    total_tokens=usage.total_tokens,
                    duration_seconds=result.duration_seconds,
                    model_duration_seconds=usage.duration_seconds,
                    estimated_cost_usd=usage.estimated_cost_usd,
                    timed_out=_timed_out(result.status, result.error),
                    patch_conflict=result.status == TerminationStatus.PATCH_CONFLICT,
                    security_blocked=security_blocked,
                    error_summary=" ".join(result.error.split())[:300],
                ))
        summaries = tuple(
            _task_summary(task.task_id, records) for task in tasks
        )
        return EvalReport(
            generated_at=datetime.now(timezone.utc).isoformat(),
            suite_path=str(Path(suite_path)) if suite_path else "",
            runs_per_task=runs_per_task,
            records=tuple(records),
            tasks=summaries,
            aggregate=_aggregate(records, len(tasks)),
        )


def write_eval_reports(
    report: EvalReport, output_directory: str | Path
) -> tuple[Path, Path]:
    """Atomically write stable JSON and Markdown report names."""
    output = Path(output_directory)
    if output.is_symlink():
        raise EvalValidationError("report output directory must not be a symbolic link")
    output.mkdir(parents=True, exist_ok=True)
    output = output.resolve(strict=True)
    json_path = output / "eval-report.json"
    markdown_path = output / "eval-report.md"
    for target in (json_path, markdown_path):
        if target.is_symlink():
            raise EvalValidationError("report file must not be a symbolic link")
    json_text = json.dumps(asdict(report), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _atomic_write(json_path, json_text)
    _atomic_write(markdown_path, render_markdown_report(report))
    return json_path, markdown_path


def render_markdown_report(report: EvalReport) -> str:
    aggregate = report.aggregate
    cost = "n/a" if aggregate.total_cost_usd is None else f"${aggregate.total_cost_usd:.6f}"
    lines = [
        "# Issue2Patch Agent Eval Report",
        "",
        f"Generated: `{report.generated_at}`  ",
        f"Tasks: **{aggregate.task_count}** · Runs per task: **{report.runs_per_task}** · Total runs: **{aggregate.total_runs}**",
        "",
        "## Overall",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Repair success rate | {_percent(aggregate.repair_success_rate)} |",
        f"| Test pass rate | {_percent(aggregate.test_pass_rate)} |",
        f"| Unrelated file changes | {aggregate.unrelated_file_changes} |",
        f"| Average tool calls | {aggregate.average_tool_calls:.2f} |",
        f"| Total tokens | {aggregate.total_tokens} |",
        f"| Average duration | {aggregate.average_duration_seconds:.3f}s |",
        f"| Estimated cost | {cost} |",
        f"| Timeouts | {aggregate.timeout_count} |",
        f"| Patch conflicts | {aggregate.patch_conflict_count} |",
        f"| Security blocks | {aggregate.security_block_count} |",
        "",
        "## Per task",
        "",
        "| Task | Success | Tests | Unrelated | Avg tools | Avg tokens | Avg seconds | Cost | Timeouts | Conflicts | Security |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for task in report.tasks:
        task_cost = "n/a" if task.total_cost_usd is None else f"${task.total_cost_usd:.6f}"
        lines.append(
            f"| {task.task_id} | {_percent(task.repair_success_rate)} | "
            f"{_percent(task.test_pass_rate)} | {task.unrelated_file_changes} | "
            f"{task.average_tool_calls:.2f} | {task.average_total_tokens:.1f} | "
            f"{task.average_duration_seconds:.3f} | {task_cost} | "
            f"{task.timeout_count} | {task.patch_conflict_count} | "
            f"{task.security_block_count} |"
        )
    lines.extend(["", "## Termination reasons", ""])
    for status, count in sorted(aggregate.termination_counts.items()):
        lines.append(f"- `{status}`: {count}")
    return "\n".join(lines) + "\n"


def _task_summary(task_id: str, records: Sequence[EvalRunRecord]) -> EvalTaskSummary:
    selected = [record for record in records if record.task_id == task_id]
    count = len(selected)
    return EvalTaskSummary(
        task_id=task_id,
        runs=count,
        repair_success_rate=_rate(sum(record.repair_success for record in selected), count),
        test_pass_rate=_rate(sum(record.tests_passed for record in selected), count),
        unrelated_file_changes=sum(len(record.unrelated_files) for record in selected),
        average_tool_calls=_average(record.tool_calls for record in selected),
        average_total_tokens=_average(record.total_tokens for record in selected),
        average_duration_seconds=_average(record.duration_seconds for record in selected),
        total_cost_usd=_total_cost(selected),
        timeout_count=sum(record.timed_out for record in selected),
        patch_conflict_count=sum(record.patch_conflict for record in selected),
        security_block_count=sum(record.security_blocked for record in selected),
    )


def _aggregate(records: Sequence[EvalRunRecord], task_count: int) -> EvalAggregate:
    count = len(records)
    return EvalAggregate(
        task_count=task_count,
        total_runs=count,
        repair_success_rate=_rate(sum(record.repair_success for record in records), count),
        test_pass_rate=_rate(sum(record.tests_passed for record in records), count),
        unrelated_file_changes=sum(len(record.unrelated_files) for record in records),
        average_tool_calls=_average(record.tool_calls for record in records),
        total_input_tokens=sum(record.input_tokens for record in records),
        total_cached_input_tokens=sum(record.cached_input_tokens for record in records),
        total_output_tokens=sum(record.output_tokens for record in records),
        total_tokens=sum(record.total_tokens for record in records),
        average_duration_seconds=_average(record.duration_seconds for record in records),
        total_duration_seconds=sum(record.duration_seconds for record in records),
        total_model_duration_seconds=sum(record.model_duration_seconds for record in records),
        total_cost_usd=_total_cost(records),
        timeout_count=sum(record.timed_out for record in records),
        patch_conflict_count=sum(record.patch_conflict for record in records),
        security_block_count=sum(record.security_blocked for record in records),
        termination_counts=dict(Counter(record.status for record in records)),
    )


def _total_cost(records: Sequence[EvalRunRecord]) -> float | None:
    costs = [record.estimated_cost_usd for record in records]
    if any(cost is None for cost in costs):
        return None
    return sum(cost for cost in costs if cost is not None)


def _security_blocked(status: TerminationStatus, error: str) -> bool:
    if status in {
        TerminationStatus.INVALID_ACTION,
        TerminationStatus.ORIGINAL_CHANGED,
        TerminationStatus.REPOSITORY_LIMIT,
    }:
        return True
    lowered = error.lower()
    return any(fragment in lowered for fragment in (
        "sensitive file", "symbolic link", "escapes", "not allowed"
    ))


def _timed_out(status: TerminationStatus, error: str) -> bool:
    if status == TerminationStatus.TEST_TIMEOUT:
        return True
    lowered = error.lower()
    return "timed out" in lowered or "timeout" in lowered


def _changed_files(diff: str) -> tuple[str, ...]:
    changed = {
        line[6:]
        for line in diff.splitlines()
        if line.startswith("+++ b/") and line[6:]
    }
    return tuple(sorted(changed))


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()

    def update(relative_path: Path, content: bytes) -> None:
        relative = relative_path.as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)

    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        retained: list[str] = []
        for name in sorted(directory_names):
            if name in _IGNORED_SNAPSHOT_NAMES:
                continue
            path = directory_path / name
            if path.is_symlink():
                update(
                    path.relative_to(root),
                    f"SYMLINK:{os.readlink(path)}".encode("utf-8"),
                )
            else:
                retained.append(name)
        directory_names[:] = retained
        for name in sorted(file_names):
            path = directory_path / name
            if name.endswith(".pyc"):
                continue
            content = (
                f"SYMLINK:{os.readlink(path)}".encode("utf-8")
                if path.is_symlink()
                else path.read_bytes()
            )
            update(path.relative_to(root), content)
    return digest.hexdigest()


def _validate_relative_path(value: str, label: str) -> None:
    path = Path(value)
    if path.is_absolute() or PureWindowsPath(value).is_absolute() or ".." in path.parts:
        raise EvalValidationError(f"unsafe {label}")


def _atomic_write(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _average(values: Iterable[float]) -> float:
    materialized = list(values)
    return sum(materialized) / len(materialized) if materialized else 0.0


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


__all__ = [
    "DEFAULT_EVAL_RUNS", "EvalAggregate", "EvalReport", "EvalRunRecord",
    "EvalRunner", "EvalTask", "EvalTaskSummary", "EvalValidationError",
    "load_eval_suite", "render_markdown_report", "write_eval_reports",
]
