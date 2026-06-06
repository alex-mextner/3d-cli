"""3d kinematics - validate joint specs and print a deterministic JSON summary."""
from __future__ import annotations

import sys

from cli.registry import Command
from errors import UsageError

USAGE = """3d kinematics <3d.yaml|project-dir>
  Validate kinematics.joints in a project file and print a deterministic JSON summary.

Supported joint types:
  revolute     axis + limits in degrees
  prismatic    axis + limits in project units
  fixed        parent/child reference with optional origin

Example:
  3d kinematics 3d.yaml"""


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0

    path = ""
    for arg in argv:
        if arg.startswith("-"):
            raise UsageError(
                f"unknown option '{arg}'",
                command="kinematics",
                remediation=["Usage is `3d kinematics <3d.yaml|project-dir>`."],
            )
        if path:
            raise UsageError(
                "too many positional arguments",
                command="kinematics",
                remediation=["Pass exactly one project file or project directory."],
            )
        path = arg

    from kinematics import summarize_project, summary_json

    sys.stdout.write(summary_json(summarize_project(path)))
    return 0


COMMAND = Command(
    name="kinematics",
    group="QA & GATES",
    summary="validate project joint specs and emit deterministic JSON",
    usage=USAGE,
    run=run,
)
