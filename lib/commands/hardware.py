"""3d hardware — list and validate machine/toolchain capabilities."""
from __future__ import annotations

import json

from cli.registry import Command
from errors import UsageError
from hardware import HardwareItem, HardwareReport, build_report

USAGE = """3d hardware <list|validate> [--json]
  Summarize local machine capabilities and the external toolchain that powers `3d`.
  Read-only: installs nothing and only uses the existing env discovery helpers.

Subcommands:
  list                 print OS/CPU/toolchain availability; always exits 0
  validate             print the same report; exits 1 when required capabilities are missing

Options:
  --json               emit a scriptable JSON report

Examples:
  3d hardware list
  3d hardware validate --json"""


def _parse(argv: list[str]) -> tuple[str, bool]:
    if not argv:
        print(USAGE)
        return ("", False)
    sub = argv[0]
    if sub in ("-h", "--help", "help"):
        print(USAGE)
        return ("help", False)
    if sub not in ("list", "validate"):
        raise UsageError(
            f"unknown subcommand '{sub}'",
            command="hardware",
            remediation=["Use `list` or `validate` — see `3d hardware --help`."],
        )
    if any(arg in ("-h", "--help", "help") for arg in argv[1:]):
        print(USAGE)
        return ("help", False)

    json_output = False
    for arg in argv[1:]:
        if arg == "--json":
            json_output = True
            continue
        raise UsageError(
            f"unknown option '{arg}'",
            command="hardware",
            remediation=["Use only `--json` after `list` or `validate`."],
        )
    return (sub, json_output)


def _line(item: HardwareItem) -> str:
    install = f"   install: {item.install}" if item.install else ""
    required = "required" if item.required else "optional"
    return f"  {item.status:<7} {item.name:<20} {item.capability:<44} {required:<8} {item.detail}{install}"


def _print_text(report: HardwareReport) -> None:
    print(f"3d hardware  —  OS={report.os_name}  machine={report.machine}  cpu={report.cpu_count}")
    print()
    print("Toolchain")
    for item in report.items:
        print(_line(item))
    print()
    if report.is_valid():
        print(">>> HARDWARE: PASS — required capabilities are available.")
    else:
        print(">>> HARDWARE: FAIL — install missing required capabilities above.")


def run(argv: list[str]) -> int:
    sub, json_output = _parse(argv)
    if sub == "":
        return 1
    if sub == "help":
        return 0

    report = build_report()
    if json_output:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        _print_text(report)

    if sub == "validate" and not report.is_valid():
        return 1
    return 0


COMMAND = Command(
    name="hardware",
    group="ENVIRONMENT",
    summary="list or validate local machine/toolchain capabilities",
    usage=USAGE,
    run=run,
)
