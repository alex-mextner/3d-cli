"""3d validate — fast parse-only syntax check (no geometry render)."""
from __future__ import annotations

import os
import subprocess
import tempfile

from cli.env import require_openscad
from cli.registry import Command
from errors import InputNotFound, GateFailure

USAGE = """3d validate <file.scad>
  Parse-only syntax check (exports echo; no geometry render). Exit 0 = OK, 1 = error."""


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    osc = require_openscad("validate")
    inp = argv[0]
    if not os.path.isfile(inp):
        raise InputNotFound(inp, command="validate")

    print(f"validate: {inp}")
    fd, tmp = tempfile.mkstemp(suffix=".echo", prefix="3d_validate.")
    os.close(fd)
    try:
        r = subprocess.run(
            [osc, "-o", tmp, "--export-format=echo", inp],
            capture_output=True, text=True,
        )
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode == 0:
            print("  syntax OK")
            try:
                with open(tmp) as fh:
                    echo = fh.read()
            except OSError:
                echo = ""
            if echo.strip():
                print("  echo output:")
                for line in echo.splitlines():
                    print(f"    {line}")
            if "ERROR:" in out:
                print("  WARNING: openscad emitted ERROR: lines:")
                for line in out.splitlines():
                    if "ERROR:" in line:
                        print(f"    {line}")
                raise GateFailure("openscad emitted ERROR: lines", command="validate", silent=True)
            return 0
        else:
            import sys
            sys.stderr.write("  validation FAILED\n")
            for line in out.splitlines():
                sys.stderr.write(f"    {line}\n")
            return 1
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


COMMAND = Command(
    name="validate",
    group="GEOMETRY & EXPORT",
    summary="fast parse-only syntax check (no render)",
    usage=USAGE,
    run=run,
)
