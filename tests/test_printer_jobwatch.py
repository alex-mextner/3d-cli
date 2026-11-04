"""`3d printer watch NAME --job` (issue #723): a lost print is stopped, the evidence is saved and the owner
is rung; a print that is fine is left alone.

The frames of tests/data/jobwatch are the real K1 SE empty bed (daylight and the dark room) with a
pasted part, half a part and a spaghetti pile; the printer is a fixture scenario (virtual clock, a
printer that records the stop it is given) or the loopback FakeMoonraker. Nothing here touches a real
printer, camera or phone: the real `tg` is refused under pytest and the fixture forces its dry run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from errors import InvalidArgument, UsageError
from printer_fakes import FakeMoonraker, u1_status
from printer_ops import jobwatch as J
from printer_ops import jobwatch_actions as A
from printer_ops import jobwatch_run as R
from printer_ops.base import PAUSED, PRINTING, PrinterRefused
from printer_ops.moonraker import MoonrakerAdapter
from printer_ops.watch import CRITICAL, WARNING, Event

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")
import bed_part  # noqa: E402  (needs numpy + OpenCV)

DATA = Path(__file__).parent / "data" / "jobwatch"
DEVICES = """k1se:
  adapter: moonraker
  host: 127.0.0.1
  port: 1
  model: Creality K1 SE
  watch:
    vision: {snapshot: "http://127.0.0.1:1/?action=snapshot"}
    bed:
      enabled: true
      roi: [[0.31, 0.48], [0.72, 0.27], [0.92, 0.55], [0.92, 0.97], [0.41, 0.97]]
"""


@pytest.fixture(autouse=True)
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var, sub in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
    ):
        monkeypatch.setenv(var, str(tmp_path / sub))
    cfg = tmp_path / "config" / "3d-cli"
    cfg.mkdir(parents=True)
    (cfg / "printer-devices.yaml").write_text(DEVICES, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(R, "run_part_tool", in_process_tool)
    return tmp_path


def in_process_tool(argv: list[str]) -> dict[str, Any]:
    return bed_part.run_check(bed_part.build_parser().parse_args(argv))


def scenario(tmp_path: Path, steps: list[dict[str, Any]]) -> Path:
    for name in {s["frame"] for s in steps if "frame" in s}:
        (tmp_path / name).write_bytes((DATA / name).read_bytes())
    path = tmp_path / "scenario.json"
    path.write_text(
        json.dumps({"printer": "k1se", "file": "part.gcode", "steps": steps}),
        encoding="utf-8",
    )
    return path


def at(t: float, frame: str, layer: int, **extra: Any) -> dict[str, Any]:
    return {
        "t": t,
        "state": "printing",
        "layer": layer,
        "total_layer": 100,
        "progress": layer / 100,
        "z": 0.2 + 0.02 * layer,
        "frame": frame,
        **extra,
    }


def end(t: float) -> dict[str, Any]:
    return {
        "t": t,
        "state": "complete",
        "layer": 100,
        "progress": 1.0,
        "frame": "empty.jpg",
    }


def watch(
    tmp_path: Path,
    steps: list[dict[str, Any]],
    *flags: str,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, list[dict[str, Any]]]:
    out_dir = tmp_path / "runs"
    code = R.run(
        [
            "k1se",
            "--job",
            "--fixture",
            str(scenario(tmp_path, steps)),
            "--out",
            str(out_dir),
            "--duration",
            "1500",
            *flags,
        ]
    )
    out = capsys.readouterr().out
    outcomes = [
        json.loads(line)["jobwatch"]
        for line in out.splitlines()
        if line.startswith('{"jobwatch"')
    ]
    return code, out, outcomes


def would_run(out: str) -> list[list[str]]:
    return [
        json.loads(line)["would_run"]
        for line in out.splitlines()
        if line.startswith('{"would_run"')
    ]


# a part is on the plate from layer 3, the camera learns it, then it comes off at t=200
HEALTHY = [at(0, "empty.jpg", 1), at(20, "part.jpg", 3), at(300, "part.jpg", 30)]


def test_a_detached_part_cancels_the_job_keeps_the_evidence_and_rings_the_owner(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    steps = [*HEALTHY[:2], at(200, "empty.jpg", 20)]
    code, out, outcomes = watch(tmp_path, steps, "--tg-dry-run", capsys=capsys)
    assert code == 3
    (o,) = outcomes
    assert o["action"] == "cancel" and o["findings"][0]["kind"] == "part_gone"
    steps_ok = {s["name"]: s for s in o["steps"]}
    assert (
        steps_ok["cancel"]["ok"]
        and "the printer is now cancelled" in steps_ok["cancel"]["detail"]
    )
    # the phone: the call FIRST, then the text with the frame that showed it
    cmds = would_run(out)
    assert (
        cmds[0][:5] == ["tg", "call", "me", "--say", cmds[0][4]]
        and "ОТМЕНЕНА" in cmds[0][4]
    )
    assert cmds[1][:3] == ["tg", "--tag", "problem"] and "--photo" in cmds[1]
    # the evidence folder
    d = Path(o["evidence_dir"])
    assert d.parent == tmp_path / "runs" and d.name.endswith("-k1se-jobwatch")
    for name in (
        "finding.json",
        "frame.raw.jpg",
        "frame.annotated.jpg",
        "baseline.jpg",
        "outcome.json",
    ):
        assert (d / name).is_file(), name
    assert (
        json.loads((d / "finding.json").read_text())["findings"][0]["kind"]
        == "part_gone"
    )


def test_the_stop_is_sent_only_after_two_readings_not_on_the_first_empty_frame(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One empty frame (a toolhead over the part, a bad frame) is not a finding."""
    steps = [*HEALTHY[:2], at(200, "empty.jpg", 20), at(205, "part.jpg", 20), end(400)]
    code, out, outcomes = watch(tmp_path, steps, "--tg-dry-run", capsys=capsys)
    assert code == 0 and outcomes == [] and would_run(out) == []
    assert "the job ended" in out


@pytest.mark.parametrize("name", ["part.jpg", "part_dark_room.jpg"])
def test_a_lighting_change_while_the_part_stands_does_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    steps = [
        at(0, "empty.jpg", 1),
        at(20, "part.jpg", 3),
        at(200, name, 20),
        at(300, name, 30),
        end(400),
    ]
    code, out, outcomes = watch(tmp_path, steps, "--tg-dry-run", capsys=capsys)
    assert code == 0 and outcomes == [] and would_run(out) == []


def test_a_lighting_change_on_an_empty_plate_never_cancels_and_never_calls(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing was ever seen on the plate (black on black, or it never stuck): a WARNING with the frame, no stop."""
    steps = [
        at(0, "empty.jpg", 1),
        at(20, "empty_dark_room.jpg", 4),
        at(400, "empty_dark_room.jpg", 40),
        end(500),
    ]
    code, out, outcomes = watch(tmp_path, steps, "--tg-dry-run", capsys=capsys)
    assert code == 0
    assert [o["action"] for o in outcomes] == ["alert"] and outcomes[0]["findings"][0][
        "kind"
    ] == J.NO_PART
    cmds = would_run(out)
    assert (
        all(c[1] != "call" for c in cmds) and len(cmds) == 1
    )  # one message, nobody is rung
    assert all(s["name"] != "cancel" for s in outcomes[0]["steps"])


def test_half_a_part_is_a_warning_not_a_cancel(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    steps = [
        *HEALTHY[:2],
        at(200, "partial.jpg", 20),
        at(300, "partial.jpg", 25),
        end(500),
    ]
    code, _, outcomes = watch(tmp_path, steps, "--tg-dry-run", capsys=capsys)
    assert code == 0
    assert [(o["action"], o["findings"][0]["kind"]) for o in outcomes] == [
        ("alert", J.PART_MOVED)
    ]


def test_spaghetti_after_the_first_layers_pauses_under_auto(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    steps = [*HEALTHY[:2], at(200, "spaghetti.jpg", 20), at(300, "spaghetti.jpg", 25)]
    code, _, outcomes = watch(tmp_path, steps, "--tg-dry-run", capsys=capsys)
    assert code == 3
    assert (
        outcomes[0]["action"] == "pause"
        and outcomes[0]["findings"][0]["kind"] == J.SPAGHETTI
    )


def test_on_fail_pause_pauses_even_a_gone_part(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    steps = [*HEALTHY[:2], at(200, "empty.jpg", 20)]
    code, _, outcomes = watch(tmp_path, steps, "--on-fail", "pause", capsys=capsys)
    assert code == 3 and outcomes[0]["action"] == "pause"


def test_dry_run_sends_nothing_and_still_says_what_it_would_do(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    steps = [*HEALTHY[:2], at(200, "empty.jpg", 20)]
    code, out, outcomes = watch(tmp_path, steps, "--dry-run", capsys=capsys)
    assert code == 3
    (cancel,) = [s for s in outcomes[0]["steps"] if s["name"] == "cancel"]
    assert cancel["detail"].startswith("dry run: would send cancel")
    assert len(would_run(out)) == 2


def test_a_firmware_runout_pauses_without_a_camera_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ev = {"kind": "filament", "severity": "critical", "text": "filament runout on left"}
    steps = [at(0, "empty.jpg", 1), at(20, "empty.jpg", 2, events=[ev])]
    code, _, outcomes = watch(tmp_path, steps, "--tg-dry-run", capsys=capsys)
    assert code == 3 and outcomes[0]["action"] == "pause"
    assert outcomes[0]["findings"][0]["kind"] == "signal_filament"


def test_the_printers_own_noodle_verdict_cancels_in_the_first_layers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    steps = [at(0, "empty.jpg", 1), at(20, "empty.jpg", 2, noodles=[["noodle", 0.91]])]
    code, _, outcomes = watch(tmp_path, steps, "--tg-dry-run", capsys=capsys)
    assert code == 3 and outcomes[0]["action"] == "cancel"


def test_without_yes_a_real_watch_is_refused_but_a_fixture_is_not(
    tmp_path: Path,
) -> None:
    with pytest.raises(UsageError, match="needs --yes"):
        R.run(["k1se", "--job"])
    scenario_path = scenario(tmp_path, [end(0)])
    assert (
        R.run(["k1se", "--job", "--fixture", str(scenario_path), "--wait-start", "5"])
        == 1
    )  # not printing


# ---- the pure judge --------------------------------------------------------------------------


def reading(**n: Any) -> J.PartReading:
    return J.PartReading("ok", n)


@pytest.mark.parametrize(
    ("armed", "numbers", "label"),
    [
        (False, {"changed": 0.001, "blob_px": 10}, J.EMPTY),
        (False, {"changed": 0.02, "blob_px": 400}, J.SEEN),
        (True, {"overlap": 0.95, "outside": 0.0}, J.PRESENT),
        (True, {"overlap": 0.4, "outside": 0.0}, J.PARTIAL),
        (True, {"overlap": 0.05, "outside": 0.1}, J.GONE),
        (True, {"overlap": 0.9, "outside": 2.5}, J.GROWING),
    ],
)
def test_labels(armed: bool, numbers: dict[str, float], label: str) -> None:
    assert J.label_reading(reading(**numbers), armed, J.JobConfig()) == label


def test_blind_is_never_gone() -> None:
    cfg = J.JobConfig()
    assert (
        J.label_reading(J.PartReading("blind", {"reason": "out of view"}), True, cfg)
        == J.BLIND
    )
    assert J.label_reading(None, True, cfg) == J.BLIND


def test_decide_policy() -> None:
    cfg = J.JobConfig()
    gone = J.Finding(J.PART_GONE, True, "x")
    spag = J.Finding(J.SPAGHETTI, True, "x")
    runout = J.Finding("signal_filament", True, "x")
    warn = J.Finding(J.PART_MOVED, False, "x")
    assert J.decide([warn], 10, cfg) == "alert"
    assert J.decide([gone], 50, cfg) == "cancel"
    assert J.decide([spag], 2, cfg) == "cancel" and J.decide([spag], 50, cfg) == "pause"
    assert J.decide([runout], 1, cfg) == "pause"
    assert J.decide([runout], 1, J.JobConfig(on_fail="cancel")) == "cancel"
    assert J.decide([gone], 1, J.JobConfig(on_fail="pause")) == "pause"


def test_checkpoints_parse_and_fire_once() -> None:
    cps = J.parse_checkpoints(J.DEFAULT_CHECKPOINTS)
    assert [str(c) for c in cps] == [
        "L1@25%",
        "L1@50%",
        "L1@100%",
        "25%",
        "50%",
        "100%",
    ]
    with pytest.raises(InvalidArgument):
        J.parse_checkpoints("L1@150%")
    t = R.CheckpointTracker(cps, (1000, 2000))
    o = J.Observation(0.0, PRINTING, layer=1, progress=0.01, file_position=1300)
    assert t.crossed(o) == ["L1@25%"] and t.crossed(o) == []
    o2 = J.Observation(1.0, PRINTING, layer=2, progress=0.30, file_position=2100)
    assert t.crossed(o2) == ["L1@50%", "L1@100%", "25%"]
    without_gcode = R.CheckpointTracker(cps, None)
    assert without_gcode.crossed(
        J.Observation(0.0, PRINTING, layer=2, progress=0.0)
    ) == ["L1@100%"]


def test_a_warning_event_and_a_critical_event_are_told_apart() -> None:
    judge = J.JobJudge(J.JobConfig())
    ev = [
        Event("p", "bed_off", WARNING, "bed off"),
        Event("p", "exception", CRITICAL, "boom"),
    ]
    v = judge.signals_only(J.Observation(0.0, PRINTING, events=ev))
    assert [(f.kind, f.critical) for f in v.findings] == [
        ("signal_bed_off", False),
        ("signal_exception", True),
    ]


# ---- the stop, against the loopback fake Moonraker -------------------------------------------


def moon(state: str = "printing", name: str = "part.gcode") -> FakeMoonraker:
    m = FakeMoonraker(status=u1_status(state, name, 0.3))
    m.history = [
        {
            "job_id": "000042",
            "filename": name,
            "status": "in_progress",
            "start_time": 1.0,
        }
    ]
    return m


def responder(adapter: MoonrakerAdapter, tmp_path: Path, **kw: Any) -> A.Responder:
    return A.Responder(
        adapter,
        A.bind(adapter),
        out_dir=tmp_path / "runs",
        tg_dry_run=True,
        sleep=lambda s: None,
        verify_s=0.0,
        echo=lambda s: None,
        evidence_fn=lambda p, j, d: "fake",
        **kw,
    )


def test_cancel_goes_to_the_fake_moonraker_once_and_is_verified(tmp_path: Path) -> None:
    m = moon()
    with m.serve():
        adapter = MoonrakerAdapter("u1", "127.0.0.1", m.port)
        r = responder(adapter, tmp_path)
        assert r.bound == A.Bound("u1", "part.gcode", "000042")
        o = r.respond(
            "cancel",
            [J.Finding(J.PART_GONE, True, "gone")],
            layer=20,
            files={},
            extra={},
        )
        again = r.respond(
            "cancel",
            [J.Finding(J.PART_GONE, True, "gone")],
            layer=20,
            files={},
            extra={},
        )
    assert [w.path for w in m.writes()] == [
        "/printer/print/cancel"
    ]  # PAUSE/CANCEL only, and once
    assert {s.name: s.ok for s in o.steps}["cancel"] is True
    assert [s for s in again.steps if s.name == "cancel"][
        0
    ].detail == "already acted on this job"


def test_another_job_or_an_idle_printer_is_never_stopped(tmp_path: Path) -> None:
    m = moon()
    with m.serve():
        adapter = MoonrakerAdapter("u1", "127.0.0.1", m.port)
        r = responder(adapter, tmp_path)
        m.status["print_stats"]["filename"] = (
            "other.gcode"  # someone started another job meanwhile
        )
        o = r.respond(
            "cancel",
            [J.Finding(J.PART_GONE, True, "gone")],
            layer=2,
            files={},
            extra={},
        )
        m.status["print_stats"]["state"] = "standby"
        o2 = A.Responder(
            adapter,
            r.bound,
            out_dir=tmp_path / "r2",
            tg_dry_run=True,
            sleep=lambda s: None,
            verify_s=0.0,
            echo=lambda s: None,
            evidence_fn=lambda p, j, d: "fake",
        ).respond(
            "cancel",
            [J.Finding(J.PART_GONE, True, "gone")],
            layer=2,
            files={},
            extra={},
        )
    assert m.writes() == []
    assert (
        "not the watched part.gcode"
        in [s for s in o.steps if s.name == "cancel"][0].detail
    )
    assert "no job to cancel" in [s for s in o2.steps if s.name == "cancel"][0].detail


def test_pause_of_an_already_paused_job_sends_nothing(tmp_path: Path) -> None:
    m = moon(PAUSED)
    with m.serve():
        adapter = MoonrakerAdapter("u1", "127.0.0.1", m.port)
        r = responder(adapter, tmp_path)
        o = r.respond(
            "pause",
            [J.Finding("signal_filament", True, "runout")],
            layer=5,
            files={},
            extra={},
        )
    assert (
        m.writes() == []
        and "already paused" in [s for s in o.steps if s.name == "pause"][0].detail
    )


def test_there_is_no_job_to_bind_to_on_an_idle_printer() -> None:
    m = moon("standby")
    with m.serve(), pytest.raises(PrinterRefused, match="no job to watch"):
        A.bind(MoonrakerAdapter("u1", "127.0.0.1", m.port))


def test_the_real_tg_runs_only_through_the_injected_runner(tmp_path: Path) -> None:
    m = moon()
    sent: list[list[str]] = []

    class Done:
        returncode, stdout, stderr = 0, "", ""

    def runner(argv: list[str], **kw: Any) -> Any:
        sent.append(argv)
        return Done()

    with m.serve():
        adapter = MoonrakerAdapter("u1", "127.0.0.1", m.port)
        r = A.Responder(
            adapter,
            A.bind(adapter),
            out_dir=tmp_path / "runs",
            runner=runner,
            sleep=lambda s: None,
            verify_s=0.0,
            echo=lambda s: None,
            evidence_fn=lambda p, j, d: "fake",
        )
        r.respond(
            "cancel",
            [J.Finding(J.PART_GONE, True, "gone")],
            layer=2,
            files={},
            extra={},
        )
    assert [a[:3] for a in sent] == [["tg", "call", "me"], ["tg", "--tag", "problem"]]
    assert PRINTING  # the fake was printing when the job was bound
