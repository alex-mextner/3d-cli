"""3d mix — colour mixing by layers (Full Spectrum / Snapmaker Orca mixed filaments).

WHAT: `check FILE.3mf` lints a project before slicing: every part printed in a mixed filament
  must show its visible face vertically (lib/slicing/mix_faces.py; the same rule is the `mix face`
  line of `3d print-preflight`).

WHY: a layer mix reads only on walls; a part lying flat shows its last layer, one plain filament
  (tg#22816: a flat tile in mix 431 came out ΔE 16.6 from the mockup). Epic #410, issue #411.

The geometry work lives in lib/slicing/mix_faces.py (stdlib); this module stays import-light.
"""

from __future__ import annotations

from pathlib import Path

from cli.registry import Command
from errors import GateFailure, InputNotFound, InvalidArgument, UsageError

USAGE = """3d mix check FILE.3mf [--json]
  Colour mixing by layers (Snapmaker Orca mixed filaments, U1).

  check FILE.3mf [--json]
      Lint a project 3MF before slicing: every part whose filament is a mixed one (a manual
      layer pattern or a Local-Z ratio row) must show its colour on walls. Per part, its
      faces in bed coordinates are summed by normal: top (within 30° of horizontal, looking
      up), side (within 30° of vertical), slope (the rest looking up). FAIL when the top area
      exceeds half the side area (a layer mix reads only on walls, a flat top shows one
      filament); WARN when the upward slopes exceed half the side area (terraces).
      Exit 1 on FAIL, 0 otherwise (WARN is printed), 2 when the file is not a project 3MF.

Examples:
  3d mix check out/u1_tiles.3mf
  3d mix check out/u1_tiles.3mf --json"""

_CMD = "mix"


def _check(argv: list[str]) -> int:
    import json

    from slicing.mix_faces import FAIL, ProjectError, check, format_lines

    as_json = "--json" in argv
    pos = [a for a in argv if a != "--json"]
    unknown = [a for a in pos if a.startswith("-")]
    if unknown:
        raise UsageError(
            f"unknown option {unknown[0]}",
            command=_CMD,
            remediation=["See `3d mix --help`."],
        )
    if len(pos) != 1:
        raise UsageError(
            f"check takes one project 3MF, got {len(pos)}",
            command=_CMD,
            remediation=["3d mix check out/plate.3mf"],
        )
    path = Path(pos[0])
    if not path.is_file():
        raise InputNotFound(str(path), command=_CMD)
    try:
        report = check(str(path))
    except ProjectError as exc:
        raise UsageError(
            str(exc),
            command=_CMD,
            remediation=[
                "Pass the project .3mf saved by Snapmaker Orca / Orca (not an STL export)."
            ],
        ) from exc
    if as_json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(f"mix check {path.name}: {report.status}")
        for line in format_lines(report):
            print(f"  {line}")
    if report.status == FAIL:
        raise GateFailure(
            f"mix check: {report.failures} mixed part(s) with the visible face horizontal",
            command=_CMD,
            silent=True,
        )
    return 0


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    sub, rest = argv[0], argv[1:]
    if sub == "check":
        return _check(rest)
    raise InvalidArgument("subcommand", sub, ["check"], command=_CMD)


COMMAND = Command(
    name="mix",
    group="SLICING",
    summary="colour mixing by layers: lint a project so mixed parts show their colour on walls",
    usage=USAGE,
    run=run,
)
