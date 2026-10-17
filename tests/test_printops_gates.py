"""PrintOps material gates of `3d print-preflight` (#164): the `gates:` policy, the G-code facts,
ΔE2000, one G-code per FAIL condition, UNVERIFIED without PrintOps data, and the 3d-api state
(`GET /v1/gates/state`, `/v1/gates/snapshot`) the gates read."""

from __future__ import annotations

import datetime
import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
import yaml
from e2e.workflow_helper import run_cli
from printops_fakes import FakeSpoolman

from errors import UsageError
from printer_ops import preflight
from printer_ops.printops_checks import (
    PrintopsContext,
    ciede2000,
    context_from_options,
    delta_e,
    read_job,
    run_checks,
)
from printops import gates, policy
from printops.client import ApiClient, ApiError
from printops.fleet import load_fleet
from printops.gates_policy import parse as parse_gates
from printops.policy import PolicyError
from printops.spoolman import encode_extra, ensure_extra_fields

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
REPO_POLICY = Path(__file__).resolve().parents[1] / "fleet" / "policy.yaml"

FLEET = """\
u1:
  model: Snapmaker U1
  owner: own
  access: full
  adapter: moonraker
  host: 127.0.0.1
  port: 9
  heads:
    - {label: T1, index: 0, nozzle: {diameter_mm: 0.4, material: stainless, type: std}, usable: true}
    - {label: T2, index: 1, nozzle: {diameter_mm: 0.4, material: stainless, type: std}, usable: false, reason: T2 is broken}
    - {label: T3, index: 2, nozzle: {diameter_mm: 0.4, material: stainless, type: std}, usable: true}
    - {label: T4, index: 3, nozzle: {diameter_mm: 0.4, material: hardened, type: std}, usable: true}
  ams: []
  enclosure: none
  chamber_heater: false
  plates: [textured_pei]
p1s:
  model: Bambu Lab P1S
  owner: neighbour
  access: offline
  heads: [{label: T1, index: 0, usable: true}]
  ams: [{id: ams1, kind: ams, slots: 4}]
"""


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fleet = tmp_path / "fleet.yaml"
    fleet.write_text(FLEET, encoding="utf-8")
    monkeypatch.setenv("THREED_FLEET", str(fleet))
    for var in (
        "PRINTOPS_POLICY",
        "PRINTOPS_API",
        "PRINTOPS_TOKEN",
        "PRINTOPS_SNAPSHOT",
        "PRINTOPS_HA_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    return fleet


def gcode(
    tmp_path: Path,
    name: str = "plate.gcode",
    *,
    model: str = "Snapmaker U1",
    types: str = "PLA;PLA;PLA;PLA",
    colours: str = "#000000;#808080;#1B5E20;#FFFFFF",
    vendors: str = "Elegoo;Elegoo;Elegoo;Elegoo",
    used_g: tuple[float, ...] = (20.0, 0.0, 50.0, 0.0),
    nozzle_diameter: str = "0.4",
    nozzle_temp: str = "215",
    first_layer: str = "220",
    bed_type: str = "Textured PEI Plate",
    plate_temp: str = "65",
    hrc: str = "0,0,0,0",
    body: str = "T0\nM104 S215 T0\nG1 X1 E1\nT2\nM109 S215\nG1 X2 E1\nM104 S175 T2 ; cooldown\n",
    extra: dict[str, str] | None = None,
) -> Path:
    """An Orca-shaped plate: start heat-up before any tool change, body, config footer."""
    mm = ", ".join(f"{g * 330:.2f}" for g in used_g)
    footer = {
        "filament used [mm]": mm,
        "filament used [g]": ", ".join(f"{g:.2f}" for g in used_g),
        "curr_bed_type": bed_type,
        "filament_colour": colours,
        "filament_type": types,
        "filament_vendor": vendors,
        "nozzle_diameter": nozzle_diameter,
        "nozzle_temperature": nozzle_temp,
        "nozzle_temperature_initial_layer": first_layer,
        "printer_model": model,
        "printer_settings_id": f"{model} (0.4 nozzle)",
        "required_nozzle_HRC": hrc,
        "textured_plate_temp": plate_temp,
        "textured_plate_temp_initial_layer": plate_temp,
        **(extra or {}),
    }
    text = (
        "M140 S"
        + plate_temp
        + "\nM109 S140\n"
        + body
        + "".join(f"; {k} = {v}\n" for k, v in footer.items())
    )
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def spool(
    sid: int = 1,
    *,
    material: str = "PLA",
    vendor: str = "Elegoo",
    color: str = "000000",
    nozzle: tuple[int, int] | None = (190, 230),
    bed: tuple[int, int] | None = (45, 70),
    abrasive: bool | None = False,
    remaining: tuple[float, float, bool] = (600.0, 10.0, True),
    drying: str = "ok",
) -> dict[str, Any]:
    mid, sigma, anchored = remaining
    return {
        "id": sid,
        "label": f"#{sid} {vendor} {material}",
        "vendor": vendor,
        "material": material,
        "name": f"{material} test",
        "color_hex": color,
        "measured_hex": None,
        "lot_nr": "L1",
        "filament_id": sid,
        "nozzle_temp_min": nozzle[0] if nozzle else None,
        "nozzle_temp_max": nozzle[1] if nozzle else None,
        "bed_temp_min": bed[0] if bed else None,
        "bed_temp_max": bed[1] if bed else None,
        "abrasive": abrasive,
        "hygroscopic_class": None,
        "remaining": {
            "mid_g": mid,
            "sigma_g": sigma,
            "lo_g": max(0.0, mid - sigma),
            "hi_g": mid + sigma,
            "anchored": anchored,
        },
        "drying": {"state": drying, "class": "low", "reasons": []},
        "conflict": [],
        "spoolman_remaining_g": mid,
    }


def state(printer: str = "u1", **by_tool: dict[str, Any] | None) -> dict[str, Any]:
    """A 3d-api state for `printer` with spools on tools t0..t3 (t0=spool(...))."""
    fleet = load_fleet()
    assert fleet is not None
    st = gates.fleet_state(fleet.printers[printer], now=gates.drying.format_ts(NOW))
    st["bindings"], st["source"] = "ledger", "3d-api"
    for tool in st["tools"]:
        tool["spool"] = by_tool.get(f"t{tool['tool']}")
    return st


def good_state() -> dict[str, Any]:
    return state(t0=spool(1, color="000000"), t2=spool(3, color="1B5E20"))


def run(path: Path, st: dict[str, Any] | None) -> dict[str, preflight.Check]:
    result = run_checks(path, PrintopsContext(printer="u1", state=st), now=NOW)
    return {c.name: c for c in result.checks}


def statuses(checks: dict[str, preflight.Check]) -> dict[str, str]:
    return {name: c.status for name, c in checks.items()}


def override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **levels: str) -> None:
    data = yaml.safe_load(REPO_POLICY.read_text(encoding="utf-8"))
    data["gates"]["levels"].update(levels)
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("PRINTOPS_POLICY", str(path))


# -- policy ---------------------------------------------------------------------------------
def test_committed_gates_policy_is_valid_and_covers_every_check() -> None:
    assert policy.check() == []
    g = policy.section("gates")
    assert (
        g.level("filament_temp_range", "u1") == "block"
        and g.level("drying_due", "u1") == "warn"
    )
    assert g.material("pla+") == "PLA" and g.material("pla  matte") == "PLA-MATTE"
    assert g.is_abrasive_material("PA12-CF") and not g.is_abrasive_material("PCTG")
    assert g.colour_rule("/home/x/babushkin-dom/print/u1.gcode").fail_delta_e == 15
    assert g.colour_rule("/tmp/other.gcode").fail_delta_e == 25


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        (
            {"levels": {"nozzle_compat": "maybe"}},
            "levels.nozzle_compat must be one of block, warn, off",
        ),
        ({"levels": {"nozle_compat": "warn"}}, "unknown check(s) nozle_compat"),
        (
            {"colour": {"warn_delta_e": 30, "fail_delta_e": 20}},
            "warn_delta_e 30 must be ≤ fail_delta_e 20",
        ),
        (
            {"nozzle_compat": {"abrasive_ok": ["steel"], "abrasive_markers": ["CF"]}},
            "unknown value(s) steel",
        ),
        (
            {
                "plate_material": [
                    {"materials": ["PETG"], "plates": ["smooth_pei"], "why": ""}
                ]
            },
            "must say why",
        ),
        (
            {"printers": {"u1": {"drying_due": "sometimes"}}},
            "printers.u1.drying_due must be one of",
        ),
        ({"snapshot_max_age_h": 0}, "snapshot_max_age_h must be > 0"),
    ],
)
def test_gates_policy_rejects_broken_rules(patch: dict[str, Any], message: str) -> None:
    body = yaml.safe_load(REPO_POLICY.read_text(encoding="utf-8"))["gates"]
    for key, value in patch.items():
        if key == "levels":
            body["levels"].update(value)
        elif key == "colour":
            body["colour"].update(value)
        else:
            body[key] = value
    with pytest.raises(PolicyError, match=__import__("re").escape(message)):
        parse_gates(body)


def test_per_printer_override_and_off_hide_a_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = yaml.safe_load(REPO_POLICY.read_text(encoding="utf-8"))
    data["gates"]["printers"] = {"u1": {"plate_material": "off", "drying_due": "block"}}
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("PRINTOPS_POLICY", str(path))
    checks = run(
        gcode(tmp_path, bed_type="Cool Plate"),
        state(t0=spool(1), t2=spool(3, color="1B5E20", drying="wet")),
    )
    assert "plate_material" not in checks
    assert checks["drying_due"].status == "FAIL"


# -- G-code facts, colour ---------------------------------------------------------------------
def test_read_job_per_tool_values_and_body_temperatures(tmp_path: Path) -> None:
    job = read_job(
        gcode(
            tmp_path,
            nozzle_temp="215,0,230,0",
            body="T0\nM104 S215 T0\nT2\nM109 S240\nM104 T3 S250\nM104 S0 T2\n",
        )
    )
    assert job.used_tools == [0, 2]
    assert (
        job.text("filament_type", 2) == "PLA"
        and job.text("filament_colour", 2) == "#1B5E20"
    )
    assert (
        job.num("nozzle_temperature", 2) == 230 and job.num("nozzle_diameter", 3) == 0.4
    )  # one value: every tool
    # `M109 S140` before the first `Tn` (start-macro nozzle wipe) is attributed to no tool
    assert job.body_nozzle_max == {0: 215, 2: 240, 3: 250}
    assert job.body_bed_max == 65 and job.grams(2) == 50


@pytest.mark.parametrize("used", ["0.00, 0.00, 0.00, 0.00", "nan, N/A, inf, -inf"])
def test_usage_footer_naming_no_tool_falls_back_to_tool_changes(
    tmp_path: Path, used: str
) -> None:
    path = gcode(tmp_path)
    text = path.read_text(encoding="utf-8")
    for key in ("filament used [mm]", "filament used [g]"):
        text = re.sub(
            rf"^; {re.escape(key)} = .*$", f"; {key} = {used}", text, flags=re.M
        )
    path.write_text(text, encoding="utf-8")
    assert read_job(path).used_tools == [0, 2]
    checks = run(path, good_state())
    assert "nozzle_compat" in checks and "filament_temp_range" in checks


def test_non_finite_footer_numbers_are_unknown_not_pass(tmp_path: Path) -> None:
    path = gcode(tmp_path, nozzle_temp="nan", first_layer="inf")
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "; filament used [g] = 20.00, 0.00, 50.00, 0.00",
            "; filament used [g] = NaN, 0.00, NaN, 0.00",
        ),
        encoding="utf-8",
    )
    job = read_job(path)
    assert job.num("nozzle_temperature", 0) is None and job.grams(0) is None
    checks = run(path, good_state())
    assert checks["filament_sufficient"].status != "PASS"


def test_offline_with_an_explicit_api_is_a_usage_error() -> None:
    with pytest.raises(UsageError, match="--offline reads the snapshot"):
        context_from_options(
            "u1",
            None,
            api="http://h:8765",
            offline=True,
            snapshot=None,
            command="print-preflight",
        )


def test_ciede2000_matches_sharma_reference_pairs() -> None:
    # Sharma, Wu, Dalal (2005) test data, pairs 1, 7 and 17
    assert ciede2000((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485)) == pytest.approx(
        2.0425, abs=1e-4
    )
    assert ciede2000((50.0, 0.0, 0.0), (50.0, -1.0, 2.0)) == pytest.approx(
        2.3669, abs=1e-4
    )
    assert ciede2000((50.0, 2.5, 0.0), (73.0, 25.0, -18.0)) == pytest.approx(
        27.1492, abs=1e-4
    )
    assert delta_e("#1B5E20", "#1B5E20") == 0
    assert delta_e("#000000", "#FFFFFF") > 90


# -- PASS and UNVERIFIED ----------------------------------------------------------------------
def test_matching_plate_passes_every_gate(tmp_path: Path) -> None:
    checks = run(gcode(tmp_path), good_state())
    assert (
        statuses(checks)
        == {
            "printops": "PASS",
            "printer_profile_match": "PASS",
            "nozzle_compat": "PASS",
            "filament_temp_range": "PASS",
            "loaded_filament_match": "PASS",
            "slot_binding_known": "PASS",
            "filament_sufficient": "PASS",
            "drying_due": "PASS",
            "maintenance_due": "UNVERIFIED",  # no maintenance records until #166 reports them
            "enclosure_material": "PASS",
            "plate_material": "PASS",
            "preset_match": "UNVERIFIED",  # the u1 fleet entry names no `slicer:` presets
        }
    )
    assert "ΔE 0.0" in checks["loaded_filament_match"].detail


def test_without_printops_data_spool_gates_are_unverified_never_pass(
    tmp_path: Path,
) -> None:
    path = gcode(tmp_path)
    result = run_checks(
        path, PrintopsContext(printer="u1"), now=NOW
    )  # no API, no snapshot
    got = {c.name: c for c in result.checks}
    assert (
        got["printops"].status == "UNVERIFIED"
        and "--printops-api" in got["printops"].detail
    )
    for name in (
        "filament_temp_range",
        "loaded_filament_match",
        "slot_binding_known",
        "filament_sufficient",
        "drying_due",
        "nozzle_compat",
        "maintenance_due",
    ):
        assert got[name].status == "UNVERIFIED", (name, got[name].detail)
    assert got["filament_sufficient"].lines == [
        "UNVERIFIED T1: needs 27 g; spool not known (no PrintOps state)",
        "UNVERIFIED T3: needs 57 g; spool not known (no PrintOps state)",
    ]
    # fleet facts alone still verify what they can
    assert (
        got["printer_profile_match"].status == "PASS"
        and got["plate_material"].status == "PASS"
    )


def test_unbound_slot_warns_and_spool_gates_stay_unverified(tmp_path: Path) -> None:
    checks = run(gcode(tmp_path), state(t0=spool(1)))  # T3 has nothing bound
    assert checks["slot_binding_known"].status == "WARN"
    assert (
        "T3: no spool bound in the ledger: usage will not be booked"
        in checks["slot_binding_known"].detail
    )
    assert checks["filament_temp_range"].status == "UNVERIFIED"
    assert (
        "UNVERIFIED T3: no spool bound to this slot"
        in checks["filament_temp_range"].lines
    )


def test_filament_without_temperature_window_is_unverified(tmp_path: Path) -> None:
    checks = run(
        gcode(tmp_path), state(t0=spool(1, nozzle=None), t2=spool(3, color="1B5E20"))
    )
    assert checks["filament_temp_range"].status == "UNVERIFIED"
    assert (
        "nozzle window (filament nozzle_temp_min/max not set)"
        in checks["filament_temp_range"].detail
    )


def test_no_target_printer_is_one_unverified_line(tmp_path: Path) -> None:
    result = run_checks(gcode(tmp_path, model="Prusa MK4"), PrintopsContext(), now=NOW)
    assert [(c.name, c.status) for c in result.checks] == [("printops", "UNVERIFIED")]
    assert "no fleet printer is a 'Prusa MK4'" in result.checks[0].detail


def test_target_printer_is_inferred_from_the_gcode_model(tmp_path: Path) -> None:
    result = run_checks(gcode(tmp_path), PrintopsContext(state=good_state()), now=NOW)
    assert (
        result.printer == "u1"
        and "the G-code's printer_model 'Snapmaker U1'" in result.checks[0].detail
    )


# -- numeric hardness and independent abrasiveness ------------------------------------------
def _nozzle_hrc_case(
    tmp_path: Path,
    *,
    required_hrc: str,
    nozzle_material: object,
    loaded: dict[str, Any] | None,
    gcode_type: str = "PLA",
) -> preflight.Check:
    """Exercise the public gate with only T4 used; other tools' HRC must be ignored."""
    st = state(t3=loaded)
    head = next(h for h in st["heads"] if h["label"] == "T4")
    head["nozzle"]["material"] = nozzle_material
    path = gcode(
        tmp_path,
        used_g=(0.0, 0.0, 0.0, 20.0),
        types=f"PLA;PLA;PLA;{gcode_type}",
        hrc=f"86,86,86,{required_hrc}",
        body="T3\nM104 S215 T3\nG1 X1 E1\n",
    )
    return run(path, st)["nozzle_compat"]


@pytest.mark.parametrize(
    ("nozzle_material", "required_hrc", "expected"),
    [
        ("stainless", "3", "PASS"),
        ("stainless", "20", "PASS"),
        ("stainless", "21", "FAIL"),
        ("stainless", "40", "FAIL"),
        ("brass", "2", "PASS"),
        ("brass", "3", "FAIL"),
        ("hardened", "55", "PASS"),
        ("hardened", "56", "FAIL"),
        ("tungsten", "85", "PASS"),
        ("tungsten", "86", "FAIL"),
    ],
)
def test_nozzle_hrc_compares_requirement_with_installed_hardness(
    tmp_path: Path, nozzle_material: str, required_hrc: str, expected: str
) -> None:
    # Ordinary Generic PLA declares HRC 3; a nonzero minimum is not an abrasive flag.
    check = _nozzle_hrc_case(
        tmp_path,
        required_hrc=required_hrc,
        nozzle_material=nozzle_material,
        loaded=spool(4, color="FFFFFF", abrasive=False),
    )
    assert check.status == expected, check
    assert f"HRC {required_hrc}" in check.detail + "\n".join(check.lines)


@pytest.mark.parametrize("nozzle_material", [None, "ruby"])
def test_nozzle_hrc_unknown_installed_hardness_is_unverified(
    tmp_path: Path, nozzle_material: str | None
) -> None:
    check = _nozzle_hrc_case(
        tmp_path,
        required_hrc="3",
        nozzle_material=nozzle_material,
        loaded=spool(4, color="FFFFFF", abrasive=False),
    )
    assert check.status == "UNVERIFIED", check
    assert "hardness" in check.detail + "\n".join(check.lines)


@pytest.mark.parametrize("malformed_material", [["stainless"], {"kind": "stainless"}])
@pytest.mark.parametrize("abrasive", [False, True])
def test_nozzle_hrc_malformed_offline_material_does_not_crash(
    tmp_path: Path, malformed_material: object, abrasive: bool
) -> None:
    check = _nozzle_hrc_case(
        tmp_path,
        required_hrc="3",
        nozzle_material=malformed_material,
        loaded=spool(4, color="FFFFFF", abrasive=abrasive),
    )
    assert check.status == ("FAIL" if abrasive else "UNVERIFIED"), check
    evidence = check.detail + "\n".join(check.lines)
    assert "hardness is not known" in evidence
    if abrasive:
        assert "abrasive filament" in evidence


@pytest.mark.parametrize("required_hrc", ["0", "3"])
@pytest.mark.parametrize("abrasive_source", ["flag", "marker"])
def test_nozzle_unknown_hardness_does_not_hide_abrasive_policy_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    required_hrc: str,
    abrasive_source: str,
) -> None:
    data = yaml.safe_load(REPO_POLICY.read_text(encoding="utf-8"))
    data["gates"]["nozzle_compat"]["abrasive_ok"] = ["hardened", "tungsten"]
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("PRINTOPS_POLICY", str(path))
    loaded = spool(4, color="FFFFFF", abrasive=abrasive_source == "flag")
    if abrasive_source == "marker":
        loaded["material"] = "PLA-CF"
    check = _nozzle_hrc_case(
        tmp_path,
        required_hrc=required_hrc,
        nozzle_material="ruby",
        loaded=loaded,
    )
    assert check.status == "FAIL", check
    evidence = check.detail + "\n".join(check.lines)
    assert "abrasive filament" in evidence
    if required_hrc != "0":
        assert "hardness is not known" in evidence


@pytest.mark.parametrize("required_hrc", ["0", "3", "20"])
def test_nozzle_explicit_abrasive_spool_still_fails_on_stainless(
    tmp_path: Path, required_hrc: str
) -> None:
    check = _nozzle_hrc_case(
        tmp_path,
        required_hrc=required_hrc,
        nozzle_material="stainless",
        loaded=spool(4, color="FFFFFF", abrasive=True),
    )
    assert check.status == "FAIL", check
    assert "abrasive" in check.detail + "\n".join(check.lines)


@pytest.mark.parametrize("marker_source", ["spool_material", "spool_name", "gcode_type"])
@pytest.mark.parametrize("required_hrc", ["0", "3"])
def test_nozzle_abrasive_marker_is_not_hidden_by_false_spool_flag(
    tmp_path: Path, marker_source: str, required_hrc: str
) -> None:
    loaded = spool(4, color="FFFFFF", abrasive=False)
    gcode_type = "PLA"
    if marker_source == "spool_material":
        loaded["material"] = "PLA-CF"
    elif marker_source == "spool_name":
        loaded["name"] = "PLA-GF test"
    else:
        gcode_type = "PLA-CF"
    check = _nozzle_hrc_case(
        tmp_path,
        required_hrc=required_hrc,
        nozzle_material="stainless",
        loaded=loaded,
        gcode_type=gcode_type,
    )
    assert check.status == "FAIL", check
    assert "abrasive" in check.detail + "\n".join(check.lines)


def test_nozzle_low_hrc_does_not_prove_an_unknown_spool_is_nonabrasive(
    tmp_path: Path,
) -> None:
    check = _nozzle_hrc_case(
        tmp_path, required_hrc="3", nozzle_material="stainless", loaded=None
    )
    assert check.status == "UNVERIFIED", check
    assert "no spool data" in check.detail + "\n".join(check.lines)


@pytest.mark.parametrize(
    ("required_hrc", "abrasive", "expected"),
    [("3", False, "PASS"), ("21", False, "FAIL"), ("3", True, "FAIL")],
)
def test_cli_nozzle_compat_compares_hrc_and_keeps_abrasive_policy(
    tmp_path: Path, _env: Path, required_hrc: str, abrasive: bool, expected: str
) -> None:
    """The normal offline CLI reports the same nozzle verdict as the gate engine."""
    st = state(t3=spool(4, color="FFFFFF", abrasive=abrasive))
    head = next(h for h in st["heads"] if h["label"] == "T4")
    head["nozzle"]["material"] = "stainless"
    snapshot = tmp_path / "snapshot.json"
    gates.write_snapshot(
        snapshot,
        {
            "version": 1,
            "taken_at": gates.drying.format_ts(datetime.datetime.now(UTC)),
            "printers": {"u1": st},
        },
    )
    plate = gcode(
        tmp_path,
        used_g=(0.0, 0.0, 0.0, 20.0),
        hrc=f"86,86,86,{required_hrc}",
        body="T3\nM104 S215 T3\nG1 X1 E1\n",
    )
    result = run_cli(
        tmp_path,
        "print-preflight",
        str(plate),
        "--offline",
        "--printops-snapshot",
        str(snapshot),
        "--json",
        env_extra={"THREED_FLEET": str(_env), "PRINTOPS_API": "http://127.0.0.1:9"},
    )
    assert result.returncode in (0, 1), (result.stdout, result.stderr)
    checks = {check["name"]: check for check in json.loads(result.stdout)["checks"]}
    # This minimal gate fixture lacks thumbnails; its unrelated overall FAIL is expected.
    assert checks["printops"]["status"] == "PASS", checks["printops"]
    assert checks["nozzle_compat"]["status"] == expected, checks["nozzle_compat"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", 123),
        ("name", ["PLA"]),
        ("name", {"text": "PLA"}),
        ("name", False),
        ("name", []),
        ("name", 0),
        ("abrasive", "false"),
        ("abrasive", 0),
        ("material", 123),
        ("material", ["PLA-CF"]),
    ],
)
def test_nozzle_malformed_marker_data_is_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    if field == "material":
        # These cases isolate the nozzle gate from other consumers of material text.
        override(tmp_path, monkeypatch, loaded_filament_match="off",
                 enclosure_material="off", plate_material="off")
    loaded = spool(4, color="FFFFFF", abrasive=False)
    loaded[field] = value
    check = _nozzle_hrc_case(
        tmp_path, required_hrc="3", nozzle_material="stainless", loaded=loaded
    )
    assert check.status == "UNVERIFIED", check
    assert f"invalid spool {field}" in check.detail + "\n".join(check.lines)


@pytest.mark.parametrize(
    "positive_source", ["flag", "material", "name", "gcode_type", "name_after_malformed_material"]
)
def test_nozzle_positive_evidence_outranks_malformed_marker_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, positive_source: str
) -> None:
    loaded = spool(4, color="FFFFFF", abrasive=False)
    loaded["name"] = 123
    gcode_type = "PLA"
    if positive_source == "flag":
        loaded["abrasive"] = True
    elif positive_source == "material":
        loaded["material"] = "PLA-CF"
    elif positive_source == "name":
        loaded["name"] = "PLA-GF test"
        loaded["abrasive"] = "false"
    elif positive_source == "name_after_malformed_material":
        loaded["material"] = 123
        loaded["name"] = "PLA-CF"
        override(tmp_path, monkeypatch, loaded_filament_match="off",
                 enclosure_material="off", plate_material="off")
    else:
        gcode_type = "PLA-CF"
    check = _nozzle_hrc_case(
        tmp_path,
        required_hrc="3",
        nozzle_material="stainless",
        loaded=loaded,
        gcode_type=gcode_type,
    )
    assert check.status == "FAIL", check
    assert "abrasive filament" in check.detail + "\n".join(check.lines)
    if positive_source == "name_after_malformed_material":
        assert "spool name 'PLA-CF'" in check.detail + "\n".join(check.lines)


@pytest.mark.parametrize(
    ("updates", "gcode_type", "expected", "evidence"),
    [
        ({"name": 123}, "PLA", "UNVERIFIED", "invalid spool name"),
        ({"name": ["PLA"]}, "PLA", "UNVERIFIED", "invalid spool name"),
        ({"name": 0}, "PLA", "UNVERIFIED", "invalid spool name"),
        ({"name": 123}, "PLA-CF", "FAIL", "G-code type"),
        ({"name": 123, "abrasive": True}, "PLA", "FAIL", "Spoolman filament abrasive"),
    ],
)
def test_cli_nozzle_malformed_marker_snapshot_keeps_structured_verdict(
    tmp_path: Path,
    _env: Path,
    updates: dict[str, Any],
    gcode_type: str,
    expected: str,
    evidence: str,
) -> None:
    """Inspect an offline snapshot with malformed metadata without losing positive evidence."""
    loaded = spool(4, color="FFFFFF", abrasive=False)
    loaded.update(updates)
    st = state(t3=loaded)
    head = next(h for h in st["heads"] if h["label"] == "T4")
    head["nozzle"]["material"] = "stainless"
    snapshot = tmp_path / "snapshot.json"
    gates.write_snapshot(
        snapshot,
        {
            "version": 1,
            "taken_at": gates.drying.format_ts(datetime.datetime.now(UTC)),
            "printers": {"u1": st},
        },
    )
    plate = gcode(
        tmp_path,
        used_g=(0.0, 0.0, 0.0, 20.0),
        types=f"PLA;PLA;PLA;{gcode_type}",
        hrc="86,86,86,3",
        body="T3\nM104 S215 T3\nG1 X1 E1\n",
    )
    result = run_cli(
        tmp_path,
        "print-preflight",
        str(plate),
        "--offline",
        "--printops-snapshot",
        str(snapshot),
        "--json",
        env_extra={"THREED_FLEET": str(_env), "PRINTOPS_API": "http://127.0.0.1:9"},
    )
    assert result.returncode in (0, 1), (result.stdout, result.stderr)
    assert "Traceback" not in result.stderr
    checks = {check["name"]: check for check in json.loads(result.stdout)["checks"]}
    assert checks["printops"]["status"] == "PASS", checks["printops"]
    check = checks["nozzle_compat"]
    assert check["status"] == expected, check
    assert evidence in check["detail"] + "\n".join(check["lines"])


# -- one G-code per FAIL condition ----------------------------------------------------------
FAIL_CASES: dict[str, tuple[dict[str, Any], dict[str, Any], str, str]] = {
    # name: (gcode kwargs, state kwargs, failing check, detail substring)
    "k1_gcode_on_u1": (
        {"model": "Creality K1 SE"},
        {},
        "printer_profile_match",
        "sliced for 'Creality K1 SE', but u1 is a 'Snapmaker U1'",
    ),
    "nozzle_diameter": (
        {"nozzle_diameter": "0.6"},
        {},
        "nozzle_compat",
        "T1, T3: G-code nozzle_diameter 0.6 mm, installed 0.4 mm",
    ),
    "cf_on_stainless": (
        {"types": "PLA-CF;PLA;PLA;PLA"},
        {"t0": spool(1, material="PLA-CF", abrasive=None)},
        "nozzle_compat",
        "abrasive filament (spool material 'PLA-CF') on a stainless nozzle",
    ),
    "hrc_required": (
        {"hrc": "40,0,0,0"},
        {},
        "nozzle_compat",
        "G-code requires nozzle HRC 40",
    ),
    "nozzle_too_hot": (
        {"nozzle_temp": "260"},
        {},
        "filament_temp_range",
        "nozzle_temperature 260 °C outside 190–230",
    ),
    "body_too_hot": (
        {"body": "T0\nM104 S215 T0\nT2\nM109 S245\n"},
        {},
        "filament_temp_range",
        "M104/M109 T2 245 °C outside 190–230",
    ),
    "first_layer_too_cold": (
        {"first_layer": "180"},
        {},
        "filament_temp_range",
        "nozzle_temperature_initial_layer 180 °C outside 190–230",
    ),
    "bed_too_hot": (
        {"plate_temp": "100"},
        {},
        "filament_temp_range",
        "bed textured_plate_temp 100 °C outside 45–70",
    ),
    "type_mismatch": (
        {"types": "PETG;PLA;PLA;PLA"},
        {},
        "loaded_filament_match",
        "G-code PETG, loaded #1 Elegoo PLA is PLA",
    ),
    "colour_far": (
        {"colours": "#FFFFFF;#808080;#1B5E20;#FFFFFF"},
        {},
        "loaded_filament_match",
        "ΔE 100.0 G-code #FFFFFF vs loaded #000000 > 25",
    ),
    "not_enough": (
        {"used_g": (590.0, 0.0, 50.0, 0.0)},
        {},
        "filament_sufficient",
        "needs 622 g",
    ),
    "broken_head": (
        {"used_g": (20.0, 10.0, 50.0, 0.0)},
        {"t1": spool(2, color="808080")},
        "maintenance_due",
        "head T2 is not usable: T2 is broken",
    ),
}


@pytest.mark.parametrize("case", sorted(FAIL_CASES))
def test_each_fail_condition_fails_its_gate(tmp_path: Path, case: str) -> None:
    gkw, skw, check, needle = FAIL_CASES[case]
    st = state(
        **{"t0": spool(1, color="000000"), "t2": spool(3, color="1B5E20"), **skw}
    )
    checks = run(gcode(tmp_path, f"{case}.gcode", **gkw), st)
    assert checks[check].status == "FAIL", checks[check]
    assert needle in checks[check].detail + "\n".join(checks[check].lines)
    others = {n: s for n, s in statuses(checks).items() if s == "FAIL" and n != check}
    assert others == {}, others


@pytest.mark.parametrize(
    ("case", "level_key", "gkw", "skw", "needle"),
    [
        (
            "wet_spool",
            "drying_due",
            {},
            {"t2": spool(3, color="1B5E20", drying="wet")},
            "#3 Elegoo PLA: wet",
        ),
        (
            "abs_open",
            "enclosure_material",
            {"types": "ABS;PLA;PLA;PLA"},
            {"t0": spool(1, material="ABS", nozzle=(230, 260), bed=(90, 110))},
            "T1 ABS on a printer with enclosure: none",
        ),
        (
            "plate_missing",
            "plate_material",
            {"bed_type": "Cool Plate"},
            {},
            "sliced for Cool Plate, printer has textured_pei",
        ),
    ],
)
def test_warn_level_gates_fail_under_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    level_key: str,
    gkw: dict[str, Any],
    skw: dict[str, Any],
    needle: str,
) -> None:
    st = state(
        **{"t0": spool(1, color="000000"), "t2": spool(3, color="1B5E20"), **skw}
    )
    path = gcode(tmp_path, f"{case}.gcode", **gkw)
    if (
        case == "abs_open"
    ):  # an ABS plate is printed hot: keep the temperature gate quiet
        path = gcode(
            tmp_path,
            f"{case}.gcode",
            nozzle_temp="245",
            first_layer="250",
            plate_temp="100",
            body="T0\nM104 S245 T0\nT2\nM109 S215\n",
            **gkw,
        )
        st["tools"][2]["spool"]["bed_temp_max"] = 110
    warned = run(path, st)
    assert warned[level_key].status == "WARN" and needle in warned[level_key].detail
    override(tmp_path, monkeypatch, **{level_key: "block"})
    failed = run(path, st)
    assert failed[level_key].status == "FAIL"


def test_never_weighed_spool_without_spoolman_remaining_is_weigh_not_empty(
    tmp_path: Path,
) -> None:
    unknown = spool(1, remaining=(0.0, 0.0, False))
    unknown["spoolman_remaining_g"] = (
        None  # imported without a weight: unknown, not 0 g
    )
    checks = run(gcode(tmp_path), state(t0=unknown, t2=spool(3, color="1B5E20")))
    assert checks["filament_sufficient"].status == "WARN"
    assert (
        "T1: #1 Elegoo PLA: needs 27 g, remaining not known — weigh it: 3d printops spool measure 1 GROSS_G"
        in checks["filament_sufficient"].detail
    )


def test_soft_findings_warn(tmp_path: Path) -> None:
    st = state(
        t0=spool(1, color="000000", vendor="Polymaker"),
        t2=spool(3, color="1B5E20", remaining=(60.0, 30.0, True), drying="due"),
    )
    checks = run(gcode(tmp_path, colours="#202020;#808080;#1B5E20;#FFFFFF"), st)
    assert checks["loaded_filament_match"].status == "WARN"
    lm = "\n".join(checks["loaded_filament_match"].lines)
    assert "vendor: G-code profile Elegoo, loaded Polymaker" in lm and "ΔE" in lm
    assert (
        checks["filament_sufficient"].status == "WARN"
        and "weigh it" in checks["filament_sufficient"].detail
    )
    assert (
        checks["drying_due"].status == "WARN"
        and "#3 Elegoo PLA: due" in checks["drying_due"].detail
    )


def test_project_colour_rule_is_stricter(tmp_path: Path) -> None:
    proj = tmp_path / "babushkin-dom" / "print"
    proj.mkdir(parents=True)
    colours = "#262626;#808080;#1B5E20;#FFFFFF"  # ΔE 9.3 from the loaded #000000
    loose = run(gcode(tmp_path, colours=colours), good_state())
    strict = run(gcode(proj, colours=colours), good_state())
    assert loose["loaded_filament_match"].status == "PASS"
    assert (
        strict["loaded_filament_match"].status == "WARN"
        and "> 6" in strict["loaded_filament_match"].detail
    )


def test_ams_printer_maps_tools_to_unit_slots() -> None:
    fleet = load_fleet()
    assert fleet is not None
    assert [(t, s) for t, s, _ in gates.tool_slots(fleet.printers["p1s"])] == [
        (0, "ams1/1"),
        (1, "ams1/2"),
        (2, "ams1/3"),
        (3, "ams1/4"),
    ]


# -- preflight integration --------------------------------------------------------------------
def test_run_preflight_appends_printops_lines_and_counts_unverified(
    tmp_path: Path,
) -> None:
    path = gcode(tmp_path)
    report = preflight.run_preflight(
        path,
        axis_limits=((0, 300), (0, 300)),
        limits_source="test",
        printops=PrintopsContext(printer="u1"),
    )
    names = [c.name for c in report.checks]
    assert names.index("printops") > names.index("visible")
    assert "unverified)" in report.render().splitlines()[-1]
    plain = preflight.run_preflight(
        path, axis_limits=((0, 300), (0, 300)), limits_source="test"
    )
    assert "printops" not in [c.name for c in plain.checks]


# -- offline snapshot -------------------------------------------------------------------------
def test_offline_snapshot_is_used_and_warns_when_stale(tmp_path: Path) -> None:
    snap = {
        "version": 1,
        "taken_at": "2026-10-15T09:00:00.000Z",
        "api": "x",
        "printers": {"u1": good_state()},
    }
    path = tmp_path / "snap.json"
    gates.write_snapshot(path, snap)
    ctx = PrintopsContext(printer="u1", offline=True, snapshot=path)
    fresh = {c.name: c for c in run_checks(gcode(tmp_path), ctx, now=NOW).checks}
    stale = {
        c.name: c
        for c in run_checks(
            gcode(tmp_path), ctx, now=NOW + datetime.timedelta(days=2)
        ).checks
    }
    assert (
        fresh["printops"].status == "PASS" and "3.0 h old" in fresh["printops"].detail
    )
    assert fresh["loaded_filament_match"].status == "PASS"
    assert (
        stale["printops"].status == "WARN"
        and "refresh with `3d printops gates snapshot`" in stale["printops"].detail
    )
    missing = run_checks(
        gcode(tmp_path),
        PrintopsContext(printer="u1", offline=True, snapshot=tmp_path / "no.json"),
        now=NOW,
    )
    assert (
        missing.checks[0].status == "UNVERIFIED"
        and "no PrintOps snapshot" in missing.checks[0].detail
    )


def test_broken_snapshot_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "snap.json"
    path.write_text(json.dumps({"version": 9}), encoding="utf-8")
    with pytest.raises(gates.SnapshotError, match="not a version-1 PrintOps snapshot"):
        gates.read_snapshot(path)


def test_concurrent_snapshot_writers_never_share_a_temp_file(tmp_path: Path) -> None:
    path = tmp_path / "cache" / "snap.json"
    snaps = [
        {"version": 1, "taken_at": f"2026-10-15T0{i}:00:00Z", "printers": {}}
        for i in range(8)
    ]
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda s: gates.write_snapshot(path, s), snaps * 4))
    assert gates.read_snapshot(path) in snaps
    assert [p.name for p in path.parent.iterdir()] == ["snap.json"]
    assert (
        path.stat().st_mode & 0o777 == 0o644
    )  # readable like any cache file, not mkstemp's 0600


# -- 3d-api state ------------------------------------------------------------------------------
def _receive_and_load(
    api: ApiClient, sm: FakeSpoolman, head: str, **filament_extra: Any
) -> int:
    got = api.post(
        "/v1/spools",
        {
            "vendor": "Elegoo",
            "material": "PLA",
            "name": f"PLA {head}",
            "color_hex": "#1B5E20",
            "net_g": 1000,
            "lot_nr": "L7",
        },
    )
    sm.update(
        "filament",
        got["filament_id"],
        {"extra": {k: encode_extra(v) for k, v in filament_extra.items()}},
    )
    api.post(f"/v1/printers/u1/slots/{head}/load", {"spool_id": got["id"]})
    return int(got["id"])


def test_api_state_reports_bound_spools_with_ledger_and_drying(
    api_client: ApiClient,
    fake_spoolman: FakeSpoolman,
    printops_db: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    ensure_extra_fields(fake_spoolman)
    sid = _receive_and_load(
        api_client,
        fake_spoolman,
        "T3",
        nozzle_temp_min=190,
        nozzle_temp_max=230,
        bed_temp_min=45,
        bed_temp_max=70,
        abrasive=False,
    )
    st = api_client.get("/v1/gates/state", printer="u1")
    assert (
        st["bindings"] == "ledger"
        and st["source"] == "3d-api"
        and st["enclosure"] == "none"
    )
    by_slot = {t["slot"]: t for t in st["tools"]}
    sp = by_slot["T3"]["spool"]
    assert by_slot["T1"]["spool"] is None and by_slot["T3"]["tool"] == 2
    assert (sp["id"], sp["material"], sp["vendor"], sp["lot_nr"]) == (
        sid,
        "PLA",
        "Elegoo",
        "L7",
    )
    assert (sp["nozzle_temp_min"], sp["nozzle_temp_max"], sp["abrasive"]) == (
        190,
        230,
        False,
    )
    assert sp["remaining"]["anchored"] and sp["remaining"]["mid_g"] == 1000
    assert (
        sp["drying"]["state"] in ("ok", "fresh") and sp["drying"]["class"] == "low"
    )  # loading sets opened_at

    snap = api_client.get("/v1/gates/snapshot")
    assert (
        set(snap["printers"]) == {"u1", "p1s"}
        and snap["printers"]["u1"]["tools"][2]["spool"]["id"] == sid
    )
    before = len(fake_spoolman.writes())
    api_client.get("/v1/gates/state", printer="u1")
    assert len(fake_spoolman.writes()) == before

    # the gates evaluate the API's state: T3 bound and in range, T1 (tool 0) unbound
    checks = run(gcode(tmp_path), st)
    assert checks["filament_temp_range"].lines == [
        "UNVERIFIED T1: no spool bound to this slot",
        f"PASS       T3: #{sid} Elegoo PLA T3: nozzle ≤220 °C in 190–230, bed in 45–70",
    ]

    with pytest.raises(ApiError) as e:
        api_client.get("/v1/gates/state", printer="nope")
    assert e.value.status == 404 and "fleet has: p1s, u1" in e.value.message


def test_api_state_needs_read_scope(
    printops_client: Any, printops_tokens: dict[str, str]
) -> None:
    r = printops_client.get(
        "/v1/gates/state",
        params={"printer": "u1"},
        headers={"Authorization": f"Bearer {printops_tokens['write']}"},
    )
    assert r.status_code == 403


# -- preset_match (#293) ----------------------------------------------------------------------
K1_FLEET = """\
k1se:
  model: Creality K1 SE
  owner: own
  access: offline
  heads: [{label: T1, index: 0, nozzle: {diameter_mm: 0.4, material: hardened, type: std}, usable: true}]
  slicer: {machine: K1 SE - Copy, process: Std 8900}
k1bare:
  model: Creality K1 SE
  owner: own
  access: offline
  heads: [{label: T1, index: 0, usable: true}]
"""


def _k1_presets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, accel: str = "8900") -> None:
    """The fleet above, Orca's K1 SE system process preset and the user preset inheriting it."""
    fleet = tmp_path / "k1.yaml"
    fleet.write_text(K1_FLEET, encoding="utf-8")
    monkeypatch.setenv("THREED_FLEET", str(fleet))
    system = tmp_path / "profiles" / "Creality" / "process"
    system.mkdir(parents=True)
    (system / "0.20mm Standard @Creality K1 SE.json").write_text(
        json.dumps({"name": "0.20mm Standard @Creality K1 SE", "default_acceleration": "12000",
                    "outer_wall_acceleration": "5000"})
    )
    user = tmp_path / "user" / "process"
    user.mkdir(parents=True)
    (user / "Std 8900.json").write_text(
        json.dumps({"name": "Std 8900", "inherits": "0.20mm Standard @Creality K1 SE",
                    "default_acceleration": accel})
    )
    monkeypatch.setenv("ORCA_PROFILES", str(tmp_path / "profiles"))
    monkeypatch.setenv("ORCA_USER_PRESETS", str(tmp_path / "user"))
    monkeypatch.delenv("SLICER", raising=False)


def _k1_gcode(tmp_path: Path, machine: str, process: str, accel: str = "8900") -> Path:
    return gcode(
        tmp_path,
        model="Creality K1 SE",
        types="PLA",
        colours="#000000",
        vendors="Elegoo",
        used_g=(20.0,),
        hrc="0",
        body="M104 S215\nG1 X1 E1\n",
        extra={"printer_settings_id": machine, "print_settings_id": process,
               "default_acceleration": accel, "outer_wall_acceleration": "5000"},
    )


def _preset(path: Path, printer: str = "k1se") -> preflight.Check:
    result = run_checks(path, PrintopsContext(printer=printer), now=NOW)
    return {c.name: c for c in result.checks}["preset_match"]


def test_preset_match_passes_on_the_fleet_presets_with_their_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _k1_presets(tmp_path, monkeypatch)
    check = _preset(_k1_gcode(tmp_path, "K1 SE - Copy", "Std 8900"))
    assert check.status == "PASS", check.detail
    assert any("default_acceleration 8900 = preset" in ln for ln in check.lines)
    assert any("outer_wall_acceleration 5000 = preset" in ln for ln in check.lines)


def test_preset_match_fails_on_stock_presets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The #293 bug: a calibration plate sliced with Orca's stock presets, not the user's."""
    _k1_presets(tmp_path, monkeypatch)
    check = _preset(
        _k1_gcode(tmp_path, "Creality K1 SE 0.4 nozzle", "0.20mm Standard @Creality K1 SE", accel="12000")
    )
    assert check.status == "FAIL"
    assert "sliced with machine preset 'Creality K1 SE 0.4 nozzle'" in "\n".join(check.lines)
    assert any("the printer prints with 'Std 8900'" in ln for ln in check.lines)


def test_preset_match_fails_on_an_unsaved_preset_edit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _k1_presets(tmp_path, monkeypatch)
    check = _preset(_k1_gcode(tmp_path, "K1 SE - Copy", "Std 8900", accel="10000"))
    assert check.status == "FAIL"
    assert "default_acceleration 10000, the process preset 'Std 8900' has 8900" in check.detail


def test_preset_match_unresolvable_preset_keeps_the_name_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _k1_presets(tmp_path, monkeypatch)
    monkeypatch.setenv("ORCA_USER_PRESETS", str(tmp_path / "nowhere"))
    check = _preset(_k1_gcode(tmp_path, "K1 SE - Copy", "Std 8900"))
    assert check.status == "PASS"
    assert any("process values not compared" in ln for ln in check.lines)


def test_preset_match_without_fleet_presets_is_unverified_and_policy_can_warn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _k1_presets(tmp_path, monkeypatch)
    path = _k1_gcode(tmp_path, "Creality K1 SE 0.4 nozzle", "0.20mm Standard @Creality K1 SE")
    check = _preset(path, "k1bare")
    assert check.status == "UNVERIFIED" and "no `slicer: {machine, process}` for k1bare" in check.detail
    override(tmp_path, monkeypatch, preset_match="warn")
    assert _preset(path).status == "WARN"
