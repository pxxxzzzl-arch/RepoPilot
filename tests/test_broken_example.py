"""Contract tests for the immutable broken demonstration template."""

from __future__ import annotations

import shutil
from pathlib import Path

from issue2patch.tools import run_tests_trusted


def test_broken_example_is_copied_and_fails_without_mutating_template(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[1]
    template = project_root / "examples" / "broken_calculator"
    working_copy = tmp_path / "broken_calculator"
    shutil.copytree(template, working_copy)
    before = (template / "calculator.py").read_bytes()

    result = run_tests_trusted(working_copy, timeout=10)

    assert result.exit_code == 1
    assert "1 failed" in result.stdout
    assert b"return a * b" in before
    assert (template / "calculator.py").read_bytes() == before
