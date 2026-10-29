"""cli/termimage.py — inline images in the terminal: protocol detection and escape-sequence encoders.

WHAT: `detect()` names the graphics protocol the current terminal speaks (`kitty`, `iterm2`, `sixel`) or None;
`encode()` turns image bytes (PNG/JPEG/WebP/...) into the escape sequence that draws a thumbnail about `cols` cells
wide at the cursor, followed by a newline. Nothing here prints or touches the network.

PROTOCOLS:
  - kitty  — Kitty graphics protocol (Kitty, Ghostty): PNG payload, base64 in 4096-byte chunks, `c=cols` so the
    terminal keeps the aspect ratio.
  - iterm2 — iTerm2 inline images (iTerm2, WezTerm): the file as is, base64, `width=cols;height=auto`.
  - sixel  — DEC sixel (foot, mlterm, contour, xterm -ti vt340, anything with `sixel` in $TERM): palette-quantised
    RGB through a built-in encoder.
  Kitty and sixel need Pillow (PNG conversion / pixels); iTerm2 does not. A missing Pillow makes `encode` return
  None for them, which callers treat like "no image support": graceful fallback, never an error.

DETECTION (`detect`): `THREED_IMAGE_PROTOCOL` (kitty|iterm2|sixel|none) wins; else stdout must be a terminal and
not inside tmux/screen (they swallow graphics escapes unless passthrough is configured — ask for it with an explicit
protocol); then env hints (KITTY_WINDOW_ID, TERM, TERM_PROGRAM, LC_TERMINAL).

IMPORT RULE: stdlib-only at module top; Pillow is imported inside the functions that need it.
"""

from __future__ import annotations

import base64
import io
import os
from collections.abc import Mapping, Sequence
from typing import Any

PROTOCOLS = ("kitty", "iterm2", "sixel")
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_SIXEL_TERMS = ("foot", "foot-extra", "mlterm", "contour", "yaft", "yaft-256color")
_KITTY_CHUNK = 4096
SIXEL_CELL_PX = 8  # assumed cell width when the terminal cannot be asked
SIXEL_COLORS = 32
IMAGE_MAX_RAW_BYTES = 300_000  # an image sent as is (no Pillow to shrink it) must stay this small


def detect(env: Mapping[str, str] | None = None, *, is_tty: bool = True) -> str | None:
    """The protocol to use, or None. `env` defaults to os.environ; `is_tty` says whether stdout is a terminal."""
    e = os.environ if env is None else env
    forced = e.get("THREED_IMAGE_PROTOCOL", "").strip().lower()
    if forced:
        return forced if forced in PROTOCOLS else None
    if not is_tty or e.get("TMUX") or e.get("STY"):
        return None
    term, program = e.get("TERM", ""), e.get("TERM_PROGRAM", "")
    if (
        e.get("KITTY_WINDOW_ID")
        or term == "xterm-kitty"
        or term == "xterm-ghostty"
        or program == "ghostty"
    ):
        return "kitty"
    if program in ("iTerm.app", "WezTerm") or e.get("LC_TERMINAL") == "iTerm2":
        return "iterm2"
    if "sixel" in term or term in _SIXEL_TERMS:
        return "sixel"
    return None


def _pillow() -> Any:
    try:
        from PIL import Image

        return Image
    except ImportError:
        return None


def _png_bytes(data: bytes, max_width_px: int) -> bytes | None:
    """`data` as PNG no wider than `max_width_px` (the original when it already is a small PNG)."""
    image_mod = _pillow()
    if image_mod is None:
        return data if data.startswith(_PNG_MAGIC) and len(data) <= IMAGE_MAX_RAW_BYTES else None
    try:
        with image_mod.open(io.BytesIO(data)) as img:
            img.load()
            if img.width > max_width_px:
                img = img.resize(
                    (max_width_px, max(1, round(img.height * max_width_px / img.width)))
                )
            out = io.BytesIO()
            img.convert("RGBA" if "A" in img.getbands() else "RGB").save(
                out, format="PNG"
            )
            return out.getvalue()
    except Exception:  # noqa: BLE001 — Pillow raises assorted types on truncated or unsupported images
        return None


def encode_kitty(png: bytes, cols: int) -> str:
    """Kitty graphics: transmit-and-display a PNG, scaled to `cols` cells wide (rows follow the aspect ratio)."""
    payload = base64.standard_b64encode(png).decode("ascii")
    chunks = [
        payload[i : i + _KITTY_CHUNK] for i in range(0, len(payload), _KITTY_CHUNK)
    ] or [""]
    parts = []
    for i, chunk in enumerate(chunks):
        more = 1 if i < len(chunks) - 1 else 0
        control = f"a=T,f=100,c={cols},q=2,m={more}" if i == 0 else f"m={more}"
        parts.append(f"\x1b_G{control};{chunk}\x1b\\")
    return "".join(parts) + "\n"


def encode_iterm2(data: bytes, cols: int) -> str:
    """iTerm2 inline image: the original bytes, `cols` cells wide."""
    payload = base64.standard_b64encode(data).decode("ascii")
    return f"\x1b]1337;File=inline=1;size={len(data)};width={cols};height=auto;preserveAspectRatio=1:{payload}\x07\n"


def encode_sixel_pixels(
    width: int,
    height: int,
    indexed: Sequence[int],
    palette: Sequence[tuple[int, int, int]],
) -> str:
    """A sixel picture from palette indices (`indexed[y * width + x]`) and an RGB palette (0..255 per channel)."""
    out = [f'\x1bPq"1;1;{width};{height}']
    for i, (r, g, b) in enumerate(palette):
        out.append(f"#{i};2;{r * 100 // 255};{g * 100 // 255};{b * 100 // 255}")
    for top in range(0, height, 6):
        band = range(top, min(top + 6, height))
        colors = sorted({indexed[y * width + x] for y in band for x in range(width)})
        for color in colors:
            cells = []
            for x in range(width):
                bits = 0
                for bit, y in enumerate(band):
                    if indexed[y * width + x] == color:
                        bits |= 1 << bit
                cells.append(chr(63 + bits))
            out.append(f"#{color}{_rle(''.join(cells))}$")
        out.append("-")
    out.append("\x1b\\")
    return "".join(out)


def _rle(cells: str) -> str:
    """Sixel run-length encoding: `!<n><char>` for runs longer than 3."""
    out: list[str] = []
    i = 0
    while i < len(cells):
        j = i
        while j < len(cells) and cells[j] == cells[i]:
            j += 1
        run = j - i
        out.append(f"!{run}{cells[i]}" if run > 3 else cells[i] * run)
        i = j
    return "".join(out)


def encode_sixel(data: bytes, cols: int) -> str | None:
    """A sixel thumbnail of `data` about `cols` cells wide; None without Pillow or for an unreadable image."""
    image_mod = _pillow()
    if image_mod is None:
        return None
    width_px = cols * SIXEL_CELL_PX
    try:
        with image_mod.open(io.BytesIO(data)) as img:
            img.load()
            img = img.convert("RGB")
            if img.width > width_px:
                img = img.resize(
                    (width_px, max(1, round(img.height * width_px / img.width)))
                )
            quant = img.quantize(colors=SIXEL_COLORS)
            flat = list(quant.getpalette() or [])
            palette = [
                (flat[i], flat[i + 1], flat[i + 2]) for i in range(0, len(flat) - 2, 3)
            ]
            return (
                encode_sixel_pixels(
                    quant.width, quant.height, list(quant.tobytes()), palette
                )
                + "\n"
            )
    except Exception:  # noqa: BLE001 — see _png_bytes
        return None


def encode(data: bytes, protocol: str, *, cols: int = 20) -> str | None:
    """The escape sequence drawing `data` with `protocol`, or None when it cannot be done here (no Pillow for
    a non-PNG kitty image or a sixel, an iTerm2 image too big to send as is, an unreadable image, an unknown
    protocol). With Pillow every protocol downsizes the picture to about `cols` cells first."""
    width_px = cols * SIXEL_CELL_PX * 2
    if protocol == "kitty":
        png = _png_bytes(data, width_px)
        return encode_kitty(png, cols) if png else None
    if protocol == "iterm2":
        shrunk = _png_bytes(data, width_px) or (data if len(data) <= IMAGE_MAX_RAW_BYTES else None)
        return encode_iterm2(shrunk, cols) if shrunk else None
    if protocol == "sixel":
        return encode_sixel(data, cols)
    return None
