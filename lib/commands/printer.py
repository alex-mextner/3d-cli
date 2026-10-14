"""3d printer — drive configured physical printers: list, status, upload, start, cancel.

WHAT: talks to the printers named in printer-devices.yaml through a protocol adapter —
  `moonraker` (Klipper/Moonraker, Snapmaker U1) or `creality-stock` (Creality K1 family on
  stock firmware: HTTP upload + WebSocket 9999). `3d printers` (plural) is the MODEL spec
  registry; this command is about the machines on the LAN.

WHY: sending and starting a job was hand-written HTTP/WebSocket code per printer and per
  project (epic https://github.com/alex-mextner/3d-cli/issues/90). One command with one set
  of safety rules (issue #95) replaces those scripts.

SAFETY (enforced here, before any write):
  - upload only stores the file; it never starts a print;
  - start and cancel need --yes; start refuses a printing/paused/busy/unreadable printer,
    re-reads state immediately before the write, and runs `3d print-preflight` (FAIL blocks
    unless --force);
  - a failed/timed-out start is reported as UNKNOWN and never resent automatically.

INVARIANTS: stdlib-only at import time (registry contract); printer_ops is imported lazily.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from cli.registry import Command
from errors import InputNotFound, UsageError

if TYPE_CHECKING:
    from pathlib import Path

    from printer_ops.base import Adapter, PrinterStatus

USAGE = """3d printer <subcommand> [args]
  Drive the printers configured in printer-devices.yaml (user: ~/.config/3d-cli/,
  project: ./printer-devices.yaml). Adapters: moonraker (Klipper/Moonraker, Snapmaker U1),
  creality-stock (Creality K1 family on stock firmware).

  list                                 configured printers (name, adapter, address, model)
  status NAME [--json] [--secs S]      state, job, progress, temperatures, loaded heads
  upload NAME FILE.gcode [--as REMOTE] store the file on the printer; never starts a print
  start NAME FILE.gcode --yes [--as REMOTE] [--source-3mf X.3mf] [--force] [--secs S]
                                       start a file that was uploaded before; runs
                                       `3d print-preflight` first
  cancel NAME --yes                    cancel the running/paused job

Options:
  --json             status as JSON (normalized state + raw firmware payload)
  --secs S           Creality: seconds to listen on ws:9999 for pushed state (default 3)
  --as REMOTE        remote file name (default: local name, spaces -> _)
  --yes              required for start and cancel: you confirm the physical action
  --force            start even when preflight FAILs (the failing checks are printed)
  --source-3mf FILE  the 3MF the G-code was sliced from; enables the halves-check

Safety: start refuses while the printer is printing, paused, busy or unreadable, and when
the remote file is missing or differs in size from FILE.gcode. A start whose outcome is
unknown is never resent. Creality upload is refused while the printer is busy.

Examples:
  3d printer list
  3d printer status u1
  3d printer status k1se --secs 5
  3d printer status u1 --json | jq '.slots[] | {label, color, material}'
  3d printer upload u1 out/part.gcode
  3d printer upload k1se "out/roof left.gcode" --as roof_left.gcode
  3d printer start u1 out/part.gcode --yes
  3d printer start u1 out/base.gcode --yes --source-3mf print/base.3mf
  3d printer start k1se out/roof.gcode --yes --force
  3d printer cancel k1se --yes"""

_VALUE = {"--secs", "--as", "--source-3mf"}
_FLAGS = {"--json", "--yes", "--force"}


def _parse(argv: list[str], allowed: set[str], n_pos: int, sub: str) -> tuple[list[str], dict[str, str]]:
    pos: list[str] = []
    opts: dict[str, str] = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in _VALUE and a in allowed:
            if i + 1 >= len(argv):
                raise UsageError(f"{a} needs a value", command="printer")
            opts[a] = argv[i + 1]
            i += 2
            continue
        if a in _FLAGS and a in allowed:
            opts[a] = "1"
        elif a.startswith("--"):
            raise UsageError(f"unknown option {a} for `printer {sub}`", command="printer",
                             remediation=["See `3d printer --help`."])
        else:
            pos.append(a)
        i += 1
    if len(pos) != n_pos:
        raise UsageError(f"`printer {sub}` takes {n_pos} positional argument(s), got {len(pos)}: {pos}",
                         command="printer", remediation=["See `3d printer --help`."])
    return pos, opts


def _adapter(name: str, opts: dict[str, str]) -> Adapter:
    from printer_ops import adapter_by_name

    return adapter_by_name(name, secs=opts.get("--secs"))


def _fmt_temp(v: float | None) -> str:
    return "-" if v is None else f"{v:.0f}" if float(v).is_integer() else f"{v:.1f}"


def _render_status(name: str, adapter: Adapter, st: PrinterStatus) -> str:
    lines = [f"{name}  ({adapter.kind}  {adapter.host}:{adapter.port}{'  ' + st.model if st.model else ''})"]
    lines.append(f"  state     {st.state}  [{st.raw_state}]")
    if st.filename:
        prog = f"  {st.progress * 100:.1f}%" if st.progress is not None else ""
        lines.append(f"  file      {st.filename}{prog}")
    if st.temps:
        lines.append("  temps     " + "  ".join(f"{k} {_fmt_temp(a)}/{_fmt_temp(t)}" for k, (a, t) in st.temps.items()))
    if st.slots:
        heads = [
            f"{s.label} {s.color or '?'} {s.material or ''}".rstrip() if s.present else f"{s.label} empty"
            for s in st.slots
        ]
        lines.append("  heads     " + " | ".join(heads))
    if st.message:
        lines.append(f"  message   {st.message}")
    return "\n".join(lines)


def _cmd_list() -> int:
    from printer_ops.devices import EXAMPLE, load_devices, search_paths

    devices = load_devices()
    if not devices:
        print("No printers configured. Looked in:")
        for p in search_paths():
            print(f"  {p}")
        print("\nCreate one of them, e.g.:\n" + EXAMPLE)
        return 0
    for d in sorted(devices.values(), key=lambda d: d.name):
        print(f"  {d.name:<10} {d.adapter:<15} {d.host}:{d.port:<6} {d.model or '-':<16} {d.source}")
    return 0


def _cmd_status(argv: list[str]) -> int:
    import json

    (name,), opts = _parse(argv, {"--json", "--secs"}, 1, "status")
    adapter = _adapter(name, opts)
    st = adapter.status()
    if "--json" in opts:
        print(json.dumps({"printer": name, "adapter": adapter.kind, **st.to_json()}, indent=1, default=str))
    else:
        print(_render_status(name, adapter, st))
    return 0


def _local(path: str) -> Path:
    from pathlib import Path

    p = Path(path)
    if not p.is_file():
        raise InputNotFound(path, command="printer")
    return p


def _cmd_upload(argv: list[str]) -> int:
    from printer_ops.base import PrinterRefused, safe_remote_name

    (name, file), opts = _parse(argv, {"--as", "--secs"}, 2, "upload")
    local = _local(file)
    remote = safe_remote_name(local, opts.get("--as"))
    adapter = _adapter(name, opts)
    st = adapter.status()
    if st.busy and (not adapter.upload_while_busy or st.filename == remote):
        why = "it would overwrite the file being printed" if st.filename == remote else \
            f"the {adapter.kind} firmware does not protect a running job from file writes"
        raise PrinterRefused(
            f"{name} is {st.state} ({st.filename or 'no file'}); upload refused: {why}",
            command="printer", remediation=[f"Wait until it is idle: 3d printer status {name}"],
        )
    res = adapter.upload(local, remote)
    print(f"uploaded {res.name} to {name}: {res.size} B, size verified on the printer. NOT started.")
    if res.note:
        print(f"note: {res.note}")
    print(f"Start it with:  3d printer start {name} {file} --yes")
    return 0


def _refuse_if_busy(name: str, st: PrinterStatus, when: str) -> None:
    from printer_ops.base import PrinterRefused

    if st.busy:
        raise PrinterRefused(
            f"{name} is {st.state} [{st.raw_state}]{' with ' + st.filename if st.filename else ''}; "
            f"start refused ({when})",
            command="printer",
            remediation=[f"Check: 3d printer status {name}", f"Cancel the job only if intended: 3d printer cancel {name} --yes"],
        )


def _cmd_start(argv: list[str]) -> int:
    from pathlib import Path

    from printer_ops.base import PrinterCommError, PrinterRefused, safe_remote_name
    from printer_ops.preflight import FAIL, run_preflight

    (name, file), opts = _parse(argv, {"--yes", "--force", "--as", "--source-3mf", "--secs"}, 2, "start")
    if "--yes" not in opts:
        raise UsageError(
            "start is a physical action and needs --yes",
            command="printer",
            remediation=[f"Confirm explicitly: 3d printer start {name} {file} --yes"],
        )
    local = _local(file)
    source = Path(opts["--source-3mf"]) if "--source-3mf" in opts else None
    if source is not None and not source.is_file():
        raise InputNotFound(str(source), command="printer")
    remote = safe_remote_name(local, opts.get("--as"))
    size = local.stat().st_size
    adapter = _adapter(name, opts)

    st = adapter.status()
    _refuse_if_busy(name, st, "before preflight")
    stored = adapter.list_files(expect=(remote, size)).get(remote)
    if stored is None or stored.size != size:
        got = "absent" if stored is None else f"{stored.size} B"
        raise PrinterRefused(
            f"{remote} on {name} is {got}; local {file} is {size} B — start refused",
            command="printer", remediation=[f"Upload it first: 3d printer upload {name} {file}"],
        )

    limits, limits_source = adapter.axis_limits()
    report = run_preflight(local, status=st, source_3mf=source, axis_limits=limits, limits_source=limits_source)
    print(report.render())
    if report.overall == FAIL:
        if "--force" not in opts:
            raise PrinterRefused("preflight FAIL; start refused", command="printer",
                                 remediation=["Fix the FAIL lines above, or pass --force to start anyway."])
        forced = ", ".join(c.name for c in report.checks if c.status == FAIL)
        print(f"FORCED: starting despite preflight FAIL ({forced})")

    _refuse_if_busy(name, adapter.status(), "state changed during preflight")
    try:
        adapter.start(remote)
    except PrinterCommError as exc:
        raise PrinterCommError(
            f"start request for {remote} on {name} failed: outcome UNKNOWN ({exc.message})",
            command="printer",
            remediation=[f"Do NOT resend blindly. Check: 3d printer status {name}"],
        ) from exc
    after = adapter.status()
    print(f"start sent: {remote} on {name}")
    print(_render_status(name, adapter, after))
    return 0


def _cmd_cancel(argv: list[str]) -> int:
    from printer_ops.base import ACTIVE_JOB_STATES, PrinterRefused

    (name,), opts = _parse(argv, {"--yes", "--secs"}, 1, "cancel")
    if "--yes" not in opts:
        raise UsageError("cancel stops a physical job and needs --yes", command="printer",
                         remediation=[f"Confirm explicitly: 3d printer cancel {name} --yes"])
    adapter = _adapter(name, opts)
    st = adapter.status()
    if st.state not in ACTIVE_JOB_STATES:
        raise PrinterRefused(f"{name} is {st.state} [{st.raw_state}]: no job to cancel", command="printer")
    adapter.cancel()
    print(f"cancel sent to {name} (job {st.filename or '?'})")
    print(_render_status(name, adapter, adapter.status()))
    return 0


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    sub, rest = argv[0], argv[1:]
    if sub in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if sub == "list":
        return _cmd_list()
    if sub == "status":
        return _cmd_status(rest)
    if sub == "upload":
        return _cmd_upload(rest)
    if sub == "start":
        return _cmd_start(rest)
    if sub == "cancel":
        return _cmd_cancel(rest)
    raise UsageError(f"unknown subcommand '{sub}'", command="printer",
                     remediation=["Use list, status, upload, start or cancel — see `3d printer --help`."])


COMMAND = Command(
    name="printer",
    group="SLICING",
    summary="drive configured printers: list / status / upload / start --yes / cancel --yes",
    usage=USAGE,
    run=run,
)
