from __future__ import annotations

import json
from pathlib import Path

from printer_fakes import FakeCreality, FakeMoonraker, k1_state, u1_status, write_devices

from .workflow_helper import run_cli

U1_GCODE = """G28
T0
G1 X10 E1
T3
G1 X20 E1
; filament used [mm] = 12.0, 0.00, 0.00, 30.5
; filament_colour = #000000;#808080;#1B5E20;#FFFFFF
; filament_type = PLA;PLA;PLA;PLA
"""


def test_printer_list_and_status_show_both_shop_printers(tmp_path: Path) -> None:
    """A user checks what the U1 and the K1 SE are doing before sending a job."""
    moon, k1 = FakeMoonraker(status=u1_status("printing", "base_1.gcode", 0.42)), FakeCreality()
    with moon.serve(), k1.serve():
        write_devices(tmp_path / "xdg-config", moon=moon, k1=k1)

        listed = run_cli(tmp_path, "printer", "list")
        u1 = run_cli(tmp_path, "printer", "status", "u1")
        k1se = run_cli(tmp_path, "printer", "status", "k1se", "--secs", "0.5")
        as_json = run_cli(tmp_path, "printer", "status", "u1", "--json")

    assert listed.returncode == 0, listed.stderr
    assert "u1" in listed.stdout and "moonraker" in listed.stdout and "creality-stock" in listed.stdout
    assert u1.returncode == 0, u1.stderr
    assert "state     printing" in u1.stdout and "base_1.gcode  42.0%" in u1.stdout
    assert "T2 empty" in u1.stdout and "T4 #FFFFFF PLA" in u1.stdout
    assert k1se.returncode == 0, k1se.stderr
    assert "state     idle" in k1se.stdout and "K1 SE" in k1se.stdout and "nozzle 210.1/210" in k1se.stdout
    assert json.loads(as_json.stdout)["slots"][0]["color"] == "#000000"
    assert moon.writes() == [] and k1.writes() == []


def test_printer_upload_stores_without_starting_then_start_needs_yes(tmp_path: Path) -> None:
    """Upload is storage only; the start is a separate, explicitly confirmed step."""
    moon = FakeMoonraker()
    (tmp_path / "part.gcode").write_text(U1_GCODE, encoding="utf-8")
    with moon.serve():
        write_devices(tmp_path / "xdg-config", moon=moon)
        up = run_cli(tmp_path, "printer", "upload", "u1", "part.gcode")
        no_yes = run_cli(tmp_path, "printer", "start", "u1", "part.gcode")
        writes_before_start = [r.path for r in moon.writes()]
        started = run_cli(tmp_path, "printer", "start", "u1", "part.gcode", "--yes", "--force")

    assert up.returncode == 0, up.stderr
    assert "NOT started" in up.stdout and "3d printer start u1 part.gcode --yes" in up.stdout
    assert no_yes.returncode == 2 and "needs --yes" in no_yes.stderr
    assert writes_before_start == ["/server/files/upload"]
    assert started.returncode == 0, started.stdout + started.stderr
    assert "PREFLIGHT part.gcode" in started.stdout and "slot T3" in started.stdout
    assert "read-back sha256 matches" in started.stdout
    assert "start acknowledged by u1: part-" in started.stdout
    assert [r.path for r in moon.writes()] == ["/server/files/upload", "/printer/print/start"]


def test_printer_start_refuses_a_same_size_stale_file_on_the_printer(tmp_path: Path) -> None:
    """The user re-slices part.gcode after uploading it; the new file happens to have the same
    length. Starting it must not print the stale copy that is still on the printer."""
    moon = FakeMoonraker()
    part = tmp_path / "part.gcode"
    part.write_text(U1_GCODE, encoding="utf-8")
    with moon.serve():
        write_devices(tmp_path / "xdg-config", moon=moon)
        up = run_cli(tmp_path, "printer", "upload", "u1", "part.gcode")
        resliced = U1_GCODE.replace("G1 X10 E1", "G1 X19 E1")
        assert len(resliced) == len(U1_GCODE)
        part.write_text(resliced, encoding="utf-8")
        started = run_cli(tmp_path, "printer", "start", "u1", "part.gcode", "--yes", "--force")

    assert up.returncode == 0, up.stderr
    assert started.returncode == 1
    assert "absent" in started.stderr and "Upload it first" in started.stderr
    assert [r.path for r in moon.writes()] == ["/server/files/upload"]


def test_printer_start_refuses_a_busy_k1_and_sends_nothing(tmp_path: Path) -> None:
    """Starting a second job on a printing K1 SE is refused before any frame is sent."""
    k1 = FakeCreality(state=k1_state(1, 1, "pfl.gcode"))
    (tmp_path / "roof.gcode").write_text("G28\n", encoding="utf-8")
    k1.files["roof-0000000000000000.gcode"] = 4  # busy check comes before any file lookup
    with k1.serve():
        write_devices(tmp_path / "xdg-config", k1=k1)
        result = run_cli(tmp_path, "printer", "start", "k1se", "roof.gcode", "--yes", "--secs", "0.5")

    assert result.returncode == 1
    assert "k1se is printing" in result.stderr and "pfl.gcode" in result.stderr
    assert k1.writes() == []


def test_print_preflight_blocks_a_wrong_colour_in_a_u1_head(tmp_path: Path) -> None:
    """The G-code wants white in head T4 but yellow is loaded: preflight fails and names both."""
    moon = FakeMoonraker()
    moon.status["print_task_config"]["filament_color_rgba"][3] = "F4EE2AFF"
    (tmp_path / "part.gcode").write_text(U1_GCODE, encoding="utf-8")
    with moon.serve():
        write_devices(tmp_path / "xdg-config", moon=moon)
        result = run_cli(tmp_path, "print-preflight", "part.gcode", "--printer", "u1", "--json")

    assert result.returncode == 1, result.stderr
    checks = {c["name"]: c for c in json.loads(result.stdout)["checks"]}
    assert checks["slot T0"]["status"] == "PASS"
    assert checks["slot T3"]["status"] == "FAIL" and "loaded #F4EE2A" in checks["slot T3"]["detail"]
    assert moon.writes() == []


def test_printer_without_config_explains_how_to_add_one(tmp_path: Path) -> None:
    """A first-time user gets the file path and a ready-to-paste example."""
    result = run_cli(tmp_path, "printer", "status", "u1")
    assert result.returncode == 2
    assert "no printers configured" in result.stderr and "printer-devices.yaml" in result.stderr
    assert "adapter: moonraker" in result.stderr


K1_EDGE_PLATE = """;TYPE:Custom
G28
G90
;LAYER_CHANGE
;TYPE:Outer wall
EXCLUDE_OBJECT_START NAME=base
G1 X62.83 Y2.774
G17
G3 Z1.36 I-.359 J-1.79 P1  F30000
G1 X46.468 Y6.057
EXCLUDE_OBJECT_END NAME=base
; printable_area = 0x0,220x0,220x220,0x220
"""


def _k1_config(tmp_path: Path) -> None:
    """A k1se entry with its axis limits; the address is unroutable on purpose: these
    commands must never connect."""
    cfg = tmp_path / "xdg-config" / "3d-cli"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "printer-devices.yaml").write_text(
        "k1se:\n  adapter: creality-stock\n  host: 192.0.2.1\n"
        "  axis_limits: {x: [-5, 229], y: [-0.5, 227]}\n", encoding="utf-8")


def test_gcode_bounds_catches_the_k1_zhop_arc_offline(tmp_path: Path) -> None:
    """A plate whose z-hop arc reaches Y -0.84 is caught before it reaches the K1 SE
    (limit -0.5), with no printer contacted; the same plate passes the area-only check
    when it is moved 5 mm away from the edge."""
    (tmp_path / "edge.gcode").write_text(K1_EDGE_PLATE, encoding="utf-8")
    (tmp_path / "moved.gcode").write_text(K1_EDGE_PLATE.replace("Y2.774", "Y7.774"), encoding="utf-8")
    _k1_config(tmp_path)

    by_name = run_cli(tmp_path, "gcode-bounds", "edge.gcode", "--printer", "k1se")
    by_spec = run_cli(tmp_path, "gcode-bounds", "edge.gcode", "--axis-limits", "-5:229,-0.5:227", "--json")
    moved = run_cli(tmp_path, "gcode-bounds", "moved.gcode", "--printer", "k1se")
    unknown = run_cli(tmp_path, "gcode-bounds", "moved.gcode")

    assert by_name.returncode == 1, by_name.stderr
    assert "FAIL  axis travel" in by_name.stdout and "line 9 (62.47, -0.84)" in by_name.stdout
    checks = {c["name"]: c["status"] for c in json.loads(by_spec.stdout)["checks"]}
    assert by_spec.returncode == 1 and checks == {"axis travel": "FAIL", "print area": "FAIL"}
    assert moved.returncode == 0, moved.stdout + moved.stderr
    assert "STATUS: PASS" in moved.stdout
    assert unknown.returncode == 0 and "SKIP  axis travel" in unknown.stdout


def test_print_preflight_includes_the_bounds_checks(tmp_path: Path) -> None:
    """The same arc fails the full preflight when the limits are given by printer name."""
    (tmp_path / "edge.gcode").write_text(K1_EDGE_PLATE, encoding="utf-8")
    _k1_config(tmp_path)
    result = run_cli(tmp_path, "print-preflight", "edge.gcode", "--axis-limits", "k1se")
    assert result.returncode == 1
    assert "axis travel" in result.stdout and "-0.84" in result.stdout and "STATUS: FAIL" in result.stdout
