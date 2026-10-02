"""3d solid - local STL to validated faceted STEP."""
from __future__ import annotations
import argparse
import json
from typing import NoReturn
import math
from pathlib import Path
import sys
from cli.registry import Command
from errors import ThreeDError, UsageError

USAGE = "3d solid INPUT.stl --units mm|cm|m|in -o OUTPUT.step [--tolerance MM] [--max-faces N] [--merge-coplanar] [--json]"

class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise UsageError(message, command="solid")


def _run(argv: list[str]) -> int:
    parser = _Parser(prog="3d solid", description="Convert one closed STL shell to a faceted B-rep. No analytic recovery, hole filling or automatic downloads.")
    parser.add_argument("source")
    parser.add_argument("--units", choices=("mm", "cm", "m", "in"), required=True)
    parser.add_argument("-o", "--out", required=True)
    parser.add_argument("--tolerance", type=float, default=1e-6)
    parser.add_argument("--max-faces", type=int, default=5000)
    parser.add_argument("--merge-coplanar", action="store_true", help="merge coplanar triangle faces without inferring curved surfaces")
    parser.add_argument("--json", action="store_true")
    if not argv:
        parser.print_help()
        return 1
    args = parser.parse_args(argv)
    if not math.isfinite(args.tolerance) or args.tolerance <= 0 or not 1 <= args.max_faces <= 50000:
        raise UsageError("tolerance must be finite and positive; face budget must be 1..50000", command="solid")
    request = {"source": str(Path(args.source).resolve()), "output": str(Path(args.out).absolute()), "units": args.units, "tolerance": args.tolerance, "max_faces": args.max_faces, "merge_coplanar": args.merge_coplanar}
    from solid_job import run_conversion
    code, report = run_conversion(request)
    if args.json:
        print(json.dumps(report, allow_nan=False))
    elif code:
        print(f"solid: {report['error']}", file=sys.stderr)
        for line in report.get("remediation", []):
            print(f"  {line}", file=sys.stderr)
    else:
        print(f"CONVERTED: {report['output']} (faceted B-rep, {report['volume_mm3']:.6g} mm3)")
        print("STEP reopened and checked. Analytic surfaces, self-intersections and manufacturing fitness are not verified.")
    return code

def run(argv: list[str]) -> int:
    try:
        return _run(argv)
    except ThreeDError as exc:
        if "--json" not in argv:
            raise
        print(json.dumps({"status": "FAILED", "error": exc.message, "remediation": exc.remediation, "exit_code": exc.exit_code}, allow_nan=False))
        return exc.exit_code


COMMAND = Command(name="solid", group="GEOMETRY & EXPORT", summary="convert one closed STL shell to checked faceted STEP locally", usage=USAGE, run=run, bootstrap_openscad=False)
