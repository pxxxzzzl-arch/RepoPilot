from collections_utils import unique_preserving_order


def test_unique_preserving_order() -> None:
    assert unique_preserving_order(["b", "a", "b"]) == ["b", "a"]
