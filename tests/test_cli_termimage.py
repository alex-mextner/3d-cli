"""Unit tests for lib/cli/termimage.py: protocol detection from the environment and the escape sequences of each
protocol. Pillow is a project dependency, so PNG/JPEG conversion is exercised for real; terminals are not."""

from __future__ import annotations

import base64
import io
import re
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from cli import termimage

LIB = Path(__file__).resolve().parents[1] / "lib"


def _image(
    fmt: str,
    size: tuple[int, int] = (64, 32),
    colour: tuple[int, int, int] = (200, 30, 30),
) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format=fmt)
    return buf.getvalue()


# -- detection ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "env,expected",
    [
        ({"KITTY_WINDOW_ID": "7", "TERM": "xterm-256color"}, "kitty"),
        ({"TERM": "xterm-kitty"}, "kitty"),
        ({"TERM": "xterm-ghostty"}, "kitty"),
        ({"TERM_PROGRAM": "ghostty"}, "kitty"),
        ({"TERM_PROGRAM": "iTerm.app"}, "iterm2"),
        ({"TERM_PROGRAM": "WezTerm"}, "iterm2"),
        ({"LC_TERMINAL": "iTerm2"}, "iterm2"),
        ({"TERM": "foot"}, "sixel"),
        ({"TERM": "xterm-sixel"}, "sixel"),
        ({"TERM": "xterm-256color"}, None),
        ({}, None),
        (
            {"TMUX": "/tmp/tmux-1/default,1,0", "KITTY_WINDOW_ID": "7"},
            None,
        ),  # tmux swallows graphics escapes
        ({"STY": "123.pts-0", "TERM_PROGRAM": "iTerm.app"}, None),
        (
            {"THREED_IMAGE_PROTOCOL": "sixel", "TMUX": "x"},
            "sixel",
        ),  # an explicit choice wins over auto-detection
        ({"THREED_IMAGE_PROTOCOL": "none", "KITTY_WINDOW_ID": "7"}, None),
        ({"THREED_IMAGE_PROTOCOL": "bogus", "KITTY_WINDOW_ID": "7"}, None),
    ],
)
def test_detect_from_the_environment(env: dict[str, str], expected: str | None) -> None:
    assert termimage.detect(env, is_tty=True) == expected


def test_detect_needs_a_terminal_on_stdout() -> None:
    assert termimage.detect({"KITTY_WINDOW_ID": "7"}, is_tty=False) is None
    assert termimage.detect({"THREED_IMAGE_PROTOCOL": "kitty"}, is_tty=False) == "kitty"


# -- kitty -------------------------------------------------------------------------------------------------
def test_kitty_sends_a_png_in_chunks_with_the_aspect_ratio_left_to_the_terminal() -> (
    None
):
    seq = termimage.encode(_image("JPEG"), "kitty", cols=20)
    assert seq is not None and seq.endswith("\x1b\\\n")
    chunks = re.findall(r"\x1b_G([^;]*);([^\x1b]*)\x1b\\", seq)
    assert chunks[0][0].startswith("a=T,f=100,c=20,q=2") and "r=" not in chunks[0][0]
    assert [c[0] for c in chunks[1:]] == [
        f"m={0 if i == len(chunks) - 2 else 1}" for i in range(len(chunks) - 1)
    ]
    assert chunks[0][0].endswith(f"m={0 if len(chunks) == 1 else 1}")
    payload = base64.b64decode("".join(c[1] for c in chunks))
    assert payload.startswith(
        b"\x89PNG\r\n\x1a\n"
    )  # a JPEG input is converted: kitty only takes PNG here
    assert Image.open(io.BytesIO(payload)).size == (64, 32)


def test_kitty_splits_a_large_payload_at_4096_bytes() -> None:
    noisy = Image.effect_noise((256, 256), 80).convert("RGB")
    buf = io.BytesIO()
    noisy.save(buf, format="PNG")
    seq = termimage.encode_kitty(buf.getvalue(), 10)
    chunks = re.findall(r"\x1b_G([^;]*);([^\x1b]*)\x1b\\", seq)
    assert len(chunks) > 2 and all(len(c[1]) <= 4096 for c in chunks)
    assert chunks[-1][0] == "m=0" and all(c[0] == "m=1" for c in chunks[1:-1])


def test_kitty_downscales_a_wide_image() -> None:
    seq = termimage.encode(_image("PNG", (2000, 1000)), "kitty", cols=10)
    assert seq is not None
    payload = base64.b64decode(
        "".join(p for _, p in re.findall(r"\x1b_G([^;]*);([^\x1b]*)\x1b\\", seq))
    )
    assert Image.open(io.BytesIO(payload)).size == (160, 80)


# -- iTerm2 ------------------------------------------------------------------------------------------------
def test_iterm2_sends_a_shrunk_png_with_a_cell_width() -> None:
    seq = termimage.encode(_image("JPEG", (2000, 1000)), "iterm2", cols=18)
    assert seq is not None and seq.startswith("\x1b]1337;File=inline=1;") and seq.endswith("\x07\n")
    header, payload = seq[2:-2].split(":", 1)
    data = base64.b64decode(payload)
    assert f"size={len(data)};width=18;height=auto;preserveAspectRatio=1" in header
    assert Image.open(io.BytesIO(data)).size == (288, 144) and len(data) < 20_000  # not the 2000 px original


# -- sixel -------------------------------------------------------------------------------------------------
def test_sixel_pixels_encode_bands_palette_and_run_lengths() -> None:
    # 2 columns x 7 rows: colour 0 everywhere except the bottom-right pixel (colour 1) -> two six-row bands.
    pixels = [0] * 14
    pixels[13] = 1
    out = termimage.encode_sixel_pixels(2, 7, pixels, [(0, 0, 0), (255, 0, 0)])
    assert out == '\x1bPq"1;1;2;7#0;2;0;0;0#1;2;100;0;0#0~~$-#0@?$#1?@$-\x1b\\'
    assert out.startswith('\x1bPq"1;1;2;7') and out.endswith("\x1b\\")


def test_sixel_run_length_encoding() -> None:
    assert (
        termimage._rle("~~~") == "~~~"
        and termimage._rle("~~~~") == "!4~"
        and termimage._rle("a~~~~~b") == "a!5~b"
    )


def test_sixel_image_quantises_with_pillow() -> None:
    seq = termimage.encode(_image("PNG", (40, 12)), "sixel", cols=10)
    assert (
        seq is not None
        and seq.startswith('\x1bPq"1;1;40;12')
        and seq.endswith("\x1b\\\n")
    )
    assert re.search(r"#\d+;2;\d+;\d+;\d+", seq)


# -- fallback ------------------------------------------------------------------------------------------------
def test_unreadable_or_unknown_input_degrades_to_none() -> None:
    assert termimage.encode(b"not an image", "kitty") is None
    assert termimage.encode(b"not an image", "sixel") is None
    assert termimage.encode(_image("PNG"), "carrier-pigeon") is None


def test_without_pillow_only_png_kitty_and_iterm2_survive() -> None:
    code = (
        "import sys, io, base64\n"
        "png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==')\n"
        "sys.modules['PIL'] = None\n"
        "from cli import termimage\n"
        "assert termimage.encode(png, 'kitty') is not None\n"
        "assert termimage.encode(b'\\xff\\xd8\\xff jpeg', 'kitty') is None\n"
        "assert termimage.encode(b'\\xff\\xd8\\xff jpeg', 'sixel') is None\n"
        "assert termimage.encode(b'\\xff\\xd8\\xff jpeg', 'iterm2') is not None\n"
        "assert termimage.encode(b'\\x89PNG\\r\\n\\x1a\\n' + b'0' * 400_000, 'kitty') is None\n"
        "assert termimage.encode(b'x' * 400_000, 'iterm2') is None\n"
        "print('OK')\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=LIB,
        env={"PYTHONPATH": str(LIB), "PATH": ""},
    )
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr
