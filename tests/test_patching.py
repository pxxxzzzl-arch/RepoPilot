"""Tests for safe, atomic, and auditable repository patching."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import issue2patch.patching as patching
from issue2patch import PatchOperation, TraceRecorder, apply_patch


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _operation(path: str, before: str, old: str, new: str) -> PatchOperation:
    return PatchOperation(
        path=path,
        old_content=old,
        new_content=new,
        expected_sha256=_hash(before),
    )


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    return root


def test_apply_patch_modifies_file_and_returns_unified_diff(repository: Path) -> None:
    before = "def divide(a, b):\n    return a * b\n"
    after = "def divide(a, b):\n    return a / b\n"
    target = repository / "calculator.py"
    target.write_text(before, encoding="utf-8")

    result = apply_patch(
        repository,
        [_operation("calculator.py", before, "return a * b", "return a / b")],
    )

    assert result.success is True
    assert result.dry_run is False
    assert target.read_text(encoding="utf-8") == after
    assert "--- a/calculator.py" in result.diff
    assert "+++ b/calculator.py" in result.diff
    assert "-    return a * b" in result.diff
    assert "+    return a / b" in result.diff
    assert result.changes[0].before_sha256 == _hash(before)
    assert result.changes[0].after_sha256 == _hash(after)
    assert result.changes[0].applied is True


def test_apply_patch_dry_run_does_not_modify_file(repository: Path) -> None:
    before = "value = 1\n"
    target = repository / "settings.py"
    target.write_text(before, encoding="utf-8")

    result = apply_patch(
        repository,
        [_operation("settings.py", before, "1", "2")],
        dry_run=True,
    )

    assert result.success is True
    assert result.dry_run is True
    assert target.read_text(encoding="utf-8") == before
    assert "+value = 2" in result.diff
    assert result.changes[0].applied is False


def test_apply_patch_rejects_old_content_mismatch(repository: Path) -> None:
    before = "value = 1\n"
    target = repository / "settings.py"
    target.write_text(before, encoding="utf-8")

    result = apply_patch(
        repository,
        [_operation("settings.py", before, "value = 2", "value = 3")],
    )

    assert result.success is False
    assert "old_content does not match" in result.error
    assert target.read_text(encoding="utf-8") == before


def test_apply_patch_rejects_sha256_conflict(repository: Path) -> None:
    before = "value = 1\n"
    target = repository / "settings.py"
    target.write_text(before, encoding="utf-8")
    operation = PatchOperation(
        path="settings.py",
        old_content="1",
        new_content="2",
        expected_sha256="0" * 64,
    )

    result = apply_patch(repository, [operation])

    assert result.success is False
    assert "expected_sha256 conflict" in result.error
    assert result.changes[0].before_sha256 == _hash(before)
    assert target.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("path", ["../outside.py", "/tmp/outside.py"])
def test_apply_patch_rejects_path_escape(
    repository: Path, tmp_path: Path, path: str
) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text("value = 1\n", encoding="utf-8")
    operation = PatchOperation(path, "1", "2", _hash("value = 1\n"))

    result = apply_patch(repository, [operation])

    assert result.success is False
    assert outside.read_text(encoding="utf-8") == "value = 1\n"


def test_apply_patch_rejects_symbolic_link(repository: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.py"
    before = "value = 1\n"
    outside.write_text(before, encoding="utf-8")
    (repository / "linked.py").symlink_to(outside)

    result = apply_patch(
        repository,
        [_operation("linked.py", before, "1", "2")],
    )

    assert result.success is False
    assert "symbolic link" in result.error
    assert outside.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("path", [".env", "private.pem", ".git/config"])
def test_apply_patch_rejects_sensitive_files(repository: Path, path: str) -> None:
    target = repository / path
    target.parent.mkdir(parents=True, exist_ok=True)
    before = "secret=old\n"
    target.write_text(before, encoding="utf-8")

    result = apply_patch(
        repository,
        [_operation(path, before, "old", "new")],
    )

    assert result.success is False
    assert "sensitive file" in result.error
    assert target.read_text(encoding="utf-8") == before


def test_apply_patch_rejects_file_over_size_limit(repository: Path) -> None:
    before = "0123456789"
    target = repository / "large.py"
    target.write_text(before, encoding="utf-8")

    result = apply_patch(
        repository,
        [_operation("large.py", before, "9", "8")],
        max_file_size=5,
    )

    assert result.success is False
    assert "exceeds 5 byte limit" in result.error
    assert target.read_text(encoding="utf-8") == before


def test_apply_patch_rejects_too_many_operations(repository: Path) -> None:
    operation = PatchOperation("unused.py", "old", "new", "0" * 64)

    result = apply_patch(repository, [operation] * 11)

    assert result.success is False
    assert "operation count 11 exceeds 10 operation limit" in result.error


def test_apply_patch_rejects_total_bytes_over_limit(repository: Path) -> None:
    first_before = "12345"
    second_before = "abcde"
    (repository / "first.py").write_text(first_before, encoding="utf-8")
    (repository / "second.py").write_text(second_before, encoding="utf-8")

    result = apply_patch(
        repository,
        [
            _operation("first.py", first_before, "5", "6"),
            _operation("second.py", second_before, "e", "f"),
        ],
        max_total_bytes=9,
    )

    assert result.success is False
    assert "total patch size exceeds 9 byte limit" in result.error
    assert (repository / "first.py").read_text(encoding="utf-8") == first_before
    assert (repository / "second.py").read_text(encoding="utf-8") == second_before


def test_all_files_are_preflighted_before_any_write(repository: Path) -> None:
    first_before = "first = 1\n"
    second_before = "second = 1\n"
    first = repository / "first.py"
    second = repository / "second.py"
    first.write_text(first_before, encoding="utf-8")
    second.write_text(second_before, encoding="utf-8")

    result = apply_patch(
        repository,
        [
            _operation("first.py", first_before, "1", "2"),
            _operation("second.py", second_before, "missing", "2"),
        ],
    )

    assert result.success is False
    assert first.read_text(encoding="utf-8") == first_before
    assert second.read_text(encoding="utf-8") == second_before


def test_multi_file_write_failure_rolls_back_all_files(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_before = "first = 1\n"
    second_before = "second = 1\n"
    first = repository / "first.py"
    second = repository / "second.py"
    first.write_text(first_before, encoding="utf-8")
    second.write_text(second_before, encoding="utf-8")
    operations = [
        _operation("first.py", first_before, "1", "2"),
        _operation("second.py", second_before, "1", "2"),
    ]

    real_replace = os.replace
    calls = 0

    def fail_second_replace(source: str | Path, destination: str | Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated second-file failure")
        real_replace(source, destination)

    monkeypatch.setattr(patching.os, "replace", fail_second_replace)

    result = apply_patch(repository, operations)

    assert result.success is False
    assert "rollback completed" in result.error
    assert first.read_text(encoding="utf-8") == first_before
    assert second.read_text(encoding="utf-8") == second_before
    assert not list(repository.glob(".*.issue2patch-*"))
    assert all(change.applied is False for change in result.changes)


def test_trace_recorder_logs_metadata_without_file_content(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = "TOKEN_FROM_SOURCE = 'old-secret'\n"
    target = repository / "config.py"
    target.write_text(before, encoding="utf-8")
    trace_path = tmp_path / "trace.jsonl"
    recorder = TraceRecorder(trace_path, run_id="run-123")
    monkeypatch.setenv("ENVIRONMENT_SECRET", "must-not-be-recorded")

    result = apply_patch(
        repository,
        [_operation("config.py", before, "old-secret", "new-secret")],
        trace_recorder=recorder,
    )

    assert result.success is True
    serialized = trace_path.read_text(encoding="utf-8")
    entry = json.loads(serialized)
    assert entry["run_id"] == "run-123"
    assert entry["tool"] == "apply_patch"
    assert entry["success"] is True
    assert entry["duration_seconds"] >= 0
    assert entry["timestamp"].endswith("+00:00")
    assert entry["files"][0]["path"] == "config.py"
    assert entry["files"][0]["before_sha256"] == _hash(before)
    assert entry["files"][0]["applied"] is True
    assert "TOKEN_FROM_SOURCE" not in serialized
    assert "old-secret" not in serialized
    assert "new-secret" not in serialized
    assert "ENVIRONMENT_SECRET" not in serialized
    assert "must-not-be-recorded" not in serialized


def test_trace_recorder_records_failure_summary(repository: Path, tmp_path: Path) -> None:
    before = "value = 1\n"
    (repository / "settings.py").write_text(before, encoding="utf-8")
    trace_path = tmp_path / "trace.jsonl"

    result = apply_patch(
        repository,
        [_operation("settings.py", before, "missing", "2")],
        trace_recorder=TraceRecorder(trace_path, run_id="failed-run"),
    )

    assert result.success is False
    entry = json.loads(trace_path.read_text(encoding="utf-8"))
    assert entry["run_id"] == "failed-run"
    assert entry["success"] is False
    assert "old_content does not match" in entry["error_summary"]
    assert entry["files"][0]["applied"] is False
