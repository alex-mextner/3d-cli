"""deploy_check.py — `3d printer vision doctor`: is the camera watch actually DELIVERING?

WHY: on 2026-10-22 a U1 print peeled off the plate and nothing reached anyone. One of the causes
was not in any detector: both user services (`3d-printer-vision@u1`, `3d-printer-watch@u1`) ran
with `--dry-run`, which makes every sink print what it WOULD send. The journal looked alive
(a verdict line every 10 s) while no event ever left the machine. This check makes that trap
loud: for every printer that has a camera it asks systemd (read-only `systemctl --user show`)
about the vision and watch services and FAILS when a running one carries `--dry-run`, either on
its ExecStart line or in the `WATCH_ARGS` of its EnvironmentFile.

It never starts, stops or edits anything; the fix and the verification are printed as commands
for the owner to run (docs/commands/printer-vision.md "Deploying the services").

INVARIANTS: stdlib only; the only subprocess is `systemctl --user show` (injectable `run` for
tests); a host without systemctl yields `unknown`, never a pass.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

SERVICES = ("vision", "watch")  # 3d-printer-<kind>@NAME.service
RUNNING = frozenset({"active", "activating", "reloading"})
# What `systemctl --user show` returns for a unit; injected in tests.
Show = Callable[[str], str]


@dataclass
class UnitVerdict:
    unit: str
    state: str  # systemd ActiveState, or "unknown" without systemctl
    dry_run: bool = False
    source: str = ""  # where --dry-run was found
    note: str = ""

    @property
    def running(self) -> bool:
        return self.state in RUNNING

    @property
    def ok(self) -> bool:
        return not (self.running and self.dry_run)


@dataclass
class PrinterVerdict:
    printer: str
    camera: bool
    units: list[UnitVerdict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def failures(self) -> list[UnitVerdict]:
        return [u for u in self.units if not u.ok]


def systemctl_show(unit: str) -> str:
    """`systemctl --user show UNIT` for the fields we read; "" when systemctl is missing."""
    if shutil.which("systemctl") is None:
        return ""
    r = subprocess.run(
        ["systemctl", "--user", "show", unit, "-p", "LoadState", "-p", "ActiveState",
         "-p", "ExecStart", "-p", "EnvironmentFiles"],
        capture_output=True, text=True, timeout=15, check=False,
    )
    return r.stdout if r.returncode == 0 else ""


def _fields(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


def _argv(exec_start: str) -> list[str]:
    """The argv[] of the first ExecStart entry of a `systemctl show` line."""
    m = re.search(r"argv\[\]=(.*?) ; ", exec_start)
    return shlex.split(m.group(1)) if m else []


def _env_file_args(env_files: str, read: Callable[[Path], str]) -> str:
    """WATCH_ARGS of the unit's EnvironmentFile(s) (`PATH (ignore_errors=yes)` entries)."""
    args = ""
    for entry in re.findall(r"(\S+) \(ignore_errors=(?:yes|no)\)", env_files):
        try:
            text = read(Path(entry.lstrip("-")))
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("WATCH_ARGS="):
                args += " " + line.partition("=")[2]
    return args


def check_unit(unit: str, show: Show, read: Callable[[Path], str] = Path.read_text) -> UnitVerdict:
    text = show(unit)
    if not text:
        return UnitVerdict(unit, "unknown", note="systemctl --user is not available here")
    f = _fields(text)
    state = f.get("ActiveState", "unknown")
    if f.get("LoadState") == "not-found":
        return UnitVerdict(unit, "not-found")
    argv = _argv(f.get("ExecStart", ""))
    if "--dry-run" in argv:
        return UnitVerdict(unit, state, True, "ExecStart")
    if "$WATCH_ARGS" in argv or "${WATCH_ARGS}" in argv:
        env_args = _env_file_args(f.get("EnvironmentFiles", ""), read)
        if "--dry-run" in shlex.split(env_args):
            return UnitVerdict(unit, state, True, "EnvironmentFile WATCH_ARGS")
    return UnitVerdict(unit, state)


def check_printer(name: str, has_camera: bool, show: Show,
                  read: Callable[[Path], str] = Path.read_text) -> PrinterVerdict:
    verdict = PrinterVerdict(name, has_camera)
    if not has_camera:
        return verdict
    verdict.units = [check_unit(f"3d-printer-{kind}@{name}.service", show, read) for kind in SERVICES]
    by = dict(zip(SERVICES, verdict.units, strict=True))
    if not by["vision"].running:
        verdict.warnings.append(
            f"no running vision service: nothing watches {name}'s camera "
            f"(3d-printer-vision@{name}.service is {by['vision'].state})")
    return verdict


def fix_commands(name: str, unit: UnitVerdict) -> list[str]:
    """What the owner runs; nothing here is executed."""
    if unit.source == "ExecStart":
        edit = f"systemctl --user edit --full {unit.unit}   # delete `--dry-run` from ExecStart"
    else:
        edit = "edit the WATCH_ARGS line of the unit's EnvironmentFile (delete `--dry-run`)"
    return [
        edit,
        "systemctl --user daemon-reload",
        f"systemctl --user restart {unit.unit}",
        f"3d printer subscriptions test {name} --kind vision_part_detached --send "
        f"--text '{name}: delivery check'   # really delivers one [TEST] event to the live subscribers; "
        "then run this check again",
    ]


def render(verdicts: list[PrinterVerdict]) -> str:
    lines: list[str] = []
    for v in verdicts:
        if not v.camera:
            lines.append(f"{v.printer}: no camera, nothing to check")
            continue
        for u in v.units:
            mark = "FAIL" if not u.ok else ("ok  " if u.running else "idle")
            extra = f" runs with --dry-run (in {u.source}): events are printed, never delivered" if not u.ok else ""
            lines.append(f"{mark} {u.unit}: {u.state}{extra}{' — ' + u.note if u.note else ''}")
            if not u.ok:
                lines += [f"       $ {c}" for c in fix_commands(v.printer, u)]
        lines += [f"warn {w}" for w in v.warnings]
    return "\n".join(lines)


USAGE = """3d printer vision doctor [NAME...] [--json]

Read-only deployment check: for every printer with a camera (or the NAMEs given), is a
`3d-printer-vision@NAME` and a `3d-printer-watch@NAME` user service running, and does any of
them run with --dry-run (events printed, never delivered)? Exit 1 when one does, with the exact
commands to fix it and to prove delivery; 0 otherwise (warnings for a missing vision service).

  3d printer vision doctor
  3d printer vision doctor u1 --json"""


def run(argv: list[str], show: Show = systemctl_show) -> int:
    import json

    from errors import GateFailure, UsageError
    from printer_ops.camera import camera_spec
    from printer_ops.devices import load_devices

    if "-h" in argv or "--help" in argv:
        print(USAGE)
        return 0
    flags = {a for a in argv if a.startswith("-")}
    if flags - {"--json"}:
        raise UsageError(f"unknown option {sorted(flags - {'--json'})[0]}", command="printer vision doctor",
                         remediation=[USAGE])
    names = [a for a in argv if not a.startswith("-")]
    devices = load_devices()
    for n in names:
        if n not in devices:
            raise UsageError(f"unknown printer {n!r}", command="printer vision doctor",
                             remediation=[f"configured: {', '.join(sorted(devices)) or 'none'}"])
    verdicts = [
        check_printer(n, camera_spec(d).present, show)
        for n, d in sorted(devices.items()) if not names or n in names
    ]
    if "--json" in flags:
        print(json.dumps([{"printer": v.printer, "camera": v.camera, "warnings": v.warnings,
                           "units": [{"unit": u.unit, "state": u.state, "dry_run": u.dry_run,
                                      "ok": u.ok, "source": u.source} for u in v.units]}
                          for v in verdicts], indent=1))
    else:
        print(render(verdicts))
    bad = [(v, u) for v in verdicts for u in v.failures]
    if bad:
        raise GateFailure(
            f"{len(bad)} service(s) run with --dry-run on a printer with a camera: "
            + ", ".join(u.unit for _, u in bad),
            command="printer vision doctor", silent=True)
    return 0
