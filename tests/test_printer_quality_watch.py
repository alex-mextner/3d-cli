"""`3d printer watch NAME --job`: the print-quality tier on REAL U1 frames (tests/data/quality).

The frames are the camera of the Snapmaker U1 during the 10 h SIMBOL sheet job of 2026-11-05, downscaled to 640 px:
`u1_f1` the empty plate, `u1_f5` the clean first layer, `u1_f7` / `u1_f10` / `u1_f12` the stringing debris of layers
2-4 that NOTHING flagged on the day (the spaghetti model scored 0.0 on all twelve frames, the job watch only asks
whether the part is still there). The printer is a fixture scenario on a virtual clock; the real `tg` is refused
under pytest and the fixture forces its dry run, so nothing here touches a printer, a camera or a phone.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from printer_ops import jobwatch_run as R

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")
import bed_part  # noqa: E402  (needs numpy + OpenCV)
import print_quality  # noqa: E402

DATA = Path(__file__).parent / "data" / "quality"
DEVICES = """u1:
  adapter: moonraker
  host: 127.0.0.1
  port: 1
  model: Snapmaker U1
  watch:
    vision: {snapshot: "http://127.0.0.1:1/?action=snapshot"}
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
    monkeypatch.setattr(
        R,
        "run_part_tool",
        lambda argv: bed_part.run_check(bed_part.build_parser().parse_args(argv)),
    )
    monkeypatch.setattr(
        R,
        "run_quality_tool",
        lambda argv: print_quality.run_check(
            print_quality.build_parser().parse_args(argv)
        ),
    )
    return tmp_path


def at(t: float, frame: str, layer: int) -> dict[str, Any]:
    return {
        "t": t,
        "state": "printing",
        "layer": layer,
        "total_layer": 100,
        "progress": layer / 100,
        "z": 0.2 + 0.2 * layer,
        "frame": frame,
    }


def end(t: float) -> dict[str, Any]:
    return {
        "t": t,
        "state": "complete",
        "layer": 100,
        "progress": 1.0,
        "frame": "u1_f1.jpg",
    }


# the first layer is on the plate (the empty plate is the job's baseline frame), then debris grows on layers 2-4
HEALTHY = [
    at(0, "u1_f1.jpg", 1),
    *[at(15 * i, "u1_f5.jpg", 1) for i in range(1, 6)],
    at(120, "u1_f5.jpg", 2),
]
STRINGY = [
    at(180, "u1_f7.jpg", 3),
    at(240, "u1_f10.jpg", 3),
    at(300, "u1_f12.jpg", 4),
    at(360, "u1_f12.jpg", 4),
]


def watch(
    tmp_path: Path,
    steps: list[dict[str, Any]],
    *flags: str,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, list[dict[str, Any]]]:
    for name in {s["frame"] for s in steps}:
        (tmp_path / name).write_bytes((DATA / name).read_bytes())
    scenario = tmp_path / "scenario.json"
    scenario.write_text(
        json.dumps({"printer": "u1", "file": "sheet.gcode", "steps": steps}),
        encoding="utf-8",
    )
    code = R.run(
        [
            "u1",
            "--job",
            "--fixture",
            str(scenario),
            "--out",
            str(tmp_path / "runs"),
            "--duration",
            "1500",
            "--checkpoints",
            "none",
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


def quality_only(outcomes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The job watch's own part check (bed_part) also reads these frames: the toolhead and the light differ from
    frame to frame, so it may add a `part_moved` WARNING of its own. This file is about the quality tier."""
    return [o for o in outcomes if o["findings"][0]["kind"].startswith("quality_")]


def report_dir(tmp_path: Path) -> Path:
    (d,) = (tmp_path / "runs").glob("*-u1-monitor-report")
    return d


def test_stringing_debris_on_real_frames_is_warned_once_and_never_stops_the_job(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, outcomes = watch(
        tmp_path, [*HEALTHY, *STRINGY, end(500)], "--tg-dry-run", capsys=capsys
    )
    assert code == 0, "a quality finding never fails the watch"
    (alert,) = quality_only(outcomes)
    assert alert["findings"][0]["kind"] == "quality_stringing_warning"
    assert alert["action"] == "alert"
    assert all(
        s["name"] not in ("pause", "cancel", "call")
        for o in outcomes
        for s in o["steps"]
    )
    msgs = [m for m in would_run(out) if "ниточки" in m[-1]]
    assert (
        len(msgs) == 1
        and msgs[0][:3] == ["tg", "--tag", "problem"]
        and "--photo" in msgs[0]
    )
    assert all(m[:2] != ["tg", "call"] for m in would_run(out)), (
        "the owner is not rung for a bad surface"
    )
    text = msgs[0][-1]
    assert "белые ниточки" in text and "Что менять" in text and "температур" in text
    assert "Evidence:" in text  # the owner can find the frame


def test_the_clean_first_layer_raises_nothing_and_the_report_says_it_was_checked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, outcomes = watch(
        tmp_path,
        [*HEALTHY, at(300, "u1_f5.jpg", 4), end(400)],
        "--tg-dry-run",
        capsys=capsys,
    )
    assert code == 0 and quality_only(outcomes) == []
    doc = json.loads((report_dir(tmp_path) / "report.json").read_text())
    assert (
        doc["max_level"] == "ok"
        and doc["per_level"]["warning"] == 0
        and doc["per_level"]["severe"] == 0
    )
    assert doc["judged"] >= 1 and doc["baseline"]["n"] == 4
    assert doc["skipped"]["absent"] >= 1, (
        "the empty plate of the first frame is not judged"
    )
    assert "print-quality monitor:" in out and "monitor-report" in out


def test_the_monitoring_report_counts_severities_and_keeps_the_worst_frame(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    watch(tmp_path, [*HEALTHY, *STRINGY, end(500)], "--tg-dry-run", capsys=capsys)
    d = report_dir(tmp_path)
    doc = json.loads((d / "report.json").read_text())
    assert doc["max_level"] == "warning"
    assert (
        doc["per_level"]["severe"] >= 1
        and doc["per_level"]["warning"] >= 2
        and doc["per_issue"]["stringing"] >= 3
    )
    assert [a["kind"] for a in doc["alerts"]] == ["quality_stringing_warning"]
    assert (
        doc["worst"]["issue"] == "stringing"
        and doc["worst"]["level"] == "severe"
        and doc["worst"]["layer"] == 3
    )
    assert (d / "worst-frame.jpg").is_file() and (
        d / "worst-frame.jpg"
    ).stat().st_size > 1000
    md = (d / "report.md").read_text()
    assert (
        "| severe |" in md
        and "Alerts (one per issue and level" in md
        and "worst-frame.jpg" in md
    )


def test_a_quality_alert_ships_the_frame_with_the_debris_marked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, _, outcomes = watch(
        tmp_path, [*HEALTHY, *STRINGY, end(500)], "--tg-dry-run", capsys=capsys
    )
    d = Path(quality_only(outcomes)[0]["evidence_dir"])
    assert (d / "frame.quality.jpg").is_file() and (d / "frame.raw.jpg").is_file()
    marked = cv2.imread(str(d / "frame.quality.jpg"))
    assert (
        marked[..., 2].astype(int) - marked[..., 0].astype(int) > 150
    ).sum() > 200, "red fleck pixels drawn in"


def test_no_quality_turns_the_tier_and_its_report_off(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, outcomes = watch(
        tmp_path,
        [*HEALTHY, *STRINGY, end(500)],
        "--tg-dry-run",
        "--no-quality",
        capsys=capsys,
    )
    assert code == 0 and quality_only(outcomes) == []
    assert not list((tmp_path / "runs").glob("*monitor-report"))
    assert "print-quality monitor" not in out


def test_the_report_is_written_even_when_a_lost_part_ends_the_watch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """What the monitor saw until the part came off is part of the evidence."""
    steps = [
        *HEALTHY,
        at(180, "u1_f7.jpg", 3),
        at(240, "u1_f10.jpg", 3),
        at(300, "u1_f1.jpg", 20),
        at(360, "u1_f1.jpg", 21),
    ]
    code, _, _ = watch(tmp_path, steps, "--tg-dry-run", capsys=capsys)
    assert code == 3
    doc = json.loads((report_dir(tmp_path) / "report.json").read_text())
    assert doc["readings"] >= 5


def test_a_watch_quality_block_tunes_the_thresholds_and_a_bad_key_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], xdg: Path
) -> None:
    from errors import InvalidArgument

    devices = xdg / "config" / "3d-cli" / "printer-devices.yaml"
    unreachable = "{warning_ratio: 99, severe_ratio: 99, notice_ratio: 99, warning_drop: 9, severe_drop: 9, notice_drop: 9}"
    devices.write_text(DEVICES + f"    quality: {unreachable}\n", encoding="utf-8")
    code, _, outcomes = watch(
        tmp_path, [*HEALTHY, *STRINGY, end(500)], "--tg-dry-run", capsys=capsys
    )
    assert code == 0 and quality_only(outcomes) == [], (
        "thresholds nobody can reach: nothing alerts"
    )
    devices.write_text(DEVICES + "    quality: {no_such_key: 1}\n", encoding="utf-8")
    with pytest.raises(InvalidArgument, match="no_such_key"):
        R.run(
            [
                "u1",
                "--job",
                "--fixture",
                str(tmp_path / "scenario.json"),
                "--wait-start",
                "5",
            ]
        )
