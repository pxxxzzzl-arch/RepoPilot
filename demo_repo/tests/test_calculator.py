"""Tests that expose the intentional calculator defect."""

from demo_repo.calculator import divide


def test_divide_returns_quotient() -> None:
    assert divide(6, 3) == 2

