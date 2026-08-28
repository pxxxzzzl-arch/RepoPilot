"""CLI approval, progress, and temporary-diff tests."""

from __future__ import annotations

import hashlib
import json
from io import StringIO
from pathlib import Path

import pytest

from issue2patch import (
    FinishAction,
    PatchAction,
    PatchOperation,
    ReadFileAction,
    RunTestsAction,
    ScriptedModel,
)
from issue2patch.cli import main
from issue2patch.sandbox import SandboxResult


@pytest.fixture
def broken_repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    (repository / "tests").mkdir(parents=True)
    before = "def divide(a, b):\n    return a * b\n"
    (repository / "calculator.py").write_text(before, encoding="utf-8")
    (repository / "tests" / "test_calculator.py").write_text(
        "def test_divide():\n    assert True\n", encoding="utf-8"
    )
    return repository, before


class ContentRunner:
    def run_tests(
        self,
        repo_root: str | Path,
        path: str | Path = "tests",
        *,
        timeout: float = 60.0,
    ) -> SandboxResult:
        del path, timeout
        content = (Path(repo_root) / "calculator.py").read_text(encoding="utf-8")
        passed = "return a / b" in content
        return SandboxResult(
            success=passed,
            stdout="1 passed\n" if passed else "1 failed\n",
            exit_code=0 if passed else 1,
            cleanup_status="removed",
        )


def test_run_requires_separate_explicit_approval(
    broken_repository: tuple[Path, str]
) -> None:
    repository, _ = broken_repository
    factory_called = False

    def factory(_: object):
        nonlocal factory_called
        factory_called = True
        raise AssertionError("must not construct a model without approval")

    stdout = StringIO()
    stderr = StringIO()
    exit_code = main(
        ["run", "--repo", str(repository), "--issue", "fix divide"],
        stdin=StringIO(""),
        stdout=stdout,
        stderr=stderr,
        model_factory=factory,
    )

    assert exit_code == 2
    assert factory_called is False
    assert stdout.getvalue() == ""
    assert "Human approval required" in stderr.getvalue()
    assert "--approve" in stderr.getvalue()


def test_approved_run_streams_progress_and_outputs_only_temporary_diff(
    broken_repository: tuple[Path, str], tmp_path: Path
) -> None:
    repository, before = broken_repository
    operation = PatchOperation(
        path="calculator.py",
        old_content="return a * b",
        new_content="return a / b",
        expected_sha256=hashlib.sha256(before.encode()).hexdigest(),
    )
    model = ScriptedModel(
        [
            ReadFileAction("calculator.py"),
            PatchAction((operation,)),
            RunTestsAction(),
            FinishAction("fixed"),
        ]
    )
    stdout = StringIO()
    stderr = StringIO()
    trace = tmp_path / "run.jsonl"

    exit_code = main(
        [
            "run",
            "--repo",
            str(repository),
            "--issue",
            "divide should return quotient",
            "--approve",
            "--trace",
            str(trace),
        ],
        stdin=StringIO(""),
        stdout=stdout,
        stderr=stderr,
        model_factory=lambda _: model,
        test_runner=ContentRunner(),
    )

    assert exit_code == 0
    assert stdout.getvalue().startswith("--- a/calculator.py")
    assert "Status:" not in stdout.getvalue()
    assert "+    return a / b" in stdout.getvalue()
    assert "[baseline] FAIL" in stderr.getvalue()
    assert "action PatchAction" in stderr.getvalue()
    assert "RunTestsAction OK" in stderr.getvalue()
    assert "Model: requests=" in stderr.getvalue()
    assert (repository / "calculator.py").read_text(encoding="utf-8") == before
    assert trace.exists()


def test_missing_environment_api_key_fails_before_execution(
    broken_repository: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, _ = broken_repository
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    stderr = StringIO()

    exit_code = main(
        [
            "run",
            "--repo",
            str(repository),
            "--issue",
            "fix divide",
            "--approve",
        ],
        stdin=StringIO(""),
        stdout=StringIO(),
        stderr=stderr,
    )

    assert exit_code == 2
    assert "OPENAI_API_KEY is not set" in stderr.getvalue()


def test_eval_cli_runs_repeated_suite_and_writes_both_reports(
    broken_repository: tuple[Path, str], tmp_path: Path
) -> None:
    repository, before = broken_repository
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps({
        "version": 1,
        "tasks": [{
            "id": "divide",
            "repository": repository.name,
            "issue": "divide should return quotient",
            "allowed_files": ["calculator.py"],
            "test_path": "tests",
        }],
    }), encoding="utf-8")
    operation = PatchOperation(
        path="calculator.py",
        old_content="return a * b",
        new_content="return a / b",
        expected_sha256=hashlib.sha256(before.encode()).hexdigest(),
    )
    output = tmp_path / "reports"
    stderr = StringIO()

    def model_factory(task: object, run_number: int, args: object):
        del task, run_number, args
        return ScriptedModel([
            PatchAction((operation,)),
            RunTestsAction(),
            FinishAction("fixed"),
        ])

    exit_code = main(
        [
            "eval",
            "--suite", str(suite),
            "--runs", "2",
            "--output", str(output),
            "--approve",
        ],
        stdin=StringIO(""),
        stdout=StringIO(),
        stderr=stderr,
        eval_model_factory=model_factory,
        test_runner=ContentRunner(),
    )

    assert exit_code == 0
    assert (output / "eval-report.json").exists()
    assert (output / "eval-report.md").exists()
    report = json.loads((output / "eval-report.json").read_text(encoding="utf-8"))
    assert report["aggregate"]["total_runs"] == 2
    assert report["aggregate"]["repair_success_rate"] == 1.0
    assert "total runs: 2" in stderr.getvalue()
    assert "Eval complete" in stderr.getvalue()
    assert (repository / "calculator.py").read_text(encoding="utf-8") == before
