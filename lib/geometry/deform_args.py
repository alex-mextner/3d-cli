"""Argument grammar of `3d mesh deform` / `fit-silhouette`, stdlib-only.

Shared by the tool (geometry/mesh_deform.py) and the lightweight `3d mesh` command (which must know the
output path the tool will write, in every spelling argparse accepts: `-o X`, `-oX`, `--out X`, `--out=X`).
"""

from __future__ import annotations

import argparse

PROG = "3d mesh"


def build_parser(sub: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=f"{PROG} {sub}", add_help=False)
    p.add_argument("mesh")
    p.add_argument("spec")
    p.add_argument("-o", "--out")
    p.add_argument("--report")
    p.add_argument("--collage")
    p.add_argument("--seed", type=int)
    p.add_argument("--labels")
    p.add_argument("--out-labels")
    p.add_argument("--verify-determinism", action="store_true")
    p.add_argument("--no-self-intersections", action="store_true")
    p.add_argument("--json", action="store_true")
    return p


def output_option(sub: str, argv: list[str]) -> str | None:
    """The `-o/--out` value as the real parser reads it (argv excludes the subcommand); None when absent
    or when the arguments do not parse (the tool then reports the usage error itself)."""
    try:
        return build_parser(sub).parse_args(argv).out
    except SystemExit:
        return None
