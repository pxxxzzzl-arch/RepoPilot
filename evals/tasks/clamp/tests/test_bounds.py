from bounds import clamp


def test_clamp() -> None:
    assert clamp(5, 0, 10) == 5
