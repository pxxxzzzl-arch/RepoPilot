"""Tests for Issue2Patch's read-only repository tools."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from issue2patch.tools import (
    ExecutionResult,
    git_diff,
    read_file,
    run_tests_trusted,
    search_code,
)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    (root / "module.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    return root


def test_execution_result_is_immutable() -> None:
    result = ExecutionResult(tool="example", success=True)

    with pytest.raises(FrozenInstanceError):
        result.success = False  # type: ignore[misc]


def test_read_file_returns_text(repository: Path) -> None:
    result = read_file(repository, "module.py")

    assert result.success is True
    assert result.stdout == "def answer():\n    return 42\n"
    assert result.exit_code == 0
    assert result.metadata == {
        "path": "module.py",
        "sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
        "bytes": len(result.stdout.encode()),
    }


def test_read_file_reports_missing_file(repository: Path) -> None:
    result = read_file(repository, "missing.py")

    assert result.success is False
    assert "unavailable" in result.stderr


@pytest.mark.parametrize("path", ["../outside.py", "/etc/passwd", r"C:\\Windows\\system.ini"])
def test_read_file_rejects_parent_and_absolute_paths(repository: Path, path: str) -> None:
    result = read_file(repository, path)

    assert result.success is False
    assert result.stdout == ""


def test_read_file_rejects_symbolic_link(repository: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (repository / "link.txt").symlink_to(outside)

    result = read_file(repository, "link.txt")

    assert result.success is False
    assert "symbolic link" in result.stderr
    assert "secret" not in result.stdout


def test_search_code_returns_relative_matches(repository: Path) -> None:
    nested = repository / "package"
    nested.mkdir()
    (nested / "other.py").write_text("ANSWER = 42\n", encoding="utf-8")

    result = search_code(repository, r"42$")

    assert result.success is True
    assert "module.py:2:    return 42" in result.stdout
    assert "package/other.py:1:ANSWER = 42" in result.stdout


def test_search_code_reports_invalid_regular_expression(repository: Path) -> None:
    result = search_code(repository, "[")

    assert result.success is False
    assert result.stderr


def test_search_code_does_not_follow_symbolic_links(repository: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("UNIQUE_SECRET = True\n", encoding="utf-8")
    (repository / "linked").symlink_to(outside, target_is_directory=True)

    result = search_code(repository, "UNIQUE_SECRET")

    assert result.success is True
    assert result.stdout == ""


def test_search_code_rejects_escaping_search_path(repository: Path) -> None:
    result = search_code(repository, "anything", "../")

    assert result.success is False
    assert "not allowed" in result.stderr


def test_search_code_limits_result_count(repository: Path) -> None:
    (repository / "many.txt").write_text("hit\nhit\nhit\nhit\n", encoding="utf-8")

    result = search_code(repository, "hit", regex=False, max_results=2)

    assert result.success is True
    assert len(result.stdout.splitlines()) == 2


def test_search_code_rejects_oversized_input(repository: Path) -> None:
    (repository / "large.txt").write_text("x" * 100, encoding="utf-8")

    result = search_code(repository, "x", regex=False, max_file_bytes=50)

    assert result.success is False
    assert "byte limit" in result.stderr


def test_search_code_reports_subprocess_timeout(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def timeout(*args: object, **kwargs: object):
        del args, kwargs
        raise subprocess.TimeoutExpired(cmd=["search-worker"], timeout=0.01)

    monkeypatch.setattr(subprocess, "run", timeout)

    result = search_code(repository, "answer", regex=False, timeout=0.01)

    assert result.success is False
    assert result.timed_out is True
    assert "timed out" in result.stderr


def _write_test(repository: Path, body: str) -> None:
    tests = repository / "tests"
    tests.mkdir()
    (tests / "test_example.py").write_text(body, encoding="utf-8")


def test_run_tests_reports_passing_suite(repository: Path) -> None:
    _write_test(repository, "def test_passes():\n    assert True\n")

    result = run_tests_trusted(repository, timeout=10)

    assert result.success is True
    assert result.exit_code == 0
    assert "1 passed" in result.stdout


def test_run_tests_reports_failing_suite(repository: Path) -> None:
    _write_test(repository, "def test_fails():\n    assert False\n")

    result = run_tests_trusted(repository, timeout=10)

    assert result.success is False
    assert result.exit_code == 1
    assert "1 failed" in result.stdout


def test_run_tests_reports_timeout(repository: Path) -> None:
    _write_test(repository, "import time\n\ndef test_slow():\n    time.sleep(2)\n")

    result = run_tests_trusted(repository, timeout=0.05)

    assert result.success is False
    assert result.timed_out is True
    assert result.exit_code is None
    assert "timed out" in result.stderr


@pytest.mark.parametrize("path", ["../tests", "/tmp/tests"])
def test_run_tests_rejects_escaping_test_path(repository: Path, path: str) -> None:
    result = run_tests_trusted(repository, path)

    assert result.success is False
    assert result.exit_code is None


def test_run_tests_rejects_symbolic_link_path(repository: Path, tmp_path: Path) -> None:
    outside_tests = tmp_path / "outside_tests"
    outside_tests.mkdir()
    (repository / "tests").symlink_to(outside_tests, target_is_directory=True)

    result = run_tests_trusted(repository)

    assert result.success is False
    assert "symbolic link" in result.stderr


def test_run_tests_rejects_nested_escaping_symbolic_link(
    repository: Path, tmp_path: Path
) -> None:
    _write_test(repository, "def test_passes():\n    assert True\n")
    outside_test = tmp_path / "outside_test.py"
    outside_test.write_text("def test_outside():\n    assert True\n", encoding="utf-8")
    (repository / "tests" / "test_link.py").symlink_to(outside_test)

    result = run_tests_trusted(repository)

    assert result.success is False
    assert "unsafe symbolic link" in result.stderr
    assert "tests/test_link.py" in result.stderr


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_git_diff_returns_unstaged_changes(repository: Path) -> None:
    _git(repository, "init", "-q")
    _git(repository, "add", "module.py")
    (repository / "module.py").write_text("def answer():\n    return 43\n", encoding="utf-8")

    result = git_diff(repository)

    assert result.success is True
    assert result.exit_code == 0
    assert "-    return 42" in result.stdout
    assert "+    return 43" in result.stdout


def test_git_diff_reports_non_repository(repository: Path) -> None:
    result = git_diff(repository)

    assert result.success is False
    assert ".git" in result.stderr


def test_git_diff_rejects_symbolic_git_metadata(repository: Path, tmp_path: Path) -> None:
    outside_git = tmp_path / "outside.git"
    outside_git.mkdir()
    (repository / ".git").symlink_to(outside_git, target_is_directory=True)

    result = git_diff(repository)

    assert result.success is False
    assert "symbolic link" in result.stderr
