"""3d inventory — maintain a local materials/parts JSON inventory."""
from __future__ import annotations

from typing import TYPE_CHECKING

from cli.registry import Command
from errors import InvalidArgument, UsageError

if TYPE_CHECKING:
    from inventory import InventoryItem

USAGE = """3d inventory <subcommand>
  Maintain a local JSON inventory of materials and parts at ~/.config/3d-cli/inventory.json.

Subcommands:
  list [materials|parts]       list all inventory, or only one kind
  add material <name> --qty N --unit U [--location TEXT] [--notes TEXT]
  add part <name> --qty N [--unit U] [--material TEXT] [--location TEXT] [--notes TEXT]
  show <material|part> <name>  show one inventory record

Why: keep a small local stock list next to the CLI, so scripts and agents can validate
what material spools and reusable parts are already available before planning a print.

Examples:
  3d inventory add material PLA --qty 1 --unit spool --location "bin 2"
  3d inventory add part "M3 nut" --qty 25 --material steel --notes "drawer A"
  3d inventory list materials
  3d inventory show part "M3 nut"
"""


def _print_usage() -> None:
    print(USAGE)


def _parse_options(argv: list[str]) -> dict[str, str]:
    opts: dict[str, str] = {}
    i = 0
    accepted = {"--qty", "--unit", "--location", "--material", "--notes"}
    while i < len(argv):
        flag = argv[i]
        if flag not in accepted:
            raise UsageError(
                f"unknown option '{flag}'",
                command="inventory",
                remediation=["Run `3d inventory --help` for accepted add options."],
            )
        if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
            raise UsageError(
                f"{flag} needs a value",
                command="inventory",
                remediation=[f"Provide a value after `{flag}`."],
            )
        key = flag[2:]
        if key in opts:
            raise UsageError(
                f"duplicate option '{flag}'",
                command="inventory",
                remediation=["Pass each add option at most once."],
            )
        opts[key] = argv[i + 1]
        i += 2
    return opts


def _require_quantity(opts: dict[str, str]) -> float:
    raw = opts.get("qty")
    if raw is None:
        raise UsageError(
            "`add` needs --qty",
            command="inventory",
            remediation=["Example:  3d inventory add part \"M3 nut\" --qty 25"],
        )
    try:
        return float(raw)
    except ValueError:
        raise InvalidArgument(
            "--qty",
            raw,
            ["positive finite number"],
            command="inventory",
        ) from None


def _format_qty(quantity: float) -> str:
    return f"{quantity:g}"


def _list(argv: list[str]) -> int:
    from inventory import list_items

    if len(argv) > 1:
        raise UsageError(
            "`list` accepts at most one kind",
            command="inventory",
            remediation=["Use `3d inventory list`, `list materials`, or `list parts`."],
        )
    if argv:
        items = list_items(argv[0])
        _print_table(argv[0], items)
        return 0
    grouped = list_items()
    _print_table("materials", grouped["materials"])
    print()
    _print_table("parts", grouped["parts"])
    return 0


def _print_table(kind: str, items: list[InventoryItem]) -> None:
    from inventory import KINDS

    title = kind if kind in KINDS else f"{kind}s"
    print(title.upper())
    if not items:
        print("  (none)")
        return
    name_w = max(max(len(i.name) for i in items), len("NAME"))
    print(f"  {'NAME':<{name_w}}  {'QTY':>8}  UNIT  LOCATION")
    for item in items:
        print(
            f"  {item.name:<{name_w}}  {_format_qty(item.quantity):>8}  "
            f"{item.unit:<4}  {item.location or '-'}"
        )


def _add(argv: list[str]) -> int:
    from inventory import add_item

    if len(argv) < 2:
        raise UsageError(
            "`add` needs a kind and name",
            command="inventory",
            remediation=["Example:  3d inventory add material PLA --qty 1 --unit spool"],
        )
    kind = argv[0]
    name = argv[1]
    opts = _parse_options(argv[2:])
    if kind in ("material", "materials") and "material" in opts:
        raise UsageError(
            "`--material` is only valid when adding a part",
            command="inventory",
            remediation=["For materials, put the material name in `<name>` and omit `--material`."],
        )
    item = add_item(
        kind,
        name,
        quantity=_require_quantity(opts),
        unit=opts.get("unit"),
        location=opts.get("location"),
        material=opts.get("material"),
        notes=opts.get("notes"),
    )
    print(f"Added {item.kind}: {item.name} ({_format_qty(item.quantity)} {item.unit})")
    return 0


def _show(argv: list[str]) -> int:
    from inventory import get_item

    if len(argv) < 2:
        raise UsageError(
            "`show` needs a kind and name",
            command="inventory",
            remediation=['Example:  3d inventory show part "M3 nut"'],
        )
    if len(argv) > 2:
        raise UsageError(
            "`show` accepts exactly a kind and name",
            command="inventory",
            remediation=["Quote names that contain spaces."],
        )
    item = get_item(argv[0], argv[1])
    rows = [
        ("kind", item.kind),
        ("quantity", f"{_format_qty(item.quantity)} {item.unit}"),
        ("location", item.location or "(unspecified)"),
        ("material", item.material or "(unspecified)"),
        ("notes", item.notes or "(unspecified)"),
    ]
    width = max(len(k) for k, _ in rows)
    print(item.name)
    for key, val in rows:
        print(f"  {key:<{width}}  {val}")
    return 0


def run(argv: list[str]) -> int:
    if not argv:
        _print_usage()
        return 1
    sub = argv[0]
    if sub in ("-h", "--help", "help"):
        _print_usage()
        return 0
    rest = argv[1:]
    if sub == "list":
        return _list(rest)
    if sub == "add":
        return _add(rest)
    if sub == "show":
        return _show(rest)
    raise UsageError(
        f"unknown subcommand '{sub}'",
        command="inventory",
        remediation=["Use `list`, `add`, or `show` — see `3d inventory --help`."],
    )


COMMAND = Command(
    name="inventory",
    group="ENVIRONMENT",
    summary="maintain local materials/parts inventory: list / add / show",
    usage=USAGE,
    run=run,
)
