"""3d print — deterministic dry-run print workflow plans."""
from __future__ import annotations

from cli.registry import Command
from errors import InvalidArgument, UsageError

USAGE = """3d print <model.scad|model.stl|model.3mf|job.gcode> --printer NAME --dry-run [options]
  Validate printer/profile/job fields and print a deterministic dry-run job plan as JSON.
  This skeleton does not upload, start, pause, resume, or cancel real printer jobs yet.

Options:
  --dry-run                 required for this skeleton; produce a plan only
  --printer NAME            printer registry name (see `3d printers list`)
  --job-name NAME           job display name (default: input file stem)
  --material NAME           material name to record in the job plan
  --copies N                positive copy count (default: 1)
  --start                   planned intent: upload and start, not just upload
  --machine-profile FILE    slicer machine/printer profile (.json or .ini)
  --process-profile FILE    slicer process profile (.json or .ini)
  --filament-profile FILE   slicer filament/material profile (.json or .ini)

Examples:
  3d print part.stl --printer "Prusa MK4" --dry-run
  3d print part.3mf --printer "Bambu Lab A1" --dry-run --start --copies 2
  3d print bracket.stl --printer "Prusa MINI" --dry-run > print-plan.json
  3d print bracket.stl --printer "Prusa MK4" --dry-run --job-name "left bracket" --material PLA
  3d print bracket.stl --printer "Prusa MK4" --dry-run --machine-profile machine.json --process-profile process.ini --filament-profile pla.json"""

_VALUE_OPTIONS = {
    "--printer",
    "--job-name",
    "--material",
    "--copies",
    "--machine-profile",
    "--process-profile",
    "--filament-profile",
}
_BOOLEAN_OPTIONS = {"--dry-run", "--start", "-h", "--help"}
_KNOWN_OPTIONS = _VALUE_OPTIONS | _BOOLEAN_OPTIONS


def _need_value(argv: list[str], index: int, flag: str) -> str:
    if index + 1 >= len(argv) or not argv[index + 1]:
        raise UsageError(f"option {flag} needs a value", command="print", remediation=[USAGE])
    value = argv[index + 1]
    if value in _KNOWN_OPTIONS:
        raise UsageError(f"option {flag} needs a value", command="print", remediation=[USAGE])
    return value


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0

    from printing import JobFields, ProfileFields, build_dry_run_plan, plan_to_json

    input_path = ""
    printer_name = ""
    job_name = ""
    material: str | None = None
    copies = 1
    start = False
    dry_run = False
    machine_profile = None
    process_profile = None
    filament_profile = None

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--dry-run":
            dry_run = True
            i += 1
        elif arg == "--printer":
            printer_name = _need_value(argv, i, arg)
            i += 2
        elif arg == "--job-name":
            job_name = _need_value(argv, i, arg)
            i += 2
        elif arg == "--material":
            material = _need_value(argv, i, arg)
            i += 2
        elif arg == "--copies":
            raw = _need_value(argv, i, arg)
            try:
                copies = int(raw)
            except ValueError:
                raise InvalidArgument("--copies", raw, ["a positive integer"], command="print") from None
            i += 2
        elif arg == "--start":
            start = True
            i += 1
        elif arg == "--machine-profile":
            machine_profile = _need_value(argv, i, arg)
            i += 2
        elif arg == "--process-profile":
            process_profile = _need_value(argv, i, arg)
            i += 2
        elif arg == "--filament-profile":
            filament_profile = _need_value(argv, i, arg)
            i += 2
        elif arg.startswith("-"):
            print(USAGE)
            raise UsageError(f"unknown option '{arg}'", command="print")
        elif not input_path:
            input_path = arg
            i += 1
        else:
            print(USAGE)
            raise UsageError(f"unexpected argument '{arg}'", command="print")

    if not input_path:
        raise UsageError(
            "no input file given",
            command="print",
            remediation=["Pass a .scad, .stl, .3mf, or .gcode input file."],
        )
    if not dry_run:
        raise UsageError(
            "`3d print` currently supports dry-run plans only",
            command="print",
            remediation=["Add --dry-run to validate and render the planned job without touching a printer."],
        )
    if not printer_name:
        raise UsageError(
            "`3d print` needs --printer NAME",
            command="print",
            remediation=["Run `3d printers list`, then pass one of those names with --printer."],
        )

    plan = build_dry_run_plan(
        input_path,
        printer_name=printer_name,
        profiles=ProfileFields(
            machine=machine_profile,
            process=process_profile,
            filament=filament_profile,
        ),
        job=JobFields(name=job_name, material=material, copies=copies, start=start),
    )
    print(plan_to_json(plan), end="")
    return 0


COMMAND = Command(
    name="print",
    group="SLICING",
    summary="validate print fields and emit deterministic dry-run job plans",
    usage=USAGE,
    run=run,
)
