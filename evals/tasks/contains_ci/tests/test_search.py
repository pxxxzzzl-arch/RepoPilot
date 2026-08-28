from search import contains_case_insensitive


def test_contains_case_insensitive() -> None:
    assert contains_case_insensitive("PYTHON", "python") is True
