"""PrintOps material gates end to end (#164): bin/3d print-preflight → real uvicorn 3d-api →
FakeSpoolman over HTTP; `3d printops gates snapshot` → `print-preflight --offline`. No printer is
contacted (--axis-limits / --offline)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from printer_fakes import FakeMoonraker, write_devices
from printops_fixtures import LiveApi

from printops.client import ApiClient
from printops.spoolman import encode_extra, ensure_extra_fields

from .workflow_helper import run_cli, run_cli_with_hatch

PRINTOPS_GATES = (
    "printer_profile_match",
    "nozzle_compat",
    "filament_temp_range",
    "loaded_filament_match",
    "slot_binding_known",
    "filament_sufficient",
    "drying_due",
    "maintenance_due",
    "enclosure_material",
    "plate_material",
    "preset_match",
)
FLEET = """\
u1:
  model: Snapmaker U1
  owner: own
  access: full
  adapter: moonraker
  host: 127.0.0.1
  port: 9
  axis_limits: {x: [0, 271], y: [0, 335]}
  heads:
    - {label: T1, index: 0, nozzle: {diameter_mm: 0.4, material: stainless, type: std}, usable: true}
    - {label: T2, index: 1, nozzle: {diameter_mm: 0.4, material: stainless, type: std}, usable: false, reason: T2 is broken}
    - {label: T3, index: 2, nozzle: {diameter_mm: 0.4, material: stainless, type: std}, usable: true}
    - {label: T4, index: 3, nozzle: {diameter_mm: 0.4, material: stainless, type: std}, usable: true}
  ams: []
  enclosure: partial
  chamber_heater: false
  plates: [textured_pei]
"""


def _plate(path: Path, types: str = "PLA;PLA;PLA;PLA") -> Path:
    footer = {
        "filament used [mm]": "6600.00, 0.00, 16500.00, 0.00",
        "filament used [g]": "20.00, 0.00, 50.00, 0.00",
        "curr_bed_type": "Textured PEI Plate",
        "filament_colour": "#000000;#808080;#1B5E20;#FFFFFF",
        "filament_type": types,
        "filament_vendor": "Elegoo;Elegoo;Elegoo;Elegoo",
        "nozzle_diameter": "0.4",
        "nozzle_temperature": "215",
        "nozzle_temperature_initial_layer": "220",
        "printer_model": "Snapmaker U1",
        "textured_plate_temp": "65",
        "textured_plate_temp_initial_layer": "65",
    }
    body = "M140 S65\nT0\nM104 S215 T0\nG1 X10 Y10 E1\nT2\nM109 S215\nG1 X20 Y20 E1\n"
    path.write_text(
        body + "".join(f"; {k} = {v}\n" for k, v in footer.items()), encoding="utf-8"
    )
    return path


@pytest.fixture
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "fleet.yaml"
    path.write_text(FLEET, encoding="utf-8")
    monkeypatch.setenv("THREED_FLEET", str(path))  # the in-process 3d-api reads it too
    return path


def _load(live: LiveApi, head: str, colour: str) -> int:
    ensure_extra_fields(live.spoolman)
    api = ApiClient(live.url, live.tokens["rw"])
    got = api.post(
        "/v1/spools",
        {
            "vendor": "Elegoo",
            "material": "PLA",
            "name": f"PLA {colour}",
            "color_hex": colour,
            "net_g": 1000,
        },
    )
    live.spoolman.update(
        "filament",
        got["filament_id"],
        {
            "extra": {
                "nozzle_temp_min": encode_extra(190),
                "nozzle_temp_max": encode_extra(230),
                "bed_temp_min": encode_extra(45),
                "bed_temp_max": encode_extra(70),
                "abrasive": encode_extra(False),
            }
        },
    )
    api.post(f"/v1/printers/u1/slots/{head}/load", {"spool_id": got["id"]})
    return int(got["id"])


def _checks(stdout: str) -> dict[str, dict[str, object]]:
    return {c["name"]: c for c in json.loads(stdout)["checks"]}


def test_preflight_reads_3d_api_then_the_offline_snapshot(
    tmp_path: Path, fleet: Path, live_api: LiveApi
) -> None:
    """The user has loaded black PLA into T1 and green into T3. Preflight against the live 3d-api
    verifies both heads; a PETG plate for the same heads FAILs on the material; after
    `3d printops gates snapshot` the slicer-hook mode (--offline, no network) gives the same
    verdicts from the file."""
    black, green = _load(live_api, "T1", "#000000"), _load(live_api, "T3", "#1B5E20")
    good, petg = (
        _plate(tmp_path / "plate.gcode"),
        _plate(tmp_path / "petg.gcode", "PETG;PLA;PLA;PLA"),
    )
    env = {
        "PRINTOPS_API": live_api.url,
        "PRINTOPS_TOKEN": live_api.tokens["read"],
        "THREED_FLEET": str(fleet),
    }
    snap = tmp_path / "snap.json"

    live = run_cli(
        tmp_path,
        "print-preflight",
        good.name,
        "--axis-limits",
        "u1",
        "--json",
        env_extra=env,
    )
    bad = run_cli(
        tmp_path, "print-preflight", petg.name, "--axis-limits", "u1", env_extra=env
    )
    saved = run_cli(
        tmp_path, "printops", "gates", "snapshot", "--out", str(snap), env_extra=env
    )
    state = run_cli(tmp_path, "printops", "gates", "state", "u1", env_extra=env)
    offline_env = {
        "THREED_FLEET": str(fleet),
        "PRINTOPS_API": "http://127.0.0.1:9",
    }  # would fail if contacted
    offline = run_cli(
        tmp_path,
        "print-preflight",
        good.name,
        "--offline",
        "--printops-snapshot",
        str(snap),
        "--json",
        env_extra=offline_env,
    )
    no_state = run_cli(
        tmp_path,
        "print-preflight",
        good.name,
        "--axis-limits",
        "u1",
        "--json",
        env_extra={"THREED_FLEET": str(fleet)},
    )

    got = _checks(
        live.stdout
    )  # overall FAILs on thumbnails/slicer warnings: not the subject here
    assert got["printops"][
        "status"
    ] == "PASS" and f"live from 3d-api {live_api.url}" in str(got["printops"]["detail"])
    for name in (
        "printer_profile_match",
        "nozzle_compat",
        "filament_temp_range",
        "loaded_filament_match",
        "slot_binding_known",
        "filament_sufficient",
        "drying_due",
        "plate_material",
    ):
        assert got[name]["status"] == "PASS", (name, got[name])
    assert f"#{black} Elegoo PLA #000000" in json.dumps(got["loaded_filament_match"])
    assert bad.returncode == 1
    assert (
        f"FAIL  loaded_filament_match T1: G-code PETG, loaded #{black} Elegoo PLA #000000 is PLA"
        in bad.stdout
    )

    assert saved.returncode == 0, saved.stderr
    assert f"saved {snap}: 1 printer(s), 2 bound spool(s)" in saved.stdout
    assert state.returncode == 0, state.stderr
    assert (
        f"tool 2  T3         #{green} Elegoo PLA #1B5E20  1000.0 ± 20.0 g"
        in state.stdout
    )

    off = _checks(offline.stdout)
    assert off["printops"]["status"] == "PASS" and f"snapshot {snap}" in str(
        off["printops"]["detail"]
    )
    gate_names = [n for n in got if n in PRINTOPS_GATES]
    assert len(gate_names) == len(PRINTOPS_GATES)
    assert {n: off[n]["status"] for n in gate_names} == {
        n: got[n]["status"] for n in gate_names
    }

    plain = _checks(no_state.stdout)
    assert plain["printops"]["status"] == "UNVERIFIED"
    assert plain["filament_temp_range"]["status"] == "UNVERIFIED"
    assert (
        "unverified)"
        in run_cli(
            tmp_path,
            "print-preflight",
            good.name,
            "--axis-limits",
            "u1",
            env_extra={"THREED_FLEET": str(fleet)},
        ).stdout
    )


def test_printer_start_refuses_a_plate_sliced_for_another_printer(
    tmp_path: Path, fleet: Path
) -> None:
    """The user starts a K1 SE plate on the U1 by mistake: `3d printer start` runs the PrintOps
    gates like print-preflight does and refuses before any start request. With 3d-api configured
    but down, the right plate still starts (forced past the plain G-code gates): PrintOps state
    degrades to fleet facts, it never blocks on its own."""
    moon = FakeMoonraker()
    good = _plate(tmp_path / "plate.gcode")
    wrong = tmp_path / "k1.gcode"
    wrong.write_text(
        good.read_text(encoding="utf-8").replace(
            "; printer_model = Snapmaker U1", "; printer_model = Creality K1 SE"
        ),
        encoding="utf-8",
    )
    env = {"THREED_FLEET": str(fleet)}
    down = {**env, "PRINTOPS_API": "http://127.0.0.1:9", "PRINTOPS_TOKEN": "x"}
    with moon.serve():
        write_devices(tmp_path / "xdg-config", moon=moon)
        ups = [
            run_cli(tmp_path, "printer", "upload", "u1", p.name, env_extra=env)
            for p in (good, wrong)
        ]
        refused = run_cli(
            tmp_path, "printer", "start", "u1", wrong.name, "--yes", env_extra=env
        )
        sent_after_refusal = [r.path for r in moon.writes()]
        started = run_cli_with_hatch(
            tmp_path,
            "printer",
            "start",
            "u1",
            good.name,
            "--yes",
            "--force",
            env_extra=down,
        )

    assert all(u.returncode == 0 for u in ups), [u.stderr for u in ups]
    assert refused.returncode == 1 and "preflight FAIL; start refused" in refused.stderr
    assert (
        "FAIL  printer_profile_match sliced for 'Creality K1 SE', but u1 is a 'Snapmaker U1'"
        in refused.stdout
    )
    assert "/printer/print/start" not in sent_after_refusal
    assert started.returncode == 0, started.stderr
    assert (
        "3d-api http://127.0.0.1:9:" in started.stdout
        and "fleet facts only" in started.stdout
    )
    forced = next(
        line for line in started.stdout.splitlines() if line.startswith("FORCED:")
    )
    assert not any(g in forced for g in PRINTOPS_GATES), forced
    assert [r.path for r in moon.writes()].count("/printer/print/start") == 1


def test_offline_preflight_with_an_explicit_api_is_a_usage_error(
    tmp_path: Path, fleet: Path
) -> None:
    """The slicer hook mode promises no network: asking for --offline and a 3d-api URL at once
    is refused with exit 2 before anything is contacted; $PRINTOPS_API alone does not trip it."""
    plate = _plate(tmp_path / "plate.gcode")
    env = {"THREED_FLEET": str(fleet), "PRINTOPS_API": "http://127.0.0.1:9"}
    both = run_cli(
        tmp_path,
        "print-preflight",
        plate.name,
        "--offline",
        "--printops-api",
        "http://127.0.0.1:9",
        env_extra=env,
    )
    env_only = run_cli(
        tmp_path, "print-preflight", plate.name, "--offline", env_extra=env
    )

    assert both.returncode == 2
    assert "--offline reads the snapshot and never calls 3d-api" in both.stderr
    assert both.stdout == ""
    assert env_only.returncode in (0, 1), env_only.stderr
    assert "no PrintOps snapshot" in env_only.stdout


def test_preflight_catches_a_plate_sliced_with_stock_presets(tmp_path: Path) -> None:
    """The user slices a K1 SE plate with Orca's stock presets instead of the ones the printer
    prints with (#293): `3d print-preflight --offline` names the presets and FAILs; the same
    plate sliced with the fleet entry's user presets passes preset_match."""
    fleet = tmp_path / "k1.yaml"
    fleet.write_text(
        "k1se:\n  model: Creality K1 SE\n  owner: own\n  access: offline\n"
        "  heads: [{label: T1, index: 0, usable: true}]\n"
        "  slicer: {machine: K1 SE - Copy, process: Std 8900}\n",
        encoding="utf-8",
    )
    user = tmp_path / "user" / "process"
    user.mkdir(parents=True)
    (user / "Std 8900.json").write_text(
        json.dumps({"name": "Std 8900", "default_acceleration": "8900"})
    )
    env = {
        "THREED_FLEET": str(fleet),
        "ORCA_USER_PRESETS": str(tmp_path / "user"),
        "PRINTOPS_SNAPSHOT": str(tmp_path / "none.json"),
    }

    def plate(name: str, machine: str, process: str, accel: str) -> Path:
        path = tmp_path / name
        path.write_text(
            "M140 S60\nM104 S215\nG1 X10 Y10 E1\n"
            "; printer_model = Creality K1 SE\n"
            f"; printer_settings_id = {machine}\n; print_settings_id = {process}\n"
            f"; default_acceleration = {accel}\n; filament_type = PLA\n; nozzle_diameter = 0.4\n",
            encoding="utf-8",
        )
        return path

    stock = plate(
        "stock.gcode",
        "Creality K1 SE 0.4 nozzle",
        "0.20mm Standard @Creality K1 SE",
        "12000",
    )
    mine = plate("mine.gcode", "K1 SE - Copy", "Std 8900", "8900")
    bad = run_cli(
        tmp_path,
        "print-preflight",
        str(stock),
        "--printer",
        "k1se",
        "--offline",
        "--json",
        env_extra=env,
    )
    good = run_cli(
        tmp_path,
        "print-preflight",
        str(mine),
        "--printer",
        "k1se",
        "--offline",
        "--json",
        env_extra=env,
    )

    assert bad.returncode == 1, bad.stderr
    got = _checks(bad.stdout)["preset_match"]
    assert got["status"] == "FAIL"
    assert got["detail"] == (
        "sliced with machine preset 'Creality K1 SE 0.4 nozzle', the printer prints with 'K1 SE - Copy' (+1 more)"
    ), got
    assert _checks(good.stdout)["preset_match"]["status"] == "PASS", good.stdout

    # printing the stock-preset plate on purpose: --accept preset_match --reason turns the FAIL into a WARN
    # that carries the reason, and the run exits 0
    accepted = run_cli(
        tmp_path,
        "print-preflight",
        str(stock),
        "--printer",
        "k1se",
        "--offline",
        "--json",
        "--accept",
        "preset_match",
        "--reason",
        "comparing stock flow on purpose",
        env_extra=env,
    )
    # the run still exits 1 for the plate's missing thumbnails / slice-warnings record: preset_match is not why
    assert accepted.returncode == 1, accepted.stderr
    failed = {
        c["name"]
        for c in json.loads(accepted.stdout)["checks"]
        if c["status"] == "FAIL"
    }
    assert "preset_match" not in failed and failed
    note = _checks(accepted.stdout)["preset_match"]
    assert note["status"] == "WARN"
    lines = json.loads(accepted.stdout)["checks"]
    text = "\n".join(next(c["lines"] for c in lines if c["name"] == "preset_match"))
    assert (
        "accepted with --accept preset_match: comparing stock flow on purpose" in text
    )
    for argv, message in (
        (["--accept", "preset_match"], "--accept needs --reason"),
        (["--accept", "plate_material", "--reason", "x"], "accepted: mix_calibration, preset_match"),
        (["--accept", "", "--reason", "x"], "--accept needs a check name"),
        (["--reason", "x"], "--reason belongs to --accept"),
    ):
        refused = run_cli(
            tmp_path,
            "print-preflight",
            str(stock),
            "--printer",
            "k1se",
            "--offline",
            *argv,
            env_extra=env,
        )
        assert refused.returncode == 2 and message in refused.stderr, (
            argv,
            refused.stderr,
        )
