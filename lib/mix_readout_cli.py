"""Entry script of `3d mix readout`: photo + tester manifest → the `mix-readout/1` record.

Run through `cli.pyrun` with numpy, Pillow and OpenCV (commands/mix.py is stdlib-only). Prints the
record as JSON on stdout and exits 0; a photo that cannot be read as the manifest's card (too few
fiducials, another tester's QR, a dark paper region) prints `{"error": "..."}` and exits 1; a bad
argument or manifest exits 2 the same way.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, NoReturn

from slicing.mix_readout import ReadoutError, read_photo
from slicing.mix_target import DEFAULT_WHITE_LEVEL


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError(message)


def main(argv: list[str]) -> int:
    ap = _Parser(prog="mix_readout_cli")
    ap.add_argument("photo")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--white", default="card")
    ap.add_argument("--white-level", type=int, default=DEFAULT_WHITE_LEVEL)
    ap.add_argument("--no-qr-check", action="store_true")
    try:
        args = ap.parse_args(argv)
        manifest_path = Path(args.manifest)
        manifest: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(json.dumps({"error": str(e)}))
        return 2
    try:
        record = read_photo(
            manifest,
            Path(args.photo),
            Path(args.out),
            white=args.white,
            white_level=args.white_level,
            check_qr=not args.no_qr_check,
            manifest_name=manifest_path.name,
        )
    except ReadoutError as e:
        print(json.dumps({"error": str(e)}))
        return 1
    print(json.dumps(record))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
