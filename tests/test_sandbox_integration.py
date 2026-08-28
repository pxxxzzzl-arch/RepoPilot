"""Real Docker acceptance tests, skipped when the sandbox image is unavailable."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from issue2patch.sandbox import DEFAULT_SANDBOX_IMAGE, DockerSandboxRunner


@pytest.fixture(scope="module")
def docker_runner() -> DockerSandboxRunner:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI is unavailable")

    try:
        daemon = subprocess.run(
            [docker, "info"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        image = subprocess.run(
            [docker, "image", "inspect", DEFAULT_SANDBOX_IMAGE],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        pytest.skip(f"Docker cannot be inspected: {error}")
    if daemon.returncode != 0:
        pytest.skip("Docker daemon is unavailable")
    if image.returncode != 0:
        pytest.skip(f"sandbox image {DEFAULT_SANDBOX_IMAGE!r} is unavailable")
    return DockerSandboxRunner(docker_binary=docker)


def _repository(tmp_path: Path, test_body: str) -> Path:
    root = tmp_path / "repository"
    tests = root / "tests"
    tests.mkdir(parents=True)
    (tests / "test_sandbox.py").write_text(test_body, encoding="utf-8")
    return root


def test_real_container_reports_passing_tests(
    docker_runner: DockerSandboxRunner, tmp_path: Path
) -> None:
    repository = _repository(tmp_path, "def test_passes():\n    assert True\n")

    result = docker_runner.run_tests(repository, timeout=20)

    assert result.success is True
    assert result.exit_code == 0
    assert "1 passed" in result.stdout
    assert result.cleanup_status == "removed"


def test_real_container_reports_test_failure(
    docker_runner: DockerSandboxRunner, tmp_path: Path
) -> None:
    repository = _repository(tmp_path, "def test_fails():\n    assert False\n")

    result = docker_runner.run_tests(repository, timeout=20)

    assert result.success is False
    assert result.exit_code == 1
    assert "1 failed" in result.stdout
    assert result.cleanup_status == "removed"


def test_malicious_test_cannot_escape_sandbox(
    docker_runner: DockerSandboxRunner, tmp_path: Path
) -> None:
    test_body = """\
import os
import socket
from pathlib import Path


def test_security_boundaries():
    assert os.geteuid() != 0

    try:
        os.setuid(0)
    except PermissionError:
        pass
    else:
        raise AssertionError("process gained root privileges")

    try:
        socket.create_connection(("1.1.1.1", 53), timeout=0.2)
    except OSError:
        pass
    else:
        raise AssertionError("container reached the network")

    for path in (Path("/workspace/escape.txt"), Path("/rootfs-escape.txt")):
        try:
            path.write_text("escape", encoding="utf-8")
        except OSError:
            pass
        else:
            raise AssertionError(f"container wrote to {path}")
"""
    repository = _repository(tmp_path, test_body)

    result = docker_runner.run_tests(repository, timeout=20)

    assert result.success is True
    assert result.cleanup_status == "removed"
    assert not (repository / "escape.txt").exists()


def test_infinite_loop_is_killed_and_container_removed(
    docker_runner: DockerSandboxRunner, tmp_path: Path
) -> None:
    repository = _repository(
        tmp_path,
        "def test_forever():\n    while True:\n        pass\n",
    )

    result = docker_runner.run_tests(repository, timeout=1)

    assert result.success is False
    assert result.timed_out is True
    assert result.exit_code is None
    assert result.cleanup_status == "removed"

