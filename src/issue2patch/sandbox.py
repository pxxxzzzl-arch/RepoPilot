"""Docker-backed test execution for untrusted repositories."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from issue2patch.tools import (
    PathSecurityError,
    _repository_root,
    _safe_path,
)

DEFAULT_SANDBOX_IMAGE = "issue2patch-sandbox:py311"
DEFAULT_CPU_LIMIT = 1.0
DEFAULT_MEMORY_LIMIT = "256m"
DEFAULT_PID_LIMIT = 64
DEFAULT_TMPFS_SIZE = "64m"
_SIZE_PATTERN = re.compile(r"[1-9][0-9]*[kmgt]", re.IGNORECASE)
_IMAGE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*")
_IGNORED_DIRECTORIES = {
    ".git",
    ".issue2patch",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
}


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """Structured result of one isolated container test run."""

    success: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    duration_seconds: float = 0.0
    timed_out: bool = False
    cleanup_status: str = "not_created"
    container_name: str = ""
    tool: str = "run_tests"


class DockerSandboxRunner:
    """Run pytest in a locked-down, disposable Docker container."""

    def __init__(
        self,
        *,
        image: str = DEFAULT_SANDBOX_IMAGE,
        docker_binary: str = "docker",
        cpu_limit: float = DEFAULT_CPU_LIMIT,
        memory_limit: str = DEFAULT_MEMORY_LIMIT,
        pid_limit: int = DEFAULT_PID_LIMIT,
        tmpfs_size: str = DEFAULT_TMPFS_SIZE,
        cleanup_timeout: float = 10.0,
    ) -> None:
        if not _IMAGE_PATTERN.fullmatch(image) or image.startswith("-"):
            raise ValueError("invalid Docker image name")
        if not docker_binary:
            raise ValueError("docker_binary must not be empty")
        if cpu_limit <= 0:
            raise ValueError("cpu_limit must be greater than zero")
        if not _SIZE_PATTERN.fullmatch(memory_limit):
            raise ValueError("memory_limit must use a positive k/m/g/t suffix")
        if pid_limit <= 0:
            raise ValueError("pid_limit must be greater than zero")
        if not _SIZE_PATTERN.fullmatch(tmpfs_size):
            raise ValueError("tmpfs_size must use a positive k/m/g/t suffix")
        if cleanup_timeout <= 0:
            raise ValueError("cleanup_timeout must be greater than zero")

        self.image = image
        self.docker_binary = docker_binary
        self.cpu_limit = cpu_limit
        self.memory_limit = memory_limit
        self.pid_limit = pid_limit
        self.tmpfs_size = tmpfs_size
        self.cleanup_timeout = cleanup_timeout

    def build_command(
        self,
        workspace: str | Path,
        test_path: str,
        container_name: str,
    ) -> list[str]:
        """Build the complete Docker CLI command without invoking it."""
        mount = (
            f"type=bind,source={Path(workspace).resolve()},"
            "target=/workspace,readonly"
        )
        return [
            self.docker_binary,
            "run",
            "--name",
            container_name,
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--user",
            "65532:65532",
            "--cpus",
            f"{self.cpu_limit:g}",
            "--memory",
            self.memory_limit,
            "--memory-swap",
            self.memory_limit,
            "--pids-limit",
            str(self.pid_limit),
            "--shm-size",
            self.tmpfs_size,
            "--ulimit",
            "nofile=1024:1024",
            "--tmpfs",
            (
                "/tmp:rw,noexec,nosuid,nodev,"
                f"size={self.tmpfs_size},mode=1777"
            ),
            "--mount",
            mount,
            "--workdir",
            "/workspace",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            self.image,
            test_path,
        ]

    def run_tests(
        self,
        repo_root: str | Path,
        path: str | Path = "tests",
        *,
        timeout: float = 60.0,
    ) -> SandboxResult:
        """Copy a repository and run its tests in an isolated container."""
        started = time.monotonic()
        container_name = f"issue2patch-{secrets.token_hex(16)}"
        if timeout <= 0:
            return self._result(
                started,
                container_name,
                stderr="timeout must be greater than zero",
            )

        try:
            root = _repository_root(repo_root)
            _, test_path = _safe_path(root, path)
        except PathSecurityError as error:
            return self._result(started, container_name, stderr=str(error))

        unsafe_symlink = self._find_unsafe_copied_symlink(root)
        if unsafe_symlink is not None:
            relative = unsafe_symlink.relative_to(root).as_posix()
            return self._result(
                started,
                container_name,
                stderr=f"unsafe symbolic link in repository: {relative}",
            )

        relative_test_path = test_path.relative_to(root).as_posix() or "."
        container_test_path = (
            "." if relative_test_path == "." else f"./{relative_test_path}"
        )
        try:
            with tempfile.TemporaryDirectory(prefix="issue2patch-sandbox-") as temporary:
                workspace = Path(temporary) / "repository"
                self._copy_repository(root, workspace)
                try:
                    command = self.build_command(
                        workspace, container_test_path, container_name
                    )
                    return self._execute(command, container_name, started, timeout)
                finally:
                    self._make_writable_for_cleanup(workspace)
        except OSError as error:
            return self._result(
                started,
                container_name,
                stderr=f"unable to prepare sandbox workspace: {error}",
            )

    def _execute(
        self,
        command: Sequence[str],
        container_name: str,
        started: float,
        timeout: float,
    ) -> SandboxResult:
        try:
            completed = subprocess.run(
                list(command),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                shell=False,
            )
            stdout = completed.stdout
            stderr = completed.stderr
            exit_code = completed.returncode
            timed_out = False
        except subprocess.TimeoutExpired as error:
            stdout = self._timeout_text(error.stdout)
            stderr = self._timeout_text(error.stderr)
            stderr = f"{stderr}\ncontainer timed out after {timeout:g} seconds".strip()
            exit_code = None
            timed_out = True
        except FileNotFoundError as error:
            return self._result(
                started,
                container_name,
                stderr=f"Docker is unavailable: {error}",
                cleanup_status="not_created",
            )
        except OSError as error:
            return self._result(
                started,
                container_name,
                stderr=f"unable to start Docker: {error}",
                cleanup_status="not_created",
            )

        cleanup_status, cleanup_error = self._force_remove(container_name)
        if cleanup_error:
            stderr = f"{stderr}\n{cleanup_error}".strip()
        success = exit_code == 0 and not timed_out and cleanup_status == "removed"
        return self._result(
            started,
            container_name,
            success=success,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            timed_out=timed_out,
            cleanup_status=cleanup_status,
        )

    def _force_remove(self, container_name: str) -> tuple[str, str]:
        try:
            completed = subprocess.run(
                [self.docker_binary, "rm", "--force", container_name],
                capture_output=True,
                text=True,
                timeout=self.cleanup_timeout,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return "failed", f"container cleanup failed: {error}"
        if completed.returncode != 0:
            detail = completed.stderr.strip() or "docker rm returned a non-zero exit code"
            return "failed", f"container cleanup failed: {detail}"
        return "removed", ""

    @staticmethod
    def _copy_repository(source: Path, destination: Path) -> None:
        ignored = shutil.ignore_patterns(
            *_IGNORED_DIRECTORIES,
            "*.pyc",
        )
        shutil.copytree(source, destination, symlinks=True, ignore=ignored)
        for directory, directory_names, file_names in os.walk(
            destination, followlinks=False
        ):
            directory_path = Path(directory)
            if not directory_path.is_symlink():
                directory_path.chmod(0o555)
            for name in (*directory_names, *file_names):
                candidate = directory_path / name
                if candidate.is_symlink():
                    continue
                current_mode = stat.S_IMODE(candidate.stat().st_mode)
                if candidate.is_dir():
                    candidate.chmod(0o555)
                elif current_mode & 0o111:
                    candidate.chmod(0o555)
                else:
                    candidate.chmod(0o444)

    @staticmethod
    def _find_unsafe_copied_symlink(root: Path) -> Path | None:
        for directory, directory_names, file_names in os.walk(
            root, followlinks=False
        ):
            directory_path = Path(directory)
            directory_names[:] = [
                name for name in directory_names if name not in _IGNORED_DIRECTORIES
            ]
            for name in (*directory_names, *file_names):
                candidate = directory_path / name
                if not candidate.is_symlink():
                    continue
                try:
                    target = candidate.resolve(strict=True)
                except (OSError, RuntimeError):
                    return candidate
                if not target.is_relative_to(root):
                    return candidate
        return None

    @staticmethod
    def _make_writable_for_cleanup(workspace: Path) -> None:
        if not workspace.exists():
            return
        for directory, directory_names, file_names in os.walk(
            workspace, topdown=False, followlinks=False
        ):
            directory_path = Path(directory)
            for name in (*directory_names, *file_names):
                candidate = directory_path / name
                if not candidate.is_symlink():
                    candidate.chmod(0o700 if candidate.is_dir() else 0o600)
            if not directory_path.is_symlink():
                directory_path.chmod(0o700)

    @staticmethod
    def _timeout_text(value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode(errors="replace")
        return value

    @staticmethod
    def _result(
        started: float,
        container_name: str,
        *,
        success: bool = False,
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = None,
        timed_out: bool = False,
        cleanup_status: str = "not_created",
    ) -> SandboxResult:
        return SandboxResult(
            success=success,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            duration_seconds=time.monotonic() - started,
            timed_out=timed_out,
            cleanup_status=cleanup_status,
            container_name=container_name,
        )


__all__ = [
    "DEFAULT_CPU_LIMIT",
    "DEFAULT_MEMORY_LIMIT",
    "DEFAULT_PID_LIMIT",
    "DEFAULT_SANDBOX_IMAGE",
    "DEFAULT_TMPFS_SIZE",
    "DockerSandboxRunner",
    "SandboxResult",
]
