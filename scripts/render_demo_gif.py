#!/usr/bin/env python3
"""Render the README's 90-second deterministic terminal walkthrough."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH = 1200
HEIGHT = 675
FRAME_MILLISECONDS = 15_000
BACKGROUND = "#0d1117"
PANEL = "#161b22"
TEXT = "#e6edf3"
MUTED = "#8b949e"
GREEN = "#3fb950"
BLUE = "#58a6ff"
YELLOW = "#d29922"
RED = "#f85149"


def font(size: int, *, bold: bool = False):
    candidates = [
        "/System/Library/Fonts/SFNSMono.ttf",
        "/System/Library/Fonts/Menlo.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    ]
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError:
                continue
    return ImageFont.load_default()


TITLE = font(38, bold=True)
BODY = font(25)
SMALL = font(20)


SLIDES = [
    (
        "RepoPilot: issue → auditable patch",
        [
            (BLUE, "$ issue2patch run --repo /tmp/broken --issue ..."),
            (MUTED, "Deterministic Docker replay · no API calls · 90 seconds"),
            (TEXT, "The real client follows the same action contract."),
        ],
    ),
    (
        "1 / Baseline fails before the model acts",
        [
            (BLUE, "[baseline] running tests in Docker"),
            (RED, "FAILED tests/test_calculator.py::test_divide"),
            (TEXT, "assert divide(6, 3) == 2"),
            (MUTED, "Outcome: ASSERTION_FAILED (not infrastructure error)"),
        ],
    ),
    (
        "2 / Read returns content + trusted metadata",
        [
            (BLUE, "[step 1] ReadFileAction calculator.py"),
            (GREEN, "OK  path=calculator.py  bytes=36"),
            (TEXT, "sha256=50d858e0985ecc7f..."),
            (MUTED, "Audit log stores the hash, never the full source."),
        ],
    ),
    (
        "3 / Patch is preflighted and atomic",
        [
            (BLUE, "[step 2] PatchAction"),
            (TEXT, "-    return a * b"),
            (GREEN, "+    return a / b"),
            (MUTED, "Hash, path, symlink, sensitive-file and byte limits: OK"),
        ],
    ),
    (
        "4 / Tests pass inside the locked sandbox",
        [
            (BLUE, "[step 3] RunTestsAction"),
            (GREEN, "1 passed in 0.02s"),
            (TEXT, "network=none · user=65532 · cap-drop=ALL"),
            (TEXT, "read-only root · no-new-privileges · resource limits"),
        ],
    ),
    (
        "5 / Human receives only the reviewable diff",
        [
            (GREEN, "status=success  tests_passed=True"),
            (GREEN, "original_unchanged=True"),
            (TEXT, "Diff touches: calculator.py"),
            (YELLOW, "No commit, push, or PR happens without a later approval."),
        ],
    ),
]


def render_slide(index: int, title: str, lines: list[tuple[str, str]]) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((45, 38, WIDTH - 45, HEIGHT - 38), radius=18, fill=PANEL)
    draw.ellipse((72, 64, 88, 80), fill=RED)
    draw.ellipse((98, 64, 114, 80), fill=YELLOW)
    draw.ellipse((124, 64, 140, 80), fill=GREEN)
    draw.text((170, 58), "repopilot — demo", font=SMALL, fill=MUTED)
    draw.line((70, 105, WIDTH - 70, 105), fill="#30363d", width=2)
    draw.text((82, 145), title, font=TITLE, fill=TEXT)
    y = 240
    for color, line in lines:
        draw.text((92, y), line, font=BODY, fill=color)
        y += 61
    progress_left = 82
    progress_right = WIDTH - 82
    progress_y = HEIGHT - 85
    draw.rounded_rectangle(
        (progress_left, progress_y, progress_right, progress_y + 8),
        radius=4,
        fill="#30363d",
    )
    progress = progress_left + (progress_right - progress_left) * (index + 1) // len(SLIDES)
    draw.rounded_rectangle(
        (progress_left, progress_y, progress, progress_y + 8),
        radius=4,
        fill=BLUE,
    )
    draw.text(
        (WIDTH - 180, HEIGHT - 68),
        f"{index + 1}/{len(SLIDES)}",
        font=SMALL,
        fill=MUTED,
    )
    return image


def main() -> None:
    output = Path(__file__).resolve().parents[1] / "docs" / "assets" / "demo.gif"
    output.parent.mkdir(parents=True, exist_ok=True)
    frames = [render_slide(index, title, lines) for index, (title, lines) in enumerate(SLIDES)]
    frames[0].save(
        output,
        save_all=True,
        append_images=frames[1:],
        duration=FRAME_MILLISECONDS,
        loop=0,
        optimize=True,
    )
    print(f"wrote {output} ({len(frames) * FRAME_MILLISECONDS // 1000}s)")


if __name__ == "__main__":
    main()
