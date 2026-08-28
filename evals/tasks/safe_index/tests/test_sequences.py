from sequences import safe_index


def test_safe_index_at_length() -> None:
    assert safe_index(["a"], 1, "missing") == "missing"
