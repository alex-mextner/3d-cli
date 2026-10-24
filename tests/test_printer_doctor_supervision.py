"""Check `printer-supervision` of `3d printers doctor` (lib/printer_doctor/checks/supervision.py): it fails
while a printer that answers is not under always-on supervision and passes when units, heartbeats, linger and
the single watcher agree. systemd, loginctl and the printer are fakes; the doctor never reaches the real ones."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from printer_doctor.context import SupervisionSources
from printer_doctor.engine import Options, run_printer
from printer_doctor.model import CheckResult, ResultRecord
from printer_doctor.registry import discover_checks
from printer_doctor_fakes import agent_sources, rig, target
from printer_ops.base import PrinterStatus
from printer_supervise_fakes import (
    CAMERA,
    Runner,
    Systemd,
    beats,
    healthy,
    unit_show,
    write_vision_beat,
    write_watch_beat,
)

CHECK = {c.id: c for c in discover_checks()}["printer-supervision"]


class StubAdapter:
    def __init__(self, state: str | None) -> None:
        self.state = state

    def status(self) -> PrinterStatus:
        from printer_ops.base import PrinterCommError

        if self.state is None:
            raise PrinterCommError("printer is switched off", command="printer")
        return PrinterStatus(self.state, self.state)


class LiveSystemd(Runner):
    """The runner of the fix AND the systemd the check asks afterwards: `enable --now X` makes X healthy."""

    def __init__(self, units: dict[str, str]) -> None:
        super().__init__(linger=True)
        self.systemd = Systemd(units)

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if list(argv[:3]) == ["systemctl", "--user", "enable"]:
            unit = argv[-1]
            self.systemd.units[unit] = unit_show(
                args=f"printer {unit.split('-')[2].split('@')[0]} p1"
            )
        return super().__call__(argv)


def check(
    tmp_path: Path,
    systemd: Any,
    *,
    state: str | None = "printing",
    linger: bool | None = True,
    tgt: Any = None,
    options: Options | None = None,
    runner: Runner | None = None,
) -> ResultRecord:
    r = rig(
        tgt or target(watch=CAMERA),
        systemctl_show=systemd,
        agents=agent_sources(tmp_path / "state"),
        supervision=SupervisionSources(
            linger=lambda: linger,
            unit_dir=tmp_path / "units",
            runner=runner or Runner(),
            alive=lambda _p: True,
        ),
        adapter=lambda _d: StubAdapter(state),
    )
    return run_printer(r.ctx, [CHECK], options or Options()).records[0]


def test_the_check_passes_for_a_printing_printer_that_is_fully_supervised(
    tmp_path: Path,
) -> None:
    beats(tmp_path)
    rec = check(tmp_path, healthy())
    assert rec.result.status == "pass" and rec.result.summary == "supervised"


def test_the_check_fails_with_error_severity_when_a_printing_printer_has_no_watcher(
    tmp_path: Path,
) -> None:
    beats(tmp_path)
    systemd = healthy()
    systemd.units["3d-printer-watch@p1.service"] = unit_show("inactive", sub="dead")
    res = check(tmp_path, systemd).result
    assert (res.status, res.severity) == ("fail", "error")
    assert "3d-printer-watch@p1.service is inactive" in res.summary


def test_the_check_fails_on_a_stale_heartbeat_of_a_live_unit(tmp_path: Path) -> None:
    beats(tmp_path, age=300.0)
    res = check(tmp_path, healthy()).result
    assert (res.status, res.severity) == (
        "fail",
        "error",
    ) and "heartbeat is 5 min old" in res.summary


def test_an_unsupervised_printer_that_is_switched_off_is_a_warning_not_an_error(
    tmp_path: Path,
) -> None:
    res = check(tmp_path, Systemd(), state=None).result
    assert (res.status, res.severity) == (
        "fail",
        "warn",
    ) and "is not installed" in res.summary


def test_a_switched_off_printer_with_waiting_units_passes(tmp_path: Path) -> None:
    write_watch_beat(
        tmp_path / "state",
        "p1",
        job=None,
        connected=False,
        unreachable_since=1_799_990_000.0,
    )
    write_vision_beat(tmp_path / "state", "p1")
    rec = check(tmp_path, healthy(), state=None)
    assert rec.result.status == "pass" and "not answering" in rec.result.summary


def test_linger_off_fails_the_check(tmp_path: Path) -> None:
    beats(tmp_path)
    res = check(tmp_path, healthy(), linger=False).result
    assert (res.status, res.severity) == (
        "fail",
        "error",
    ) and "linger is off" in res.summary


def test_the_check_skips_without_systemctl_and_for_printers_nothing_supervises(
    tmp_path: Path,
) -> None:
    assert check(tmp_path, lambda _u: "").result.status == "skip"
    neighbour = target(watch=CAMERA)
    neighbour = replace(neighbour, fleet=replace(neighbour.fleet, owner="neighbour"))  # type: ignore[type-var]
    res = check(tmp_path, healthy(), tgt=neighbour).result
    assert (
        res.status == "skip" and "not an owned, fully controlled printer" in res.summary
    )
    assert (
        check(
            tmp_path, healthy(), tgt=target(access="read-only", watch=CAMERA)
        ).result.status
        == "skip"
    )
    assert (
        check(tmp_path, healthy(), tgt=target(access="offline")).result.status == "skip"
    )


def _fix(rec: ResultRecord) -> Any:
    assert rec.fix is not None
    return rec.fix


def test_the_fix_is_needs_yes_local_only_and_prints_the_install_command(
    tmp_path: Path,
) -> None:
    assert (
        CHECK.fix is not None
        and CHECK.fix.risk == "needs-yes"
        and CHECK.fix.touches_printer is False
    )
    rec = check(
        tmp_path, Systemd(), options=Options(fix=True)
    )  # no --yes: reported, not applied
    fix = _fix(rec)
    assert not fix.applied
    assert "$ 3d printer supervise install p1 --yes" in fix.manual_steps
    assert "$ 3d printer supervise status p1" in fix.manual_steps
    assert not (tmp_path / "units").exists()


def test_the_fix_with_yes_installs_the_units_and_the_check_passes_afterwards(
    tmp_path: Path,
) -> None:
    write_watch_beat(tmp_path / "state", "p1", job=None)
    write_vision_beat(tmp_path / "state", "p1")
    live = LiveSystemd({})
    rec = check(
        tmp_path,
        live.systemd,
        state="idle",
        runner=live,
        options=Options(fix=True, yes=True),
    )
    fix = _fix(rec)
    assert fix.applied and fix.verified, (fix, rec.result)
    assert (tmp_path / "units" / "3d-printer-watch@.service").exists()
    assert [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "3d-printer-vision@p1.service",
    ] in live.calls
    assert not any(c[0] == "tg" for c in live.calls)  # the fix never messages anyone


def test_the_check_never_contacts_the_printer_with_anything_but_a_status_read(
    tmp_path: Path,
) -> None:
    beats(tmp_path)
    seen: list[str] = []

    class Spy(StubAdapter):
        def status(self) -> PrinterStatus:
            seen.append("status")
            return super().status()

    r = rig(
        target(watch=CAMERA),
        systemctl_show=healthy(),
        agents=agent_sources(tmp_path / "state"),
        supervision=SupervisionSources(
            linger=lambda: True,
            unit_dir=tmp_path / "units",
            runner=Runner(),
            alive=lambda _p: True,
        ),
        adapter=lambda _d: Spy("printing"),
    )
    run_printer(r.ctx, [CHECK], Options())
    assert seen == ["status"]


def test_the_result_carries_the_assessment_evidence(tmp_path: Path) -> None:
    beats(tmp_path)
    res: CheckResult = check(tmp_path, healthy()).result
    a = res.evidence["assessment"]
    assert (
        isinstance(a, dict)
        and a["printer"] == "p1"
        and a["printer_state"] == "printing"
        and a["linger"] is True
    )
