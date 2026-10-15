"""PrintOps material gates end to end (#164): bin/3d print-preflight → real uvicorn 3d-api →
FakeSpoolman over HTTP; `3d printops gates snapshot` → `print-preflight --offline`. No printer is
contacted (--axis-limits / --offline)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from printops_fixtures import LiveApi

from printops.client import ApiClient
from printops.spoolman import encode_extra, ensure_extra_fields

from .workflow_helper import run_cli

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
