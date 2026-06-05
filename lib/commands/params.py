"""3d params — extract Customizer-style parameters (stdlib-only; imports extract_params)."""
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
