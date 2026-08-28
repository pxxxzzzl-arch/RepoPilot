"""Internal, killable worker for bounded repository searches."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path


def _fail(message: str, code: int = 2) -> int:
    print(message, file=sys.stderr)
    return code


def main(arguments: list[str] | None = None) -> int:
    args = sys.argv[1:] if arguments is None else arguments
    if len(args) != 8:
        return _fail("invalid search worker arguments")

    root = Path(args[0])
    search_root = Path(args[1])
    pattern = args[2]
    mode = args[3]
    try:
        max_results, max_files, max_file_bytes, max_total_bytes = map(int, args[4:])
    except ValueError:
        return _fail("invalid numeric search limit")

    if mode == "regex":
        try:
            expression = re.compile(pattern)
        except re.error as error:
            return _fail(f"invalid regular expression: {error}")
        matches = expression.search
    elif mode == "fixed":
        matches = lambda line: pattern in line
    else:
        return _fail("invalid search mode")

    file_count = 0
    total_bytes = 0
    result_count = 0
    try:
        for directory, directory_names, file_names in os.walk(
            search_root, followlinks=False
        ):
            directory_path = Path(directory)
            directory_names[:] = sorted(
                name
                for name in directory_names
                if name != ".git" and not (directory_path / name).is_symlink()
            )
            for name in sorted(file_names):
                file_path = directory_path / name
                if file_path.is_symlink():
                    continue
                resolved = file_path.resolve(strict=True)
                if not resolved.is_relative_to(root):
                    continue
                size = resolved.stat().st_size
                file_count += 1
                total_bytes += size
                if file_count > max_files:
                    return _fail(f"search file count exceeds limit ({max_files})", 3)
                if size > max_file_bytes:
                    relative = resolved.relative_to(root).as_posix()
                    return _fail(
                        f"search file exceeds byte limit ({max_file_bytes}): {relative}",
                        3,
                    )
                if total_bytes > max_total_bytes:
                    return _fail(
                        f"search total bytes exceed limit ({max_total_bytes})", 3
                    )
                try:
                    content = resolved.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    continue
                relative = resolved.relative_to(root).as_posix()
                for line_number, line in enumerate(content.splitlines(), start=1):
                    if matches(line):
                        print(f"{relative}:{line_number}:{line}")
                        result_count += 1
                        if result_count >= max_results:
                            return 0
    except OSError as error:
        return _fail(f"search failed: {error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
