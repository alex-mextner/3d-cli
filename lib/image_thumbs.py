"""image_thumbs.py — encode image files as terminal thumbnails and print ONE JSON object `{path: sequence}`.

The tool behind `library.images.render_images` when the `3d` interpreter (the system Python) has no Pillow: it runs
through `cli.pyrun` with deps `pillow`. `sequence` is the escape sequence `cli.termimage.encode` builds for the
protocol (kitty, iterm2, sixel or blocks), or null for an image that could not be read.

Usage: python image_thumbs.py PROTOCOL COLS PATH [PATH...]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from cli import termimage


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[0] not in (*termimage.PROTOCOLS, termimage.BLOCKS) or not argv[1].isdecimal():
        print("usage: image_thumbs.py kitty|iterm2|sixel|blocks COLS PATH [PATH...]", file=sys.stderr)
        return 2
    protocol, cols = argv[0], int(argv[1])
    print(json.dumps({p: termimage.encode(Path(p).read_bytes(), protocol, cols=cols) for p in argv[2:]}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
