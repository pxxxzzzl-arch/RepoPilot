"""Tests for repeatable Agent evaluation suites and reports."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from issue2patch import (
    AgentContext,
    EvalRunner,
    EvalTask,
    EvalValidationError,
    FinishAction,
    InvalidModelActionError,
    ModelUsage,
    PatchAction,
    PatchOperation,
    ReadFileAction,
    RunTestsAction,
    ScriptedModel,
    load_eval_suite,
    write_eval_reports,
)
from issue2patch.sandbox import SandboxResult
from issue2patch.tools import run_tests_trusted


class TrustedRunnerAdapter:
    def run_tests(
        self,
        repo_root: str | Path,
        path: str | Path = "tests",
        *,
        timeout: float = 60.0,
    ) -> SandboxResult:
        result = run_tests_trusted(repo_root, path, timeout=timeout)
        return SandboxResult(
            success=result.success,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            duration_seconds=result.duration_seconds,
            timed_out=result.timed_out,
            cleanup_status="removed",
        )


class MetadataRepairModel:
    def __init__(self) -> None:
        self.calls = 0
        self.usage = ModelUsage(
            request_count=4,
            input_tokens=100,
            output_tokens=20,
            total_tokens=120,
            duration_seconds=0.01,
            estimated_cost_usd=0.0001,
        )

    def next_action(self, context: AgentContext):
        self.calls += 1
        if self.calls == 1:
            return ReadFileAction("calculator.py")
        if self.calls == 2:
            metadata = context.observations[-1].metadata
            return PatchAction((PatchOperation(
                path="calculator.py",
                old_content="return a * b",
                new_content="return a / b",
                expected_sha256=str(metadata["sha256"]),
            ),))
        if self.calls == 3:
            return RunTestsAction()
        return FinishAction("fixed")


@pytest.fixture
def eval_task(tmp_path: Path) -> EvalTask:
    repository = tmp_path / "task"
    (repository / "tests").mkdir(parents=True)
    (repository / "calculator.py").write_text(
        "def divide(a, b):\n    return a * b\n", encoding="utf-8"
    )
    (repository / "notes.txt").write_text("leave me alone\n", encoding="utf-8")
    (repository / "tests" / "test_calculator.py").write_text(
        "from calculator import divide\n\n"
        "def test_divide():\n    assert divide(6, 3) == 2\n",
        encoding="utf-8",
    )
    return EvalTask(
        task_id="divide",
        repository=repository,
        issue="divide should return quotient",
        allowed_files=("calculator.py",),
    )


def test_bundled_suite_has_ten_fixed_failing_tasks() -> None:
    project_root = Path(__file__).resolve().parents[1]
    tasks = load_eval_suite(project_root / "evals" / "suite.json")
    runner = TrustedRunnerAdapter()

    assert len(tasks) == 10
    assert len({task.task_id for task in tasks}) == 10
    for task in tasks:
        result = runner.run_tests(task.repository, task.test_path, timeout=10)
        assert result.exit_code == 1, task.task_id


def test_eval_runner_repeats_three_times_and_aggregates_success(
    eval_task: EvalTask, tmp_path: Path
) -> None:
    original = (eval_task.repository / "calculator.py").read_bytes()
    runner = EvalRunner(
        lambda task, run_number: MetadataRepairModel(),
        test_runner_factory=TrustedRunnerAdapter,
    )

    report = runner.run([eval_task], runs_per_task=3, suite_path="suite.json")
    json_path, markdown_path = write_eval_reports(report, tmp_path / "reports")

    assert report.aggregate.total_runs == 3
    assert report.aggregate.repair_success_rate == 1.0
    assert report.aggregate.test_pass_rate == 1.0
    assert report.aggregate.average_tool_calls == 4.0
    assert report.aggregate.total_tokens == 360
    assert report.aggregate.total_cost_usd == pytest.approx(0.0003)
    assert report.aggregate.unrelated_file_changes == 0
    assert all(record.original_unchanged for record in report.records)
    assert (eval_task.repository / "calculator.py").read_bytes() == original
    decoded = json.loads(json_path.read_text(encoding="utf-8"))
    assert decoded["aggregate"]["repair_success_rate"] == 1.0
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "Repair success rate | 100.0%" in markdown
    assert "return a / b" not in markdown


def test_unrelated_file_change_prevents_strict_repair_success(
    eval_task: EvalTask,
) -> None:
    calculator = (eval_task.repository / "calculator.py").read_text(encoding="utf-8")
    notes = (eval_task.repository / "notes.txt").read_text(encoding="utf-8")
    model = ScriptedModel([
        PatchAction((
            PatchOperation(
                "calculator.py",
                "return a * b",
                "return a / b",
                hashlib.sha256(calculator.encode()).hexdigest(),
            ),
            PatchOperation(
                "notes.txt",
                "leave me alone",
                "unrelated change",
                hashlib.sha256(notes.encode()).hexdigest(),
            ),
        )),
        RunTestsAction(),
        FinishAction(),
    ])
    runner = EvalRunner(
        lambda task, run_number: model,
        test_runner_factory=TrustedRunnerAdapter,
    )

    report = runner.run([eval_task], runs_per_task=1)

    record = report.records[0]
    assert record.agent_success is True
    assert record.tests_passed is True
    assert record.repair_success is False
    assert record.unrelated_files == ("notes.txt",)
    assert report.aggregate.unrelated_file_changes == 1


def test_suite_rejects_repository_path_escape(tmp_path: Path) -> None:
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps({
        "version": 1,
        "tasks": [{
            "id": "escape",
            "repository": "../outside",
            "issue": "bad",
            "allowed_files": ["x.py"],
            "test_path": "tests",
        }],
    }), encoding="utf-8")

    with pytest.raises(EvalValidationError):
        load_eval_suite(suite)


def test_eval_counts_timeout_patch_conflict_and_security_blocks(
    eval_task: EvalTask,
) -> None:
    class TimeoutRunner:
        def run_tests(self, *args: object, **kwargs: object) -> SandboxResult:
            del args, kwargs
            return SandboxResult(
                success=False,
                timed_out=True,
                cleanup_status="removed",
                stderr="timed out",
            )

    timeout_report = EvalRunner(
        lambda task, run_number: ScriptedModel([]),
        test_runner_factory=TimeoutRunner,
    ).run([eval_task], runs_per_task=1)

    conflict_model = ScriptedModel([
        PatchAction((PatchOperation(
            "calculator.py", "return a * b", "return a / b", "0" * 64
        ),)),
    ])
    conflict_report = EvalRunner(
        lambda task, run_number: conflict_model,
        test_runner_factory=TrustedRunnerAdapter,
    ).run([eval_task], runs_per_task=1)

    class UnsafeModel:
        def next_action(self, context: AgentContext):
            del context
            raise InvalidModelActionError("shell action rejected")

    security_report = EvalRunner(
        lambda task, run_number: UnsafeModel(),
        test_runner_factory=TrustedRunnerAdapter,
    ).run([eval_task], runs_per_task=1)

    assert timeout_report.aggregate.timeout_count == 1
    assert conflict_report.aggregate.patch_conflict_count == 1
    assert security_report.aggregate.security_block_count == 1
