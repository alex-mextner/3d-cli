"""Fakes for the always-on supervision tests (printer_ops/supervise*.py and its doctor check): a fake
`systemctl --user show`, a recording systemctl/loginctl runner, heartbeat file writers and a fake tg. Nothing
here reaches systemd, loginctl, tg or a printer."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from printer_ops import supervise as sv
from printer_ops import supervise_units as su
from printer_ops.subscriptions import DeliveryState

NOW = 1_800_000_000.0  # the doctor rig's ManualClock starts here too
MONO = 5_000.0  # CLOCK_MONOTONIC "now" of the fake systemd: its timestamps are seconds since boot
LAYOUT = su.Layout(
    Path("/srv/3d-cli"), Path("/srv/3d-cli/.venv/bin/python"), Path("/unused"), None
)
CAMERA = {
    "vision": {"camera": {"kind": "snapshot-url", "url": "http://127.0.0.1:1/snap"}}
}


def unit_show(
    active: str = "active",
    *,
    enabled: str = "enabled",
    args: str = "printer watch p1",
    sub: str = "running",
    restarts: int = 0,
    load: str = "loaded",
    inactive_mono: float | None = None,
    active_mono: float | None = None,
) -> str:
    """`systemctl --user show` of one unit. `inactive_mono`: CLOCK_MONOTONIC seconds it last became inactive (the
    one-shot check service: when the last check finished)."""
    inactive = (
        f"InactiveEnterTimestampMonotonic={int(inactive_mono * 1e6)}\n"
        if inactive_mono is not None
        else ""
    )
    armed = (
        f"ActiveEnterTimestampMonotonic={int(active_mono * 1e6)}\n"
        if active_mono is not None
        else ""
    )
    return (
        inactive
        + armed
        + (
            f"LoadState={load}\nActiveState={active}\nSubState={sub}\nUnitFileState={enabled}\nNRestarts={restarts}\n"
            f"ExecStart={{ path=/usr/bin/3d ; argv[]=/usr/bin/3d {args} ; ignore_errors=no ; pid=0 }}\n"
            "EnvironmentFiles=\n"
        )
    )


class Systemd:
    """A fake `systemctl --user show`: unit name -> text; anything else is not installed. `last_checks` is what
    each printer's one-shot check left in its state file (`ondemand.read_status`): name -> {printer, phase}."""

    def __init__(
        self,
        units: dict[str, str] | None = None,
        last_checks: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.units = units or {}
        self.last_checks = last_checks or {}

    def __call__(self, unit: str) -> str:
        return self.units.get(unit, "LoadState=not-found\nActiveState=inactive\n")

    def last_check(self, name: str) -> dict[str, Any]:
        return self.last_checks.get(name, {})


def parse_status(text: str) -> tuple[dict[str, str], float | None]:
    """`printer=idle phase=idle polled=<epoch>` -> (the check's state-file view, the timer's last trigger)."""
    fields = dict(w.split("=", 1) for w in text.split() if "=" in w)
    polled = fields.pop("polled", "")
    return fields, float(polled) if polled.isdigit() else None


def healthy(
    name: str = "p1",
    camera: bool = True,
    *,
    status_text: str = "",
    supervisor: bool = True,
    **over: Any,
) -> Systemd:
    """A supervisor timer that is armed (enabled) and workers that run too (not enabled: the check starts them,
    `enabled` in `over` overrides both). `status_text` (`printer=idle phase=idle polled=<epoch>`) is what the
    supervisor's last check saw: the state file view plus when the timer last fired."""
    units: dict[str, str] = {}
    last_checks: dict[str, dict[str, Any]] = {}
    if supervisor:
        seen, polled = parse_status(status_text)
        if seen:
            last_checks[name] = seen
        units[f"3d-printer-ondemand@{name}.timer"] = unit_show(
            **{
                "active": "active",
                "sub": "waiting",
                "enabled": "enabled",
                "args": "",
                **over,
            }
        )
        units[f"3d-printer-ondemand@{name}.service"] = unit_show(
            active="inactive",
            sub="dead",
            enabled="static",
            args="",
            inactive_mono=None if polled is None else MONO - (NOW - polled),
        )
    kinds = ("watch", "vision") if camera else ("watch",)
    for kind in kinds:
        units[f"3d-printer-{kind}@{name}.service"] = unit_show(
            **{"enabled": "static", "args": f"printer {kind} {name}", **over}
        )
    return Systemd(units, last_checks)


class Runner:
    """Records every systemctl/loginctl call; `loginctl show-user` answers `linger`."""

    def __init__(self, linger: bool | None = True) -> None:
        self.calls: list[list[str]] = []
        self.linger = linger
        self.fail: set[str] = set()

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if argv[0] == "loginctl" and argv[1] == "show-user":
            if self.linger is None:
                return subprocess.CompletedProcess(list(argv), 1, "", "no session bus")
            return subprocess.CompletedProcess(
                list(argv), 0, f"Linger={'yes' if self.linger else 'no'}\n", ""
            )
        if " ".join(argv) in self.fail:
            return subprocess.CompletedProcess(
                list(argv), 1, "", "Failed to connect to bus"
            )
        return subprocess.CompletedProcess(list(argv), 0, "", "")


def plan(name: str = "p1", camera: bool = True) -> su.PrinterPlan:
    return su.PrinterPlan(name, "moonraker", camera)


def write_watch_beat(
    state: Path,
    name: str,
    *,
    age: float = 5.0,
    job: Any = None,
    connected: bool = True,
    unreachable_since: float | None = None,
    pid: int = 4242,
) -> None:
    (state / "watch").mkdir(parents=True, exist_ok=True)
    doc = {
        "pid": pid,
        "updated": NOW - age,
        "printers": {
            name: {
                "connected": connected,
                "state": "printing" if job else "idle",
                "job": job,
                "unreachable_since": unreachable_since,
                "last_poll_ok": NOW - age,
            }
        },
    }
    (state / "watch" / f"{name}.json").write_text(json.dumps(doc), encoding="utf-8")


def write_vision_beat(
    state: Path,
    name: str,
    *,
    age: float = 5.0,
    supervising: bool = False,
    frame_age: float | None = 5.0,
    printer_state: str = "idle",
) -> None:
    (state / "vision").mkdir(parents=True, exist_ok=True)
    doc = {
        "schema": 1,
        "printer": name,
        "ts": NOW - age,
        "state": printer_state,
        "supervising": supervising,
        "last_frame_ts": None if frame_age is None else NOW - frame_age,
        "interval_s": 10.0,
        "reachable": True,
    }
    (state / "vision" / f"{name}.json").write_text(json.dumps(doc), encoding="utf-8")


def env_for(
    tmp_path: Path,
    systemd: Systemd,
    runner: Runner | None = None,
    state: str | None = "printing",
    plans: list[su.PrinterPlan] | None = None,
) -> sv.Env:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return sv.Env(
        show=systemd,
        run=runner or Runner(),
        state_dir=state_dir,
        unit_dir=tmp_path / "units",
        clock=lambda: NOW,
        state_probe=lambda _n: state,
        alive=lambda _pid: True,
        plans=lambda: plans if plans is not None else [plan()],
        ledger=DeliveryState(tmp_path / "ledger.json"),
        last_check=systemd.last_check,
        monotonic=lambda: MONO,
    )


def replace_env_show(env: sv.Env, show: Any) -> sv.Env:
    env.show = show
    return env


def assess_status(
    tmp_path: Path,
    systemd: Systemd,
    *,
    state: str | None = "printing",
    runner: Runner | None = None,
    camera: bool = True,
) -> sv.Assessment:
    env = env_for(tmp_path, systemd, runner, state, [plan(camera=camera)])
    layout = su.Layout(LAYOUT.repo, LAYOUT.python, env.units_dir(), None)
    return sv.gather(
        plan(camera=camera),
        env,
        live=True,
        layout=layout,
        linger=sv.linger_enabled(env.runner()),
    )


def beats(tmp_path: Path, **kw: Any) -> Path:
    state = tmp_path / "state"
    write_watch_beat(
        state,
        "p1",
        job=kw.pop("job", {"file": "a.gcode", "started": NOW - 60, "adopted": False}),
        **{
            k: v
            for k, v in kw.items()
            if k in ("age", "connected", "unreachable_since")
        },
    )
    write_vision_beat(
        state,
        "p1",
        supervising=True,
        printer_state="printing",
        **{k.removeprefix("v_"): v for k, v in kw.items() if k.startswith("v_")},
    )
    return state


class Tg:
    def __init__(self, code: int = 0) -> None:
        self.sent: list[list[str]] = []
        self.code = code

    def __call__(self, argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        self.sent.append(argv)
        return subprocess.CompletedProcess(argv, self.code, "", "")
