"""Append-only JSONL audit tracing for Issue2Patch tools."""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from issue2patch.patching import PatchResult


class TraceRecorder:
    """Write content-free tool metadata as one JSON object per line."""

    def __init__(self, log_path: str | Path, run_id: str | None = None) -> None:
        self.log_path = Path(log_path)
        self.run_id = run_id or uuid.uuid4().hex
        if not self.run_id.strip():
            raise ValueError("run_id must not be empty")

    def record(self, result: PatchResult) -> None:
        """Append a patch result without source content, diff, or environment."""
        entry = {
            "run_id": self.run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool": result.tool,
            "success": result.success,
            "duration_seconds": round(result.duration_seconds, 6),
            "dry_run": result.dry_run,
            "files": [
                {
                    "path": change.path,
                    "expected_sha256": change.expected_sha256,
                    "before_sha256": change.before_sha256,
                    "after_sha256": change.after_sha256,
                    "bytes_before": change.bytes_before,
                    "bytes_after": change.bytes_after,
                    "applied": change.applied,
                }
                for change in result.changes
            ],
            "error_summary": self._error_summary(result.error),
        }
        self._append(entry)

    def record_agent_action(
        self,
        *,
        step: int,
        action_type: str,
        success: bool,
        duration_seconds: float,
        output: str = "",
        error: str = "",
        files: list[dict[str, object]] | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        """Record content-free metadata for one orchestrator action."""
        import hashlib

        output_bytes = output.encode("utf-8")
        self._append(
            {
                "event": "agent_action",
                "step": step,
                "action_type": action_type,
                "success": success,
                "duration_seconds": round(duration_seconds, 6),
                "tool_result": {
                    "output_chars": len(output),
                    "output_sha256": hashlib.sha256(output_bytes).hexdigest(),
                    "error_summary": self._error_summary(error),
                },
                "metadata": self._safe_tool_metadata(metadata),
                "files": files or [],
            }
        )

    def record_agent_termination(
        self,
        *,
        reason: str,
        success: bool,
        duration_seconds: float,
        steps: int,
        tool_calls: int,
        error: str = "",
        model_usage: dict[str, object] | None = None,
    ) -> None:
        """Record the structured reason an orchestrator run ended."""
        self._append(
            {
                "event": "agent_termination",
                "termination_reason": reason,
                "success": success,
                "duration_seconds": round(duration_seconds, 6),
                "steps": steps,
                "tool_calls": tool_calls,
                "error_summary": self._error_summary(error),
                "model_usage": model_usage or {},
            }
        )

    def _append(self, entry: dict[str, object]) -> None:
        entry = {
            "run_id": self.run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **entry,
        }
        serialized = (json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n").encode(
            "utf-8"
        )

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if self.log_path.is_symlink():
            raise OSError("trace log must not be a symbolic link")

        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.log_path, flags, 0o600)
        try:
            os.write(descriptor, serialized)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _error_summary(error: str) -> str:
        summary = " ".join(error.split())
        patterns = (
            re.compile(r"\bsk-[A-Za-z0-9_-]{6,}"),
            re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{6,}"),
            re.compile(r"\bgithub_pat_[A-Za-z0-9_]{6,}"),
            re.compile(
                r"(?i)(api[_ -]?key\s*[:=]\s*)([^\s,;]+)"
            ),
        )
        for pattern in patterns:
            if pattern.groups == 2:
                summary = pattern.sub(r"\1[REDACTED]", summary)
            else:
                summary = pattern.sub("[REDACTED]", summary)
        return summary[:300]

    @staticmethod
    def _safe_tool_metadata(metadata: dict[str, object] | None) -> dict[str, object]:
        """Whitelist content-free read metadata for the audit log."""
        if not metadata:
            return {}
        safe: dict[str, object] = {}
        path = metadata.get("path")
        sha256 = metadata.get("sha256")
        byte_count = metadata.get("bytes")
        if isinstance(path, str):
            safe["path"] = path[:500]
        if isinstance(sha256, str) and re.fullmatch(r"[0-9a-f]{64}", sha256):
            safe["sha256"] = sha256
        if isinstance(byte_count, int) and not isinstance(byte_count, bool) and byte_count >= 0:
            safe["bytes"] = byte_count
        return safe


__all__ = ["TraceRecorder"]
