"""macro_cli.py — `3d printer -n NAME macro list|run`, (the shared gates `execute`, `device_of`, `adapter_of`, `heads_of` also serve printer_ops/alias_cli.py).

WHAT: the CLI side of printer_ops/macros.py (what may be sent) and printer_ops/aliases.py (the per-adapter,
  per-model, per-printer shortcut table): discovery of the macros a Klipper printer defines, a checked
  passthrough of one macro with `KEY=VALUE` parameters, and the aliases that expand to G-code lines.

SAFETY, in the order it is enforced (nothing is sent before every step passed):
  1. names/values are typed tokens (macros.build_line, aliases.bind): no newline, `;`, quote, M112;
  2. the printer must be `access: full` (read-only and offline fleet printers refuse; offline are never contacted);
  3. the adapter must have a G-code channel (Moonraker; creality-stock has none);
  4. the command must be defined on the printer (`/printer/gcode/help`); a standard G/M code only through an alias;
  5. `--yes` for a macro outside `trusted_macros`, for dangerous ones and for aliases that ask for it;
  6. an idle printer for motion/heating/dangerous (state re-read here; pause/resume/cancel style is exempt).
  `--dry-run` runs 1-6 (reads only) and prints the exact request instead of sending it; `--offline` contacts
  nothing and prints the script from registry data only.

INVARIANTS: stdlib-only at import time; printer_ops pieces are imported lazily by the caller.
Issue https://github.com/alex-mextner/3d-cli/issues/718.
"""

from __future__ import annotations

import difflib
import json
from typing import TYPE_CHECKING, Any

from errors import InvalidArgument, UsageError
from printer_ops import aliases, macros
from printer_ops.base import BUSY_STATES, PrinterRefused

if TYPE_CHECKING:
    from printer_ops.base import Adapter
    from printer_ops.devices import Device

FLAGS = frozenset({"--yes", "--dry-run", "--offline", "--json", "--all"})
VALUE_OPTS = frozenset({"--timeout"})

MACRO_USAGE = """3d printer -n NAME macro <list|run> ...
  list [--offline] [--dry-run] [--all] [--json]
                          the macros NAME defines (Moonraker: /printer/objects/list + /printer/gcode/help),
                          with trust (* = in its trusted_macros), risk class and description; `_private`
                          ones with --all. --offline: registry data only (trusted_macros + alias targets),
                          nothing is contacted. --dry-run: print the requests, send none.
  run MACRO [KEY=VALUE ...] [--yes] [--dry-run] [--offline] [--timeout S]
                          run ONE macro: the line `MACRO KEY=VALUE ...` is printed exactly as sent.
                          Refused: names that are not identifiers, values with anything but A-Za-z0-9_.,:+/-,
                          M112/EMERGENCY_STOP anywhere, read-only/offline printers, G/M codes that the
                          printer does not define as macros, names the printer does not define.
                          --yes: needed for a macro outside the printer's trusted_macros and for dangerous
                          ones (FIRMWARE_RESTART, RESTART, SAVE_CONFIG, ...). Motion/heating/unknown macros
                          need an idle printer; PAUSE/RESUME/CANCEL_PRINT-style ones do not.
                          --dry-run: checks everything (reads only), prints the request, sends nothing.

Examples:
  3d printer -n u1 macro list
  3d printer -n u1 macro list --offline --json
  3d printer -n u1 macro run SM_PRINT_AUTO_FEED EXTRUDER=1 --yes --dry-run
  3d printer -n k1se macro run PAUSE
  3d printer -n u1 macro run MOVE_TO_XY_IDLE_POSITION_EXTRUDER X_OFFSET=3.5 --yes"""


def split_opts(
    argv: list[str], flags: frozenset[str], values: frozenset[str] = frozenset()
) -> tuple[list[str], dict[str, str]]:
    """Pull known option words out of argv; everything else (macro/alias words, `--nozzle 200`) stays in order."""
    rest: list[str] = []
    opts: dict[str, str] = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in flags:
            opts[a] = "1"
        elif a in values:
            if i + 1 >= len(argv):
                raise UsageError(f"{a} needs a value", command="printer")
            opts[a] = argv[i + 1]
            i += 1
        else:
            rest.append(a)
        i += 1
    return rest, opts


def timeout_of(opts: dict[str, str], default: float) -> float:
    if "--timeout" not in opts:
        return default
    try:
        value = float(opts["--timeout"])
    except ValueError:
        value = -1.0
    if not 0 < value <= 3600:
        raise InvalidArgument(
            "--timeout", opts["--timeout"], ["seconds, 1-3600"], command="printer"
        )
    return value


def device_of(name: str | None, sub: str) -> Device:
    from printer_ops.devices import get_device

    if name is None:
        raise UsageError(
            f"`{sub}` needs the printer: 3d printer -n NAME {sub} ...",
            command="printer",
            remediation=["`3d printer list` shows the configured printers."],
        )
    return get_device(name, offline_ok=True)


def heads_of(device: Device) -> dict[str, aliases.HeadInfo]:
    """Heads of the printer by label (fleet/printers.yaml); a printer the fleet does not describe has T1 only."""
    from printer_ops.moonraker import extruder_name
    from printops.fleet import load_fleet

    fleet = load_fleet()
    entry = fleet.printers.get(device.name) if fleet is not None else None
    if entry is None or not entry.heads:
        return {"T1": aliases.HeadInfo(0, "extruder", True, None)}
    return {
        h.label.upper(): aliases.HeadInfo(
            h.index, h.extruder or extruder_name(h.index), h.usable, h.reason
        )
        for h in entry.heads
    }


def adapter_of(device: Device) -> Adapter:
    from printer_ops import make_adapter

    adapter = make_adapter(device)
    adapter.gcode_request(
        ""
    )  # UnsupportedOperation when the adapter has no G-code channel (creality-stock)
    return adapter


def _check_defined(
    device: Device, lines: list[str], catalog: macros.MacroCatalog, *, curated: bool
) -> None:
    for line in lines:
        cmd = macros.command_of(line)
        if cmd in device.trusted_macros:
            continue
        if macros.is_numeric_code(cmd):
            if curated or cmd in catalog.macros:
                continue
            raise PrinterRefused(
                f"{cmd} is a standard G-code, not a macro {device.name} defines: `macro run` sends macros only",
                command="printer",
                remediation=[
                    "Use an alias for the usual ones (`3d printer aliases`), or define the macro in the "
                    "printer's config."
                ],
            )
        if not catalog.knows(cmd):
            near = difflib.get_close_matches(
                cmd, sorted(catalog.commands.keys() | catalog.macros), n=3
            )
            raise PrinterRefused(
                f"{device.name} does not define {cmd}",
                command="printer",
                remediation=[
                    f"Did you mean: {', '.join(near)}"
                    if near
                    else f"`3d printer -n {device.name} macro list` shows what it defines."
                ],
            )


def require_full_access(device: Device, what: str) -> None:
    """Only `access: full` printers take a write; read-only ones may still be read, offline ones are never contacted."""
    if device.access == "full":
        return
    hint = (f"Only read it: 3d printer status {device.name}" if device.access == "read-only"
            else "3d never contacts an offline fleet printer; run its jobs by hand.")
    raise PrinterRefused(f"{device.name} is {device.access} in {device.source}: {what} refused",
                         command="printer", remediation=[hint])


def execute(
    device: Device,
    lines: list[str],
    *,
    what: str,
    again: str,
    kind: str | None,
    curated: bool,
    entry_yes: bool,
    timeout: float,
    opts: dict[str, str],
) -> int:
    require_full_access(device, what)
    verdict = macros.assess(
        lines, device.trusted_macros, curated=curated, kind=kind, entry_yes=entry_yes
    )
    script = "\n".join(lines)
    adapter = adapter_of(device)
    dry, offline = "--dry-run" in opts, "--offline" in opts
    header = f"{device.name} ({device.adapter} {device.host}:{device.port}): {what}"
    if not offline:
        _check_defined(device, lines, adapter.macro_catalog(), curated=curated)
    need_yes = bool(verdict.yes_reasons) and "--yes" not in opts
    if need_yes and not (dry or offline):
        raise UsageError(
            f"{what} needs --yes: {'; '.join(verdict.yes_reasons)}",
            command="printer",
            remediation=[
                f"Confirm explicitly: 3d printer -n {device.name} {again} --yes"
            ],
        )
    if verdict.needs_idle and not offline:
        st = adapter.status()
        if st.state in BUSY_STATES:
            raise PrinterRefused(
                f"{device.name} is {st.state} [{st.raw_state}]: {what} is {verdict.kind} and needs an idle printer",
                command="printer",
                remediation=[
                    "Wait for the job to end; pause/resume/cancel style macros (PAUSE, RESUME, CANCEL_PRINT) "
                    "are allowed while it runs."
                ],
            )
    print(header)
    print("gcode:")
    for line in lines:
        print(f"  {line}")
    if need_yes:
        print(f"would need --yes: {'; '.join(verdict.yes_reasons)}")
    if offline:
        print(
            f"{adapter.gcode_request(script)}\noffline: nothing was contacted; the printer's idle state and its "
            "macro list were not checked"
        )
        return 0
    if dry:
        print(f"{adapter.gcode_request(script)}\ndry-run: nothing was sent")
        return 0
    adapter.run_gcode(script, timeout=timeout)
    print(f"sent to {device.name}; the printer answered ok")
    return 0


def _registry_rows(device: Device) -> list[dict[str, Any]]:
    """Macros known without contacting the printer: its trusted_macros and the commands of its aliases."""
    rows: dict[str, dict[str, Any]] = {}
    for n in sorted(device.trusted_macros):
        rows[n] = {
            "name": n,
            "trusted": True,
            "kind": macros.classify(n),
            "description": "",
            "source": "trusted_macros",
        }
    table = aliases.supported(
        aliases.resolve(device.adapter, device.model, aliases.overrides_for(device.name))
    )
    for alias, spec in table.items():
        for line in spec.gcode:
            cmd = macros.command_of(line)
            if cmd and "{" not in cmd and cmd not in rows:
                rows[cmd] = {
                    "name": cmd,
                    "trusted": False,
                    "kind": macros.classify(cmd),
                    "description": "",
                    "source": f"alias {alias}",
                }
    return sorted(rows.values(), key=lambda r: str(r["name"]))


def _print_rows(rows: list[dict[str, Any]], as_json: bool, title: str) -> None:
    if as_json:
        print(json.dumps(rows, indent=2))
        return
    print(title)
    width = max((len(str(r["name"])) for r in rows), default=4)
    print(f"  {'NAME':<{width}}  T  {'KIND':<9} DESCRIPTION")
    for r in rows:
        print(
            f"  {r['name']:<{width}}  {'*' if r['trusted'] else ' '}  {r['kind']:<9} {r['description'] or r.get('source', '')}"
        )


def _macro_list(device: Device, opts: dict[str, str]) -> int:
    as_json = "--json" in opts
    if device.access == "offline" or "--offline" in opts:
        rows = _registry_rows(device)
        _print_rows(
            rows,
            as_json,
            f"{device.name}: {len(rows)} macros known from the registry (nothing contacted; "
            "T * = in trusted_macros)",
        )
        return 0
    adapter = adapter_of(device)
    if "--dry-run" in opts:
        print("\n".join(adapter.catalog_requests()))
        print("dry-run: nothing was sent")
        return 0
    catalog = adapter.macro_catalog()
    names = sorted(
        catalog.macros
        if "--all" in opts
        else {m for m in catalog.macros if not m.startswith("_")}
    )
    rows = [
        {
            "name": n,
            "trusted": n in device.trusted_macros,
            "kind": macros.classify(n),
            "description": catalog.describe(n),
            "source": "printer",
        }
        for n in names
    ]
    hidden = len(catalog.macros) - len(names)
    note = f", {hidden} private (_NAME) hidden: --all" if hidden else ""
    _print_rows(
        rows,
        as_json,
        f"{device.name}: {len(rows)} macros defined by the printer{note} (T * = in trusted_macros)",
    )
    return 0


def _macro_run(device: Device, words: list[str], opts: dict[str, str]) -> int:
    if not words:
        raise UsageError(
            "`macro run` needs the macro name",
            command="printer",
            remediation=["3d printer macro --help"],
        )
    line = macros.build_line(words[0], words[1:])
    return execute(
        device,
        [line],
        what=f"macro run {line}",
        again=f"macro run {line}",
        kind=None,
        curated=False,
        entry_yes=False,
        timeout=timeout_of(opts, 120.0),
        opts=opts,
    )


def run_macro(name: str | None, argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(MACRO_USAGE)
        return 0 if argv else 1
    sub, rest = argv[0], argv[1:]
    if sub not in ("list", "run"):
        raise UsageError(
            f"unknown macro subcommand '{sub}'",
            command="printer",
            remediation=["Use list or run."],
        )
    if any(a in ("-h", "--help") for a in rest):
        print(MACRO_USAGE)
        return 0
    words, opts = split_opts(rest, FLAGS, VALUE_OPTS)
    device = device_of(name, f"macro {sub}")
    if sub == "list":
        if words:
            raise UsageError(
                f"`macro list` takes no arguments, got {words}", command="printer"
            )
        return _macro_list(device, opts)
    return _macro_run(device, words, opts)
