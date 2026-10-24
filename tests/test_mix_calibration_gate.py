"""The `mix_calibration` preflight line (#418): a plate with a mixed filament needs a current
`mix_color` record for its mix on the printer, else a WARN naming the tester command.

Real in-process 3d-api (FastAPI TestClient + FakeSpoolman) behind an ApiClient; the project 3MFs are
the synthetic Orca projects of tests/mix_fixtures.py. Plate: virtual filament 5 = pattern `431`
(heads T4, T3, T1), virtual 6 = Local-Z row (T1 75 % / T4 25 %).
"""

from __future__ import annotations

import datetime
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml
from mix_fixtures import orca_project, tile, with_model_colour
from printops_fakes import FakeSpoolman

from errors import UsageError
from printer_ops.printops_checks import (
    PrintopsContext,
    context_from_options,
    run_checks,
)
from printops import gates
from printops.client import ApiClient, ApiError
from printops.fleet import load_fleet
from printops.spoolman import ensure_extra_fields
from slicing.mix_uses import read_uses

REPO = Path(__file__).resolve().parents[1]
NOW = datetime.datetime(2026, 10, 24, 12, 0, tzinfo=datetime.timezone.utc)
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
# Spoolman filament per head: T1 bordeaux (id 3), T3 orange (id 12), T4 Snapdragon (id 7)
FILAMENTS = {1: ("Bordeaux", 3), 3: ("Orange", 12), 4: ("Snapdragon", 7)}
KEY_431 = "h0.12:f3.f7.f12"  # heads 4,3,1 -> filaments 7,12,3 -> smallest rotation
KEY_RATIO = "h0.12:lz:f3=75,f7=25"


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fleet = tmp_path / "fleet.yaml"
    fleet.write_text(FLEET, encoding="utf-8")
    monkeypatch.setenv("THREED_FLEET", str(fleet))
    for var in (
        "PRINTOPS_POLICY",
        "PRINTOPS_API",
        "PRINTOPS_TOKEN",
        "PRINTOPS_SNAPSHOT",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def shelf(fake_spoolman: FakeSpoolman) -> dict[int, int]:
    """Spoolman filaments of the plate's heads (head -> filament id), forced to the ids above."""
    ensure_extra_fields(fake_spoolman)
    vendor = fake_spoolman.create("vendor", {"name": "Elegoo"})["id"]
    made: dict[int, int] = {}
    for head, (name, want) in sorted(FILAMENTS.items(), key=lambda kv: kv[1][1]):
        while True:  # the fake numbers filaments 1, 2, 3…: burn ids until the wanted one comes up
            fid = fake_spoolman.create(
                "filament",
                {
                    "name": name,
                    "vendor_id": vendor,
                    "material": "PLA",
                    "density": 1.24,
                    "diameter": 1.75,
                    "weight": 1000,
                },
            )["id"]
            if fid == want:
                made[head] = fid
                break
            assert fid < want, f"filament ids overshot {want}"
    return made


def plate_gcode(tmp_path: Path) -> Path:
    path = tmp_path / "plate.gcode"
    path.write_text(
        "T0\nG1 X1 E1\nT3\nG1 X2 E1\n"
        "; printer_model = Snapmaker U1\n; filament_type = PLA;PLA;PLA;PLA\n"
        "; filament_colour = #C0392B;#808080;#E8732A;#F6C700\n",
        encoding="utf-8",
    )
    return path


def project(tmp_path: Path, *, extruder: int = 5, name: str = "plate.3mf") -> Path:
    return orca_project(tmp_path / name, [tile("tile", extruder, standing=True)])


def state(
    shelf: dict[int, int], *, drop: tuple[int, ...] = (), no_fid: tuple[int, ...] = ()
) -> dict[str, Any]:
    fleet = load_fleet()
    assert fleet is not None
    st = gates.fleet_state(fleet.printers["u1"], now="2026-10-24T12:00:00.000Z")
    st["bindings"], st["source"] = "ledger", "3d-api"
    for tool in st["tools"]:
        head = tool["tool"] + 1
        if head in shelf and head not in drop:
            name = FILAMENTS[head][0]
            tool["spool"] = {
                "id": 100 + head,
                "name": name,
                "material": "PLA",
                "filament_id": None if head in no_fid else shelf[head],
                "color_hex": "000000",
            }
    return st


def mix_line(
    tmp_path: Path,
    api: ApiClient | None,
    st: dict[str, Any],
    proj: Path | None,
    **ctx: Any,
) -> Any:
    result = run_checks(
        plate_gcode(tmp_path),
        PrintopsContext(printer="u1", state=st, client=api, **ctx),
        project=proj,
        now=NOW,
    )
    return next((c for c in result.checks if c.name == "mix_calibration"), None)


def record_mix(api: ApiClient, key: str, hex_: str, **extra: Any) -> dict[str, Any]:
    got: dict[str, Any] = api.post(
        "/v1/calibrations",
        {
            "kind": "mix_color",
            "printer": "u1",
            "mix": key,
            "method": "manual",
            "values": {"hex": hex_},
            **extra,
        },
    )
    return got


def set_recorded_at(conn: sqlite3.Connection, cal_id: int, when: str) -> None:
    conn.execute("UPDATE calibration SET recorded_at = ? WHERE id = ?", (when, cal_id))
    conn.commit()


# -- project side ----------------------------------------------------------------------------


def test_read_uses_names_heads_ratio_and_model_colour(tmp_path: Path) -> None:
    plain = read_uses(str(project(tmp_path)))
    assert [
        (u.virtual_id, u.heads, u.ratio, u.name, u.layer_height) for u in plain
    ] == [(5, (4, 3, 1), None, "431", 0.12)]
    assert plain[0].target_hex is None and plain[0].objects == ("tile",)
    coloured = read_uses(
        str(with_model_colour(project(tmp_path, name="c.3mf"), "#B5654F"))
    )
    assert coloured[0].target_hex == "#B5654F" and coloured[0].target_from == "tile"
    ratio = read_uses(str(project(tmp_path, extruder=6, name="r.3mf")))
    assert (
        ratio[0].ratio == (75, 25)
        and ratio[0].heads == (1, 4)
        and ratio[0].name == "local-z 1:75 4:25"
    )


def test_plate_without_mixed_parts_has_no_line(
    tmp_path: Path, api_client: ApiClient, shelf: dict[int, int]
) -> None:
    assert (
        mix_line(tmp_path, api_client, state(shelf), project(tmp_path, extruder=2))
        is None
    )


def test_no_project_has_no_line(
    tmp_path: Path, api_client: ApiClient, shelf: dict[int, int]
) -> None:
    assert mix_line(tmp_path, api_client, state(shelf), None) is None


# -- the lookup ------------------------------------------------------------------------------


def test_unmeasured_mix_warns_naming_spools_and_the_tester_command(
    tmp_path: Path, api_client: ApiClient, shelf: dict[int, int]
) -> None:
    line = mix_line(tmp_path, api_client, state(shelf), project(tmp_path))
    assert line.status == "WARN"
    assert (
        "mix 431 (T4 Snapdragon, T3 Orange, T1 Bordeaux) never measured" in line.detail
    )
    assert "3d mix tester --target '#RRGGBB' --printer u1" in line.detail


def test_tester_command_carries_the_model_colour(
    tmp_path: Path, api_client: ApiClient, shelf: dict[int, int]
) -> None:
    proj = with_model_colour(project(tmp_path), "#B5654F")
    line = mix_line(tmp_path, api_client, state(shelf), proj)
    assert "3d mix tester --target '#B5654F' --printer u1" in line.detail


def test_current_record_passes_whatever_the_rotation_of_the_key(
    tmp_path: Path, api_client: ApiClient, shelf: dict[int, int]
) -> None:
    record_mix(
        api_client, "h0.12:f12.f3.f7", "#B5654F"
    )  # the same cycle, another rotation
    line = mix_line(tmp_path, api_client, state(shelf), project(tmp_path))
    assert line.status == "PASS" and "measured #B5654F" in line.detail


def test_record_older_than_a_filament_remeasure_is_stale(
    tmp_path: Path,
    api_client: ApiClient,
    shelf: dict[int, int],
    printops_db: sqlite3.Connection,
) -> None:
    mix = record_mix(api_client, KEY_431, "#B5654F")
    set_recorded_at(printops_db, mix["id"], "2026-10-01T10:00:00.000Z")
    api_client.post(
        "/v1/calibrations",
        {
            "kind": "filament_color",
            "method": "manual",
            "filament_id": shelf[4],
            "values": {"hex": "#E8B400"},
        },
    )
    line = mix_line(tmp_path, api_client, state(shelf), project(tmp_path))
    assert line.status == "WARN"
    assert "measurement is stale" in line.detail and "filament_color" in line.detail
    assert f"filament {shelf[4]}" in line.detail and "3d mix tester" in line.detail


def test_filament_record_of_the_same_readout_does_not_outdate_the_mix(
    tmp_path: Path,
    api_client: ApiClient,
    shelf: dict[int, int],
    printops_db: sqlite3.Connection,
) -> None:
    sha = "ab" * 32
    mix = record_mix(api_client, KEY_431, "#B5654F", params={"readout_sha256": sha})
    set_recorded_at(printops_db, mix["id"], "2026-10-01T10:00:00.000Z")
    api_client.post(
        "/v1/calibrations",
        {
            "kind": "filament_color",
            "method": "manual",
            "filament_id": shelf[4],
            "values": {"hex": "#E8B400"},
            "params": {"readout_sha256": sha},
        },
    )
    assert (
        mix_line(tmp_path, api_client, state(shelf), project(tmp_path)).status == "PASS"
    )
    other = api_client.post(
        "/v1/calibrations",
        {
            "kind": "filament_color",
            "method": "manual",
            "filament_id": shelf[3],
            "values": {"hex": "#E86A2A"},
            "params": {"readout_sha256": "cd" * 32},
        },
    )
    assert other["id"]
    assert (
        mix_line(tmp_path, api_client, state(shelf), project(tmp_path)).status == "WARN"
    )


def test_filament_record_of_a_filament_outside_the_mix_does_not_matter(
    tmp_path: Path,
    api_client: ApiClient,
    shelf: dict[int, int],
    fake_spoolman: FakeSpoolman,
    printops_db: sqlite3.Connection,
) -> None:
    mix = record_mix(api_client, KEY_431, "#B5654F")
    set_recorded_at(printops_db, mix["id"], "2026-10-01T10:00:00.000Z")
    vendor = fake_spoolman.find("vendor")[0]["id"]
    other = fake_spoolman.create(
        "filament",
        {
            "name": "Grey",
            "vendor_id": vendor,
            "material": "PLA",
            "density": 1.24,
            "diameter": 1.75,
            "weight": 1000,
        },
    )["id"]
    assert other not in shelf.values()
    api_client.post(
        "/v1/calibrations",
        {
            "kind": "filament_color",
            "method": "manual",
            "filament_id": other,
            "values": {"hex": "#E8B400"},
        },
    )
    assert (
        mix_line(tmp_path, api_client, state(shelf), project(tmp_path)).status == "PASS"
    )


def test_expired_record_warns(
    tmp_path: Path, api_client: ApiClient, shelf: dict[int, int]
) -> None:
    record_mix(api_client, KEY_431, "#B5654F", valid_until="2026-10-01")
    line = mix_line(tmp_path, api_client, state(shelf), project(tmp_path))
    assert (
        line.status == "WARN"
        and "measurement is expired" in line.detail
        and "2026-10-01" in line.detail
    )


def test_record_of_another_printer_or_mix_does_not_count(
    tmp_path: Path, api_client: ApiClient, shelf: dict[int, int]
) -> None:
    api_client.post(
        "/v1/calibrations",
        {
            "kind": "mix_color",
            "printer": "u1",
            "mix": "h0.12:f1.f2",
            "method": "manual",
            "values": {"hex": "#808080"},
        },
    )
    assert (
        mix_line(tmp_path, api_client, state(shelf), project(tmp_path)).status == "WARN"
    )


def test_local_z_ratio_mix_is_keyed_by_percent(
    tmp_path: Path, api_client: ApiClient, shelf: dict[int, int]
) -> None:
    proj = project(tmp_path, extruder=6)
    line = mix_line(tmp_path, api_client, state(shelf), proj)
    assert (
        line.status == "WARN"
        and "mix local-z 1:75 4:25 (T1 Bordeaux, T4 Snapdragon) never measured"
        in line.detail
    )
    record_mix(api_client, KEY_RATIO, "#8A4B50")
    assert mix_line(tmp_path, api_client, state(shelf), proj).status == "PASS"


# -- missing data is UNVERIFIED, never PASS --------------------------------------------------


def test_unbound_head_or_missing_filament_id_is_unverified(
    tmp_path: Path, api_client: ApiClient, shelf: dict[int, int]
) -> None:
    unbound = mix_line(tmp_path, api_client, state(shelf, drop=(3,)), project(tmp_path))
    assert (
        unbound.status == "UNVERIFIED"
        and "T3" in unbound.detail
        and "no spool bound" in unbound.detail
    )
    no_id = mix_line(tmp_path, api_client, state(shelf, no_fid=(4,)), project(tmp_path))
    assert no_id.status == "UNVERIFIED" and "no Spoolman filament id" in no_id.detail


def test_without_3d_api_the_mix_is_unverified(
    tmp_path: Path, shelf: dict[int, int]
) -> None:
    line = mix_line(tmp_path, None, state(shelf), project(tmp_path))
    assert line.status == "UNVERIFIED" and "PRINTOPS_API" in line.detail


# -- target ΔE, policy, --accept -------------------------------------------------------------


def block_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **gates_patch: Any
) -> None:
    data = yaml.safe_load((REPO / "fleet" / "policy.yaml").read_text(encoding="utf-8"))
    data["gates"]["levels"]["mix_calibration"] = "block"
    data["gates"].update(gates_patch)
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("PRINTOPS_POLICY", str(path))


def test_measured_mix_near_the_model_colour_passes_far_one_is_hard(
    tmp_path: Path,
    api_client: ApiClient,
    shelf: dict[int, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proj = with_model_colour(project(tmp_path), "#B5654F")
    record_mix(api_client, KEY_431, "#B7664F")
    near = mix_line(tmp_path, api_client, state(shelf), proj)
    assert (
        near.status == "PASS" and "from the part's colour #B5654F (tile)" in near.detail
    )
    record_mix(
        api_client, KEY_431, "#7A8B6F"
    )  # a newer record replaces it: green, not terracotta
    far = mix_line(tmp_path, api_client, state(shelf), proj)
    assert (
        far.status == "WARN" and "above the limit 6" in far.detail
    )  # hard under `warn` is WARN
    block_policy(tmp_path, monkeypatch)
    assert mix_line(tmp_path, api_client, state(shelf), proj).status == "FAIL"
    block_policy(tmp_path, monkeypatch, mix_color={"max_delta_e": 60})
    assert mix_line(tmp_path, api_client, state(shelf), proj).status == "PASS"


def test_accept_waives_a_block_failure_and_is_logged(
    tmp_path: Path,
    api_client: ApiClient,
    shelf: dict[int, int],
    monkeypatch: pytest.MonkeyPatch,
    printops_db: sqlite3.Connection,
) -> None:
    proj = with_model_colour(project(tmp_path), "#B5654F")
    rec = record_mix(api_client, KEY_431, "#7A8B6F")
    block_policy(tmp_path, monkeypatch)
    waived = mix_line(
        tmp_path,
        api_client,
        state(shelf),
        proj,
        accept=frozenset({"mix_calibration"}),
        reason="test tile, colour irrelevant",
    )
    assert waived.status == "WARN" and any(
        "accepted with --accept mix_calibration: test tile, colour irrelevant" in x
        for x in waived.lines
    )
    rows = printops_db.execute(
        "SELECT subject, payload, actor FROM events WHERE kind = 'calibration.accepted'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["subject"] == f"u1/{KEY_431}" and rows[0]["actor"] == "test-rw"
    payload = json.loads(rows[0]["payload"])
    assert payload["reason"] == "test tile, colour irrelevant"
    assert payload["record_id"] == rec["id"] and payload["kind"] == "mix_color"
    assert payload["mix"] == KEY_431 and re.fullmatch(
        r"[0-9a-f]{64}", payload["gcode_sha"]
    )
    assert (
        payload["detail"].startswith("mix 431 (T4 Snapdragon")
        and "above the limit 6" in payload["detail"]
    )
    # without --accept the same plate still fails
    assert mix_line(tmp_path, api_client, state(shelf), proj).status == "FAIL"


def test_accept_mix_calibration_needs_a_reason_and_is_an_acceptable_check() -> None:
    kw: dict[str, Any] = {
        "api": None,
        "offline": True,
        "snapshot": None,
        "command": "print-preflight",
    }
    with pytest.raises(UsageError, match="--accept needs --reason"):
        context_from_options("u1", None, accept="mix_calibration", **kw)
    ctx = context_from_options("u1", None, accept="mix_calibration", reason="why", **kw)
    assert ctx.accept == frozenset({"mix_calibration"}) and ctx.reason == "why"


# -- the 3d-api endpoint ---------------------------------------------------------------------


def test_mix_check_endpoint_rejects_bad_requests(
    api_client: ApiClient, printops_client: Any, printops_tokens: dict[str, str]
) -> None:
    def post(body: dict[str, Any], token: str = "rw") -> Any:
        return printops_client.post(
            "/v1/calibrations/mix-check",
            json=body,
            headers={"Authorization": f"Bearer {printops_tokens[token]}"},
        )

    good = {"printer": "u1", "mixes": [{"key": KEY_431, "filament_ids": [3, 7, 12]}]}
    assert post(good).status_code == 200
    assert post({**good, "printer": "mk4"}).status_code == 404
    assert post({**good, "mixes": [{"key": "431"}]}).status_code == 422
    assert post({**good, "mixes": []}).status_code == 422
    assert (
        post({**good, "accept": [KEY_431]}).status_code == 422
    )  # a reason is mandatory
    assert (
        post({**good, "accept": ["h0.12:f1.f2"], "reason": "x"}).status_code == 422
    )  # not asked about
    assert (
        post({**good, "accept": [KEY_431], "reason": "x"}, token="read").status_code
        == 403
    )
    with pytest.raises(ApiError):
        api_client.post("/v1/calibrations/mix-check", {"mixes": good["mixes"]})
