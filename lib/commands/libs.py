"""3d libs — OpenSCAD library info (path / list). Install is automatic on first run."""
from __future__ import annotations

import os

from cli.env import repo_root
from cli.registry import Command
from errors import UsageError

USAGE = """3d libs <subcommand>   (info only — libraries auto-install on first run)
  path                           print the OPENSCADPATH line to export
  list                           show installed libraries

Notes:
  OpenSCAD libraries (BOSL2, NopSCADlib) are cloned into libs/ automatically on the
  first `3d` invocation, and OPENSCADPATH is auto-exported by the CLI — so
  'include <BOSL2/std.scad>' just resolves. `libs path` prints the line if you want it
  in your own (non-3d) shell. To re-install, remove ~/.config/3d-cli/.bootstrapped and rerun.

Examples:
  3d libs list
  export $(3d libs path)"""


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    sub = argv[0]
    if sub in ("-h", "--help", "help"):
        print(USAGE)
        return 0

    libs_dir = os.path.join(repo_root(), "libs")

    if sub == "install":
        # Keep the friendly removed-message (good UX, not a regression).
        raise UsageError(
            "'install' was removed — libraries auto-install on first run.",
            command="libs",
            remediation=["To force a re-install: rm ~/.config/3d-cli/.bootstrapped && 3d help"],
        )
    if sub == "path":
        print(f"OPENSCADPATH={os.environ.get('OPENSCADPATH', libs_dir)}")
        return 0
    if sub == "list":
        print(f"Installed OpenSCAD libraries in {libs_dir}:")
        found = False
        if os.path.isdir(libs_dir):
            for name in sorted(os.listdir(libs_dir)):
                if os.path.isdir(os.path.join(libs_dir, name)):
                    found = True
                    print(f"  - {name}")
        if not found:
            print("  (none — re-run after removing ~/.config/3d-cli/.bootstrapped)")
        print()
        print("To use:  export $(3d libs path)")
        return 0

    raise UsageError(f"unknown subcommand '{sub}'", command="libs")


COMMAND = Command(
    name="libs",
    group="LIBRARIES",
    summary="OpenSCAD library info: path / list (install is automatic on first run)",
    usage=USAGE,
    run=run,
)
