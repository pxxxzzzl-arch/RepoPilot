from text import normalize


def test_normalize() -> None:
    assert normalize("  Hello  ") == "hello"
