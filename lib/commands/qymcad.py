"""3d qymcad - explicit external local CAD fallback."""
from __future__ import annotations
import argparse
import json
from typing import NoReturn
from cli.registry import Command
from errors import ThreeDError, UsageError

USAGE = "3d qymcad doctor|launch [--executable PATH] [--json] [--dry-run]"

class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise UsageError(message, command="qymcad")


def _run(argv: list[str]) -> int:
    parser = _Parser(prog="3d qymcad", description="Discover or explicitly launch QymCAD; no headless editing or CAM is claimed.")
    parser.add_argument("action", choices=("doctor", "launch"))
    parser.add_argument("--executable")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    if not argv:
        parser.print_help()
        return 1
    args = parser.parse_args(argv)
    from qymcad_tools import doctor, launch
    if args.action == "doctor":
        result = doctor(args.executable)
        code = 0 if result["available"] else 127
    else:
        result = launch(args.executable, dry_run=args.dry_run)
        code = 0
    print(json.dumps(result) if args.json else json.dumps(result, indent=2))
    return code

def run(argv: list[str]) -> int:
    try:
        return _run(argv)
    except ThreeDError as exc:
        if "--json" not in argv:
            raise
        print(json.dumps({"status": "FAILED", "error": exc.message, "remediation": exc.remediation, "exit_code": exc.exit_code}, allow_nan=False))
        return exc.exit_code


COMMAND = Command(name="qymcad", group="ENVIRONMENT", summary="discover and explicitly launch the local QymCAD fallback", usage=USAGE, run=run, bootstrap_openscad=False)
