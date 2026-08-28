def safe_index(values: list[object], index: int, default: object = None) -> object:
    if 0 <= index <= len(values):
        return values[index]
    return default
