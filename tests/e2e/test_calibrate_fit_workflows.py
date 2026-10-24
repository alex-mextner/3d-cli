"""`3d calibrate fit` end to end through bin/3d: plan, coupon files, reading a result, `3d fits resolve` (#573)."""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pytest
from fleet_fixtures import REPO_FLEET
from printops_fixtures import LiveApi

from printops import db, events
from printops.spoolman import encode_extra

from .workflow_helper import run_cli

needs_openscad = pytest.mark.skipif(shutil.which("openscad") is None, reason="OpenSCAD not installed")


@pytest.fixture(autouse=True)
def fleet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THREED_FLEET", str(REPO_FLEET))
    monkeypatch.delenv("PRINTOPS_POLICY", raising=False)
    monkeypatch.delenv("PRINTOPS_CALIBRATIONS", raising=False)


def fit_resolve(tmp_path: Path, *extra: str, env: dict[str, str] | None = None, shape: str = "round") -> dict:
    out = run_cli(
        tmp_path, "fits", "resolve", "--fit", "sliding", "--shape", shape, "--printer", "k1se",
        "--material", "PLA", "--nozzle", "0.4", "--json", *extra, env_extra=env,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def correction(doc: dict) -> dict:
    return next(p for p in doc["provenance"] if p["kind"] == "correction")


def test_help_lists_every_subcommand_and_flag(tmp_path: Path) -> None:
    """A user reads `3d calibrate fit --help` before printing the first coupon."""
    out = run_cli(tmp_path, "calibrate", "fit", "--help")
    assert out.returncode == 0
    for word in ("plan", "gen", "quick", "read", "record", "check", "--result", "--measured", "--mating-mm",
                 "--from-readout", "--gap-offset", "--max-age-days", "--yes", "--json", "--fit LIST",
                 "step=N", "3d fits resolve"):
        assert word in out.stdout, word
    unknown = run_cli(tmp_path, "calibrate", "fit", "wibble")
    assert unknown.returncode == 2 and "accepted: plan, gen, quick, read, record, check" in unknown.stderr


def test_plan_json_lists_coupons_gaps_estimate_and_the_readout_of_every_coupon(tmp_path: Path) -> None:
    """Before printing anything the user asks what the coupon is: ladders, gaps, minutes and how to read each."""
    out = run_cli(tmp_path, "calibrate", "fit", "plan", "--printer", "k1se", "--material", "pla", "--json")
    assert out.returncode == 0, out.stderr
    doc = json.loads(out.stdout)
    assert doc["schema"] == "3d.calibrate_fit/1" and doc["material"] == "PLA" and doc["model"] == "Creality K1 SE"
    assert doc["estimate"]["minutes"] <= 10 and doc["coupon"]["fits_bed"] is True
    assert len(doc["coupons"]) == 6
    for coupon in doc["coupons"]:
        assert len(coupon["steps"]) == 7 and "--result step=N" in coupon["readout"] and coupon["record"]
    text = run_cli(tmp_path, "calibrate", "fit", "plan", "--printer", "u1", "--fit", "sliding", "--shape", "slot")
    assert text.returncode == 0 and "270x270" in text.stdout and "sliding/slot" in text.stdout
    assert "tight" not in text.stdout


@needs_openscad
def test_gen_writes_a_3mf_stl_scad_and_map_centred_on_the_printers_bed(tmp_path: Path) -> None:
    """The user generates the coupon for the K1 SE and for the U1 and gets slicer-ready files for each bed."""
    out = run_cli(tmp_path, "calibrate", "fit", "gen", "--printer", "k1se", "--out", "k1.3mf", "--json")
    assert out.returncode == 0, out.stderr
    doc = json.loads(out.stdout)
    assert sorted(Path(p).suffix for p in doc["written"]) == [".3mf", ".json", ".scad", ".stl"]
    origin, size = doc["coupon"]["origin_mm"], doc["coupon"]["footprint_mm"]
    assert origin[0] + size[0] / 2 == pytest.approx(110.0, abs=0.01)  # centred on the 220 mm bed
    with zipfile.ZipFile(tmp_path / "k1.3mf") as z:
        root = ElementTree.fromstring(z.read("3D/3dmodel.model"))
    ns = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
    assert len(root.findall("m:build/m:item", ns)) == 7  # six ladders and the spares
    xs = [float(v.attrib["x"]) for v in root.iter(f"{{{ns['m']}}}vertex")]
    assert min(xs) == pytest.approx(origin[0], abs=0.01) and max(xs) <= 220 - 5
    saved = json.loads((tmp_path / "k1.json").read_text())
    assert saved["coupons"][0]["steps"][0]["gap_mm"] == 0.05 and saved["printer"] == "k1se"

    big = run_cli(tmp_path, "calibrate", "fit", "gen", "--printer", "u1", "--out", "u1.stl", "--json")
    assert big.returncode == 0, big.stderr
    assert json.loads(big.stdout)["coupon"]["origin_mm"][0] > origin[0]  # the 270 mm bed centres it further out
    mesh = pytest.importorskip("trimesh").load(tmp_path / "u1.stl")
    assert mesh.is_watertight and mesh.bounds[1][0] <= 270 - 5 and mesh.bounds[1][1] <= 270 - 5

    check = run_cli(tmp_path, "mesh", "u1.stl")
    assert check.returncode == 0, check.stdout + check.stderr  # the repo's own mesh gate (watertight, manifold)
    assert "watertight        : yes" in check.stdout and "MESH CHECK: PASS" in check.stdout


def test_gen_needs_an_out_path_and_quick_is_a_dry_run_until_yes(tmp_path: Path) -> None:
    """`quick` shows what it would write and writes only with --yes."""
    no_out = run_cli(tmp_path, "calibrate", "fit", "gen", "--printer", "k1se")
    assert no_out.returncode == 2 and "--out is required" in no_out.stderr
    dry = run_cli(tmp_path, "calibrate", "fit", "quick", "--printer", "k1se", "--material", "PETG")
    assert dry.returncode == 0, dry.stderr
    assert "dry run: would write fit-coupon-k1se-petg.3mf" in dry.stdout and not list(tmp_path.glob("fit-coupon*"))
    if shutil.which("openscad"):
        done = run_cli(tmp_path, "calibrate", "fit", "quick", "--printer", "k1se", "--material", "PETG", "--yes")
        assert done.returncode == 0, done.stderr
        assert (tmp_path / "fit-coupon-k1se-petg.3mf").is_file() and (tmp_path / "fit-coupon-k1se-petg.json").is_file()


def test_a_step_result_changes_fits_resolve_and_shows_where_the_number_came_from(tmp_path: Path) -> None:
    """The K1 SE + PLA story: sliding fit read as step 3 → resolve says calibrated, dated, with the record id."""
    before = fit_resolve(tmp_path)
    assert before["clearance_mm"] == 0.2 and correction(before)["status"] == "default, not calibrated"

    dry = run_cli(
        tmp_path, "calibrate", "fit", "record", "--printer", "k1se", "--material", "pla", "--fit", "sliding",
        "--shape", "round", "--result", "step=3",
    )
    assert dry.returncode == 0, dry.stderr
    assert "gap 0.15 mm (table 0.2) -> delta -0.05 mm; clearance 0.2 (default, not calibrated) -> 0.15 mm (calibrated)" in dry.stdout
    assert "dry run: would store" in dry.stdout and "add --yes" in dry.stdout
    assert fit_resolve(tmp_path)["clearance_mm"] == 0.2  # nothing stored

    done = run_cli(
        tmp_path, "calibrate", "fit", "record", "--printer", "k1se", "--material", "pla", "--fit", "sliding",
        "--shape", "round", "--result", "step=3", "--yes", "--json",
    )
    assert done.returncode == 0, done.stderr
    rec = json.loads(done.stdout)
    assert rec["ok"] is True and rec["stored"]["origin"] == "buffer" and rec["dry_run"] is False
    assert rec["before_after"] == [{
        "fit_type": "sliding", "shape": "round", "before_mm": 0.2, "after_mm": 0.15, "change_mm": -0.05,
        "before_status": "default, not calibrated", "after_status": "calibrated",
    }]
    buffer = tmp_path / "xdg-data" / "3d-cli" / "printer-history" / "k1se.jsonl"
    assert buffer.is_file() and json.loads(buffer.read_text())["kind"] == "fit_clearance"

    after = fit_resolve(tmp_path)
    entry = correction(after)
    assert after["clearance_mm"] == 0.15 and entry["status"] == "calibrated" and entry["source"] == "calibration"
    assert entry["measured_at"].endswith("Z") and rec["stored"]["id"] in entry["note"]
    text = run_cli(tmp_path, "fits", "resolve", "--fit", "sliding", "--shape", "round", "--printer", "k1se",
                   "--material", "PLA", "--nozzle", "0.4")
    assert "[calibrated, measured 20" in text.stdout and rec["stored"]["id"] in text.stdout
    assert fit_resolve(tmp_path, shape="slot")["clearance_mm"] == 0.2  # another shape is still the table value
    listed = run_cli(tmp_path, "fits", "corrections", "list", "--origin", "calibration", "--json")
    assert [r["origin"] for r in json.loads(listed.stdout)["corrections"]] == ["calibration"]


def test_a_second_ladder_keeps_the_first_and_check_flags_missing_stale_and_partial(tmp_path: Path) -> None:
    """After two ladders `check` says PARTIAL, a wider nozzle says STALE, another plastic says MISSING (exit 1)."""
    for fit, shape, result in (("sliding", "round", "step=3"), ("snap", "slot", "step=5")):
        out = run_cli(tmp_path, "calibrate", "fit", "record", "--printer", "k1se", "--material", "PLA", "--fit", fit,
                      "--shape", shape, "--result", result, "--yes")
        assert out.returncode == 0, out.stderr
    assert fit_resolve(tmp_path)["clearance_mm"] == 0.15  # the first ladder survived the second record
    snap = run_cli(tmp_path, "fits", "resolve", "--fit", "snap", "--shape", "slot", "--printer", "k1se",
                   "--material", "PLA", "--nozzle", "0.4", "--json")
    assert json.loads(snap.stdout)["clearance_mm"] == 0.35

    partial = run_cli(tmp_path, "calibrate", "fit", "check", "--printer", "k1se", "--material", "PLA", "--json")
    assert partial.returncode == 0, partial.stdout
    pair = json.loads(partial.stdout)["pairs"][0]
    assert pair["level"] == "PARTIAL" and set(pair["covered"]) == {"sliding/round", "snap/slot"}

    wide = run_cli(tmp_path, "calibrate", "fit", "check", "--printer", "k1se", "--material", "PLA", "--nozzle", "0.6")
    assert wide.returncode == 1 and "STALE" in wide.stdout and "taken with a 0.4 mm nozzle" in wide.stdout
    assert "fix: 3d calibrate fit quick --printer k1se --material PLA --yes" in wide.stdout

    petg = run_cli(tmp_path, "calibrate", "fit", "check", "--printer", "k1se", "--material", "PETG", "--json")
    assert petg.returncode == 1 and json.loads(petg.stdout)["pairs"][0]["level"] == "MISSING"


def test_all_fused_exits_1_with_the_advice_to_widen_the_range_and_stores_nothing(tmp_path: Path) -> None:
    """Every step of the tight ladder fused: exit 1 with `widen the range`, JSON says ok=false, nothing is stored."""
    out = run_cli(
        tmp_path, "calibrate", "fit", "record", "--printer", "k1se", "--material", "PLA", "--fit", "tight",
        "--shape", "round", "--result", "fused,fused,fused,fused,fused,fused,fused", "--yes",
    )
    assert out.returncode == 1
    assert "every step is fused" in out.stdout and "widen the range" in out.stderr
    assert not (tmp_path / "xdg-data").exists()
    as_json = run_cli(
        tmp_path, "calibrate", "fit", "read", "--printer", "k1se", "--material", "PLA", "--fit", "tight",
        "--shape", "round", "--result", "fused,fused,fused,fused,fused,fused,fused", "--json",
    )
    doc = json.loads(as_json.stdout)
    assert as_json.returncode == 1 and doc["ok"] is False and "widen the range" in doc["picks"][0]["problem"]


def test_the_wider_coupon_is_read_with_the_same_gap_offset(tmp_path: Path) -> None:
    """After an all-fused tight ladder the user prints it 0.1 mm wider and reads that one."""
    out = run_cli(
        tmp_path, "calibrate", "fit", "read", "--printer", "k1se", "--material", "PLA", "--fit", "tight",
        "--shape", "round", "--gap-offset", "0.1", "--result", "step=2", "--json",
    )
    pick = json.loads(out.stdout)["picks"][0]
    # the tight ladder is 0.00 ... 0.30; shifted by 0.1 it is 0.10 ... 0.40: step 2 = 0.15, 0.05 above the table's 0.10
    assert pick["gap_mm"] == 0.15 and pick["delta_mm"] == 0.05
    assert out.returncode == 0


def test_odd_readouts_warn_ties_and_non_monotonic(tmp_path: Path) -> None:
    """A readout where step 3 grips but step 2 slid is recorded, with the warning to re-check those steps."""
    out = run_cli(
        tmp_path, "calibrate", "fit", "read", "--printer", "k1se", "--material", "PLA", "--fit", "sliding",
        "--shape", "round", "--result", "press,free,press,free,free,free,free", "--json",
    )
    pick = json.loads(out.stdout)["picks"][0]
    assert pick["gap_mm"] == 0.1 and "non-monotonic" in pick["warnings"][0]
    assert out.returncode == 0


def test_caliper_numbers_and_a_readout_file_feed_record(tmp_path: Path) -> None:
    """Holes measured 0.07 mm per side small (--measured) plus a file with two more ladders, in one record."""
    measured = ",".join(str(round(4.0 + 2 * (g - 0.07), 3)) for g in (0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35))
    one = run_cli(
        tmp_path, "calibrate", "fit", "read", "--printer", "k1se", "--material", "PLA", "--fit", "sliding",
        "--shape", "round", "--measured", measured, "--json",
    )
    assert one.returncode == 0, one.stderr
    assert json.loads(one.stdout)["picks"][0]["delta_mm"] == 0.1
    readout = tmp_path / "readout.json"
    readout.write_text(json.dumps({
        "printer": "k1se", "material": "PLA",
        "ladders": [
            {"fit": "sliding", "shape": "round", "measured": [float(x) for x in measured.split(",")]},
            {"fit": "press", "shape": "slot", "result": "step=4"},
        ],
    }))
    out = run_cli(tmp_path, "calibrate", "fit", "record", "--from-readout", str(readout), "--yes", "--json")
    assert out.returncode == 0, out.stderr
    assert {(p["fit_type"], p["shape"], p["delta_mm"]) for p in json.loads(out.stdout)["picks"]} == {
        ("sliding", "round", 0.1), ("press", "slot", 0.025)}
    press = run_cli(tmp_path, "fits", "resolve", "--fit", "press", "--shape", "slot", "--printer", "k1se",
                    "--material", "PLA", "--nozzle", "0.4", "--json")
    assert json.loads(press.stdout)["clearance_mm"] == -0.075  # gaps run -0.15 ... 0: step 4 = 0.075 interference


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--fit", "sliding", "--shape", "round"], "--result"),
        (["--fit", "sliding", "--result", "step=3"], "needs --fit and --shape"),
        (["--fit", "glue", "--shape", "round", "--result", "step=3"], "accepted: press, sliding, snap, tight"),
        (["--fit", "sliding", "--shape", "round", "--result", "step=9"], "outside 1..7"),
        (["--fit", "sliding", "--shape", "round", "--result", "step=3", "--measured", "4.1"], "not both"),
    ],
)
def test_bad_readouts_are_usage_errors(tmp_path: Path, args: list[str], message: str) -> None:
    """A user mistypes the readout (no result, no shape, an unknown fit, a step off the ladder): exit 2, with the fix."""
    out = run_cli(tmp_path, "calibrate", "fit", "record", "--printer", "k1se", "--material", "PLA", *args)
    assert message in out.stderr, out.stderr
    assert out.returncode == 2


# --------------------------------------------------------------------------- with a real (loopback) 3d-api
def _load(live: LiveApi, material: str, slot: str) -> int:
    sm = live.spoolman
    vendor = sm.create("vendor", {"name": "Elegoo"})
    fil = sm.create("filament", {"name": f"{material} Grey", "vendor_id": vendor["id"], "material": material,
                                 "density": 1.24, "diameter": 1.75, "weight": 1000})
    return int(sm.create("spool", {"filament_id": fil["id"], "lot_nr": "L1",
                                   "extra": {"printops_slot": encode_extra(slot)}})["id"])


def test_with_3d_api_the_record_is_a_calibration_plan_shows_it_and_a_nozzle_swap_makes_it_stale(
    tmp_path: Path, live_api: LiveApi
) -> None:
    """K1 SE with PLA loaded: plan nags (WARN), record --yes stores a calibration, resolve cites its id,
    a maintenance nozzle swap invalidates it again."""
    _load(live_api, "PLA", "k1se/T1")
    env = {"PRINTOPS_API": live_api.url, "PRINTOPS_TOKEN": live_api.tokens["rw"], "THREED_FIT_CALIBRATION_API": "on"}

    plan = run_cli(tmp_path, "calibrate", "plan", "--printer", "k1se", "--json", env_extra=env)
    items = {i["kind"]: i for i in json.loads(plan.stdout)["items"]}
    assert (items["fit_clearance"]["level"], items["fit_clearance"]["status"]) == ("WARN", "new_material_type")

    wrong = run_cli(tmp_path, "calibrate", "fit", "record", "--printer", "k1se", "--material", "PETG", "--fit",
                    "sliding", "--shape", "round", "--result", "step=3", "--yes", env_extra=env)
    assert wrong.returncode == 2 and "PLA loaded" in wrong.stderr  # refused before anything was stored

    rec = run_cli(tmp_path, "calibrate", "fit", "record", "--printer", "k1se", "--material", "PLA", "--fit",
                  "sliding", "--shape", "round", "--result", "step=3", "--yes", "--json", env_extra=env)
    assert rec.returncode == 0, rec.stderr
    stored = json.loads(rec.stdout)["stored"]
    assert stored["origin"] == "3d-api" and stored["id"] == "#1"

    shown = run_cli(tmp_path, "calibrate", "show", "1", env_extra=env)
    assert json.loads(shown.stdout)["kind"] == "fit_clearance" and json.loads(shown.stdout)["head"] == "T1"
    after = fit_resolve(tmp_path, env=env)
    entry = correction(after)
    assert after["clearance_mm"] == 0.15 and entry["status"] == "calibrated" and "calibration record #1" in entry["note"]
    assert entry["measured_at"] == json.loads(shown.stdout)["recorded_at"]
    plan = run_cli(tmp_path, "calibrate", "plan", "--printer", "k1se", "--json", env_extra=env)
    assert "fit_clearance" not in {i["kind"] for i in json.loads(plan.stdout)["items"]}  # current: not listed
    check = run_cli(tmp_path, "calibrate", "fit", "check", "--printer", "k1se", "--material", "PLA", env_extra=env)
    assert check.stdout.startswith("PARTIAL") and check.returncode == 0

    again = run_cli(tmp_path, "calibrate", "fit", "record", "--printer", "k1se", "--material", "PLA", "--fit",
                    "sliding", "--shape", "round", "--result", "step=5", "--yes", env_extra=env)
    assert again.returncode == 0 and "recorded #2" in again.stdout
    assert fit_resolve(tmp_path, env=env)["clearance_mm"] == 0.25  # #2 superseded #1

    conn = db.open_db(live_api.settings.db_path)
    try:
        events.emit(
            conn, "calibration.invalidated", "k1se/T1",
            {"printer": "k1se", "head": "T1", "trigger": "nozzle_swap", "kinds": None,
             "reason": "nozzle 0.4 → 0.6", "maintenance_id": 1, "nozzle_install_id": 2},
            "maintenance",
        )
    finally:
        conn.close()
    stale = run_cli(tmp_path, "calibrate", "plan", "--printer", "k1se", env_extra=env)
    line = next(ln for ln in stale.stdout.splitlines() if "fit_clearance" in ln)
    assert "WARN" in line and "invalidated" in line and "nozzle_swap" in line


def test_without_a_reachable_3d_api_the_buffer_answers_and_says_so(tmp_path: Path) -> None:
    """3d-api configured but down: record --yes falls back to the buffer, `fits resolve` still sees it."""
    env = {"PRINTOPS_API": "http://127.0.0.1:9", "PRINTOPS_TOKEN": "x", "THREED_FIT_CALIBRATION_API": "on"}
    rec = run_cli(tmp_path, "calibrate", "fit", "record", "--printer", "k1se", "--material", "PLA", "--fit",
                  "sliding", "--shape", "round", "--result", "step=3", "--yes", env_extra=env)
    assert rec.returncode == 0, rec.stderr
    assert "offline buffer" in rec.stdout and "3d settings import --spool" in rec.stdout
    out = run_cli(tmp_path, "fits", "resolve", "--fit", "sliding", "--shape", "round", "--printer", "k1se",
                  "--material", "PLA", "--nozzle", "0.4", "--json", env_extra=env)
    assert json.loads(out.stdout)["clearance_mm"] == 0.15
    assert "fit calibrations come from the offline buffer only" in out.stderr
