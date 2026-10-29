"""supervise_units.py — the systemd user units of the always-on printer supervision, shipped from the repo.

WHAT: renders the three unit TEMPLATES `3d-printer-watch@.service`, `3d-printer-vision@.service` (the
  camera loop) and plans/applies their installation for every OWNED, fully controlled fleet printer
  (fleet/printers.yaml: owner own, access full, an adapter with a watch stream; vision only when a
  camera is configured). `3d printer supervise install|uninstall` (supervise.py) drives this module.

WHY (#639): before this module the units were hand-made: templates ending in `--dry-run` plus per-printer
  drop-ins nobody could recreate, and k1se carried an `intentional-off.conf` gate that kept both units
  stopped while a marker file existed (#640), so a printer switched on again was unwatched.

THE UNITS: no `--dry-run`, `Restart=always`, `StartLimitIntervalSec=0` (a crash loop never gives up),
  `WantedBy=default.target` (with `loginctl enable-linger` they start at boot without a login), ordered
  `After=network-online.target hsw-observer-agent.service` (ordering only: no Wants, no dependency on Home
  Assistant or an omp session; a hand-made drop-in with Wants= keeps working), and
  `ExecStopPost=-... supervise notify-failure` — systemd never puts a `Restart=always` unit into the
  `failed` state, so OnFailure= would never fire; ExecStopPost runs after EVERY exit and the handler stays
  silent for a clean stop (SERVICE_RESULT=success) (#642).

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

if TYPE_CHECKING:
    from printops.fleet import Fleet

MANAGED_MARK = (
    "# managed-by: 3d printer supervise install (do not edit; override with a drop-in)"
)
KINDS = ("watch", "vision")
WATCH_ADAPTERS = frozenset({"moonraker", "creality-stock"})
RESTART_SEC = {"watch": 15, "vision": 30}
RUNNING = frozenset({"active", "activating", "reloading"})

# A systemctl/loginctl call: argv -> CompletedProcess. The default refuses to run under pytest.
Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def real_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError(f"refusing to run {argv[0]} from a test: inject a runner")
    return subprocess.run(
        list(argv), capture_output=True, text=True, timeout=60, check=False
    )


def unit_name(kind: str, printer: str) -> str:
    return f"3d-printer-{kind}@{printer}.service"


def template_name(kind: str) -> str:
    return f"3d-printer-{kind}@.service"


def default_unit_dir() -> Path:
    return (
        Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        / "systemd"
        / "user"
    )


@dataclass(frozen=True)
class PrinterPlan:
    """One printer to supervise: its units follow from `camera` (a vision unit only with a camera)."""

    name: str
    adapter: str
    camera: bool

    @property
    def kinds(self) -> tuple[str, ...]:
        return KINDS if self.camera else ("watch",)

    def units(self) -> list[str]:
        return [unit_name(k, self.name) for k in self.kinds]


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
        if kind == "watch":
            return [
                str(self.python),
                str(self.repo / "bin" / "3d"),
                "printer",
                "watch",
                "%i",
            ]
        return [
            str(self.python),
            str(self.repo / "bin" / "3d"),
            "printer",
            "vision",
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
    "watch": "3D printer watch %i (always on: Klipper state, errors, pauses, print start; AI alerts + auto pause)",
    "vision": "3D printer vision %i (always on: camera frames, spaghetti / detached part; AI alerts + auto pause)",
}
DOCS = {
    "watch": "https://github.com/alex-mextner/3d-cli/blob/main/docs/commands/printer.md#always-on-supervision",
    "vision": "https://github.com/alex-mextner/3d-cli/blob/main/docs/commands/printer-vision.md",
}


def render_template(kind: str, layout: Layout) -> str:
    """The text of `3d-printer-KIND@.service`; deterministic for a given layout (snapshot-tested)."""
    if kind not in KINDS:
        raise ValueError(f"unknown supervision unit kind {kind!r}")
    pin = (
        f"Environment=XDG_CONFIG_HOME={layout.xdg_config}\n"
        if layout.xdg_config
        else ""
    )
    return f"""{MANAGED_MARK}
[Unit]
Description={DESCRIPTIONS[kind]}
Documentation={DOCS[kind]}
After=network-online.target hsw-observer-agent.service
StartLimitIntervalSec=0

[Service]
Type=simple
Environment=PATH={layout.repo}/.venv/bin:%h/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=PYTHONUNBUFFERED=1
{pin}WorkingDirectory={layout.repo}
UMask=0077
ExecStart={shlex.join(layout.exec_argv(kind))}
ExecStopPost=-{shlex.join(layout.stop_post_argv(kind))}
Restart=always
RestartSec={RESTART_SEC[kind]}

[Install]
WantedBy=default.target
"""


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


def classify_dropin(path: Path, text: str, canonical: list[str]) -> DropIn:
    """What a hand-made drop-in does to a supervision unit. `canonical` is the template's ExecStart argv with
    the instance already substituted."""
    body = [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith(("#", ";"))
    ]
    directives = [ln for ln in body if not ln.startswith("[")]
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
        try:
            argv = shlex.split(execs[-1])
        except ValueError:
            return DropIn(
                path, "custom-exec", "ExecStart override that cannot be parsed"
            )
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
        return DropIn(path, "custom-exec", f"ExecStart override: {execs[-1]}")
    if gates:
        return DropIn(
            path,
            "other",
            "an intentional-off ConditionPathExists plus other directives: edit by hand",
        )
    return DropIn(path, "other", "other directives (kept)")


def find_dropins(
    unit_dir: Path, plans: Sequence[PrinterPlan], layout: Layout
) -> list[DropIn]:
    """Every `*.conf` drop-in of the supervision templates and instances, classified."""
    found: list[DropIn] = []
    dirs: list[tuple[Path, str, str]] = [
        (unit_dir / f"{template_name(kind)}.d", kind, "%i") for kind in KINDS
    ]
    dirs.extend(
        (unit_dir / f"{unit_name(kind, plan.name)}.d", kind, plan.name)
        for plan in plans
        for kind in plan.kinds
    )
    for directory, kind, instance in dirs:
        if not directory.is_dir():
            continue
        canonical = [a.replace("%i", instance) for a in layout.exec_argv(kind)]
        for conf in sorted(directory.glob("*.conf")):
            try:
                text = conf.read_text(encoding="utf-8")
            except OSError:
                continue
            found.append(classify_dropin(conf, text, canonical))
    return found


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
) -> list[Action]:
    """The ordered actions that make the supervision of `plans` right. `installed` is unit file name -> its text
    (missing = absent); `enabled` unit -> is enabled; `loaded_dry_run` the units whose RUNNING definition
    still carries --dry-run. Pure: nothing is read or written here."""
    actions: list[Action] = []
    kinds = sorted({k for p in plans for k in p.kinds}, key=KINDS.index)
    for kind in kinds:
        name = template_name(kind)
        desired = render_template(kind, layout)
        have = installed.get(name)
        path = layout.unit_dir / name
        if have == desired:
            continue
        if have is None:
            actions.append(
                Action("write", name, "new template", path=path, text=desired)
            )
        elif is_managed(have):
            actions.append(
                Action(
                    "write",
                    name,
                    "update the managed template",
                    path=path,
                    text=desired,
                )
            )
        else:
            how = "carries --dry-run" if "--dry-run" in have else "hand-made"
            actions.append(
                Action(
                    "replace",
                    name,
                    f"replace the {how} template (a .bak-{_stamp(now)} copy is kept)",
                    needs_yes=True,
                    path=path,
                    text=desired,
                )
            )
    actions.extend(
        Action("retire", d.path.name, d.detail, needs_yes=True, path=d.path)
        for d in dropins
        if d.retirable
    )
    changed = bool(actions)
    if changed:
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
    for plan in plans:
        for unit in plan.units():
            if not enabled.get(unit, False):
                actions.append(
                    Action(
                        "enable",
                        unit,
                        "enable and start",
                        argv=["systemctl", "--user", "enable", "--now", unit],
                    )
                )
            elif unit in loaded_dry_run:
                actions.append(
                    Action(
                        "restart",
                        unit,
                        "the running definition still has --dry-run",
                        argv=["systemctl", "--user", "restart", unit],
                    )
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
            if a.op in ("write", "replace"):
                assert a.path is not None and a.text is not None
                a.path.parent.mkdir(parents=True, exist_ok=True)
                if a.op == "replace" and a.path.exists():
                    a.path.rename(a.path.with_name(f"{a.path.name}.bak-{_stamp(now)}"))
                _write_atomic(a.path, a.text)
            elif a.op == "retire":
                assert a.path is not None
                a.path.rename(a.path.with_name(f"{a.path.name}.retired-{_stamp(now)}"))
            elif a.op == "remove":
                assert a.path is not None
                a.path.unlink(missing_ok=True)
            elif a.op == "linger":
                if not yes:
                    report.skipped.append(label + " (needs --yes)")
                    continue
                _run_checked(run, a.argv, report, label)
                continue
            else:
                _run_checked(run, a.argv, report, label)
                continue
            report.done.append(label)
        except OSError as exc:
            report.failed.append(f"{label}: {exc.strerror or exc}")
    return report


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


def plan_uninstall(
    plans: Sequence[PrinterPlan],
    layout: Layout,
    *,
    installed: dict[str, str],
    all_units: bool,
    enabled: dict[str, bool],
) -> list[Action]:
    """Disable the printers' instances; with `all_units` (no printer left to supervise) also remove the managed
    templates. A hand-made template is never removed here."""
    actions: list[Action] = []
    actions.extend(
        Action(
            "disable",
            unit,
            "stop and disable",
            argv=["systemctl", "--user", "disable", "--now", unit],
        )
        for plan in plans
        for unit in plan.units()
        if enabled.get(unit, False)
    )
    if all_units:
        for kind in KINDS:
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
    for kind in KINDS:
        name = template_name(kind)
        try:
            out[name] = (unit_dir / name).read_text(encoding="utf-8")
        except OSError:
            continue
    return out


__all__ = [
    "KINDS",
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
