"""`3d printer bed` / the `bed` start gate / the bed monitor in `3d printer watch` (issue #303),
end to end through bin/3d against a loopback fake Moonraker that serves real U1 camera frames
(tests/data/bed: parts standing on the bed after a job at Z 200, then the empty bed)."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from printer_fakes import FakeMoonraker, u1_status

from .workflow_helper import run_cli

pytest.importorskip("numpy")
pytest.importorskip("cv2")

DATA = Path(__file__).resolve().parents[1] / "data" / "bed"
GCODE = """G28
T0
G1 X10 E1
; filament used [mm] = 12.0, 0.00, 0.00, 0.00
; filament_colour = #000000;#808080;#1B5E20;#FFFFFF
; filament_type = PLA;PLA;PLA;PLA
"""


def jpeg(name: str) -> bytes:
    return (DATA / name).read_bytes()


def u1_after_job(state: str = "complete") -> FakeMoonraker:
    status = u1_status(state)
    status["toolhead"] = {"position": [5.0, 5.0, 200.0, 0.0]}  # PRINT_END parks the bed at Z 200
    return FakeMoonraker(status=status, camera=jpeg("u1_z200_clear_ref.jpg"))


def write_u1(tmp_path: Path, moon: FakeMoonraker) -> None:
    d = tmp_path / "xdg-config" / "3d-cli"
    d.mkdir(parents=True, exist_ok=True)
    (d / "printer-devices.yaml").write_text(f"""u1:
  adapter: moonraker
  host: 127.0.0.1
  port: {moon.port}
  model: Snapmaker U1
  trusted_macros: [T0, T1, T2, T3]
  watch:
    vision: {{keepalive: false}}
    bed: {{enabled: true, interval_s: 0.2}}
""", encoding="utf-8")


def test_reference_then_status_sees_parts_and_the_cleared_bed(tmp_path: Path) -> None:
    """The user captures the empty-bed reference once; later `bed status` tells parts from a clear bed."""
    moon = u1_after_job()
    with moon.serve():
        write_u1(tmp_path, moon)
        none = run_cli(tmp_path, "printer", "bed", "status", "u1")
        cap = run_cli(tmp_path, "printer", "bed", "reference", "u1", "--capture")
        listed = run_cli(tmp_path, "printer", "bed", "reference", "u1")
        moon.camera = jpeg("u1_occupied_1400.jpg")
        occupied = run_cli(tmp_path, "printer", "bed", "status", "u1", "--json")
        moon.camera = jpeg("u1_z200_clear_1426.jpg")
        clear = run_cli(tmp_path, "printer", "bed", "status", "u1")

    assert none.returncode == 0, none.stderr
    assert "bed unknown" in none.stdout and "3d printer bed reference u1 --capture" in none.stdout
    assert cap.returncode == 0, cap.stderr
    assert "plate reference of u1 at Z 200.0" in cap.stdout and "camera calibration" in cap.stdout
    assert "plate" in listed.stdout and "Z  200.0" in listed.stdout
    v = json.loads(occupied.stdout)
    assert v["bed"] == "occupied" and v["source"] == "camera now" and Path(v["frame"]).is_file()
    # the #262 refit puts the plate's front edge in view at Z 200, so the plate is judged there
    assert "u1: bed clear, plate present" in clear.stdout
    assert moon.writes() == []  # read-only


def test_start_is_refused_while_parts_are_on_the_bed(tmp_path: Path) -> None:
    """The user starts a print with the last job's parts still on the bed: start refuses; --force overrides."""
    moon = u1_after_job()
    (tmp_path / "part.gcode").write_text(GCODE, encoding="utf-8")
    with moon.serve():
        write_u1(tmp_path, moon)
        assert run_cli(tmp_path, "printer", "bed", "reference", "u1", "--capture").returncode == 0
        assert run_cli(tmp_path, "printer", "upload", "u1", "part.gcode").returncode == 0
        moon.camera = jpeg("u1_occupied_1351.jpg")
        refused = run_cli(tmp_path, "printer", "start", "u1", "part.gcode", "--yes")
        writes_refused = [r.path for r in moon.writes()]
        forced = run_cli(tmp_path, "printer", "start", "u1", "part.gcode", "--yes", "--force")

    assert refused.returncode == 1
    assert "FAIL  bed" in refused.stdout and "objects on the bed" in refused.stdout
    assert writes_refused == ["/server/files/upload"]
    assert forced.returncode == 0, forced.stdout + forced.stderr
    assert "FORCED" in forced.stdout and "bed" in forced.stdout.split("FORCED", 1)[1]


def _swap_after(moon: FakeMoonraker, delay: float, name: str) -> threading.Thread:
    def swap() -> None:
        time.sleep(delay)
        moon.camera = jpeg(name)

    t = threading.Thread(target=swap, daemon=True)
    t.start()
    return t


def test_monitor_reports_occupied_then_clear_when_the_parts_are_taken(tmp_path: Path) -> None:
    """`bed monitor` reports bed_occupied, then bed_clear once the user takes the parts off."""
    moon = u1_after_job()
    with moon.serve():
        write_u1(tmp_path, moon)
        assert run_cli(tmp_path, "printer", "bed", "reference", "u1", "--capture").returncode == 0
        moon.camera = jpeg("u1_occupied_1351.jpg")
        swap = _swap_after(moon, 3.0, "u1_z200_clear_1412.jpg")
        mon = run_cli(tmp_path, "printer", "bed", "monitor", "u1", "--duration", "7")
        swap.join()

    assert mon.returncode == 0, mon.stderr
    lines = [json.loads(x) for x in mon.stdout.splitlines() if x.strip()]
    events = [x["event"] for x in lines if "event" in x]
    assert [e["kind"] for e in events] == ["bed_occupied", "bed_clear"], mon.stderr
    assert "the bed is clear" in events[1]["text"] and Path(events[1]["data"]["frame"]).is_file()
    assert moon.writes() == []  # no clear_ack configured: nothing sent


def test_watch_runs_the_bed_monitor_and_emits_its_events(tmp_path: Path) -> None:
    """`3d printer watch` with watch.bed.enabled: bed events arrive on the watch's JSONL stdout."""
    moon = u1_after_job()
    with moon.serve():
        write_u1(tmp_path, moon)
        assert run_cli(tmp_path, "printer", "bed", "reference", "u1", "--capture").returncode == 0
        moon.camera = jpeg("u1_occupied_1400.jpg")
        swap = _swap_after(moon, 5.0, "u1_z200_clear_1405.jpg")
        watch = run_cli(tmp_path, "printer", "watch", "u1", "--duration", "11",
                        "--heartbeat", str(tmp_path / "hb.json"))
        swap.join()

    events = [json.loads(x) for x in watch.stdout.splitlines() if x.strip().startswith("{")]
    kinds = [e["kind"] for e in events if e["kind"].startswith("bed_")]
    assert kinds == ["bed_occupied", "bed_clear"], watch.stderr[-2000:]
    assert all(e["severity"] == "info" for e in events if e["kind"].startswith("bed_"))
    assert "bed monitor: u1" in watch.stderr
    hb = json.loads((tmp_path / "hb.json").read_text(encoding="utf-8"))
    assert hb["printers"]["u1"]["bed"]["bed"] == "clear"
