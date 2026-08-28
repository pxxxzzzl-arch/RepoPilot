"""Read-only tools used to inspect and test a repository."""

from __future__ import annotations

import os
import hashlib
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Mapping, Sequence


DEFAULT_SEARCH_TIMEOUT = 5.0
DEFAULT_MAX_SEARCH_RESULTS = 100
DEFAULT_MAX_SEARCH_FILES = 2_000
DEFAULT_MAX_SEARCH_FILE_BYTES = 2_000_000
DEFAULT_MAX_SEARCH_TOTAL_BYTES = 20_000_000


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Structured outcome returned by every Issue2Patch tool."""

    tool: str
    success: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    duration_seconds: float = 0.0
    metadata: Mapping[str, object] = field(default_factory=dict)


class PathSecurityError(ValueError):
    """Raised when a requested path is not safely contained by a repository."""


def _repository_root(repo_root: str | Path) -> Path:
    root = Path(repo_root)
    if root.is_symlink():
        raise PathSecurityError("repository root must not be a symbolic link")

    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise PathSecurityError(f"repository root is unavailable: {error}") from error

    if not resolved.is_dir():
        raise PathSecurityError("repository root must be a directory")
    return resolved


def _safe_path(
    repo_root: str | Path,
    requested_path: str | Path,
    *,
    must_exist: bool = True,
) -> tuple[Path, Path]:
    root = _repository_root(repo_root)
    relative = Path(requested_path)

    if relative.is_absolute() or PureWindowsPath(str(requested_path)).is_absolute():
        raise PathSecurityError("absolute paths are not allowed")
    if ".." in relative.parts:
        raise PathSecurityError("parent path components ('..') are not allowed")

    candidate = root.joinpath(relative)
    current = root
    for part in relative.parts:
        if part in ("", "."):
            continue
        current = current / part
        if current.is_symlink():
            raise PathSecurityError("symbolic links are not allowed")

    try:
        resolved = candidate.resolve(strict=must_exist)
    except (OSError, RuntimeError) as error:
        raise PathSecurityError(f"path is unavailable: {error}") from error

    if not resolved.is_relative_to(root):
        raise PathSecurityError("path escapes the repository root")
    if must_exist and not resolved.exists():
        raise PathSecurityError("path does not exist")
    return root, resolved


def _failure(tool: str, message: str, *, duration: float = 0.0) -> ExecutionResult:
    return ExecutionResult(
        tool=tool,
        success=False,
        stderr=message,
        duration_seconds=duration,
    )


def _find_unsafe_symlink(root: Path) -> Path | None:
    """Find a broken symlink or one whose target escapes ``root``."""
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        directory_names[:] = [name for name in directory_names if name != ".git"]
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


def _run_subprocess(
    tool: str,
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
    env: Mapping[str, str] | None = None,
) -> ExecutionResult:
    if timeout <= 0:
        return _failure(tool, "timeout must be greater than zero")

    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
            env=env,
        )
    except subprocess.TimeoutExpired as error:
        duration = time.monotonic() - started
        stdout = _timeout_text(error.stdout)
        stderr = _timeout_text(error.stderr)
        timeout_message = f"command timed out after {timeout:g} seconds"
        stderr = f"{stderr}\n{timeout_message}".strip()
        return ExecutionResult(
            tool=tool,
            success=False,
            stdout=stdout,
            stderr=stderr,
            exit_code=None,
            timed_out=True,
            duration_seconds=duration,
        )
    except OSError as error:
        return _failure(tool, f"unable to start command: {error}", duration=time.monotonic() - started)

    return ExecutionResult(
        tool=tool,
        success=completed.returncode == 0,
        stdout=completed.stdout,
        stderr=completed.stderr,
        exit_code=completed.returncode,
        duration_seconds=time.monotonic() - started,
    )


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def read_file(repo_root: str | Path, path: str | Path) -> ExecutionResult:
    """Read one UTF-8 text file without allowing repository escape."""
    tool = "read_file"
    started = time.monotonic()
    try:
        root, safe_file = _safe_path(repo_root, path)
        if not safe_file.is_file():
            return _failure(tool, "requested path is not a file", duration=time.monotonic() - started)
        raw_content = safe_file.read_bytes()
        content = raw_content.decode("utf-8")
    except (PathSecurityError, OSError, UnicodeError) as error:
        return _failure(tool, str(error), duration=time.monotonic() - started)

    return ExecutionResult(
        tool=tool,
        success=True,
        stdout=content,
        exit_code=0,
        duration_seconds=time.monotonic() - started,
        metadata={
            "path": safe_file.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(raw_content).hexdigest(),
            "bytes": len(raw_content),
        },
    )


def search_code(
    repo_root: str | Path,
    pattern: str,
    path: str | Path = ".",
    *,
    regex: bool = True,
    timeout: float = DEFAULT_SEARCH_TIMEOUT,
    max_results: int = DEFAULT_MAX_SEARCH_RESULTS,
    max_files: int = DEFAULT_MAX_SEARCH_FILES,
    max_file_bytes: int = DEFAULT_MAX_SEARCH_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_SEARCH_TOTAL_BYTES,
) -> ExecutionResult:
    """Search repository files in a time- and resource-bounded subprocess.

    Symbolic links and Git metadata are never traversed. Callers should prefer
    fixed-string mode (``regex=False``) for untrusted model input.
    """
    tool = "search_code"
    started = time.monotonic()
    if timeout <= 0:
        return _failure(tool, "timeout must be greater than zero")
    limits = {
        "max_results": max_results,
        "max_files": max_files,
        "max_file_bytes": max_file_bytes,
        "max_total_bytes": max_total_bytes,
    }
    invalid = next((name for name, value in limits.items() if value <= 0), None)
    if invalid:
        return _failure(tool, f"{invalid} must be greater than zero")
    try:
        root, search_root = _safe_path(repo_root, path)
    except PathSecurityError as error:
        return _failure(tool, str(error), duration=time.monotonic() - started)

    if not search_root.is_dir():
        return _failure(tool, "search path is not a directory", duration=time.monotonic() - started)

    command = [
        sys.executable,
        "-m",
        "issue2patch._search_worker",
        str(root),
        str(search_root),
        pattern,
        "regex" if regex else "fixed",
        str(max_results),
        str(max_files),
        str(max_file_bytes),
        str(max_total_bytes),
    ]
    return _run_subprocess(
        tool,
        command,
        cwd=root,
        timeout=timeout,
    )


def run_tests_trusted(
    repo_root: str | Path,
    path: str | Path = "tests",
    *,
    timeout: float = 60.0,
) -> ExecutionResult:
    """Run pytest locally for a repository that is already trusted."""
    tool = "run_tests_trusted"
    try:
        root, test_path = _safe_path(repo_root, path)
    except PathSecurityError as error:
        return _failure(tool, str(error))

    unsafe_symlink = _find_unsafe_symlink(root)
    if unsafe_symlink is not None:
        relative_symlink = unsafe_symlink.relative_to(root).as_posix()
        return _failure(tool, f"unsafe symbolic link in repository: {relative_symlink}")

    relative_test_path = test_path.relative_to(root).as_posix() or "."
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return _run_subprocess(
        tool,
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", relative_test_path],
        cwd=root,
        timeout=timeout,
        env=environment,
    )


def run_tests(
    repo_root: str | Path,
    path: str | Path = "tests",
    *,
    timeout: float = 60.0,
):
    """Run untrusted repository tests in the default Docker sandbox."""
    from issue2patch.sandbox import DockerSandboxRunner

    return DockerSandboxRunner().run_tests(repo_root, path, timeout=timeout)


def git_diff(repo_root: str | Path, *, timeout: float = 10.0) -> ExecutionResult:
    """Return the unstaged Git diff for a repository with local metadata."""
    tool = "git_diff"
    try:
        root = _repository_root(repo_root)
        _, git_metadata = _safe_path(root, ".git")
    except PathSecurityError as error:
        return _failure(tool, str(error))

    if not git_metadata.is_dir():
        return _failure(tool, ".git must be a directory inside the repository root")

    environment = os.environ.copy()
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_PAGER"] = "cat"
    return _run_subprocess(
        tool,
        ["git", "diff", "--no-ext-diff", "--no-textconv", "--"],
        cwd=root,
        timeout=timeout,
        env=environment,
    )


__all__ = [
    "ExecutionResult",
    "git_diff",
    "read_file",
    "run_tests",
    "run_tests_trusted",
    "search_code",
]
