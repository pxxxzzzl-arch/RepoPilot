"""Unit tests for Docker sandbox command and lifecycle behavior."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

import issue2patch.sandbox as sandbox
from issue2patch.sandbox import DockerSandboxRunner
from issue2patch.tools import run_tests


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    tests = root / "tests"
    tests.mkdir(parents=True)
    (tests / "test_example.py").write_text(
        "def test_example():\n    assert True\n", encoding="utf-8"
    )
    return root


def _option(command: list[str], name: str) -> str:
    return command[command.index(name) + 1]


def test_command_disables_network_and_privilege_escalation(tmp_path: Path) -> None:
    runner = DockerSandboxRunner()

    command = runner.build_command(
        tmp_path / "copy", "./tests", "issue2patch-example"
    )

    assert _option(command, "--network") == "none"
    assert _option(command, "--cap-drop") == "ALL"
    assert _option(command, "--security-opt") == "no-new-privileges:true"
    assert _option(command, "--user") == "65532:65532"
    assert _option(command, "--pull") == "never"


def test_command_uses_read_only_filesystems_and_resource_limits(
    tmp_path: Path,
) -> None:
    runner = DockerSandboxRunner(
        cpu_limit=0.5,
        memory_limit="128m",
        pid_limit=32,
        tmpfs_size="16m",
    )

    command = runner.build_command(
        tmp_path / "copy", "./tests", "issue2patch-example"
    )

    assert "--read-only" in command
    assert _option(command, "--cpus") == "0.5"
    assert _option(command, "--memory") == "128m"
    assert _option(command, "--memory-swap") == "128m"
    assert _option(command, "--pids-limit") == "32"
    assert _option(command, "--shm-size") == "16m"
    assert _option(command, "--ulimit") == "nofile=1024:1024"
    assert _option(command, "--tmpfs") == (
        "/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777"
    )
    mount = _option(command, "--mount")
    assert "target=/workspace" in mount
    assert mount.endswith(",readonly")
    assert _option(command, "--workdir") == "/workspace"


def test_normal_run_returns_structured_success_and_uses_copy(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    mounted_sources: list[Path] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1] == "run":
            mount = _option(command, "--mount")
            source = Path(mount.split(",source=", 1)[1].split(",target=", 1)[0])
            mounted_sources.append(source)
            assert command[-1] == "./tests"
            copied_test = source / "tests" / "test_example.py"
            copied_test.chmod(0o600)
            copied_test.write_text("changed copy", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "1 passed\n", "")
        return subprocess.CompletedProcess(command, 0, "removed\n", "")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)

    result = DockerSandboxRunner().run_tests(repository)

    assert result.success is True
    assert result.exit_code == 0
    assert result.stdout == "1 passed\n"
    assert result.stderr == ""
    assert result.timed_out is False
    assert result.cleanup_status == "removed"
    assert re.fullmatch(r"issue2patch-[0-9a-f]{32}", result.container_name)
    assert calls[-1] == ["docker", "rm", "--force", result.container_name]
    assert (repository / "tests" / "test_example.py").read_text(encoding="utf-8").startswith(
        "def test_example"
    )
    assert mounted_sources[0] != repository
    assert not mounted_sources[0].exists()


def test_failing_tests_preserve_exit_code_and_output(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[1] == "run":
            return subprocess.CompletedProcess(
                command, 1, "1 failed\n", "assertion failed\n"
            )
        return subprocess.CompletedProcess(command, 0, "removed\n", "")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)

    result = DockerSandboxRunner().run_tests(repository)

    assert result.success is False
    assert result.exit_code == 1
    assert result.stdout == "1 failed\n"
    assert result.stderr == "assertion failed\n"
    assert result.cleanup_status == "removed"


def test_timeout_force_removes_container(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1] == "run":
            raise subprocess.TimeoutExpired(
                command, 0.01, output="partial output", stderr=""
            )
        return subprocess.CompletedProcess(command, 0, "removed\n", "")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)

    result = DockerSandboxRunner().run_tests(repository, timeout=0.01)

    assert result.success is False
    assert result.exit_code is None
    assert result.timed_out is True
    assert result.stdout == "partial output"
    assert "container timed out" in result.stderr
    assert result.cleanup_status == "removed"
    assert calls[-1] == ["docker", "rm", "--force", result.container_name]


def test_docker_unavailable_returns_structured_failure(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing_docker(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("docker")

    monkeypatch.setattr(sandbox.subprocess, "run", missing_docker)

    result = DockerSandboxRunner().run_tests(repository)

    assert result.success is False
    assert result.exit_code is None
    assert result.timed_out is False
    assert result.cleanup_status == "not_created"
    assert "Docker is unavailable" in result.stderr


def test_ignored_virtualenv_symlinks_do_not_block_sandbox(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    virtualenv_bin = repository / ".venv" / "bin"
    virtualenv_bin.mkdir(parents=True)
    (virtualenv_bin / "python").symlink_to(tmp_path / "external-python")

    def missing_docker(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("docker")

    monkeypatch.setattr(sandbox.subprocess, "run", missing_docker)

    result = DockerSandboxRunner().run_tests(repository)

    assert "Docker is unavailable" in result.stderr
    assert "symbolic link" not in result.stderr


def test_container_names_are_unpredictable_and_unique(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)

    first = DockerSandboxRunner().run_tests(repository)
    second = DockerSandboxRunner().run_tests(repository)

    assert first.container_name != second.container_name
    assert re.fullmatch(r"issue2patch-[0-9a-f]{32}", first.container_name)
    assert re.fullmatch(r"issue2patch-[0-9a-f]{32}", second.container_name)


def test_run_tests_defaults_to_docker_sandbox(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing_docker(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("docker")

    monkeypatch.setattr(sandbox.subprocess, "run", missing_docker)

    result = run_tests(repository)

    assert result.tool == "run_tests"
    assert result.cleanup_status == "not_created"
    assert "Docker is unavailable" in result.stderr
