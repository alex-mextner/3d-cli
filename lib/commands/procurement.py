"""3d procurement — turn local BOM/inventory gaps into purchase-plan output.

ACCESSED VIA: `3d procurement plan --bom <file> --inventory <file>`. Thin CLI over
lib/procurement.py; it performs no supplier lookup, price lookup, or network call.
"""
from __future__ import annotations

from cli.registry import Command
from errors import InvalidArgument, UsageError

USAGE = """3d procurement <subcommand>
  Convert a local BOM plus local inventory into deterministic purchase-plan output.
  No network calls are made; supplier names and package sizes come only from the input files.

Subcommands:
  plan --bom <file> --inventory <file> [--format table|json]
                         print only items with a positive shortage

Input files may be JSON or YAML. Use either:
  items:
    - sku: m3-bolt
      description: M3 bolt
      quantity: 24
      unit: each
      supplier: BoltCo
      package_qty: 50

or a compact mapping:
  items:
    m3-bolt: 24

Examples:
  3d procurement plan --bom bom.yaml --inventory inventory.yaml
  3d procurement plan --bom bom.json --inventory inventory.json --format json"""


def _parse_plan_args(argv: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    accepted = {"--bom", "--inventory", "--format"}
    i = 0
    while i < len(argv):
        flag = argv[i]
        if flag not in accepted:
            raise UsageError(
                f"unknown procurement plan argument '{flag}'",
                command="procurement",
                remediation=["Use:  3d procurement plan --bom bom.yaml --inventory inventory.yaml"],
            )
        if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
            raise UsageError(
                f"{flag} needs a value",
                command="procurement",
                remediation=[f"Example:  3d procurement plan {flag} bom.yaml --inventory inventory.yaml"],
            )
        values[flag] = argv[i + 1]
        i += 2
    return values


def _plan(argv: list[str]) -> int:
    args = _parse_plan_args(argv)
    bom = args.get("--bom")
    inventory = args.get("--inventory")
    fmt = args.get("--format", "table")
    if bom is None or inventory is None:
        raise UsageError(
            "`procurement plan` needs --bom <file> and --inventory <file>",
            command="procurement",
            remediation=["Example:  3d procurement plan --bom bom.yaml --inventory inventory.yaml"],
        )
    if fmt not in ("table", "json"):
        raise InvalidArgument(
            "--format",
            fmt,
            ("table", "json"),
            command="procurement",
            extra="Use `--format table` for humans or `--format json` for scripts.",
        )

    from procurement import format_plan_table, load_purchase_plan, plan_to_json

    plan = load_purchase_plan(bom, inventory)
    print(plan_to_json(plan) if fmt == "json" else format_plan_table(plan))
    return 0


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    sub = argv[0]
    if sub in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if sub == "plan":
        return _plan(argv[1:])
    raise UsageError(
        f"unknown subcommand '{sub}'",
        command="procurement",
        remediation=["Use `plan` — see `3d procurement --help`."],
    )


COMMAND = Command(
    name="procurement",
    group="LIBRARIES",
    summary="build a deterministic purchase plan from local BOM + inventory files",
    usage=USAGE,
    run=run,
)
