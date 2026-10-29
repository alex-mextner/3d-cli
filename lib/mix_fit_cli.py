"""Entry script of `3d mix fit`: readout JSON files → the `mix-fit/1` record.

Run through `cli.pyrun` with numpy and scipy (commands/mix.py is stdlib-only). Prints the record as
JSON on stdout and exits 0; unreadable or unfittable readouts print `{"error": "..."}` and exit 2.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, NoReturn

from slicing.mix_fit import DEFAULT_LAMBDA, FitError, fit


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError(message)


def main(argv: list[str]) -> int:
    ap = _Parser(prog="mix_fit_cli")
    ap.add_argument("readouts", nargs="+")
    ap.add_argument("--lambda", dest="lam", type=float, default=DEFAULT_LAMBDA)
    try:
        args = ap.parse_args(argv)
        docs: list[Any] = [
            json.loads(Path(p).read_text(encoding="utf-8")) for p in args.readouts
        ]
        record = fit(docs, args.lam)
    except (OSError, ValueError, FitError) as e:
        print(json.dumps({"error": str(e)}))
        return 2
    except (KeyError, TypeError, AttributeError) as e:  # a readout row without its fields
        print(json.dumps({"error": f"malformed readout: {type(e).__name__} {e}"}))
        return 2
    print(json.dumps(record))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
