"""Entry script of `3d mix target` and of `--target-photo`: reads the target colour of an image region.

Run through `cli.pyrun` with numpy and Pillow (commands/mix.py is stdlib-only and may run on a
system python without them). Prints the `mix-target/1` record (slicing/mix_target.py) as JSON on
stdout and exits 0; a bad box, a box outside the image or an unreadable image prints
`{"error": "..."}` and exits 2.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from slicing.mix_target import (
    DEFAULT_WHITE_LEVEL,
    MixTargetError,
    parse_box,
    read_target,
)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="mix_target_cli")
    ap.add_argument("image")
    ap.add_argument("--box", required=True)
    ap.add_argument("--white")
    ap.add_argument("--white-level", type=int, default=DEFAULT_WHITE_LEVEL)
    args = ap.parse_args(argv)
    try:
        reading = read_target(
            Path(args.image),
            parse_box(args.box, "--box"),
            parse_box(args.white, "--white") if args.white else None,
            args.white_level,
        )
    except MixTargetError as e:
        print(json.dumps({"error": str(e)}))
        return 2
    print(json.dumps(reading.to_dict()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
