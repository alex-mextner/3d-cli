"""supervise.py — `3d printer supervise`: install, check and repair the always-on printer supervision.

WHAT: one `3d-printer-watch@NAME` unit (Klipper/Moonraker state, errors, pauses, print start) and, with a
  camera, one `3d-printer-vision@NAME` unit (frames: spaghetti, detached part) per OWNED, fully controlled
  fleet printer, shipped from this repo (supervise_units.py) and judged here from three independent
  witnesses: systemd (is it running, enabled, started with --dry-run?), the units' own heartbeat files
  (watch/NAME.json every 15 s, vision/NAME.json every cycle) and the printer itself (printing? answering?).
  `status` prints the verdict and fails (exit 1) when a printer that answers is not supervised;
  `install`/`uninstall` change the units; `quiet` writes the intentional-off marker; `notify-failure` is the
  ExecStopPost hook of the units (one short tg line when a supervisor dies during a print).

WHY (#639 #640 #641 #642): the owner's requirement is that AI supervision ALWAYS works while a print runs, also
  when the printer is switched off between prints, and catches a print started from anywhere, with or
  without Home Assistant. That needs units that are always up (no gate on a marker), a way to prove they are,
  and a signal when they stop.

WHAT RUNS ALWAYS (the supervision path, no Home Assistant, no omp session, no printer power state assumed):
  3d-printer-watch@NAME  - websocket to the printer (Moonraker / creality-stock ws:9999), retries every <= 30 s
                           while the printer is off, emits `print_started` for a print started by ANY route
  3d-printer-vision@NAME - polls the printer's state, fetches camera frames only while it prints, resets the
                           per-print baselines at each print start, hands findings to the alert chain
  alert chain            - live omp subscriber -> temporary omp agent -> tg fallback -> autonomous pause
                           (watch.pause_without_agent, once per job, only PAUSE, never resume/cancel)

INVARIANTS: `status` and `notify-failure` never write anything but the delivery ledger; `install` writes only
  the unit directory and never replaces or retires a hand-made file without `--yes`; no real systemctl,
  loginctl or tg call happens under pytest unless a runner is injected.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from printer_ops.supervise_units import (
    KINDS,
    RUNNING,
    Action,
    DropIn,
    Layout,
    PrinterPlan,
    Runner,
    apply,
    blocked,
    default_unit_dir,
    find_dropins,
    plan_install,
    plan_printers,
    plan_uninstall,
    read_installed,
    real_runner,
    unit_name,
)

if TYPE_CHECKING:
    from printer_ops.deploy_check import Show
    from printer_ops.subscriptions import DeliveryState

WATCH_BEAT_STALE_S = 75.0  # the watcher rewrites its heartbeat every 15 s
VISION_STALE_MIN_S = (
    90.0  # a vision cycle is `interval_s` (10 s) plus the work; 4 cycles or 90 s
)
FRAME_STALE_S = 180.0  # printing + supervising, and no camera frame for this long
UNREACHABLE_GRACE_S = (
    120.0  # the printer answers us but the watcher has not been connected for this long
)
JOB_MAX_AGE_S = 600.0  # notify-failure: a heartbeat older than this says nothing about the print any more
NOTIFY_EVERY_S = (
    1800.0  # one tg line per unit per failure episode (a crash loop must not repeat it)
)
ACTIVE = frozenset({"printing", "paused", "busy"})

OK, WARN, FAIL = "ok", "warn", "fail"
_RANK = {OK: 0, WARN: 1, FAIL: 2}

USAGE = """3d printer supervise status [NAME...|--all] [--json] [--no-live] [--unit-dir DIR]
3d printer supervise install [NAME...|--all] [--yes] [--plan] [--repo DIR] [--unit-dir DIR]
3d printer supervise uninstall [NAME...|--all] [--yes] [--unit-dir DIR]
3d printer supervise quiet NAME [--reason TEXT] | --clear

The always-on AI supervision of the owned printers: one `3d-printer-watch@NAME` unit (printer state, errors,
pauses, print start) and, with a camera, one `3d-printer-vision@NAME` unit (spaghetti, detached part), generated
from the fleet registry (owner own, access full). They run without a login (linger), restart forever, never
use --dry-run, need no Home Assistant and no omp session, and wait for a printer that is switched off.

  status      per printer: unit active/enabled, heartbeat age, printer state, camera frame age, linger, the
              intentional-off marker. Exit 1 (FAIL) when a printer that is printing or answering has no live
              unit or a stale heartbeat, or when linger is off. Reads the printer (one status GET) unless
              --no-live.
  install     writes the unit templates, `daemon-reload`, `enable --now` per printer, `enable-linger` (--yes).
              A hand-made template or a drop-in gate (`intentional-off.conf`) is replaced/retired only with
              --yes (a `.bak-<time>` / `.retired-<time>` copy stays); without --yes the plan is printed and
              nothing is changed. --plan only prints.
  uninstall   disables the printers' units; with --all --yes also removes the managed templates.
  quiet       write (or --clear) NAME's intentional-off marker: ONLY silences the "printer unreachable"
              alarms; it never stops a unit and is cleared when the printer answers.
  notify-failure NAME KIND   ExecStopPost hook of the units (KIND watch|vision); not for humans.

  3d printer supervise status
  3d printer supervise install --all --plan
  3d printer supervise install --all --yes
  3d printer supervise status u1 --json
  3d printer supervise uninstall k1se --yes
  3d printer supervise quiet k1se --reason "K1 switched off for the week\""""


# ---- witnesses ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class UnitState:
    unit: str
    known: bool  # systemctl answered at all
    load: str = ""
    active: str = ""
    sub: str = ""
    enabled: bool = False
    dry_run: bool = False
    restarts: int | None = None
    since: str = ""

    @property
    def running(self) -> bool:
        return self.known and self.active in RUNNING


def probe_unit(unit: str, show: Show) -> UnitState:
    from printer_ops.deploy_check import _fields, check_unit

    text = show(unit)
    if not text:
        return UnitState(unit, known=False)
    verdict = check_unit(unit, lambda _u: text)
    f = _fields(text)
    restarts = f.get("NRestarts", "")
    return UnitState(
        unit,
        True,
        f.get("LoadState", ""),
        f.get("ActiveState", ""),
        f.get("SubState", ""),
        f.get("UnitFileState", "") in ("enabled", "enabled-runtime", "linked", "alias"),
        verdict.dry_run,
        int(restarts) if restarts.isdigit() else None,
        f.get("ActiveEnterTimestamp", ""),
    )


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


@dataclass(frozen=True)
class Beat:
    """A heartbeat file seen at `now`: `age_s` since its own `updated`/`ts` stamp."""

    doc: dict[str, Any]
    age_s: float


def _stamp_age(doc: Mapping[str, Any], key: str, now: float) -> float | None:
    value = doc.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0.0, now - float(value))


def watch_beat(name: str, state_dir: Path, now: float) -> Beat | None:
    doc = read_json(state_dir / "watch" / f"{name}.json")
    if doc is None:
        return None
    age = _stamp_age(doc, "updated", now)
    return None if age is None else Beat(doc, age)


def vision_beat(name: str, state_dir: Path, now: float) -> Beat | None:
    doc = read_json(state_dir / "vision" / f"{name}.json")
    if doc is None or doc.get("schema") != 1:
        return None
    age = _stamp_age(doc, "ts", now)
    return None if age is None else Beat(doc, age)


def duplicate_watchers(
    name: str, state_dir: Path, now: float, alive: Callable[[int], bool]
) -> list[str]:
    """Other LIVE watcher processes that cover `name` (a hand-run `3d printer watch --all`, a second unit): their
    heartbeat files other than `watch/NAME.json` that list the printer and are fresh with a live pid."""
    found: list[str] = []
    directory = state_dir / "watch"
    if not directory.is_dir():
        return found
    for path in sorted(directory.glob("*.json")):
        if path.name == f"{name}.json":
            continue
        doc = read_json(path)
        if doc is None or name not in (doc.get("printers") or {}):
            continue
        age = _stamp_age(doc, "updated", now)
        pid = doc.get("pid")
        if (
            age is not None
            and age <= WATCH_BEAT_STALE_S
            and isinstance(pid, int)
            and alive(pid)
        ):
            found.append(f"{path.name} (pid {pid})")
    return found


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists, not ours to signal
    return True


def linger_enabled(run: Runner, user: str | None = None) -> bool | None:
    """`loginctl show-user USER -p Linger`: True/False, None when loginctl cannot say."""
    import getpass

    try:
        r = run(["loginctl", "show-user", user or getpass.getuser(), "-p", "Linger"])
    except (OSError, RuntimeError):
        return None
    if r.returncode != 0:
        return None
    value = (r.stdout or "").strip().partition("=")[2].strip()
    return {"yes": True, "no": False}.get(value)


def live_state(name: str, timeout: float = 5.0) -> str | None:
    """The printer's normalized state through its adapter (one read-only GET); None = it did not answer."""
    from errors import ThreeDError
    from printer_ops import make_adapter
    from printer_ops.devices import get_device

    if os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError(
            "refusing to contact a printer from a test: inject a state probe"
        )
    try:
        return make_adapter(get_device(name), timeout=timeout).status().state
    except (ThreeDError, OSError):
        return None


# ---- the verdict -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    level: str  # ok | warn | fail
    text: str
    fix: str = ""

    def to_json(self) -> dict[str, str]:
        return {"level": self.level, "text": self.text, "fix": self.fix}


@dataclass
class Assessment:
    printer: str
    camera: bool
    state: str | None  # None = the printer did not answer / was not asked
    live: bool  # the printer state came from a read just now
    findings: list[Finding] = field(default_factory=list)
    units: dict[str, UnitState] = field(default_factory=dict)
    watch: Beat | None = None
    vision: Beat | None = None
    marker: dict[str, Any] | None = None
    linger: bool | None = None
    now: float = 0.0

    @property
    def level(self) -> str:
        return max((f.level for f in self.findings), key=_RANK.__getitem__, default=OK)

    def to_json(self) -> dict[str, Any]:
        job = (
            ((self.watch.doc.get("printers") or {}).get(self.printer) or {}).get("job")
            if self.watch
            else None
        )
        return {
            "printer": self.printer,
            "level": self.level,
            "printer_state": self.state,
            "camera": self.camera,
            "linger": self.linger,
            "intentional_off": self.marker,
            "units": {
                k: {
                    "unit": u.unit,
                    "active": u.active,
                    "sub": u.sub,
                    "enabled": u.enabled,
                    "dry_run": u.dry_run,
                    "restarts": u.restarts,
                    "since": u.since,
                }
                for k, u in self.units.items()
            },
            "watch_heartbeat_age_s": None
            if self.watch is None
            else round(self.watch.age_s, 1),
            "vision_heartbeat_age_s": None
            if self.vision is None
            else round(self.vision.age_s, 1),
            "job": job,
            "findings": [f.to_json() for f in self.findings],
        }


def _age(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def assess(
    plan: PrinterPlan,
    *,
    units: Mapping[str, UnitState],
    watch: Beat | None,
    vision: Beat | None,
    state: str | None,
    live: bool,
    linger: bool | None,
    marker: dict[str, Any] | None,
    dropins: Sequence[DropIn],
    duplicates: Sequence[str],
    now: float,
) -> Assessment:
    """Judge one printer's supervision from the witnesses (pure: the clock is `now`).

    A printer that ANSWERS (printing, idle, ...) must have every unit running with a fresh heartbeat: any gap is
    a FAIL. A printer that does not answer (switched off) is judged by the same rules at WARN level: the
    supervision has to be ready for the moment it comes back, but nothing is being missed right now."""
    a = Assessment(
        plan.name,
        plan.camera,
        state,
        live,
        units=dict(units),
        watch=watch,
        vision=vision,
        marker=marker,
        linger=linger,
        now=now,
    )
    need = FAIL if state is not None else WARN
    add = a.findings.append
    install = "3d printer supervise install --all --yes"

    if linger is False:
        add(
            Finding(
                FAIL,
                "linger is off: the units stop when you log out and do not start after a reboot",
                "3d printer supervise install --all --yes   # runs `loginctl enable-linger`",
            )
        )
    elif linger is None:
        add(Finding(WARN, "cannot tell whether linger is on (loginctl did not answer)"))

    for kind in plan.kinds:
        u = units.get(kind)
        label = f"{kind} unit"
        if u is None or not u.known:
            add(
                Finding(
                    FAIL,
                    f"{label}: systemctl --user cannot be read here: run this on the host of the services",
                )
            )
            continue
        if u.load == "not-found":
            add(Finding(need, f"{label} {u.unit} is not installed", install))
            continue
        if not u.running:
            held = [
                d
                for d in dropins
                if d.retirable and d.path.parent.name.startswith(f"3d-printer-{kind}@")
            ]
            why = (
                " (held by an intentional-off ConditionPathExists drop-in)"
                if held
                else ""
            )
            add(
                Finding(
                    need,
                    f"{label} {u.unit} is {u.active or 'not running'}{why}",
                    install,
                )
            )
            continue
        if u.dry_run:
            add(
                Finding(
                    FAIL,
                    f"{label} {u.unit} runs with --dry-run: alerts are printed, never delivered",
                    install,
                )
            )
        if not u.enabled:
            add(
                Finding(
                    WARN,
                    f"{label} {u.unit} runs but is not enabled: it will not start after a reboot",
                    f"systemctl --user enable {u.unit}",
                )
            )
        if u.restarts is not None and u.restarts >= 5:
            add(
                Finding(
                    WARN,
                    f"{label} {u.unit} restarted {u.restarts} times since it started"
                    " (journalctl --user -u " + u.unit + ")",
                )
            )

    watch_unit = units.get("watch")
    if watch_unit is not None and watch_unit.running:
        if watch is None:
            add(
                Finding(
                    need,
                    "the watcher has never written a heartbeat (watch/NAME.json)",
                    f"journalctl --user -u {watch_unit.unit} -n 50",
                )
            )
        elif watch.age_s > WATCH_BEAT_STALE_S:
            add(
                Finding(
                    need,
                    f"the watcher heartbeat is {_age(watch.age_s)} old: the process is alive but wedged",
                    f"systemctl --user restart {watch_unit.unit}",
                )
            )
        else:
            p = (watch.doc.get("printers") or {}).get(plan.name) or {}
            since = p.get("unreachable_since")
            if (
                state is not None
                and p.get("connected") is False
                and isinstance(since, (int, float))
                and now - since > UNREACHABLE_GRACE_S
            ):
                add(
                    Finding(
                        FAIL,
                        f"the printer answers but the watcher has been disconnected for "
                        f"{_age(now - since)} ({p.get('last_error') or 'no reason recorded'})",
                        f"systemctl --user restart {watch_unit.unit}",
                    )
                )

    vision_unit = units.get("vision")
    if vision_unit is not None and vision_unit.running:
        if vision is None:
            add(
                Finding(
                    need,
                    "the vision loop has never written its status (vision/NAME.json)",
                    f"journalctl --user -u {vision_unit.unit} -n 50",
                )
            )
        else:
            interval = vision.doc.get("interval_s")
            limit = (
                max(VISION_STALE_MIN_S, 4 * float(interval))
                if isinstance(interval, (int, float))
                else 120.0
            )
            if vision.age_s > limit:
                add(
                    Finding(
                        need,
                        f"the vision status is {_age(vision.age_s)} old: the loop is alive but wedged",
                        f"systemctl --user restart {vision_unit.unit}",
                    )
                )
            elif vision.doc.get("supervising") is True:
                frame_age = _stamp_age(vision.doc, "last_frame_ts", now)
                if frame_age is None or frame_age > FRAME_STALE_S:
                    add(
                        Finding(
                            FAIL,
                            f"the printer prints and the camera gave no frame for {_age(frame_age)}",
                            "3d printer vision doctor; 3d printers doctor --check camera-config",
                        )
                    )

    if duplicates:
        add(
            Finding(
                FAIL,
                "another watcher covers this printer too: "
                + ", ".join(duplicates)
                + " (one watcher per printer: two would double every alert and race the pause)",
                "stop the extra process; the unit is the one watcher",
            )
        )
    for d in dropins:
        if d.kind == "gate":
            add(
                Finding(
                    WARN,
                    f"obsolete drop-in {d.path}: " + d.detail,
                    "3d printer supervise install --all --yes   # retires it, keeps a copy",
                )
            )
        elif d.kind == "dry-run":
            add(
                Finding(
                    FAIL,
                    f"drop-in {d.path}: {d.detail}",
                    f"edit it: remove --dry-run, then {install}",
                )
            )

    if marker is not None:
        add(
            Finding(
                OK,
                f"intentional-off marker set ({marker.get('reason') or 'no reason'}): unreachable alarms "
                "are silenced; it is cleared when the printer answers",
            )
        )
    if state is None and not a.findings:
        add(
            Finding(
                OK,
                "printer is not answering (switched off?): the units wait and supervise it as soon as it does",
            )
        )
    if not a.findings:
        add(Finding(OK, "supervised"))
    return a


def render(assessments: Sequence[Assessment]) -> str:
    lines: list[str] = []
    for a in assessments:
        state = (
            a.state
            if a.state is not None
            else ("unreachable" if a.live else "unknown (not asked)")
        )
        lines.append(f"{a.level.upper():4} {a.printer}: printer {state}")
        for kind, u in a.units.items():
            if not u.known:
                lines.append(f"       {kind:6} {u.unit}: cannot be inspected")
                continue
            beat = a.watch if kind == "watch" else a.vision
            extra = "" if beat is None else f", heartbeat {_age(beat.age_s)} ago"
            if kind == "vision" and a.vision is not None:
                frame = _stamp_age(a.vision.doc, "last_frame_ts", a.now)
                extra += ", " + ("supervising a print" if a.vision.doc.get("supervising") else "idle")
                if frame is not None:
                    extra += f", last frame {_age(frame)} ago"
            flags = ("enabled" if u.enabled else "not enabled") + (
                ", --dry-run" if u.dry_run else ""
            )
            lines.append(
                f"       {kind:6} {u.unit}: {u.active or '?'}/{u.sub or '?'} ({flags}){extra}"
            )
        lines.append(
            f"       linger: {'yes' if a.linger else 'NO' if a.linger is False else 'unknown'}"
        )
        for f in a.findings:
            mark = {OK: "  ok", WARN: "warn", FAIL: "FAIL"}[f.level]
            lines.append(f"       {mark} {f.text}")
            if f.fix and f.level != OK:
                lines.append(f"            $ {f.fix}")
    return "\n".join(lines)


# ---- environment (every seam a test replaces) --------------------------------------------------


@dataclass
class Env:
    show: Show | None = None
    run: Runner | None = None
    state_dir: Path | None = None
    unit_dir: Path | None = None
    clock: Callable[[], float] = time.time
    state_probe: Callable[[str], str | None] | None = None
    alive: Callable[[int], bool] = pid_alive
    plans: Callable[[], list[PrinterPlan]] | None = None
    ledger: DeliveryState | None = None

    def runner(self) -> Runner:
        return self.run or real_runner

    def shower(self) -> Show:
        if self.show is not None:
            return self.show
        from printer_ops.deploy_check import systemctl_show

        return systemctl_show

    def states(self) -> Path:
        if self.state_dir is not None:
            return self.state_dir
        from cli.paths import state_dir

        return state_dir()

    def units_dir(self) -> Path:
        return self.unit_dir or default_unit_dir()

    def eligible(self) -> list[PrinterPlan]:
        if self.plans is not None:
            return self.plans()
        from printer_ops.devices import load_devices
        from printops.fleet import load_fleet

        return plan_printers(load_fleet(), dict(load_devices()))


def gather(
    plan: PrinterPlan, env: Env, *, live: bool, layout: Layout, linger: bool | None
) -> Assessment:
    from printer_ops import intentional_off

    now = env.clock()
    show = env.shower()
    units = {k: probe_unit(unit_name(k, plan.name), show) for k in plan.kinds}
    states = env.states()
    watch = watch_beat(plan.name, states, now)
    state: str | None = None
    if live:
        state = (env.state_probe or live_state)(plan.name)
    elif watch is not None and watch.age_s <= WATCH_BEAT_STALE_S:
        # not asked: the watcher's own fresh reading of the printer stands in for it
        mine = (watch.doc.get("printers") or {}).get(plan.name) or {}
        if mine.get("connected") is True and isinstance(mine.get("state"), str):
            state = mine["state"]
    return assess(
        plan,
        units=units,
        watch=watch,
        vision=vision_beat(plan.name, states, now) if plan.camera else None,
        state=state,
        live=live,
        linger=linger,
        marker=intentional_off.read_marker(plan.name),
        dropins=find_dropins(env.units_dir(), [plan], layout),
        duplicates=duplicate_watchers(plan.name, states, now, env.alive),
        now=now,
    )


def _select(env: Env, names: list[str], every: bool) -> list[PrinterPlan]:
    from errors import UsageError

    plans = env.eligible()
    if not names:
        if not plans:
            raise UsageError(
                "no owned, fully controlled printer with a watch stream is configured",
                command="printer supervise",
                remediation=[
                    "fleet/printers.yaml: owner own, access full, adapter moonraker or creality-stock"
                ],
            )
        return plans
    by = {p.name: p for p in plans}
    unknown = [n for n in names if n not in by]
    if unknown:
        raise UsageError(
            f"{', '.join(unknown)}: not a supervised printer (owner own + access full + a watch adapter)",
            command="printer supervise",
            remediation=[f"supervised printers: {', '.join(sorted(by)) or 'none'}"],
        )
    return [by[n] for n in names]


def _parse(
    argv: list[str], flags: set[str], values: set[str], command: str
) -> tuple[list[str], dict[str, Any]]:
    from errors import UsageError

    names: list[str] = []
    opts: dict[str, Any] = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in flags:
            opts[a] = True
        elif a in values:
            if i + 1 >= len(argv):
                raise UsageError(f"{a} needs a value", command=command)
            opts[a] = argv[i + 1]
            i += 1
        elif a.startswith("-"):
            raise UsageError(
                f"unknown option {a}", command=command, remediation=[USAGE]
            )
        else:
            names.append(a)
        i += 1
    return names, opts


# ---- commands ----------------------------------------------------------------------------------


def cmd_status(argv: list[str], env: Env) -> int:
    from errors import GateFailure

    names, opts = _parse(
        argv, {"--all", "--json", "--no-live"}, {"--unit-dir"}, "printer supervise status"
    )
    if "--unit-dir" in opts:
        env.unit_dir = Path(opts["--unit-dir"]).expanduser()
    plans = _select(env, names, bool(opts.get("--all")))
    layout = Layout.detect(unit_dir=env.units_dir())
    linger = linger_enabled(env.runner())
    results = [
        gather(p, env, live=not opts.get("--no-live"), layout=layout, linger=linger)
        for p in plans
    ]
    if opts.get("--json"):
        print(json.dumps([r.to_json() for r in results], ensure_ascii=False, indent=1))
    else:
        print(render(results))
    bad = [r.printer for r in results if r.level == FAIL]
    if bad:
        raise GateFailure(
            f"supervision FAILED for {', '.join(bad)}",
            command="printer supervise",
            silent=True,
        )
    return 0


def _installed_state(
    plans: Sequence[PrinterPlan], env: Env
) -> tuple[dict[str, bool], set[str]]:
    show = env.shower()
    enabled: dict[str, bool] = {}
    dry: set[str] = set()
    for p in plans:
        for unit in p.units():
            u = probe_unit(unit, show)
            enabled[unit] = u.known and u.enabled and u.running
            if u.running and u.dry_run:
                dry.add(unit)
    return enabled, dry


def cmd_install(argv: list[str], env: Env) -> int:
    from errors import GateFailure

    names, opts = _parse(
        argv,
        {"--all", "--yes", "--plan"},
        {"--repo", "--unit-dir"},
        "printer supervise install",
    )
    if "--unit-dir" in opts:
        env.unit_dir = Path(opts["--unit-dir"]).expanduser()
    plans = _select(env, names, bool(opts.get("--all")))
    layout = Layout.detect(
        Path(opts["--repo"]).expanduser() if "--repo" in opts else None, env.units_dir()
    )
    if ".config/superpowers/worktrees" in str(layout.repo):
        print(
            f"warning: the units would run from the worktree {layout.repo}; install from the main checkout "
            "(or pass --repo) so they survive its removal"
        )
    installed = read_installed(env.units_dir())
    enabled, dry = _installed_state(plans, env)
    dropins = find_dropins(env.units_dir(), plans, layout)
    linger = linger_enabled(env.runner())
    now = env.clock()
    actions = plan_install(
        plans,
        layout,
        installed=installed,
        dropins=dropins,
        linger=linger,
        enabled=enabled,
        loaded_dry_run=dry,
        now=now,
    )
    _print_plan(plans, actions, dropins, linger)
    needs = blocked(actions)
    if opts.get("--plan"):
        return 0
    if not actions:
        print("nothing to do: the supervision is installed as shipped")
        return 0
    if needs and not opts.get("--yes"):
        print(
            "\nnothing was changed: the actions marked [needs --yes] replace or retire files you made "
            f"(a copy is kept). Re-run with --yes: 3d printer supervise install {' '.join(names) or '--all'} --yes"
        )
        raise GateFailure(
            "install needs --yes", command="printer supervise", silent=True
        )
    report = apply(actions, yes=bool(opts.get("--yes")), run=env.runner(), now=now)
    for line in report.done:
        print(f"done    {line}")
    for line in report.skipped:
        print(f"skipped {line}")
    for line in report.failed:
        print(f"FAILED  {line}")
    if linger is False and not opts.get("--yes"):
        print(
            "linger is off: run `loginctl enable-linger` (or re-run with --yes) so the units start at boot"
        )
    running_old = [a.target for a in actions if a.op == "restart"]
    if running_old:
        print(
            "note: restarted because they ran with --dry-run: " + ", ".join(running_old)
        )
    print("check: 3d printer supervise status")
    if report.failed:
        raise GateFailure(
            "some install steps failed", command="printer supervise", silent=True
        )
    return 0


def _print_plan(
    plans: Sequence[PrinterPlan],
    actions: Sequence[Action],
    dropins: Sequence[DropIn],
    linger: bool | None,
) -> None:
    print(
        "supervised printers: "
        + ", ".join(
            f"{p.name} ({'watch+vision' if p.camera else 'watch'})" for p in plans
        )
    )
    for a in actions:
        print(
            f"plan    {a.op:8} {a.target}: {a.detail}"
            + ("  [needs --yes]" if a.needs_yes else "")
        )
    for d in dropins:
        if not d.retirable:
            print(f"keep    drop-in {d.path}: {d.detail}")


def cmd_uninstall(argv: list[str], env: Env) -> int:
    from errors import GateFailure

    names, opts = _parse(
        argv, {"--all", "--yes"}, {"--unit-dir"}, "printer supervise uninstall"
    )
    if "--unit-dir" in opts:
        env.unit_dir = Path(opts["--unit-dir"]).expanduser()
    plans = _select(env, names, bool(opts.get("--all")))
    layout = Layout.detect(unit_dir=env.units_dir())
    enabled, _ = _installed_state(plans, env)
    actions = plan_uninstall(
        plans,
        layout,
        installed=read_installed(env.units_dir()),
        all_units=bool(opts.get("--all")),
        enabled=enabled,
    )
    for a in actions:
        print(
            f"plan    {a.op:8} {a.target}: {a.detail}"
            + ("  [needs --yes]" if a.needs_yes else "")
        )
    if not actions:
        print("nothing to do")
        return 0
    if blocked(actions) and not opts.get("--yes"):
        print("nothing was changed: re-run with --yes to remove the managed templates")
        raise GateFailure(
            "uninstall needs --yes", command="printer supervise", silent=True
        )
    report = apply(
        actions, yes=bool(opts.get("--yes")), run=env.runner(), now=env.clock()
    )
    for line in report.done:
        print(f"done    {line}")
    for line in report.failed:
        print(f"FAILED  {line}")
    if report.failed:
        raise GateFailure("some steps failed", command="printer supervise", silent=True)
    return 0


def cmd_quiet(argv: list[str], env: Env) -> int:
    from errors import UsageError
    from printer_ops import intentional_off

    names, opts = _parse(argv, {"--clear"}, {"--reason"}, "printer supervise quiet")
    if len(names) != 1:
        raise UsageError(
            "`printer supervise quiet` takes exactly one PRINTER",
            command="printer supervise",
            remediation=[
                "3d printer supervise quiet k1se --reason 'switched off for the week'"
            ],
        )
    (name,) = names
    _select(env, [name], False)
    if opts.get("--clear"):
        print(
            f"{name}: marker {'removed' if intentional_off.clear_marker(name) else 'was not set'}"
        )
        return 0
    path = intentional_off.set_marker(name, str(opts.get("--reason", "")), env.clock())
    print(
        f"{name}: unreachable alarms are silenced ({path}); the units keep running and supervise the printer "
        "the moment it answers, which also clears the marker"
    )
    return 0


def printing_when_stopped(name: str, kind: str, state_dir: Path, now: float) -> bool:
    """Was a job active when the supervisor died, by its own last heartbeat? Unknown counts as yes: a missing or
    ancient heartbeat must not hide the death of a supervisor from the owner."""
    if kind == "watch":
        beat = watch_beat(name, state_dir, now)
        if beat is None or beat.age_s > JOB_MAX_AGE_S:
            return True
        return bool(((beat.doc.get("printers") or {}).get(name) or {}).get("job"))
    beat = vision_beat(name, state_dir, now)
    if beat is None or beat.age_s > JOB_MAX_AGE_S:
        return True
    return beat.doc.get("state") in ACTIVE


def failure_text(name: str, kind: str, result: str, status: str) -> str:
    what = f"{result}, exit {status}" if status else result
    return (
        f"[{name}] the {kind} supervisor stopped during a print ({what}); systemd restarts it. "
        f"Check: 3d printer supervise status {name}"
    )


def cmd_notify_failure(
    argv: list[str],
    env: Env,
    environ: Mapping[str, str] | None = None,
    runner: Any = None,
) -> int:
    """ExecStopPost of the units. Silent for a clean stop and when the heartbeat says no print was running; else
    ONE short tg line per unit per NOTIFY_EVERY_S through the delivery ledger. Never raises: it must not turn a
    restart into a failure."""
    import sys

    env_vars = os.environ if environ is None else environ
    if len(argv) != 2 or argv[1] not in KINDS:
        print("supervise notify-failure NAME watch|vision", file=sys.stderr)
        return 2
    name, kind = argv
    result = env_vars.get("SERVICE_RESULT", "")
    if result in ("", "success"):
        return 0
    now = env.clock()
    try:
        if not printing_when_stopped(name, kind, env.states(), now):
            return 0
        from printer_ops.subscriptions import DeliveryState
        from printer_ops.watch_sinks import find_tool, run_real_tg

        ledger = env.ledger or DeliveryState()
        key = f"supervisor-down:{name}:{kind}"
        if not ledger.claim_warning(key, now, NOTIFY_EVERY_S, fail_open=True):
            return 0
        run = runner or run_real_tg
        status = env_vars.get("EXIT_STATUS", "")
        r = run(
            [
                find_tool("tg") or "tg",
                "--tag",
                "problem",
                failure_text(name, kind, result, status),
            ],
            text=True,
            capture_output=True,
            timeout=60,
        )
        if r.returncode != 0:
            ledger.release_warning(key)  # not delivered: the next stop may try again
            print(
                f"supervise: tg refused the failure line: exit {r.returncode}",
                file=sys.stderr,
            )
    except Exception as exc:  # best effort: the restart is systemd's job, not ours
        print(
            f"supervise: cannot report the failure: {exc.__class__.__name__}: {exc}",
            file=sys.stderr,
        )
    return 0


def run(argv: list[str], env: Env | None = None, **kw: Any) -> int:
    from errors import UsageError

    env = env or Env()
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0 if argv else 1
    sub, rest = argv[0], argv[1:]
    if sub == "status":
        return cmd_status(rest, env)
    if sub == "install":
        return cmd_install(rest, env)
    if sub == "uninstall":
        return cmd_uninstall(rest, env)
    if sub == "quiet":
        return cmd_quiet(rest, env)
    if sub == "notify-failure":
        return cmd_notify_failure(rest, env, **kw)
    raise UsageError(
        f"unknown supervise subcommand '{sub}'",
        command="printer supervise",
        remediation=[
            "Use status, install, uninstall or quiet — see `3d printer supervise --help`."
        ],
    )


__all__ = [
    "USAGE",
    "Assessment",
    "Beat",
    "Env",
    "Finding",
    "UnitState",
    "assess",
    "duplicate_watchers",
    "gather",
    "linger_enabled",
    "printing_when_stopped",
    "probe_unit",
    "render",
    "run",
    "vision_beat",
    "watch_beat",
]
