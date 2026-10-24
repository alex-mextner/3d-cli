"""E2E: a plate with a colour mix is preflighted against a real 3d-api (#418).

`bin/3d print-preflight` → uvicorn 3d-api → FakeSpoolman over HTTP. The user loads three spools,
slices a standing tile in mix 431, and asks "is this ready to go on the U1?": no mix_color record →
WARN with the tester command; after a measurement → PASS; a measurement far from the model colour
→ FAIL under policy `block`, waived by `--accept … --reason` and logged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from mix_fixtures import orca_project, tile, with_model_colour
from printops_fixtures import LiveApi

from printops.client import ApiClient
from printops.spoolman import ensure_extra_fields

from .workflow_helper import run_cli

REPO = Path(__file__).resolve().parents[2]
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
    - {label: T1, index: 0, usable: true}
    - {label: T2, index: 1, usable: true}
    - {label: T3, index: 2, usable: true}
    - {label: T4, index: 3, usable: true}
  ams: []
  enclosure: partial
  chamber_heater: false
  plates: [textured_pei]
"""


@pytest.fixture
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "fleet.yaml"
    path.write_text(FLEET, encoding="utf-8")
    monkeypatch.setenv("THREED_FLEET", str(path))  # the in-process 3d-api reads it too
    return path


def _load(api: ApiClient, head: str, name: str) -> int:
    got = api.post(
        "/v1/spools",
        {
            "vendor": "Elegoo",
            "material": "PLA",
            "name": name,
            "color_hex": "#808080",
            "net_g": 1000,
        },
    )
    api.post(f"/v1/printers/u1/slots/{head}/load", {"spool_id": got["id"]})
    return int(got["filament_id"])


def _preflight(
    tmp_path: Path, env: dict[str, str], *extra: str
) -> tuple[int, dict[str, dict[str, str]], str]:
    r = run_cli(
        tmp_path,
        "print-preflight",
        "plate.gcode",
        "--axis-limits",
        "u1",
        "--source-3mf",
        "plate.3mf",
        "--json",
        *extra,
        env_extra=env,
    )
    checks = (
        {c["name"]: c for c in json.loads(r.stdout)["checks"]}
        if r.stdout.startswith("{")
        else {}
    )
    return r.returncode, checks, r.stdout + r.stderr


def test_mix_calibration_warns_passes_fails_and_is_accepted(
    tmp_path: Path, fleet: Path, live_api: LiveApi
) -> None:
    """The user loads three spools, slices a standing tile in mix 431 and preflights it: WARN with the
    tester command, PASS after a measurement, FAIL under `block` when the measure is far from the model
    colour, and a logged waiver with --accept mix_calibration --reason."""
    ensure_extra_fields(live_api.spoolman)
    rw = ApiClient(live_api.url, live_api.tokens["rw"])
    # mix 431 = heads T4, T3, T1; Spoolman filament ids follow loading order
    f1, f3, f4 = (
        _load(rw, "T1", "Bordeaux"),
        _load(rw, "T3", "Orange"),
        _load(rw, "T4", "Snapdragon"),
    )
    key = "h0.12:" + ".".join(
        f"f{i}" for i in min((f4, f3, f1), (f3, f1, f4), (f1, f4, f3))
    )
    (tmp_path / "plate.gcode").write_text(
        "T0\nG1 X1 E1\n; printer_model = Snapmaker U1\n; filament_type = PLA;PLA;PLA;PLA\n",
        encoding="utf-8",
    )
    orca_project(tmp_path / "plate.3mf", [tile("tile", 5, standing=True)])
    env = {
        "PRINTOPS_API": live_api.url,
        "PRINTOPS_TOKEN": live_api.tokens["rw"],
        "THREED_FLEET": str(fleet),
    }

    _code, checks, out = _preflight(tmp_path, env)
    line = checks["mix_calibration"]
    assert line["status"] == "WARN"
    assert (
        "mix 431 (T4 Snapdragon, T3 Orange, T1 Bordeaux) never measured"
        in line["detail"]
    )
    assert "3d mix tester --target '#RRGGBB' --printer u1" in line["detail"]

    rw.post(
        "/v1/calibrations",
        {
            "kind": "mix_color",
            "printer": "u1",
            "mix": key,
            "method": "manual",
            "values": {"hex": "#7A8B6F"},
        },
    )
    code, checks, out = _preflight(tmp_path, env)
    assert checks["mix_calibration"]["status"] == "PASS", out

    with_model_colour(tmp_path / "plate.3mf", "#B5654F")
    policy = yaml.safe_load(
        (REPO / "fleet" / "policy.yaml").read_text(encoding="utf-8")
    )
    policy["gates"]["levels"]["mix_calibration"] = "block"
    (tmp_path / "policy.yaml").write_text(
        yaml.safe_dump(policy, allow_unicode=True), encoding="utf-8"
    )
    block = {**env, "PRINTOPS_POLICY": str(tmp_path / "policy.yaml")}

    code, checks, out = _preflight(tmp_path, block)
    assert checks["mix_calibration"]["status"] == "FAIL", out
    assert (
        "from the part's colour #B5654F (tile), above the limit 6"
        in checks["mix_calibration"]["detail"]
    )

    code, checks, out = _preflight(tmp_path, block, "--accept", "mix_calibration")
    assert code == 2 and "--accept needs --reason" in out

    code, checks, out = _preflight(
        tmp_path,
        block,
        "--accept",
        "mix_calibration",
        "--reason",
        "test tile, colour irrelevant",
    )
    assert checks["mix_calibration"]["status"] == "WARN", out
    assert any(
        "accepted with --accept mix_calibration: test tile, colour irrelevant" in x
        for x in checks["mix_calibration"]["lines"]
    )
    events = rw.get("/v1/events", last=20)["events"]
    accepted = [e for e in events if e["kind"] == "calibration.accepted"]
    assert len(accepted) == 1 and accepted[0]["payload"]["mix"] == key
    assert accepted[0]["payload"]["reason"] == "test tile, colour irrelevant"


def test_print_preflight_help_documents_mix_calibration(tmp_path: Path) -> None:
    """A user reads `3d print-preflight --help` and finds the mix_calibration line and its --accept flags."""
    out = run_cli(tmp_path, "print-preflight", "--help").stdout
    assert (
        "mix_calibration" in out
        and "--accept mix_calibration" in out
        and "--reason TEXT" in out
    )
