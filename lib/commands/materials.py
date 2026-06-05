"""3d materials — inspect the FDM material registry.

WHAT: lists every known filament or resin, and shows the full datasheet (density,
  mechanical properties, max temp, finish) for a single material by name.

WHY: every part in 3d.yaml references a material BY NAME. Before you write that name
  you need to know what the tool knows — which survives heat, which is dense, what
  the cross-layer strength knockdown is. `list` is the menu; `show` is the spec sheet.

Examples:
  3d materials list                   # table: name, density, max temp, finish
  3d materials show PETG              # full datasheet for one material
  3d materials show PLA               # see the anisotropic knockdown factor

ROADMAP §2a: "Materials & printers — shared, cross-cutting vocabularies.
  Single canonical registries materials.yaml + printers.yaml (built-in defaults +
  user/project overrides), referenced BY NAME everywhere."

ACCESSED VIA: `3d materials list` / `3d materials show <name>`. Thin CLI over the headless
lib/materials.py loader (stdlib-only at top level; the loader lazy-imports yaml). INVARIANT:
this module only parses argv and prints — all merge/validation/error logic lives in the loader.
"""
from __future__ import annotations

from cli.registry import Command
from errors import UsageError

USAGE = """3d materials <subcommand>
  Why: every part picks a material BY NAME (in 3d.yaml). This command shows what those names
  mean — density (for mass/cost), max service temp, and the mechanical numbers `3d strength`
  uses — so you can choose a filament without leaving the terminal or guessing its properties.

  list                    table of all materials: name, density, max temp, finish
                            Why: pick a filament at a glance (which survives heat? which is dense?).
                            Example:  3d materials show
  show <name>             all properties of one material (mechanical + display + anisotropy)
                            Why: see the full datasheet-ish view, incl. the cross-layer strength
                            knockdown, before committing a part to it.
                            Example:  3d materials show PETG

The registry merges three layers (later overrides earlier, field-by-field):
  built-in defaults  <  ~/.config/3d-cli/materials.yaml  <  ./materials.yaml (next to 3d.yaml)
so you can override a single property (e.g. a spool's real density) without redefining the rest.

Examples:
  3d materials list
  3d materials show PLA"""


def _fmt_temp(c: float) -> str:
    return f"{c:g} C"


def _list() -> int:
    from materials import load_materials  # lazy: pulls the loader (and yaml) only when used

    mats = load_materials()
    name_w = max((len(n) for n in mats), default=4)
    name_w = max(name_w, len("NAME"))
    print(f"{'NAME':<{name_w}}  {'DENSITY':>9}  {'MAX TEMP':>9}  FINISH")
    for name in sorted(mats):
        m = mats[name]
        print(
            f"{name:<{name_w}}  {m.density:>6.2f} g/cm3  {_fmt_temp(m.max_temp_c):>9}  {m.finish}"
        )
    print()
    print("Detail:  3d materials show <name>   (e.g. 3d materials show PETG)")
    return 0


def _show(name: str) -> int:
    from materials import get_material  # lazy: pulls the loader (and yaml) only when used

    m = get_material(name)  # raises InvalidArgument (listing accepted names) on an unknown one
    rows = [
        ("density", f"{m.density:g} g/cm3"),
        ("e_modulus", f"{m.e_modulus_mpa:g} MPa  (tensile / Young's, in-plane)"),
        ("tensile", f"{m.tensile_mpa:g} MPa  (ultimate, in-plane)"),
        ("yield", f"{m.yield_mpa:g} MPa  (in-plane)"),
        ("max_temp", _fmt_temp(m.max_temp_c) + "  (practical continuous service)"),
        ("color", m.color),
        ("finish", m.finish),
        ("layer_adhesion", f"{m.layer_adhesion:g}  (cross-layer strength factor, 1.0 = isotropic)"),
    ]
    label_w = max(len(k) for k, _ in rows)
    print(m.name)
    for key, val in rows:
        print(f"  {key:<{label_w}}  {val}")
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
        return _list()
    if sub == "show":
        if len(argv) < 2:
            raise UsageError(
                "`show` needs a material name",
                command="materials",
                remediation=["Run `3d materials list` for the names, then e.g. `3d materials show PLA`."],
            )
        return _show(argv[1])
    raise UsageError(
        f"unknown subcommand '{sub}'",
        command="materials",
        remediation=["Use `list` or `show <name>` — see `3d materials --help`."],
    )


COMMAND = Command(
    name="materials",
    group="ENVIRONMENT",
    summary="inspect FDM material properties: list / show <name>",
    usage=USAGE,
    run=run,
)
