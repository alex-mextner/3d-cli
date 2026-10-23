"""3d printer — drive configured physical printers: list, status, upload, start, cancel, watch, vision, bed, subscribe, agent, todo.

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
    re-reads state immediately before the write, and runs `3d print-preflight` plus the camera
    `bed` check (printer_ops/bed.py; FAIL blocks unless --force);
  - a failed/timed-out start is reported as UNKNOWN and never resent automatically.

INVARIANTS: stdlib-only at import time (registry contract); printer_ops is imported lazily.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cli.registry import Command
from errors import InputNotFound, UsageError

if TYPE_CHECKING:
    from pathlib import Path

    from printer_ops.base import Adapter, PrinterStatus
    from printer_ops.devices import Device
    from printops.fleet import FleetPrinter

USAGE = """3d printer [-n NAME] <subcommand> [args]
  Drive the printers configured in printer-devices.yaml (user: ~/.config/3d-cli/,
  project: ./printer-devices.yaml) on top of the fleet registry fleet/printers.yaml in the
  3d-cli repo. Adapters: moonraker (Klipper/Moonraker, Snapmaker U1), creality-stock
  (Creality K1 family on stock firmware). Fleet printers with access `offline` (other
  people's) are listed but never contacted; `read-only` ones refuse upload/start/cancel.

  list [--json]                        every fleet + configured printer: owner, access,
                                       model, address, heads (unusable ones with reason)
  status NAME [--json] [--secs S]      state, job, progress, temperatures, loaded heads
                                       (everything about one printer — web UIs, queue, job
                                       history, settings: `3d printers show NAME`)
  upload NAME FILE.gcode [--as REMOTE] store the file on the printer; never starts a print
  start NAME FILE.gcode --yes [--as REMOTE] [--source-3mf X.3mf] [--bridge-max MM] [--force] [--secs S]
                                       start a file that was uploaded before; runs
                                       `3d print-preflight` first, plus the camera bed check
                                       (objects on the bed / plate missing FAIL) when the
                                       printer has watch.bed.enabled
  cancel NAME --yes                    cancel the running/paused job
  guard NAME --job-id ID --yes         one-job automatic PAUSE on confirmed camera failure;
                                       local evidence only (see `3d printer guard --help`)
  watch NAME...|--all [options]        live read-only subscription: anomaly events to
                                       stdout JSONL / --exec / --steer SESSION / subscribers
                                       (see `3d printer watch --help`)
  vision NAME [options]                camera failure detection (spaghetti, detached part, stale/dark camera)
                                       into the same event sinks as watch; also
                                       `vision eval --pos P.. --neg N..`, `vision record NAME DIR [--timeout S]`,
                                       `vision fetch-model [--tier a|b|all]`, `vision fit-head ..`,
                                       `vision doctor` (read-only: does any service run --dry-run?)
                                       (see docs/commands/printer-vision.md)
  bed status|reference|monitor NAME    bed state from the camera: clear/occupied, build plate
                                       present/missing; `reference NAME --capture` takes the
                                       empty-bed reference (see `3d printer bed --help`)
  subscribe --session ID [..]          printer alerts go to that omp session (it looks at the
                                       frame, tells the user only if real); `unsubscribe`,
                                       `subscriptions [list|test]` (see `3d printer subscribe --help`)
  agent list|stop|handoff              the temporary per-printer omp agent that gets the alerts
                                       no live subscriber took (see `3d printer agent --help`)
  doctor [NAME|--all] [--fix] [--yes] [--json]
                                       diagnose (and with --fix repair) the printer's setup: Klippy,
                                       Moonraker, Spoolman link/CORS, active spool, camera, watch
                                       services (same as `3d printers doctor`; see `3d printers doctor --help`)
  todo list|add|done|dismiss|snooze|reopen|import   (alias: issues)
                                       the printer's to-do list: needed calibrations, due
                                       maintenance, repairs, manual tasks (3d-api; offline a
                                       JSONL buffer — see `3d printer todo --help`). A printer's
                                       problems are tasks of this list, not GitHub tickets.
  -n, --name NAME                      the printer for `todo`/`issues`:
                                       `3d printer -n u1 issues list`

Options:
  --json             list/status as JSON (status: normalized state + raw firmware payload)
  --secs S           Creality: seconds to listen on ws:9999 for pushed state (default 3)
  --as REMOTE        remote name stem (default: local name, spaces -> _); the stored name
                     always ends in -<sha256[:16]>.gcode; give start the same --as
  --yes              required for start, cancel and guard: you confirm the physical action
  --force            start even when preflight FAILs (the failing checks are printed)
  --source-3mf FILE  the project 3MF the G-code was sliced from: head-sequence check + halves-check
                     (without it the head-sequence check uses NAME.3mf next to the G-code)
  --bridge-max MM    preflight overhangs gate: longest bridge; overrides the printer's
                     bridge_max_mm in printer-devices.yaml (default 15)

Safety: start refuses while the printer is printing, paused, busy or unreadable, and when
the content-addressed remote file is missing, differs in size, or (Moonraker, read back)
differs in SHA-256 from FILE.gcode. Untrusted macros fail preflight. A start counts only
when the printer acknowledges it; otherwise, or when the state after it is unreadable, the
outcome is UNKNOWN and nothing is resent. Creality upload is refused while the printer is busy.

Examples:
  3d printer list
  3d printer list --json | jq '.[] | {name, access, heads: [.heads[] | select(.usable | not)]}'
  3d printer status u1
  3d printer status k1se --secs 5
  3d printer status u1 --json | jq '.slots[] | {label, color, material}'
  3d printer upload u1 out/part.gcode
  3d printer upload k1se "out/roof left.gcode" --as roof_left.gcode
  3d printer start u1 out/part.gcode --yes
  3d printer start u1 out/base.gcode --yes --source-3mf print/base.3mf
  3d printer start k1se out/roof.gcode --yes --force
  3d printer cancel k1se --yes
  3d printer watch u1 k1se --steer 01a12cd6
  3d printer vision u1 --once --save /tmp/u1.jpg
  3d printer vision u1
  3d printer subscribe --session 01a12cd6 --printer u1,k1se
  3d printer subscriptions
  3d printer agent list
  3d printer guard u1 --job-id 00006D --yes
  3d printer doctor --all
  3d printer doctor k1se --fix
  3d printer todo list u1
  3d printer -n k1se issues add --title "X belt squeaks" --type inspect"""

_VALUE = {"--secs", "--as", "--source-3mf", "--bridge-max"}
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


def _adapter(name: str, opts: dict[str, str], *, write: bool = False) -> Adapter:
    """Adapter of printer `name`; `write` (upload/start/cancel) refuses a read-only printer."""
    from printer_ops import adapter_by_name

    if write:
        from printer_ops.base import PrinterRefused
        from printer_ops.devices import get_device

        device = get_device(name)
        if device.access != "full":
            raise PrinterRefused(f"{name} is {device.access} in {device.source}: upload/start/cancel refused",
                                 command="printer",
                                 remediation=[f"Only read it: 3d printer status {name}"])
    return adapter_by_name(name, secs=opts.get("--secs"))


def _list_row(name: str, printer: FleetPrinter | None, device: Device | None) -> dict[str, Any]:
    """One `printer list` entry: fleet facts (owner, topology) + the device that would be contacted."""
    from dataclasses import asdict

    return {
        "name": name,
        "model": (device.model if device else None) or (printer.model if printer else None),
        "owner": printer.owner if printer else None,
        "access": device.access if device else printer.access if printer else None,
        "adapter": device.adapter if device else None,
        "address": f"{device.host}:{device.port}" if device else None,
        "settings": str(device.source) if device else None,
        "fleet": str(printer.source) if printer else None,
        "heads": [asdict(h) for h in printer.heads] if printer else [],
        "ams": [asdict(u) for u in printer.ams] if printer and printer.ams is not None else None,
        "enclosure": printer.enclosure if printer else None,
        "chamber_heater": printer.chamber_heater if printer else None,
        "plates": list(printer.plates) if printer and printer.plates is not None else None,
    }


def _cmd_list(argv: list[str]) -> int:
    import json

    from printer_ops.devices import EXAMPLE, load_devices, search_paths
    from printops.fleet import fleet_path, load_fleet

    _, opts = _parse(argv, {"--json"}, 0, "list")
    fleet = load_fleet()
    printers = fleet.printers if fleet else {}
    devices = load_devices()
    rows = [_list_row(n, printers.get(n), devices.get(n)) for n in sorted(set(printers) | set(devices))]
    if "--json" in opts:
        print(json.dumps(rows, indent=1, ensure_ascii=False))
        return 0
    if not rows:
        print("No printers configured. Looked in:")
        for p in [fleet_path(), *search_paths()]:
            print(f"  {p}")
        print("\nCreate one of them, e.g.:\n" + EXAMPLE)
        return 0
    print(f"  {'NAME':<13} {'OWNER':<12} {'ACCESS':<9} {'MODEL':<20} {'ADDRESS':<31} HEADS")
    for r in rows:
        address = f"{r['adapter']} {r['address']}" if r["adapter"] else "- (never contacted)"
        heads = " | ".join(h["label"] + ("" if h["usable"] else " UNUSABLE") for h in r["heads"]) or "-"
        print(f"  {r['name']:<13} {r['owner'] or '-':<12} {r['access']:<9} {r['model'] or '-':<20} "
              f"{address:<31} {heads}")
        for h in r["heads"]:
            if not h["usable"]:
                print(f"      {h['label']}: {h['reason']}")
        if r["settings"] and r["settings"] != r["fleet"]:
            print(f"      device settings: {r['settings']}")
    if fleet is not None:
        print(f"\nfleet: {fleet.path}")
    return 0


def _cmd_status(argv: list[str]) -> int:
    import json

    from printer_ops.render import render_status

    (name,), opts = _parse(argv, {"--json", "--secs"}, 1, "status")
    adapter = _adapter(name, opts)
    st = adapter.status()
    if "--json" in opts:
        print(json.dumps({"printer": name, "adapter": adapter.kind, **st.to_json()}, indent=1, default=str))
    else:
        print(render_status(name, adapter, st))
    return 0


def _local(path: str) -> Path:
    from pathlib import Path

    p = Path(path)
    if not p.is_file():
        raise InputNotFound(path, command="printer")
    return p


def _cmd_upload(argv: list[str]) -> int:
    import shutil
    import tempfile
    from pathlib import Path

    (name, file), opts = _parse(argv, {"--as", "--secs"}, 2, "upload")
    original = _local(file)
    # The bytes named by the hash are the bytes sent: hash and upload one snapshot, so a
    # slicer rewriting FILE meanwhile cannot store other content under this name.
    with tempfile.TemporaryDirectory(prefix="3d-upload-") as tmp:
        local = Path(tmp) / original.name
        shutil.copyfile(original, local)
        return _upload_snapshot(name, file, local, opts)


def _upload_snapshot(name: str, file: str, local: Path, opts: dict[str, str]) -> int:
    from printer_ops.base import PrinterRefused, safe_remote_name

    remote = safe_remote_name(local, opts.get("--as"))
    adapter = _adapter(name, opts, write=True)
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
    print(f"Start it with:  3d printer start {name} {file} --yes"
          + (f" --as {opts['--as']}" if "--as" in opts else ""))
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
    import shutil
    import tempfile
    from pathlib import Path

    (name, file), opts = _parse(argv, {"--yes", "--force", "--as", "--source-3mf", "--secs", "--bridge-max"}, 2, "start")
    if "--yes" not in opts:
        raise UsageError(
            "start is a physical action and needs --yes",
            command="printer",
            remediation=[f"Confirm explicitly: 3d printer start {name} {file} --yes"],
        )
    original = _local(file)
    source = Path(opts["--source-3mf"]) if "--source-3mf" in opts else None
    if source is not None and not source.is_file():
        raise InputNotFound(str(source), command="printer")
    # One immutable snapshot: the name hash, the read-back comparison and the preflight all see
    # the same bytes even if a slicer rewrites FILE meanwhile.
    with tempfile.TemporaryDirectory(prefix="3d-start-") as tmp:
        local = Path(tmp) / original.name
        shutil.copyfile(original, local)
        return _start_snapshot(name, file, original, local, source, opts)


def _start_snapshot(name: str, file: str, original: Path, local: Path, source: Path | None,
                    opts: dict[str, str]) -> int:
    from printer_ops.base import UNKNOWN, PrinterCommError, PrinterRefused, file_sha256, safe_remote_name
    from printer_ops.render import render_status
    from printer_ops import bridge_max_for
    from printer_ops.preflight import FAIL, run_preflight
    from printer_ops.printops_checks import context_from_options

    remote = safe_remote_name(local, opts.get("--as"))  # content-addressed: <stem>-<sha256[:16]>.gcode
    size = local.stat().st_size
    adapter = _adapter(name, opts, write=True)

    st = adapter.status()
    _refuse_if_busy(name, st, "before preflight")
    stored = adapter.list_files(expect=(remote, size)).get(remote)
    if stored is None or stored.size != size:
        got = "absent" if stored is None else f"{stored.size} B"
        raise PrinterRefused(
            f"{remote} on {name} is {got}; local {file} is {size} B — start refused",
            command="printer", remediation=[f"Upload it first: 3d printer upload {name} {file}"
                                            + (f" --as {opts['--as']}" if "--as" in opts else "")],
        )
    # Identity of what will be printed with what is checked below: the read-back hash where
    # the firmware can return the file, else the hash in the name plus the size.
    remote_hash = adapter.remote_sha256(remote)
    local_hash = file_sha256(local)
    if remote_hash is not None and remote_hash != local_hash:
        raise PrinterRefused(
            f"{remote} on {name} has different content than {file} "
            f"(printer sha256 {remote_hash[:12]}…, local {local_hash[:12]}…) — start refused",
            command="printer", remediation=[f"Upload it again: 3d printer upload {name} {file}"],
        )
    identity = "read-back sha256 matches" if remote_hash else "content hash in the file name + size"
    print(f"remote file {remote}: {identity}")

    limits, limits_source = adapter.axis_limits()
    bridge, bridge_source = bridge_max_for(opts.get("--bridge-max"), adapter.device, command="printer")
    # the snapshot lives in a temp dir: NAME.3mf and NAME.slice-warnings.json sit next to the original
    from printer_ops.preflight import check_bed

    # plus the PrintOps material gates of `3d print-preflight --printer NAME` (live 3d-api state
    # when configured, else fleet facts only)
    printops = context_from_options(name, axis_limits=None, api=None, offline=False, snapshot=None,
                                    command="printer")
    report = run_preflight(local, status=st, source_3mf=source, axis_limits=limits, limits_source=limits_source,
                           trusted_macros=adapter.trusted_macros, origin=original,
                           bridge_max=bridge, bridge_source=bridge_source, printops=printops,
                           bed=check_bed(adapter.device) if adapter.device is not None else None)
    report.file = f"{original} (snapshot)"
    print(report.render())
    if report.overall == FAIL:
        if "--force" not in opts:
            raise PrinterRefused("preflight FAIL; start refused", command="printer",
                                 remediation=["Fix the FAIL lines above, or pass --force to start anyway."])
        forced = ", ".join(c.name for c in report.checks if c.status == FAIL)
        print(f"FORCED: starting despite preflight FAIL ({forced})")

    _refuse_if_busy(name, adapter.status(), "state changed during preflight")
    dont_resend = [f"Do NOT resend blindly. Check: 3d printer status {name}"]
    try:
        adapter.start(remote)
    except PrinterCommError as exc:
        raise PrinterCommError(
            f"start request for {remote} on {name} failed: outcome UNKNOWN ({exc.message})",
            command="printer", remediation=dont_resend,
        ) from exc
    print(f"start acknowledged by {name}: {remote}")
    try:
        after = adapter.status()
    except PrinterCommError as exc:
        raise PrinterCommError(
            f"start of {remote} on {name} was acknowledged, but the state after it is unreadable: "
            f"outcome UNKNOWN ({exc.message})",
            command="printer", remediation=dont_resend,
        ) from exc
    if after.state == UNKNOWN:
        raise PrinterCommError(
            f"start of {remote} on {name} was acknowledged, but the printer's state is unknown "
            f"[{after.raw_state}]: outcome UNKNOWN",
            command="printer", remediation=dont_resend,
        )
    print(render_status(name, adapter, after))
    return 0


def _cmd_cancel(argv: list[str]) -> int:
    from printer_ops.base import ACTIVE_JOB_STATES, PrinterRefused
    from printer_ops.render import render_status

    (name,), opts = _parse(argv, {"--yes", "--secs"}, 1, "cancel")
    if "--yes" not in opts:
        raise UsageError("cancel stops a physical job and needs --yes", command="printer",
                         remediation=[f"Confirm explicitly: 3d printer cancel {name} --yes"])
    adapter = _adapter(name, opts, write=True)
    st = adapter.status()
    if st.state not in ACTIVE_JOB_STATES:
        raise PrinterRefused(f"{name} is {st.state} [{st.raw_state}]: no job to cancel", command="printer")
    adapter.cancel()
    print(f"cancel sent to {name} (job {st.filename or '?'})")
    print(render_status(name, adapter, adapter.status()))
    return 0


# Everything vision_detect.py imports: outside the repo's .venv, `uv run --with` gets only these
# (no project deps), and printer-devices.yaml (pyyaml) and the camera geometry (scipy) need theirs.
VISION_DEPS = "numpy,opencv-python-headless,onnxruntime,pyyaml,scipy"


def _cmd_vision(argv: list[str]) -> int:
    """`vision NAME ...` is shorthand for `vision run NAME ...`; eval/record/fetch-model pass
    through."""
    if argv and argv[0] == "doctor":  # stdlib-only: never starts the vision toolchain
        from printer_ops.deploy_check import run as doctor

        return doctor(argv[1:])
    if argv and argv[0] not in ("eval", "record", "fetch-model", "fit-head", "run", "-h", "--help"):
        argv = ["run", *argv]
    from cli.pyrun import exec_tool

    return exec_tool(VISION_DEPS, "vision_detect.py", argv)


def _cmd_bed(argv: list[str]) -> int:
    from cli.pyrun import exec_tool
    from printer_ops.bed import BED_DEPS, BED_TOOL

    return exec_tool(BED_DEPS, BED_TOOL, argv)


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    name, argv = _printer_name(argv)
    if not argv:
        print(USAGE)
        return 1
    sub, rest = argv[0], argv[1:]
    if sub in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if name is not None and sub not in ("todo", "issues"):
        raise UsageError(f"-n/--name selects the printer for `todo`/`issues`; `{sub}` takes the printer as NAME",
                         command="printer", remediation=[f"3d printer {sub} {name} …"])
    if sub == "list":
        return _cmd_list(rest)
    if sub == "status":
        return _cmd_status(rest)
    if sub == "upload":
        return _cmd_upload(rest)
    if sub == "start":
        return _cmd_start(rest)
    if sub == "cancel":
        return _cmd_cancel(rest)
    if sub == "watch":
        from printer_ops.watch_run import run as watch

        return watch(rest)
    if sub == "vision":
        return _cmd_vision(rest)
    if sub == "guard":
        from printer_ops.guard import run as guard

        return guard(rest, vision_deps=VISION_DEPS)
    if sub == "bed":
        return _cmd_bed(rest)
    if sub in ("subscribe", "unsubscribe", "subscriptions"):
        from printer_ops.subscriptions import run as subscriptions

        return subscriptions(sub, rest)
    if sub == "agent":
        from printer_ops.printer_agent import run as agent

        return agent(rest)
    if sub == "doctor":
        from printers_cli import dispatch

        return dispatch(["doctor", *rest])
    if sub in ("todo", "issues"):
        from printops.todo_cli import run as todo

        return todo(rest, printer=name)
    raise UsageError(f"unknown subcommand '{sub}'", command="printer",
                     remediation=["Use list, status, upload, start, cancel, watch, vision, guard, bed, subscribe, unsubscribe, "
                                  "subscriptions, agent, doctor or todo (issues) — see `3d printer --help`."])


def _printer_name(argv: list[str]) -> tuple[str | None, list[str]]:
    """A leading `-n NAME` / `--name NAME` / `--name=NAME`: the printer, by its fleet name."""
    if argv and argv[0].startswith("--name="):
        name = argv[0].split("=", 1)[1]
        if not name:
            raise UsageError("--name= needs the printer name, e.g. `3d printer --name=u1 issues list`",
                             command="printer")
        return name, argv[1:]
    if argv and argv[0] in ("-n", "--name"):
        if len(argv) < 2 or argv[1].startswith("-"):
            raise UsageError(f"{argv[0]} needs the printer name, e.g. `3d printer -n u1 issues list`",
                             command="printer")
        return argv[1], argv[2:]
    return None, argv


COMMAND = Command(
    name="printer",
    group="SLICING",
    summary="drive configured printers: list / status / upload / start --yes / cancel --yes / watch / vision / subscribe / agent / todo",
    usage=USAGE,
    run=run,
)
