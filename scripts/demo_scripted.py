#!/usr/bin/env python3
"""Run the public demo path with a deterministic model and real Docker tests."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from issue2patch import (
    AgentContext,
    AgentOrchestrator,
    AgentProgressEvent,
    FinishAction,
    PatchAction,
    PatchOperation,
    ReadFileAction,
    RunTestsAction,
)


class CalculatorRepairModel:
    """Choose deterministic actions while consuming real read metadata."""

    def next_action(self, context: AgentContext):
        if context.step == 1:
            return ReadFileAction("calculator.py")
        if context.step == 2:
            metadata = context.observations[-1].metadata
            return PatchAction((PatchOperation(
                path="calculator.py",
                old_content="return a * b",
                new_content="return a / b",
                expected_sha256=str(metadata["sha256"]),
            ),))
        if context.step == 3:
            return RunTestsAction()
        return FinishAction("Docker tests passed")


def show_progress(event: AgentProgressEvent) -> None:
    marker = "OK" if event.success else "INFO"
    print(f"[{event.event_type:8}] step={event.step} {marker} {event.summary}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("repo", type=Path)
    args = parser.parse_args()
    target = args.repo.resolve(strict=True)
    calculator = target / "calculator.py"
    before = hashlib.sha256(calculator.read_bytes()).hexdigest()

    result = AgentOrchestrator(
        CalculatorRepairModel(), progress_callback=show_progress
    ).run(target, issue="divide should return quotient")

    after = hashlib.sha256(calculator.read_bytes()).hexdigest()
    print(f"status={result.status.value}")
    print(f"tests_passed={result.last_test_passed}")
    print(f"original_unchanged={before == after}")
    print("\n--- reviewable diff from temporary copy ---")
    print(result.diff.rstrip())
    return 0 if result.success and before == after else 1


if __name__ == "__main__":
    raise SystemExit(main())
