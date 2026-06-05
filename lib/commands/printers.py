"""3d printers — inspect the printer registry.

WHAT: lists every known printer and shows the full spec sheet (build volume, nozzle,
  firmware, default material) for a single printer by name.

WHY: a project's 3d.yaml references a printer BY NAME (`printer: Prusa MK4`). Before you
  write that name you need to know which printers the tool knows, what build volume each
  has, and whether a 240 mm part will fit. `list` is the menu; `show` is the spec sheet.

Examples:
  3d printers list                    # valid names + bed sizes at a glance
  3d printers show "Prusa MK4"        # full spec sheet for one machine
  3d printers show "Bambu A1"         # check bed size before slicing

ROADMAP §2a: "Materials & printers — shared, cross-cutting vocabularies.
  Single canonical registries materials.yaml + printers.yaml (built-in defaults +
  user/project overrides), referenced BY NAME everywhere."

ACCESSED VIA: `3d printers list` and `3d printers show <name>`. A thin CLI frontend over
lib/printers.py: it resolves the merged built-in+user+project registry and prints it.

INVARIANTS: stdlib-only at import time (the registry contract); the printers loader and
yaml are reached lazily inside run(). Errors come from lib/errors.py via the loader; this
module only adds a UsageError for a bad subcommand.
"""
from __future__ import annotations

from cli.registry import Command
from errors import UsageError

USAGE = """3d printers <subcommand>
  Inspect the printer registry — the machines `3d` knows by name (build volume, nozzle,
  firmware, default material). A project's 3d.yaml picks one with `printer: <name>`.

  list                    list every known printer with its build volume
  show <name>             print the full spec sheet for one printer

Why: you reference a printer BY NAME in 3d.yaml; `list` shows the valid names and `show`
lets you check a part against a bed before slicing ("does a 240mm part fit?"). The
registry merges built-in + ~/.config/3d-cli/printers.yaml + ./printers.yaml, so you can
add or correct a machine without editing the shipped data.

Examples:
  3d printers list
  3d printers show "Prusa MK4\""""


def _print_list() -> int:
    from printers import load_printers  # lazy: keep module import-light (registry contract)

    printers = load_printers(command="printers")
    if not printers:
        print("(no printers in the registry)")
        return 0
    print("Known printers (3d.yaml `printer:` names):")
    for name in sorted(printers.keys()):
        p = printers[name]
        x, y, z = p.bed
        fw = p.firmware or "?"
        print(f"  - {name:<20} bed {x:g}x{y:g}x{z:g} mm   nozzle {p.nozzle_mm:g} mm   {fw}")
    print()
    print('Detail:  3d printers show "<name>"')
    return 0


def _print_show(name: str) -> int:
    from printers import get_printer  # lazy: keep module import-light (registry contract)

    p = get_printer(name, command="printers")  # raises InvalidArgument on unknown name
    x, y, z = p.bed
    print(f"{p.name}")
    print(f"  build volume   {x:g} x {y:g} x {z:g} mm  (max part that fits)")
    print(f"  nozzle         {p.nozzle_mm:g} mm")
    print(f"  firmware       {p.firmware or '(unspecified)'}")
    print(f"  default mat.   {p.material or '(unspecified)'}")
    return 0


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    sub = argv[0]
    if sub in ("-h", "--help", "help"):
        print(USAGE)
        return 0

    if sub == "list":
        return _print_list()
    if sub == "show":
        if len(argv) < 2:
            raise UsageError(
                "`printers show` needs a printer name",
                command="printers",
                remediation=['Run `3d printers list` for the names, then e.g.  3d printers show "Prusa MK4"'],
            )
        return _print_show(argv[1])

    raise UsageError(
        f"unknown subcommand '{sub}'",
        command="printers",
        remediation=["Use `list` or `show <name>` — see `3d printers --help`."],
    )


COMMAND = Command(
    name="printers",
    group="LIBRARIES",
    summary="inspect the printer registry: list / show <name>",
    usage=USAGE,
    run=run,
)
