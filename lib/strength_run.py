"""Solver tool behind `3d strength` (run through cli.pyrun with numpy/scipy/trimesh/manifold3d/scikit-fem/pytetwild).

The command module validates argv and execs this script with the same argv; it re-parses (cheap),
runs structural.run.analyze, prints the text or `--json` report and exits 0 (PASS), 1 (WEAK / FAIL)
or the structured-error exit code (2 bad input, 127 missing dependency).
"""

from __future__ import annotations

import json
import sys

from errors import ThreeDError
from strength import SCHEMA, StrengthRequest, exit_code_for, format_text, parse_args


def _emit(result: dict[str, object], request: StrengthRequest) -> None:
    if request.as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    print(format_text(result))  # type: ignore[arg-type]


def main(argv: list[str]) -> int:
    from structural import run

    try:
        request = parse_args(argv)
        result = run.tidy(run.analyze(request))
    except ThreeDError as exc:
        print(exc.render(), file=sys.stderr)
        return exc.exit_code
    except (
        ValueError
    ) as exc:  # closed-form validators reject non-positive / non-finite geometry
        print(f"strength: {exc}", file=sys.stderr)
        return 2
    result.setdefault("schema", SCHEMA)
    _emit(result, request)
    if result.get("verdict") is None:  # --dry-run: planned, not solved
        return 0
    return exit_code_for(str(result["verdict"]))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
