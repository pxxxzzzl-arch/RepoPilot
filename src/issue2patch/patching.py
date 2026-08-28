"""Safe, auditable code modification primitives."""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

from issue2patch.tools import PathSecurityError, _repository_root, _safe_path
from issue2patch.trace import TraceRecorder

DEFAULT_MAX_FILE_SIZE = 1_000_000
DEFAULT_MAX_OPERATIONS = 10
DEFAULT_MAX_TOTAL_BYTES = 2_000_000
_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}")
_SENSITIVE_NAMES = {
    ".netrc",
    ".npmrc",
    ".pypirc",
    "authorized_keys",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "secrets.json",
}
_SENSITIVE_SUFFIXES = {".key", ".p12", ".pem", ".pfx"}
_SENSITIVE_DIRECTORIES = {".git", ".issue2patch", ".ssh"}


@dataclass(frozen=True, slots=True)
class PatchOperation:
    """One exact replacement guarded by the current file hash."""

    path: str
    old_content: str
    new_content: str
    expected_sha256: str


@dataclass(frozen=True, slots=True)
class FileChange:
    """Content-free metadata describing one attempted file change."""

    path: str
    expected_sha256: str
    before_sha256: str = ""
    after_sha256: str = ""
    bytes_before: int = 0
    bytes_after: int = 0
    applied: bool = False


@dataclass(frozen=True, slots=True)
class PatchResult:
    """Structured result from :func:`apply_patch`."""

    success: bool
    dry_run: bool
    diff: str = ""
    changes: tuple[FileChange, ...] = ()
    error: str = ""
    duration_seconds: float = 0.0
    tool: str = "apply_patch"


@dataclass(frozen=True, slots=True)
class _PreparedChange:
    path: Path
    before: bytes
    after: bytes
    mode: int
    metadata: FileChange


class PatchValidationError(ValueError):
    """Raised when a patch fails preflight validation."""


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _is_sensitive_path(relative_path: Path) -> bool:
    lowered_parts = tuple(part.lower() for part in relative_path.parts)
    if any(part in _SENSITIVE_DIRECTORIES for part in lowered_parts):
        return True

    name = relative_path.name.lower()
    if name == ".env" or name.startswith(".env."):
        return True
    if name in _SENSITIVE_NAMES:
        return True
    return relative_path.suffix.lower() in _SENSITIVE_SUFFIXES


def _result(
    *,
    success: bool,
    dry_run: bool,
    started: float,
    diff: str = "",
    changes: Sequence[FileChange] = (),
    error: str = "",
) -> PatchResult:
    return PatchResult(
        success=success,
        dry_run=dry_run,
        diff=diff,
        changes=tuple(changes),
        error=error,
        duration_seconds=time.monotonic() - started,
    )


def _record(recorder: TraceRecorder | None, result: PatchResult) -> PatchResult:
    if recorder is not None:
        recorder.record(result)
    return result


def _preflight_operation(
    root: Path,
    operation: PatchOperation,
    *,
    max_file_size: int,
) -> _PreparedChange:
    relative = Path(operation.path)
    if _is_sensitive_path(relative):
        raise PatchValidationError(f"sensitive file cannot be modified: {operation.path}")
    if not _SHA256_PATTERN.fullmatch(operation.expected_sha256):
        raise PatchValidationError(f"invalid expected_sha256 for {operation.path}")
    if not operation.old_content:
        raise PatchValidationError(f"old_content must not be empty for {operation.path}")

    _, target = _safe_path(root, operation.path)
    if not target.is_file():
        raise PatchValidationError(f"target is not a regular file: {operation.path}")

    size = target.stat(follow_symlinks=False).st_size
    if size > max_file_size:
        raise PatchValidationError(
            f"file exceeds {max_file_size} byte limit: {operation.path}"
        )

    before = target.read_bytes()
    if len(before) > max_file_size:
        raise PatchValidationError(
            f"file exceeds {max_file_size} byte limit: {operation.path}"
        )
    before_hash = _sha256(before)
    metadata = FileChange(
        path=target.relative_to(root).as_posix(),
        expected_sha256=operation.expected_sha256.lower(),
        before_sha256=before_hash,
        bytes_before=len(before),
    )
    if before_hash != operation.expected_sha256.lower():
        raise _PreflightFailure(
            f"expected_sha256 conflict for {operation.path}", metadata
        )

    try:
        before_text = before.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _PreflightFailure(
            f"target is not valid UTF-8: {operation.path}", metadata
        ) from error

    occurrences = before_text.count(operation.old_content)
    if occurrences == 0:
        raise _PreflightFailure(
            f"old_content does not match {operation.path}", metadata
        )
    if occurrences > 1:
        raise _PreflightFailure(
            f"old_content is ambiguous in {operation.path}: {occurrences} matches",
            metadata,
        )

    after_text = before_text.replace(operation.old_content, operation.new_content, 1)
    after = after_text.encode("utf-8")
    if len(after) > max_file_size:
        raise _PreflightFailure(
            f"patched file exceeds {max_file_size} byte limit: {operation.path}",
            metadata,
        )
    if after == before:
        raise _PreflightFailure(
            f"replacement does not change {operation.path}", metadata
        )

    metadata = replace(
        metadata,
        after_sha256=_sha256(after),
        bytes_after=len(after),
    )
    mode = stat.S_IMODE(target.stat(follow_symlinks=False).st_mode)
    return _PreparedChange(target, before, after, mode, metadata)


class _PreflightFailure(PatchValidationError):
    def __init__(self, message: str, metadata: FileChange) -> None:
        super().__init__(message)
        self.metadata = metadata


def _unified_diff(root: Path, prepared: Sequence[_PreparedChange]) -> str:
    chunks: list[str] = []
    for change in prepared:
        relative = change.path.relative_to(root).as_posix()
        chunks.extend(
            difflib.unified_diff(
                change.before.decode("utf-8").splitlines(keepends=True),
                change.after.decode("utf-8").splitlines(keepends=True),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
            )
        )
    return "".join(chunks)


def _write_temporary(target: Path, content: bytes, mode: int) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.issue2patch-",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        stream = os.fdopen(descriptor, "wb")
        descriptor = -1
        with stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _cleanup(paths: Sequence[Path]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)


def _commit_changes(prepared: Sequence[_PreparedChange]) -> str:
    temporary_files: list[Path] = []
    committed: list[_PreparedChange] = []
    try:
        for change in prepared:
            temporary_files.append(
                _write_temporary(change.path, change.after, change.mode)
            )

        for change, temporary in zip(prepared, temporary_files, strict=True):
            os.replace(temporary, change.path)
            committed.append(change)
    except Exception as error:
        rollback_errors: list[str] = []
        for change in reversed(committed):
            rollback_temp: Path | None = None
            try:
                rollback_temp = _write_temporary(
                    change.path, change.before, change.mode
                )
                os.replace(rollback_temp, change.path)
            except Exception as rollback_error:
                rollback_errors.append(f"{change.metadata.path}: {rollback_error}")
            finally:
                if rollback_temp is not None:
                    rollback_temp.unlink(missing_ok=True)

        detail = f"atomic write failed: {error}"
        if rollback_errors:
            detail += "; rollback errors: " + "; ".join(rollback_errors)
        else:
            detail += "; rollback completed"
        return detail
    finally:
        _cleanup(temporary_files)

    return ""


def apply_patch(
    repo_root: str | Path,
    operations: Sequence[PatchOperation],
    *,
    dry_run: bool = False,
    max_file_size: int = DEFAULT_MAX_FILE_SIZE,
    max_operations: int = DEFAULT_MAX_OPERATIONS,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    trace_recorder: TraceRecorder | None = None,
) -> PatchResult:
    """Validate and atomically apply exact replacements within a repository.

    Every operation is validated before any target file is changed. Writes are
    staged in each target directory and rolled back if a later replacement
    fails.
    """
    started = time.monotonic()
    changes: list[FileChange] = []
    prepared: list[_PreparedChange] = []
    total_bytes = 0

    if not operations:
        return _record(
            trace_recorder,
            _result(
                success=False,
                dry_run=dry_run,
                started=started,
                error="at least one patch operation is required",
            ),
        )
    if max_file_size <= 0:
        return _record(
            trace_recorder,
            _result(
                success=False,
                dry_run=dry_run,
                started=started,
                error="max_file_size must be greater than zero",
            ),
        )
    if max_operations <= 0:
        return _record(
            trace_recorder,
            _result(
                success=False,
                dry_run=dry_run,
                started=started,
                error="max_operations must be greater than zero",
            ),
        )
    if len(operations) > max_operations:
        return _record(
            trace_recorder,
            _result(
                success=False,
                dry_run=dry_run,
                started=started,
                error=(
                    f"operation count {len(operations)} exceeds "
                    f"{max_operations} operation limit"
                ),
            ),
        )
    if max_total_bytes <= 0:
        return _record(
            trace_recorder,
            _result(
                success=False,
                dry_run=dry_run,
                started=started,
                error="max_total_bytes must be greater than zero",
            ),
        )

    try:
        root = _repository_root(repo_root)
        seen_paths: set[str] = set()
        for operation in operations:
            lexical_path = Path(operation.path).as_posix()
            if lexical_path in seen_paths:
                raise PatchValidationError(
                    f"duplicate patch target: {operation.path}"
                )
            seen_paths.add(lexical_path)
            try:
                candidate = _preflight_operation(
                    root, operation, max_file_size=max_file_size
                )
            except _PreflightFailure as error:
                changes.append(error.metadata)
                raise
            candidate_bytes = max(len(candidate.before), len(candidate.after))
            if total_bytes + candidate_bytes > max_total_bytes:
                changes.append(candidate.metadata)
                raise PatchValidationError(
                    f"total patch size exceeds {max_total_bytes} byte limit"
                )
            total_bytes += candidate_bytes
            prepared.append(candidate)
            changes.append(candidate.metadata)
    except (PatchValidationError, PathSecurityError, OSError) as error:
        return _record(
            trace_recorder,
            _result(
                success=False,
                dry_run=dry_run,
                started=started,
                changes=changes,
                error=str(error),
            ),
        )

    diff = _unified_diff(root, prepared)
    if dry_run:
        return _record(
            trace_recorder,
            _result(
                success=True,
                dry_run=True,
                started=started,
                diff=diff,
                changes=changes,
            ),
        )

    try:
        for candidate in prepared:
            _, current_path = _safe_path(root, candidate.metadata.path)
            if _sha256(current_path.read_bytes()) != candidate.metadata.before_sha256:
                raise PatchValidationError(
                    f"file changed after preflight: {candidate.metadata.path}"
                )
    except (PatchValidationError, PathSecurityError, OSError) as error:
        return _record(
            trace_recorder,
            _result(
                success=False,
                dry_run=False,
                started=started,
                diff=diff,
                changes=changes,
                error=str(error),
            ),
        )

    write_error = _commit_changes(prepared)
    if write_error:
        return _record(
            trace_recorder,
            _result(
                success=False,
                dry_run=False,
                started=started,
                diff=diff,
                changes=changes,
                error=write_error,
            ),
        )

    applied_changes = tuple(replace(change, applied=True) for change in changes)
    return _record(
        trace_recorder,
        _result(
            success=True,
            dry_run=False,
            started=started,
            diff=diff,
            changes=applied_changes,
        ),
    )


__all__ = [
    "DEFAULT_MAX_FILE_SIZE",
    "DEFAULT_MAX_OPERATIONS",
    "DEFAULT_MAX_TOTAL_BYTES",
    "FileChange",
    "PatchOperation",
    "PatchResult",
    "apply_patch",
]
