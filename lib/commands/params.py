"""3d params — extract Customizer-style parameters from a .scad file.

WHAT: parses OpenSCAD source for Customizer-style parameter declarations
  (name = value; // [min:max] desc) and emits them as a human-readable list or JSON.

WHY: parametric models are only useful if you know what parameters exist and what they
  do. `params` introspects a .scad without running it, giving you the tunable constants
  you can feed into batch renders, the match loop, or manual design exploration.

Examples:
  3d params bracket.scad                  # human-readable parameter list
  3d params bracket.scad --json | jq '.[] | {name, value}'
  3d params bracket.scad --json > params.json   # drive a batch script

ROADMAP §3: "Core command surface: export (mesh-validated, nonzero on bad geometry),
  validate, params."
"""
from __future__ import annotations

import os

import extract_params
from cli.registry import Command
from errors import InputNotFound

USAGE = """3d params <file.scad> [--json]
  Extract Customizer-style parameters (name = value; // [min:max] desc).
  Use this to inspect tunable constants, generate parameter tables, drive batch
  renders, or feed values into the match loop.

Options:
  --json                emit JSON instead of a human-readable list. Use this when
                         feeding the output into a script or the match loop.

Examples:
  3d params bracket.scad
  3d params bracket.scad --json | jq '.[] | {name, value}'
  3d params bracket.scad --json > params.json"""


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    inp = argv[0]
    if not os.path.isfile(inp):
        raise InputNotFound(inp, command="params")
    # extract_params.main expects argv[0] = progname, then file, then flags.
    return extract_params.main(["params", *argv])


COMMAND = Command(
    name="params",
    group="GEOMETRY & EXPORT",
    summary="extract Customizer-style parameters (--json)",
    usage=USAGE,
    run=run,
)
