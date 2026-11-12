"""supervise_units.py — the systemd user units of the on-demand printer supervision, shipped from the repo.

WHAT: renders the unit TEMPLATES `3d-printer-ondemand@.timer` (the supervisor: a timer, no process between
  checks), `3d-printer-ondemand@.service` (the one-shot check the timer runs, printer_ops/ondemand.py),
  `3d-printer-watch@.service` and `3d-printer-vision@.service` (the camera loop; the heavy WORKERS) and
  plans/applies their installation for every OWNED, fully controlled fleet printer (fleet/printers.yaml: owner
  own, access full, an adapter with a watch stream; vision only when a camera is configured). Unit and
  template names live in unit_names.py. `3d printer supervise install|uninstall` (supervise.py) drives this
  module.

WHY (#639): before this module the units were hand-made: templates ending in `--dry-run` plus per-printer
  drop-ins nobody could recreate, and k1se carried an `intentional-off.conf` gate that kept both units
  stopped while a marker file existed (#640), so a printer switched on again was unwatched.
WHY ON DEMAND (#747): always-on workers held 1.7 GiB and 4.6 % of a core while nothing printed, and the first
  fix still kept one resident supervisor process per printer. Now only a TIMER per printer is armed (systemd
  holds it, no process): every 30 s it runs a ~20 ms one-shot check that starts the workers when a print runs
  and stops them after it. The worker templates have NO `[Install]` section: nothing starts them at boot, and
  a stray `systemctl enable` is refused instead of silently reviving the always-on mode.

THE UNITS: no `--dry-run`, workers `Restart=always`, `StartLimitIntervalSec=0` (a crash loop never gives up),
  the supervisor timer `WantedBy=timers.target` (with `loginctl enable-linger` it starts at boot without a
  login), the check `Type=oneshot` with no `[Install]` and `LogLevelMax=notice` (no Starting/Finished journal
  lines every 30 s), the workers ordered `After=network-online.target hsw-observer-agent.service` (ordering
  only: no Wants, no dependency on Home Assistant or an omp session; a hand-made drop-in with Wants= keeps
  working), and the workers' `ExecStopPost=-... supervise notify-failure` — systemd never puts a
  `Restart=always` unit into the `failed` state, so OnFailure= would never fire; ExecStopPost runs after EVERY
  exit and the handler stays silent for a clean stop (SERVICE_RESULT=success), which is what the check's
  `systemctl stop` is (#642).

OWNERSHIP: a file whose first line is MANAGED_MARK is ours: it is rewritten freely. A hand-made file with
  the same name is replaced only with `--yes`, after a `.bak-<time>` copy next to it. Drop-ins are never
  deleted: a pure `ConditionPathExists=!...intentional-off` gate (the retired marker semantics) is
  renamed to `.conf.retired-<time>` with `--yes`; every other drop-in is only reported.

INVARIANTS: stdlib only; every systemctl/loginctl call goes through an injected runner (tests never reach
  systemd); no function here writes outside the unit directory it is given.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from printer_ops.unit_names import (
    CHECK,
    KINDS,
    RUNNING,
    SUPERVISOR,
    TEMPLATE_KINDS,
    template_name,
    unit_name,
)

if TYPE_CHECKING:
    from printops.fleet import Fleet

MANAGED_MARK = (
    "# managed-by: 3d printer supervise install (do not edit; override with a drop-in)"
)
WATCH_ADAPTERS = frozenset({"moonraker", "creality-stock"})
RESTART_SEC = {"watch": 15, "vision": 30}
CHECK_INTERVAL_S = 30  # the timer's OnUnitInactiveSec

# A systemctl/loginctl call: argv -> CompletedProcess. The default refuses to run under pytest.
Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def real_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError(f"refusing to run {argv[0]} from a test: inject a runner")
    return subprocess.run(
        list(argv), capture_output=True, text=True, timeout=60, check=False
    )


def default_unit_dir() -> Path:
    return (
        Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        / "systemd"
        / "user"
    )


@dataclass(frozen=True)
class PrinterPlan:
    """One printer to supervise: its worker units follow from `camera` (a vision unit only with a camera);
    its supervisor unit is always there."""

    name: str
    adapter: str
    camera: bool

    @property
    def kinds(self) -> tuple[str, ...]:
        """The worker kinds."""
        return KINDS if self.camera else ("watch",)

    def worker_units(self) -> list[str]:
        return [unit_name(k, self.name) for k in self.kinds]

    def supervisor_unit(self) -> str:
        return unit_name(SUPERVISOR, self.name)

    def units(self) -> list[str]:
        """Every unit of the printer, the supervisor first."""
        return [self.supervisor_unit(), *self.worker_units()]


def plan_printers(fleet: Fleet | None, devices: dict[str, object]) -> list[PrinterPlan]:
    """Every owned, fully controlled fleet printer that 3d may contact and that has a watch stream."""
    from errors import ThreeDError
    from printer_ops.camera import camera_spec

    out: list[PrinterPlan] = []
    if fleet is None:
        return out
    for name, entry in sorted(fleet.printers.items()):
        device = devices.get(name)
        if entry.owner != "own" or entry.access != "full" or device is None:
            continue
        adapter = str(getattr(device, "adapter", ""))
        if adapter not in WATCH_ADAPTERS:
            continue
        try:
            camera = camera_spec(device).present  # type: ignore[arg-type]
        except ThreeDError:
            camera = False  # a broken camera config is the camera check's finding, not a reason to skip the watcher
        out.append(PrinterPlan(name, adapter, camera))
    return out


@dataclass(frozen=True)
class Layout:
    """Where the units run from: the checkout, its python, the unit directory."""

    repo: Path
    python: Path
    unit_dir: Path
    xdg_config: str | None = None

    @classmethod
    def detect(cls, repo: Path | None = None, unit_dir: Path | None = None) -> Layout:
        from cli.env import repo_root

        root = (repo or Path(repo_root())).resolve()
        venv = root / ".venv" / "bin" / "python"
        return cls(
            root,
            venv if venv.exists() else Path(sys.executable),
            unit_dir or default_unit_dir(),
            os.environ.get("XDG_CONFIG_HOME"),
        )

    def exec_argv(self, kind: str) -> list[str]:
        if kind == CHECK:
            # `-S -m` with PYTHONPATH, not bin/3d: the dispatcher imports every command module (~35 MB and
            # ~0.4 s), the check runs every 30 s and must cost milliseconds (#747). `-S`: no site import
            # (the check is stdlib only).
            return [str(self.python), "-S", "-m", "printer_ops.ondemand", "%i"]
        return [
            str(self.python),
            str(self.repo / "bin" / "3d"),
            "printer",
            kind,
            "%i",
        ]

    def stop_post_argv(self, kind: str) -> list[str]:
        return [
            str(self.python),
            str(self.repo / "bin" / "3d"),
            "printer",
            "supervise",
            "notify-failure",
            "%i",
            kind,
        ]


DESCRIPTIONS = {
    SUPERVISOR: "3D printer supervisor %i (a timer: every 30 s a one-shot check, no process in between; starts the watch and vision workers only while a print runs)",
    CHECK: "3D printer on-demand check %i (one-shot: asks the printer, starts or stops the watch and vision workers)",
    "watch": "3D printer watch %i (while a print runs: Klipper state, errors, pauses; AI alerts + auto pause)",
    "vision": "3D printer vision %i (while a print runs: camera frames, spaghetti / detached part; AI alerts + auto pause)",
}
_ONDEMAND_DOC = "https://github.com/alex-mextner/3d-cli/blob/main/docs/commands/printer.md#on-demand-supervision-747"
DOCS = {
    SUPERVISOR: _ONDEMAND_DOC,
    CHECK: _ONDEMAND_DOC,
    "watch": "https://github.com/alex-mextner/3d-cli/blob/main/docs/commands/printer.md#always-on-supervision",
    "vision": "https://github.com/alex-mextner/3d-cli/blob/main/docs/commands/printer-vision.md",
}


def _timer_section() -> str:
    """[Timer] and [Install] of the supervisor: the first check 5 s after the timer is armed (boot, install),
    then 30 s after the previous check FINISHED (so a slow printer never stacks checks). AccuracySec=1s keeps
    wake-ups from being coalesced into minutes."""
    return f"""[Timer]
OnActiveSec=5s
OnUnitInactiveSec={CHECK_INTERVAL_S}s
AccuracySec=1s

[Install]
WantedBy=timers.target
"""


def _check_service(layout: Layout, pin: str) -> str:
    """[Service] of the one-shot check the timer starts: no [Install] (only the timer starts it), no Restart
    (the timer is the retry), `LogLevelMax=notice` + `SyslogLevel=notice` so systemd's own Starting/Finished
    lines (info) are not written to the journal every 30 s while the check's real transition lines (stdout at
    notice) are, and a memory cap above the config resolver's peak (~30 MB), which runs only after a config
    change."""
    return f"""[Service]
Type=oneshot
LogLevelMax=notice
SyslogLevel=notice
Environment=PATH={layout.repo}/.venv/bin:%h/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=PYTHONPATH={layout.repo}/lib
Environment=PYTHONUNBUFFERED=1
{pin}WorkingDirectory={layout.repo}
UMask=0077
MemoryMax=96M
TimeoutStartSec=90
ExecStart={shlex.join(layout.exec_argv(CHECK))}
"""


def _worker_service(kind: str, layout: Layout, pin: str) -> str:
    """[Service] of a worker: started and stopped by the on-demand check, so no [Install] section.
    SuccessExitStatus=143: the check stops it with SIGTERM and `uv run` (the vision loop's launcher) answers
    with 143; without it every stop would leave the unit `failed` and run the failure hook (#747)."""
    return f"""[Service]
Type=simple
Environment=PATH={layout.repo}/.venv/bin:%h/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=PYTHONUNBUFFERED=1
{pin}WorkingDirectory={layout.repo}
UMask=0077
ExecStart={shlex.join(layout.exec_argv(kind))}
ExecStopPost=-{shlex.join(layout.stop_post_argv(kind))}
Restart=always
RestartSec={RESTART_SEC[kind]}
SuccessExitStatus=143
"""


def render_template(kind: str, layout: Layout) -> str:
    """The text of the unit file of `kind` (see unit_names.TEMPLATES); deterministic for a given layout
    (snapshot-tested)."""
    if kind not in TEMPLATE_KINDS:
        raise ValueError(f"unknown supervision unit kind {kind!r}")
    pin = (
        f"Environment=XDG_CONFIG_HOME={layout.xdg_config}\n"
        if layout.xdg_config
        else ""
    )
    if kind == SUPERVISOR:
        body, unit_extra = _timer_section(), ""
    elif kind == CHECK:
        # the checkout lives under a bind mount of the backups disk: without the disk the check is skipped
        # silently (an unmet Condition is not a failure and logs below notice) instead of failing every 30 s
        body = _check_service(layout, pin)
        unit_extra = f"ConditionPathExists={layout.repo}/lib/printer_ops/ondemand.py\n"
    else:
        body = _worker_service(kind, layout, pin)
        unit_extra = "After=network-online.target hsw-observer-agent.service\nStartLimitIntervalSec=0\n"
    return f"""{MANAGED_MARK}
[Unit]
Description={DESCRIPTIONS[kind]}
Documentation={DOCS[kind]}
{unit_extra}
{body}"""


# ---- what is on disk ---------------------------------------------------------------------------


def is_managed(text: str) -> bool:
    return text.splitlines()[:1] == [MANAGED_MARK]


_GATE = re.compile(r"^\s*ConditionPathExists\s*=\s*!\s*\S*\.intentional-off\s*$")


@dataclass(frozen=True)
class DropIn:
    path: Path
    kind: str  # gate | dry-run | redundant-exec | custom-exec | other
    detail: str

    @property
    def retirable(self) -> bool:
        return self.kind == "gate"


def _exec_values(lines: list[str]) -> list[str]:
    return [
        ln.split("=", 1)[1].strip()
        for ln in lines
        if ln.strip().startswith("ExecStart=")
    ]


def _classify_exec(path: Path, command: str, canonical: list[str]) -> DropIn:
    """A drop-in whose last ExecStart is `command`: unparseable, --dry-run, the shipped command, or custom."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return DropIn(path, "custom-exec", "ExecStart override that cannot be parsed")
    if "--dry-run" in argv:
        return DropIn(
            path,
            "dry-run",
            "ExecStart override with --dry-run: alerts are printed, never delivered",
        )
    if argv == canonical:
        return DropIn(
            path,
            "redundant-exec",
            "ExecStart override equal to the shipped command (harmless)",
        )
    return DropIn(path, "custom-exec", f"ExecStart override: {command}")


def _directives(text: str) -> list[str]:
    """The non-comment, non-section lines of a unit file."""
    body = [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith(("#", ";"))
    ]
    return [ln for ln in body if not ln.startswith("[")]


def classify_dropin(path: Path, text: str, canonical: list[str]) -> DropIn:
    """What a hand-made drop-in does to a supervision unit. `canonical` is the template's ExecStart argv with
    the instance already substituted."""
    directives = _directives(text)
    gates = [ln for ln in directives if _GATE.match(ln)]
    if gates and len(gates) == len(directives):
        return DropIn(
            path,
            "gate",
            "ConditionPathExists on the intentional-off marker keeps the unit stopped "
            "while the marker exists: a printer switched on again is unwatched (#640)",
        )
    execs = [v for v in _exec_values(directives) if v]
    if execs:
        return _classify_exec(path, execs[-1], canonical)
    if gates:
        return DropIn(
            path,
            "other",
            "an intentional-off ConditionPathExists plus other directives: edit by hand",
        )
    return DropIn(path, "other", "other directives (kept)")


def _dropin_dirs(
    unit_dir: Path, plans: Sequence[PrinterPlan]
) -> list[tuple[Path, str, str]]:
    """(drop-in directory, unit kind, instance) of every template and every printer's instance."""
    dirs = [
        (unit_dir / f"{template_name(kind)}.d", kind, "%i") for kind in TEMPLATE_KINDS
    ]
    dirs.extend(
        (unit_dir / f"{unit_name(kind, plan.name)}.d", kind, plan.name)
        for plan in plans
        for kind in (*plan.kinds, SUPERVISOR, CHECK)
    )
    return dirs


def _classify_dir(
    directory: Path, kind: str, instance: str, layout: Layout
) -> list[DropIn]:
    """The classified `*.conf` files of one drop-in directory (a timer has no ExecStart to compare against)."""
    if not directory.is_dir():
        return []
    canonical = (
        []
        if kind == SUPERVISOR
        else [a.replace("%i", instance) for a in layout.exec_argv(kind)]
    )
    found: list[DropIn] = []
    for conf in sorted(directory.glob("*.conf")):
        try:
            text = conf.read_text(encoding="utf-8")
        except OSError:
            continue
        found.append(classify_dropin(conf, text, canonical))
    return found


def find_dropins(
    unit_dir: Path, plans: Sequence[PrinterPlan], layout: Layout
) -> list[DropIn]:
    """Every `*.conf` drop-in of the supervision templates and instances, classified."""
    return [
        d
        for directory, kind, instance in _dropin_dirs(unit_dir, plans)
        for d in _classify_dir(directory, kind, instance, layout)
    ]


# ---- install plan ------------------------------------------------------------------------------


@dataclass
class Action:
    """One step of an install/uninstall. `needs_yes`: it replaces or retires a file the owner made."""

    op: str  # write | replace | retire | remove | enable | disable | reload | restart | linger
    target: str
    detail: str
    needs_yes: bool = False
    path: Path | None = None
    text: str | None = None
    argv: list[str] = field(default_factory=list)


def _stamp(now: float) -> str:
    return time.strftime("%Y%m%dT%H%M%S", time.gmtime(now))


def _template_action(
    kind: str, layout: Layout, installed: dict[str, str], now: float
) -> Action | None:
    """The write/replace that brings one template to the shipped text (None = already right)."""
    name = template_name(kind)
    desired = render_template(kind, layout)
    have = installed.get(name)
    path = layout.unit_dir / name
    if have == desired:
        return None
    if have is None:
        return Action("write", name, "new template", path=path, text=desired)
    if is_managed(have):
        return Action(
            "write", name, "update the managed template", path=path, text=desired
        )
    how = "carries --dry-run" if "--dry-run" in have else "hand-made"
    return Action(
        "replace",
        name,
        f"replace the {how} template (a .bak-{_stamp(now)} copy is kept)",
        needs_yes=True,
        path=path,
        text=desired,
    )


def _template_actions(
    plans: Sequence[PrinterPlan], layout: Layout, installed: dict[str, str], now: float
) -> list[Action]:
    kinds = sorted(
        {SUPERVISOR, CHECK, *(k for p in plans for k in p.kinds)},
        key=TEMPLATE_KINDS.index,
    )
    found = (_template_action(kind, layout, installed, now) for kind in kinds)
    return [action for action in found if action is not None]


def _plan_enable_actions(
    plan: PrinterPlan, enabled: dict[str, bool], old_supervisors: set[str]
) -> list[Action]:
    """A leftover always-on supervisor (the long-lived `3d-printer-ondemand@NAME.service` of the first #747
    design) is stopped and disabled first: its workers are left running, the next check adopts them. The timer
    supervisor is enabled and started when it is not (enabled AND running); a worker that an older install
    left enabled is DISABLED, never stopped (a print may be running: the check stops it after its grace
    period)."""
    supervisor = plan.supervisor_unit()
    actions: list[Action] = []
    old = unit_name(CHECK, plan.name)
    if old in old_supervisors:
        actions.append(
            Action(
                "disable",
                old,
                "retire the always-on supervisor process: the timer runs a one-shot check instead "
                "(its workers keep running and are adopted)",
                argv=["systemctl", "--user", "disable", "--now", old],
            )
        )
    if not enabled.get(supervisor, False):
        actions.append(
            Action(
                "enable",
                supervisor,
                "enable and start the timer that runs the on-demand check",
                argv=["systemctl", "--user", "enable", "--now", supervisor],
            )
        )
    actions.extend(
        Action(
            "disable",
            unit,
            "no longer always on: the check starts it while a print runs (a running copy "
            "is stopped by the check after its grace period)",
            argv=["systemctl", "--user", "disable", unit],
        )
        for unit in plan.worker_units()
        if enabled.get(unit, False)
    )
    return actions


def _enable_actions(
    plans: Sequence[PrinterPlan],
    enabled: dict[str, bool],
    loaded_dry_run: set[str],
    old_supervisors: set[str],
) -> list[Action]:
    """Per printer: retire an old always-on supervisor process, enable the timer, disable always-on workers;
    then restart every unit whose running definition still has --dry-run. `enabled` says "enabled" for a worker
    and "enabled and running" for the timer (a stopped timer is started again); `old_supervisors` are the
    leftover long-lived supervisor services that are still enabled."""
    actions = [
        a
        for plan in plans
        for a in _plan_enable_actions(plan, enabled, old_supervisors)
    ]
    actions.extend(
        Action(
            "restart",
            unit,
            "the running definition still has --dry-run",
            argv=["systemctl", "--user", "restart", unit],
        )
        for unit in (u for plan in plans for u in plan.units())
        if unit in loaded_dry_run
    )
    return actions


def plan_install(
    plans: Sequence[PrinterPlan],
    layout: Layout,
    *,
    installed: dict[str, str],
    dropins: Sequence[DropIn],
    linger: bool | None,
    enabled: dict[str, bool],
    loaded_dry_run: set[str],
    now: float,
    old_supervisors: set[str] | None = None,
) -> list[Action]:
    """The ordered actions that make the supervision of `plans` right. `installed` is unit file name -> its text
    (missing = absent); `enabled` unit -> is enabled; `loaded_dry_run` the units whose RUNNING definition
    still carries --dry-run; `old_supervisors` the still-enabled long-lived `3d-printer-ondemand@NAME.service`
    instances of the first #747 design (retired in favour of the timer). Pure: nothing is read or written
    here."""
    actions = _template_actions(plans, layout, installed, now)
    actions.extend(
        Action("retire", d.path.name, d.detail, needs_yes=True, path=d.path)
        for d in dropins
        if d.retirable
    )
    if actions:
        actions.append(
            Action(
                "reload",
                "systemd --user",
                "daemon-reload",
                argv=["systemctl", "--user", "daemon-reload"],
            )
        )
    if linger is False:
        actions.append(
            Action(
                "linger",
                "loginctl",
                "enable-linger: the units run without a login and after a reboot",
                needs_yes=False,
                argv=["loginctl", "enable-linger"],
            )
        )
    actions.extend(
        _enable_actions(plans, enabled, loaded_dry_run, old_supervisors or set())
    )
    return actions


@dataclass
class ApplyReport:
    done: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


def blocked(actions: Sequence[Action]) -> list[Action]:
    return [a for a in actions if a.needs_yes]


def apply(
    actions: Sequence[Action],
    *,
    yes: bool,
    run: Runner,
    now: float,
) -> ApplyReport:
    """Run the actions. Without `yes` nothing that needs it is touched AND nothing else runs (an install
    that enabled units on a template that is still the old hand-made one would only look done).
    `linger` is applied with `yes` only (it changes the account, not a unit)."""
    report = ApplyReport()
    if blocked(actions) and not yes:
        report.skipped = [f"{a.op} {a.target}" for a in actions]
        return report
    for a in actions:
        label = f"{a.op} {a.target}"
        try:
            _apply_one(a, label, report, yes=yes, run=run, now=now)
        except OSError as exc:
            report.failed.append(f"{label}: {exc.strerror or exc}")
    return report


def _apply_write(a: Action, now: float) -> None:
    """`write` and `replace`: a replaced hand-made file is renamed to a .bak first so it is never lost."""
    assert a.path is not None and a.text is not None
    a.path.parent.mkdir(parents=True, exist_ok=True)
    if a.op == "replace" and a.path.exists():
        a.path.rename(a.path.with_name(f"{a.path.name}.bak-{_stamp(now)}"))
    _write_atomic(a.path, a.text)


def _apply_retire(a: Action, now: float) -> None:
    assert a.path is not None
    a.path.rename(a.path.with_name(f"{a.path.name}.retired-{_stamp(now)}"))


def _apply_remove(a: Action) -> None:
    assert a.path is not None
    a.path.unlink(missing_ok=True)


def _apply_one(
    a: Action, label: str, report: ApplyReport, *, yes: bool, run: Runner, now: float
) -> None:
    """Perform one action and record it; file ops report done here, commands via `_run_checked`."""
    if a.op in ("write", "replace"):
        _apply_write(a, now)
    elif a.op == "retire":
        _apply_retire(a, now)
    elif a.op == "remove":
        _apply_remove(a)
    elif a.op == "linger" and not yes:
        report.skipped.append(label + " (needs --yes)")
        return
    else:
        _run_checked(run, a.argv, report, label)
        return
    report.done.append(label)


def _run_checked(run: Runner, argv: list[str], report: ApplyReport, label: str) -> None:
    r = run(argv)
    if r.returncode == 0:
        report.done.append(label)
    else:
        report.failed.append(
            f"{label}: exit {r.returncode}: {(r.stderr or r.stdout or '').strip()[:200]}"
        )


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _uninstall_actions(
    plan: PrinterPlan,
    enabled: dict[str, bool],
    old_supervisors: set[str],
    absent: set[str],
) -> list[Action]:
    """Per printer, in this order: the timer, then a check that is still running (it must not start a worker
    after the uninstall returned), then the workers (an enabled leftover is disabled, an on-demand one is
    stopped). A unit systemd has no file for (`absent`: an install of the first #747 design has no timer yet)
    is skipped: `systemctl stop|disable` of it exits 5 and would fail the whole uninstall."""
    timer = plan.supervisor_unit()
    check = unit_name(CHECK, plan.name)
    actions: list[Action] = []
    if (
        check in old_supervisors
    ):  # the resident process of the first #747 design: its boot enablement too
        actions.append(
            Action(
                "disable",
                check,
                "stop and disable the always-on supervisor process of the first design",
                argv=["systemctl", "--user", "disable", "--now", check],
            )
        )
    # unconditionally: a timer that is enabled but stopped, or running but not enabled (`disable` without
    # `--now`), would otherwise survive, and a check in flight could still start workers
    actions.append(
        Action(
            "disable",
            timer,
            "stop and disable the timer",
            argv=["systemctl", "--user", "disable", "--now", timer],
        )
    )
    actions.append(
        Action(
            "stop",
            check,
            "stop a check that is still running: it must not start workers after the uninstall",
            argv=["systemctl", "--user", "stop", check],
        )
    )
    for unit in plan.worker_units():
        if enabled.get(unit, False):
            actions.append(
                Action(
                    "disable",
                    unit,
                    "stop and disable",
                    argv=["systemctl", "--user", "disable", "--now", unit],
                )
            )
        else:  # on-demand workers are not enabled: only stop them
            actions.append(
                Action(
                    "stop",
                    unit,
                    "stop the worker: nothing starts it any more",
                    argv=["systemctl", "--user", "stop", unit],
                )
            )
    return [a for a in actions if a.target not in absent]


def plan_uninstall(
    plans: Sequence[PrinterPlan],
    layout: Layout,
    *,
    installed: dict[str, str],
    all_units: bool,
    enabled: dict[str, bool],
    old_supervisors: set[str] | None = None,
    absent: set[str] | None = None,
) -> list[Action]:
    """Disable the printers' instances; with `all_units` (no printer left to supervise) also remove the managed
    templates. A hand-made template is never removed here."""
    actions = [
        a
        for plan in plans
        for a in _uninstall_actions(
            plan, enabled, old_supervisors or set(), absent or set()
        )
    ]
    if all_units:
        for kind in TEMPLATE_KINDS:
            name = template_name(kind)
            text = installed.get(name)
            if text is not None and is_managed(text):
                actions.append(
                    Action(
                        "remove",
                        name,
                        "remove the managed template",
                        needs_yes=True,
                        path=layout.unit_dir / name,
                    )
                )
        actions.append(
            Action(
                "reload",
                "systemd --user",
                "daemon-reload",
                argv=["systemctl", "--user", "daemon-reload"],
            )
        )
    return actions


def read_installed(unit_dir: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for kind in TEMPLATE_KINDS:
        name = template_name(kind)
        try:
            out[name] = (unit_dir / name).read_text(encoding="utf-8")
        except OSError:
            continue
    return out


__all__ = [
    "CHECK",
    "KINDS",
    "RUNNING",
    "SUPERVISOR",
    "TEMPLATE_KINDS",
    "MANAGED_MARK",
    "WATCH_ADAPTERS",
    "Action",
    "ApplyReport",
    "DropIn",
    "Layout",
    "PrinterPlan",
    "Runner",
    "apply",
    "blocked",
    "classify_dropin",
    "default_unit_dir",
    "find_dropins",
    "is_managed",
    "plan_install",
    "plan_printers",
    "plan_uninstall",
    "read_installed",
    "real_runner",
    "render_template",
    "template_name",
    "unit_name",
]
