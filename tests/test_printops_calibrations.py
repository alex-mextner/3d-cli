"""Calibration catalog, policy, requirement engine, `calibration_current` gate and REST (#167)."""

from __future__ import annotations

import dataclasses
import datetime
import json
import re
import sqlite3
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from fleet_fixtures import write_fleet_with_synthetic_broken_t2
from printops_fakes import FakeSpoolman

from printops import calibrations as cal
from printops import datalint, db, events, policy
from printops.calibration_catalog import CatalogError, load_catalog, parse
from printops.calibration_policy import parse as parse_policy
from printops.client import ApiClient, ApiError
from printops.fleet import load_fleet
from printops.policy import PolicyError
from printops.spoolman import Spoolman, encode_extra, ensure_extra_fields

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def repo_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The committed fleet (T2 synthetically broken), policy and catalog."""
    monkeypatch.setenv("THREED_FLEET", str(write_fleet_with_synthetic_broken_t2(tmp_path / "fleet" / "printers.yaml")))
    monkeypatch.delenv("PRINTOPS_POLICY", raising=False)
    monkeypatch.delenv("PRINTOPS_CALIBRATIONS", raising=False)


@pytest.fixture
def ctx(repo_files: None, printops_db: sqlite3.Connection, fake_spoolman: FakeSpoolman) -> cal.Ctx:
    ensure_extra_fields(fake_spoolman)
    fleet = load_fleet()
    assert fleet is not None
    return cal.Ctx(conn=printops_db, spoolman=fake_spoolman, actor="test", catalog=load_catalog(),
                   policy=policy.section("calibrations"), fleet=fleet)


def spool(sm: Spoolman | None, vendor: str, material: str, name: str, *, lot: str | None = None,
          slot: str | None = None) -> int:
    """A spool of (vendor, name) — the filament is reused when it already exists."""
    assert sm is not None
    vendors = [v for v in sm.find("vendor") if v["name"] == vendor]
    vid = vendors[0]["id"] if vendors else sm.create("vendor", {"name": vendor})["id"]
    fils = [f for f in sm.find("filament") if f["name"] == name and f["vendor"]["id"] == vid]
    fid = fils[0]["id"] if fils else sm.create("filament", {
        "name": name, "vendor_id": vid, "material": material, "density": 1.24, "diameter": 1.75,
        "weight": 1000})["id"]
    extra = {"printops_slot": encode_extra(slot)} if slot else {}
    return int(sm.create("spool", {"filament_id": fid, "lot_nr": lot, "extra": extra})["id"])


def unbind(sm: Spoolman | None, spool_id: int) -> None:
    assert sm is not None
    sm.update("spool", spool_id, {"extra": {"printops_slot": encode_extra("")}})


def by_kind(rs: cal.RequirementSet | cal.GateResult, head: str | None = "T1") -> dict[str, cal.Requirement]:
    return {r.kind.id: r for r in rs.items if r.kind is not None and r.head == head}


def record(ctx: cal.Ctx, kind: str, values: dict[str, Any], **scope: Any) -> cal.Calibration:
    return cal.record(ctx, cal.RunRequest(kind=kind, method=scope.pop("method", "manual"), **scope),
                      {"values": values})


def invalidate(conn: sqlite3.Connection, printer: str, head: str | None, trigger: str,
               kinds: list[str] | None = None) -> None:
    events.emit(conn, "calibration.invalidated", f"{printer}/{head}" if head else printer,
                {"printer": printer, "head": head, "trigger": trigger, "kinds": kinds,
                 "reason": f"test {trigger}", "maintenance_id": None, "nozzle_install_id": None},
                "maintenance")


# -- catalog + policy files -------------------------------------------------------------------


def test_committed_catalog_and_policy_are_valid(repo_files: None) -> None:
    catalog = load_catalog()
    assert {"flow_ratio", "pressure_advance", "temp_tower", "max_volumetric_speed", "retraction",
            "input_shaper", "z_offset", "bed_mesh", "head_offsets", "rotation_distance",
            "belt_tension", "filament_color", "mix_color"} == set(catalog.kinds)
    assert policy.check() == []
    p = policy.section("calibrations")
    assert p.kind_names() <= set(catalog.kinds)
    fleet = load_fleet()
    assert fleet is not None and set(p.printers) <= set(fleet.printers)
    flow = catalog.kinds["flow_ratio"]
    assert flow.lot_sensitive and flow.baseline == "required"
    assert [g.key for g in flow.gcode] == ["filament_flow_ratio"]
    assert catalog.kinds["head_offsets"].applies_to("Snapmaker U1")
    assert not catalog.kinds["head_offsets"].applies_to("Creality K1 SE")


def _kind_body(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "title": "T", "scope": ["head", "filament"], "lot_sensitive": False, "baseline": "required",
        "invalidated_by": [], "methods": ["manual"], "values": {"v": {"type": "number", "min": 0, "max": 1}},
        "apply": ["journal"], "estimate": {"minutes": 1, "grams": 0}, "sources": ["x"],
    }
    body.update(over)
    return {"kinds": {"k": body}}


@pytest.mark.parametrize(("over", "message"), [
    ({"scope": ["shelf"]}, "unknown shelf"),
    ({"scope": ["nozzle", "filament"]}, "nozzle needs head"),
    ({"scope": ["printer", "head", "filament"]}, "head already implies printer"),
    ({"baseline": "assumed"}, "baseline is `required` exactly"),
    ({"scope": ["printer"], "baseline": "required"}, "baseline is `required` exactly"),
    ({"lot_sensitive": True, "scope": ["head"], "baseline": "assumed"}, "lot_sensitive needs filament"),
    ({"invalidated_by": ["sneeze"]}, "unknown sneeze"),
    ({"values": {"v": {"type": "number", "min": 2, "max": 1}}}, "min > max"),
    ({"values": {"v": {"type": "string", "min": 1}}}, "numbers only"),
    ({"values": {"v": {"type": "color", "max": 1}}}, "numbers only"),
    ({"scope": ["head", "mix"], "baseline": "assumed"}, "mix goes with printer only"),
    ({"scope": ["mix"], "baseline": "assumed"}, "mix goes with printer only"),
    ({"gcode": {"v": {"key": "x", "compare": "ne", "tolerance": 0}}}, "compare must be"),
    ({"apply": ["slicer:x"]}, "orca:<key>"),
    ({"colour": "red"}, "unknown key(s) colour"),
])
def test_catalog_schema_rejects(over: dict[str, Any], message: str) -> None:
    with pytest.raises(CatalogError, match=re.escape(message)):
        parse(_kind_body(**over))


def test_catalog_checks_result_values(repo_files: None) -> None:
    catalog = load_catalog()
    assert catalog.kinds["pressure_advance"].check_values({"pressure_advance": 0.035}) == {"pressure_advance": 0.035}
    temp = catalog.kinds["temp_tower"]
    with pytest.raises(CatalogError, match="nozzle_temp_best=240 must be ≤ nozzle_temp_max=230"):
        temp.check_values({"nozzle_temp_min": 200, "nozzle_temp_best": 240, "nozzle_temp_max": 230})
    with pytest.raises(CatalogError, match="flow_ratio=3 is outside 0.5..1.5"):
        catalog.kinds["flow_ratio"].check_values({"flow_ratio": 3})
    with pytest.raises(CatalogError, match="unknown value"):
        catalog.kinds["flow_ratio"].check_values({"flow": 1})
    with pytest.raises(CatalogError, match="required"):
        catalog.kinds["flow_ratio"].check_values({})
    with pytest.raises(CatalogError, match="must be a number"):
        catalog.kinds["flow_ratio"].check_values({"flow_ratio": "ninety"})
    assert catalog.kinds["flow_ratio"].check_values({"flow_ratio": "0.9"}) == {"flow_ratio": 0.9}


def _levels(**over: str) -> dict[str, Any]:
    base = dict.fromkeys(("new_material_type", "new_vendor", "new_filament", "new_nozzle",
                          "invalidated", "expired", "gcode_mismatch"), "block")
    base["new_lot"] = "warn"
    base.update(over)
    return base


def test_policy_precedence_most_specific_wins() -> None:
    p = parse_policy({
        "levels": _levels(),
        "kinds": {"temp_tower": {"new_lot": "off", "new_vendor": "warn"}, "retraction": "warn"},
        "printers": {"p1s": "off", "k1se": {"all": {"new_lot": "block"}, "flow_ratio": "warn"}},
    })
    assert p.level("u1", "flow_ratio", "new_vendor") == "block"  # D6 default
    assert p.level("u1", "flow_ratio", "new_lot") == "warn"  # D6 default
    assert p.level("u1", "temp_tower", "new_lot") == "off"
    assert p.level("u1", "temp_tower", "new_vendor") == "warn"
    assert p.level("u1", "temp_tower", "new_filament") == "block"  # not overridden → levels
    assert p.level("u1", "retraction", "invalidated") == "warn"
    assert p.level("k1se", "flow_ratio", "new_vendor") == "warn"  # printer × kind beats all
    assert p.level("k1se", "pressure_advance", "new_lot") == "block"  # printer all × reason
    assert p.level("k1se", "pressure_advance", "new_vendor") == "block"
    assert p.disabled("p1s", "flow_ratio") and not p.disabled("u1", "flow_ratio")
    assert p.kind_names() == {"temp_tower", "retraction", "flow_ratio"}


@pytest.mark.parametrize(("body", "message"), [
    ({"levels": {}}, "missing reason"),
    ({"levels": _levels(new_lot="maybe")}, "levels.new_lot must be one of"),
    ({"levels": {**_levels(), "new_colour": "warn"}}, "unknown reason"),
    ({"levels": _levels(), "kinds": {"flow_ratio": {"lot": "warn"}}}, "unknown reason 'lot'"),
    ({"levels": _levels(), "printers": {"u1": []}}, "printers.u1 must be"),
    ({"levels": _levels(), "gates": {}}, "unknown key"),
])
def test_policy_schema_rejects(body: dict[str, Any], message: str) -> None:
    with pytest.raises(PolicyError, match=message):
        parse_policy(body)


def test_policy_file_reports_a_broken_calibrations_section(tmp_path: Path) -> None:
    bad = tmp_path / "policy.yaml"
    text = (REPO / "fleet" / "policy.yaml").read_text(encoding="utf-8")
    bad.write_text(text.replace("new_lot: warn", "new_lot: shrug"), encoding="utf-8")
    problems = policy.check(bad)
    assert len(problems) == 1 and problems[0].startswith("calibrations: levels.new_lot")


# -- requirement engine (D6) ------------------------------------------------------------------


def test_new_material_blocks_until_a_calibration_is_recorded(ctx: cal.Ctx) -> None:
    """D6: nothing recorded for PETG on u1 T1 → flow + PA FAIL; recording flow clears it."""
    sid = spool(ctx.spoolman, "Elegoo", "PETG", "PETG Black", lot="L1", slot="u1/T1")
    rs = cal.requirements(ctx, "u1", head="T1")
    k = by_kind(rs)
    assert (k["flow_ratio"].status, k["flow_ratio"].level) == ("new_material_type", "FAIL")
    assert (k["pressure_advance"].status, k["pressure_advance"].level) == ("new_material_type", "FAIL")
    assert k["temp_tower"].level == "WARN"  # policy: advice only
    assert k["max_volumetric_speed"].level == "WARN" and k["retraction"].level == "WARN"
    assert "no PETG recorded yet" in k["flow_ratio"].detail
    printer_level = {r.kind.id: r for r in rs.items if r.kind and r.head is None}
    assert printer_level["input_shaper"].status == "assumed" and printer_level["input_shaper"].level == "PASS"
    assert "head_offsets" in printer_level  # U1 only
    assert rs.to_dict()["overall"] == "FAIL"

    rec = record(ctx, "flow_ratio", {"flow_ratio": 0.95}, printer="u1", head="T1")
    assert rec.filament is not None and rec.filament.spool_id == sid and rec.filament.lot_nr == "L1"
    assert rec.nozzle == cal.Nozzle(None, 0.4, "stainless", "std")  # fleet/printers.yaml
    k = by_kind(cal.requirements(ctx, "u1", head="T1"))
    assert (k["flow_ratio"].status, k["flow_ratio"].level) == ("current", "PASS")
    assert k["pressure_advance"].level == "FAIL"
    shown = cal.requirements(ctx, "u1", head="T1").to_dict()
    assert "flow_ratio" not in {i["kind"] for i in shown["items"]}  # PASS hidden unless all
    assert shown["estimate"]["minutes"] > 0


def test_new_lot_warns_vendor_and_filament_changes_block(ctx: cal.Ctx) -> None:
    sm = ctx.spoolman
    first = spool(sm, "Elegoo", "PLA", "PLA Grey", lot="L1", slot="u1/T1")
    record(ctx, "flow_ratio", {"flow_ratio": 0.96}, printer="u1", head="T1")
    record(ctx, "pressure_advance", {"pressure_advance": 0.03}, printer="u1", head="T1")
    unbind(sm, first)

    second = spool(sm, "Elegoo", "PLA", "PLA Grey", lot="L2", slot="u1/T1")
    k = by_kind(cal.requirements(ctx, "u1", head="T1"))
    assert (k["flow_ratio"].status, k["flow_ratio"].level) == ("new_lot", "WARN")
    assert "lot L2 is new" in k["flow_ratio"].detail and "lot L1" in k["flow_ratio"].detail
    assert k["pressure_advance"].status == "current"  # PA is not lot-sensitive
    unbind(sm, second)

    spool(sm, "Elegoo", "PLA", "PLA Matte Green", slot="u1/T1")
    k = by_kind(cal.requirements(ctx, "u1", head="T1"))
    assert (k["flow_ratio"].status, k["flow_ratio"].level) == ("new_filament", "FAIL")
    assert (k["pressure_advance"].status, k["pressure_advance"].level) == ("new_filament", "FAIL")

    k = by_kind(cal.requirements(ctx, "u1", head="T1",
                                 spool_id=spool(sm, "Bambu", "PLA", "PLA Basic")))
    assert (k["flow_ratio"].status, k["flow_ratio"].level) == ("new_vendor", "FAIL")


def test_records_are_per_head_and_unusable_heads_are_skipped(ctx: cal.Ctx) -> None:
    spool(ctx.spoolman, "Elegoo", "PLA", "PLA Grey", slot="u1/T1")
    spool(ctx.spoolman, "Elegoo", "PLA", "PLA Grey", slot="u1/T3")
    record(ctx, "flow_ratio", {"flow_ratio": 0.96}, printer="u1", head="T1")
    rs = cal.requirements(ctx, "u1")
    assert by_kind(rs, "T1")["flow_ratio"].status == "current"
    assert by_kind(rs, "T3")["flow_ratio"].status == "new_material_type"  # records are per head
    assert by_kind(rs, "T4")["flow_ratio"].status == "no_spool"
    assert by_kind(rs, "T4")["flow_ratio"].level == "UNVERIFIED"
    assert "no spool bound to u1/T4" in by_kind(rs, "T4")["flow_ratio"].detail
    assert [s["head"] for s in rs.skipped] == ["T2"]
    assert "does not work" in rs.skipped[0]["reason"]


def test_invalidation_events_make_records_stale(ctx: cal.Ctx) -> None:
    spool(ctx.spoolman, "Elegoo", "PLA", "PLA Grey", slot="u1/T1")
    spool(ctx.spoolman, "Elegoo", "PLA", "PLA Grey", slot="u1/T3")
    for head in ("T1", "T3"):
        record(ctx, "flow_ratio", {"flow_ratio": 0.96}, printer="u1", head=head)
        record(ctx, "pressure_advance", {"pressure_advance": 0.03}, printer="u1", head=head)
        record(ctx, "retraction", {"retraction_length": 0.8}, printer="u1", head=head)

    invalidate(ctx.conn, "u1", "T1", "ptfe")  # PTFE: PA + retraction, not flow
    rs = cal.requirements(ctx, "u1")
    t1, t3 = by_kind(rs, "T1"), by_kind(rs, "T3")
    assert t1["flow_ratio"].status == "current"
    assert (t1["pressure_advance"].status, t1["pressure_advance"].level) == ("invalidated", "FAIL")
    assert "predates ptfe" in t1["pressure_advance"].detail and "test ptfe" in t1["pressure_advance"].detail
    assert t1["pressure_advance"].invalidation is not None
    assert (t1["retraction"].status, t1["retraction"].level) == ("invalidated", "WARN")  # policy: warn
    assert t3["pressure_advance"].status == "current"  # another head

    invalidate(ctx.conn, "u1", None, "repair", kinds=["flow_ratio"])  # printer-wide, explicit kinds
    rs = cal.requirements(ctx, "u1")
    assert by_kind(rs, "T3")["flow_ratio"].status == "invalidated"
    assert by_kind(rs, "T3")["pressure_advance"].status == "current"

    record(ctx, "pressure_advance", {"pressure_advance": 0.032}, printer="u1", head="T1")
    assert by_kind(cal.requirements(ctx, "u1", head="T1"))["pressure_advance"].status == "current"


def test_assumed_kinds_need_a_record_only_after_maintenance(ctx: cal.Ctx) -> None:
    def shaper() -> cal.Requirement:
        rs = cal.requirements(ctx, "k1se")
        return next(r for r in rs.items if r.kind is not None and r.kind.id == "input_shaper")

    assert shaper().status == "assumed"
    invalidate(ctx.conn, "k1se", None, "belts")
    assert (shaper().status, shaper().level) == ("invalidated", "FAIL")
    assert "never recorded since belts" in shaper().detail
    record(ctx, "input_shaper", {"shaper_type_x": "mzv", "shaper_freq_x": 54.2,
                                 "shaper_type_y": "ei", "shaper_freq_y": 41.0},
           printer="k1se", method="k1_auto")
    assert shaper().status == "current"
    assert "head_offsets" not in {r.kind.id for r in cal.requirements(ctx, "k1se").items if r.kind}


def test_expiry_and_supersede(ctx: cal.Ctx) -> None:
    spool(ctx.spoolman, "Elegoo", "PLA", "PLA Grey", slot="u1/T1")
    old = record(ctx, "flow_ratio", {"flow_ratio": 0.96}, printer="u1", head="T1")
    new = cal.record(ctx, cal.RunRequest(kind="flow_ratio", method="manual", printer="u1", head="T1"),
                     {"values": {"flow_ratio": 0.97}, "valid_until": "2026-01-01"})
    assert cal.get(ctx.conn, old.id).superseded_by == new.id
    assert new.valid_until == "2026-01-01T00:00:00.000Z"
    ev = [json.loads(r["payload"]) for r in ctx.conn.execute(
        "SELECT payload FROM events WHERE kind = 'calibration.recorded' ORDER BY id")]
    assert ev[-1]["superseded"] == [old.id]
    k = by_kind(cal.requirements(ctx, "u1", head="T1", now="2026-10-15T00:00:00.000Z"))
    assert (k["flow_ratio"].status, k["flow_ratio"].level) == ("expired", "WARN")
    assert k["flow_ratio"].record is not None and k["flow_ratio"].record.id == new.id


def test_nozzle_swap_from_maintenance_install_table(ctx: cal.Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    """#166's printops.maintenance.current_nozzle_install decides the nozzle when present."""
    installs = {"id": 1, "diameter_mm": 0.4, "material": "hardened", "type": "std", "installed_at": None}
    fake = types.ModuleType("printops.maintenance")
    setattr(fake, "current_nozzle_install", lambda conn, printer, head: dict(installs))  # noqa: B010 — module attribute
    monkeypatch.setitem(sys.modules, "printops.maintenance", fake)
    spool(ctx.spoolman, "Creality", "PLA", "Hyper PLA", slot="k1se/T1")
    rec = record(ctx, "flow_ratio", {"flow_ratio": 0.98}, printer="k1se", head="T1")
    assert rec.nozzle is not None and rec.nozzle.install_id == 1
    installs.update(id=2, diameter_mm=0.6)
    k = by_kind(cal.requirements(ctx, "k1se", head="T1"))
    assert (k["flow_ratio"].status, k["flow_ratio"].level) == ("new_nozzle", "FAIL")
    assert "0.4 mm hardened std (install #1)" in k["flow_ratio"].detail
    assert "0.6 mm hardened std (install #2)" in k["flow_ratio"].detail


def test_offline_printers_are_off_by_policy(ctx: cal.Ctx) -> None:
    rs = cal.requirements(ctx, "p1s")
    assert rs.items == []  # D7 printers: `p1s: off`


def test_run_requests_are_checked(ctx: cal.Ctx) -> None:
    def err(**kw: Any) -> cal.CalibrationError:
        with pytest.raises(cal.CalibrationError) as e:
            cal.start_run(ctx, cal.RunRequest(**{"kind": "flow_ratio", "method": "manual", **kw}))
        return e.value

    assert err(kind="flow").status == 422
    assert err(printer="u1", head="T1", method="eyeball").status == 422
    assert "pass head" in err(printer="u1").message
    assert err(printer="u1", head="T2").status == 409  # broken head
    assert err(printer="mk4", head="T1").status == 404
    assert "spool_id or filament_id" in err(printer="u1", head="T1").message
    assert ctx.spoolman is not None
    fil = ctx.spoolman.create("filament", {"name": "X", "material": "pla", "density": 1.2, "diameter": 1.75})
    run = cal.start_run(ctx, cal.RunRequest(kind="temp_tower", method="temp_tower", filament_id=int(fil["id"]),
                                            lot_nr="B7"))
    assert run.status == "planned" and run.printer is None and run.filament is not None
    assert run.filament.material == "PLA" and run.filament.lot_nr == "B7"
    with pytest.raises(cal.CalibrationError, match="nozzle_temp_min is required"):
        cal.record_result(ctx, run.id, {"values": {}})
    done = cal.record_result(ctx, run.id, {"values": {"nozzle_temp_min": 200, "nozzle_temp_best": 210,
                                                      "nozzle_temp_max": 225}})
    assert done.status == "recorded"
    with pytest.raises(cal.CalibrationError) as again:
        cal.record_result(ctx, run.id, {"values": {"nozzle_temp_min": 200, "nozzle_temp_best": 210,
                                                   "nozzle_temp_max": 225}})
    assert again.value.status == 409
    assert cal.parse_accept(["calibration:flow_ratio", "pressure_advance"], "why", ctx.catalog) == {
        "flow_ratio": "why", "pressure_advance": "why"}
    with pytest.raises(cal.CalibrationError, match="needs a reason"):
        cal.parse_accept(["calibration:flow_ratio"], " ", ctx.catalog)


# -- gate -------------------------------------------------------------------------------------


def footer(**over: str) -> dict[str, str]:
    base = {"filament_settings_id": '"Elegoo PLA Grey"', "filament_type": "PLA",
            "filament_flow_ratio": "0.96", "pressure_advance": "0.03", "enable_pressure_advance": "1",
            "filament_max_volumetric_speed": "15"}
    base.update(over)
    return base


def test_gate_compares_footer_values_with_the_records(ctx: cal.Ctx) -> None:
    spool(ctx.spoolman, "Elegoo", "PLA", "PLA Grey", lot="L1", slot="u1/T1")
    record(ctx, "flow_ratio", {"flow_ratio": 0.96}, printer="u1", head="T1")
    pa = record(ctx, "pressure_advance", {"pressure_advance": 0.03}, printer="u1", head="T1")
    record(ctx, "max_volumetric_speed", {"max_volumetric_speed": 20}, printer="u1", head="T1")
    record(ctx, "retraction", {"retraction_length": 0.8}, printer="u1", head="T1")
    record(ctx, "temp_tower", {"nozzle_temp_min": 200, "nozzle_temp_best": 215, "nozzle_temp_max": 230},
           printer="u1", head="T1", method="temp_tower")

    ok = cal.check_gcode(ctx, "u1", footer(), [0])
    assert ok.overall == "PASS", [r.detail for r in ok.items if r.level != "PASS"]

    bad = cal.check_gcode(ctx, "u1", footer(filament_flow_ratio="0.98"), [0])
    k = by_kind(bad)
    assert (k["flow_ratio"].status, k["flow_ratio"].level) == ("gcode_mismatch", "FAIL")
    assert "G-code filament_flow_ratio 0.98 ≠ recorded 0.96" in k["flow_ratio"].detail
    assert bad.overall == "FAIL"

    fast = by_kind(cal.check_gcode(ctx, "u1", footer(filament_max_volumetric_speed="25"), [0]))
    assert fast["max_volumetric_speed"].level == "WARN"  # le: 25 > 20; policy warns for MVS
    assert by_kind(cal.check_gcode(ctx, "u1", footer(filament_max_volumetric_speed="12"), [0]))[
        "max_volumetric_speed"].level == "PASS"

    firmware = by_kind(cal.check_gcode(ctx, "u1", footer(enable_pressure_advance="0",
                                                             pressure_advance="0.02"), [0]))
    assert firmware["pressure_advance"].level == "WARN"
    assert "never applied to the printer" in firmware["pressure_advance"].detail
    ctx.conn.execute("UPDATE calibration SET applied_to = ? WHERE id = ?",
                     (json.dumps([{"target": "klipper:SET_PRESSURE_ADVANCE", "at": "2026-10-15T00:00:00Z"}]),
                      pa.id))
    firmware = by_kind(cal.check_gcode(ctx, "u1", footer(enable_pressure_advance="0"), [0]))
    assert firmware["pressure_advance"].level == "PASS"
    assert "firmware holds pressure_advance" in firmware["pressure_advance"].detail


def test_gate_multi_tool_single_footer_value_is_unverified_for_later_tools(ctx: cal.Ctx) -> None:
    """Snapmaker Orca writes one flow ratio for four filaments (babushkin-dom u1_mix_tester)."""
    for head in ("T1", "T3"):
        spool(ctx.spoolman, "Snapmaker", "PLA", "PLA Basic", slot=f"u1/{head}")
        record(ctx, "flow_ratio", {"flow_ratio": 0.98}, printer="u1", head=head)
        record(ctx, "pressure_advance", {"pressure_advance": 0.02}, printer="u1", head=head)
    f = footer(filament_settings_id='"A";"B";"C";"D"', filament_type="PLA;PLA;PLA;PLA",
               filament_flow_ratio="0.98", pressure_advance="0.02")
    res = cal.check_gcode(ctx, "u1", f, [0, 1, 2])
    t1, t3 = by_kind(res, "T1"), by_kind(res, "T3")
    assert t1["flow_ratio"].level == "PASS"
    assert t3["flow_ratio"].level == "UNVERIFIED"
    assert "one value for 4 filaments" in t3["flow_ratio"].detail
    no_head = [r for r in res.items if r.status == "no_head"]
    assert len(no_head) == 1 and no_head[0].tool == 1 and "T2 is not usable" in no_head[0].detail
    per_tool = cal.check_gcode(ctx, "u1", footer(filament_settings_id='"A";"B";"C";"D"',
                                                 filament_flow_ratio="0.98,0.9,0.95,0.9",
                                                 pressure_advance="0.02,0.02,0.02,0.02"), [0, 2])
    assert by_kind(per_tool, "T3")["flow_ratio"].level == "FAIL"
    assert "0.95 ≠ recorded 0.98" in by_kind(per_tool, "T3")["flow_ratio"].detail


def test_gate_accept_logs_and_downgrades(ctx: cal.Ctx) -> None:
    spool(ctx.spoolman, "Elegoo", "PETG", "PETG Black", slot="u1/T1")
    res = cal.check_gcode(ctx, "u1", footer(), [0], accept={"flow_ratio": "test cube"},
                          gcode_sha="a" * 64, file="cube.gcode")
    k = by_kind(res)
    assert (k["flow_ratio"].level, k["flow_ratio"].accepted) == ("WARN", "test cube")
    assert k["pressure_advance"].level == "FAIL"
    rows = ctx.conn.execute("SELECT subject, payload, actor FROM events WHERE kind = 'calibration.accepted'").fetchall()
    assert len(rows) == 1 and rows[0]["subject"] == "u1/T1" and rows[0]["actor"] == "test"
    payload = json.loads(rows[0]["payload"])
    assert payload["kind"] == "flow_ratio" and payload["reason"] == "test cube"
    assert payload["status"] == "new_material_type" and payload["gcode_sha"] == "a" * 64


# -- REST + lint ------------------------------------------------------------------------------


@pytest.fixture
def po(repo_files: None, api_client: ApiClient) -> ApiClient:
    return api_client


def test_rest_run_result_required_and_check(po: ApiClient, fake_spoolman: FakeSpoolman,
                                            printops_db: sqlite3.Connection) -> None:
    spool(fake_spoolman, "Elegoo", "PLA", "PLA Grey", lot="L1", slot="u1/T1")
    need = po.get("/v1/calibrations/required", printer="u1", slot="T1")
    kinds = {i["kind"]: i for i in need["items"]}
    assert kinds["flow_ratio"]["level"] == "FAIL" and kinds["flow_ratio"]["status"] == "new_material_type"
    assert need["overall"] == "FAIL" and need["skipped"] == []

    run = po.post("/v1/calibrations/runs", {"kind": "pressure_advance", "method": "pa_pattern", "printer": "u1",
                                            "head": "T1", "params": {"start": 0, "end": 0.08, "step": 0.005},
                                            "gcode_sha": "b" * 64})
    assert run["status"] == "planned" and run["params"]["step"] == 0.005
    with pytest.raises(ApiError) as bad:
        po.post(f"/v1/calibrations/{run['id']}/result", {"values": {"pressure_advance": 9}})
    assert bad.value.status == 422
    done = po.post(f"/v1/calibrations/{run['id']}/result", {"values": {"pressure_advance": 0.035},
                                                            "evidence": ["https://cam/1.jpg"]})
    assert done["status"] == "recorded" and done["values"] == {"pressure_advance": 0.035}
    flow = po.post("/v1/calibrations", {"kind": "flow_ratio", "printer": "u1", "head": "T1",
                                        "values": {"flow_ratio": 0.96}})
    assert flow["filament"]["lot_nr"] == "L1"
    assert po.get(f"/v1/calibrations/{flow['id']}")["id"] == flow["id"]
    assert [c["kind"] for c in po.get("/v1/calibrations", printer="u1", current="true")["calibrations"]] == [
        "flow_ratio", "pressure_advance"]

    kinds = {i["kind"]: i for i in po.get("/v1/calibrations/required", printer="u1", head="T1",
                                         all="true")["items"]}
    assert kinds["flow_ratio"]["level"] == "PASS" and kinds["pressure_advance"]["level"] == "PASS"

    body = {"printer": "u1", "footer": footer(filament_flow_ratio="0.96", pressure_advance="0.035"),
            "used_tools": [0]}
    res = po.post("/v1/calibrations/check", body)
    items = {i["kind"]: i for i in res["items"] if i["head"] == "T1"}
    assert items["flow_ratio"]["level"] == "PASS" and items["pressure_advance"]["level"] == "PASS"
    res = po.post("/v1/calibrations/check", {**body, "footer": footer(pressure_advance="0.02")})
    assert res["overall"] == "FAIL"

    kinds_logged = [r["kind"] for r in printops_db.execute("SELECT kind FROM events WHERE kind LIKE 'calibration.%'")]
    assert kinds_logged == ["calibration.planned", "calibration.recorded", "calibration.recorded"]


def test_rest_errors_and_scopes(po: ApiClient, printops_client: Any, printops_tokens: dict[str, str]) -> None:
    def status(method: str, path: str, token: str = "rw", **kw: Any) -> int:
        r = printops_client.request(method, path, headers={"Authorization": f"Bearer {printops_tokens[token]}"}, **kw)
        return int(r.status_code)

    assert status("GET", "/v1/calibrations/999") == 404
    assert status("GET", "/v1/calibrations/required?printer=mk4") == 404
    assert status("GET", "/v1/calibrations/required?printer=u1&head=T1&slot=T3") == 422
    assert status("POST", "/v1/calibrations/runs", json={"kind": "nope", "printer": "u1"}) == 422
    assert status("POST", "/v1/calibrations/runs", json={"kind": "flow_ratio", "values": {}}) == 422
    assert status("POST", "/v1/calibrations", "read", json={"kind": "flow_ratio"}) == 403
    check = {"printer": "u1", "footer": footer(), "used_tools": [0], "accept": ["calibration:flow_ratio"],
             "reason": "test"}
    assert status("POST", "/v1/calibrations/check", "read", json=check) == 403
    assert status("POST", "/v1/calibrations/check", "read", json={**check, "accept": []}) == 200
    assert status("POST", "/v1/calibrations/check", json={**check, "reason": None}) == 422
    assert status("POST", "/v1/calibrations/check", json={**check, "used_tools": []}) == 422
    assert status("POST", "/v1/calibrations/check", json={**check, "footer": {"a": 1}}) == 422


def test_rest_503_when_catalog_is_broken(po: ApiClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    broken = tmp_path / "calibrations.yaml"
    broken.write_text("kinds: {}\n", encoding="utf-8")
    monkeypatch.setenv("PRINTOPS_CALIBRATIONS", str(broken))
    with pytest.raises(ApiError) as e:
        po.get("/v1/calibrations/required", printer="u1")
    assert e.value.status == 503 and "calibration engine unusable" in e.value.message


def test_lint_rules(ctx: cal.Ctx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from printops import lint_calibrations

    assert ctx.spoolman is not None
    lctx = datalint.LintContext(conn=ctx.conn, spoolman=ctx.spoolman, spools=[],
                                now=datetime.datetime.now(datetime.timezone.utc))
    assert lint_calibrations.lint(lctx) == []
    old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")
    with db.transaction(ctx.conn):
        ctx.conn.execute("INSERT INTO calibration (kind, status, method, created_at, created_by) "
                         "VALUES ('belt_tension', 'planned', 'frequency_meter', ?, 't')", (old,))
        ctx.conn.execute("INSERT INTO calibration (kind, status, method, created_at, created_by) "
                         "VALUES ('vibes', 'abandoned', 'manual', ?, 't')", (old,))
    rules = sorted(f.rule for f in lint_calibrations.lint(lctx))
    assert rules == ["calibrations/stale-run", "calibrations/unknown-kind"]

    pol = tmp_path / "policy.yaml"
    pol.write_text((REPO / "fleet" / "policy.yaml").read_text(encoding="utf-8").replace(
        "    retraction: warn", "    retracton: warn\n    retraction: warn").replace(
        "    p1s: off", "    p1s: off\n    mk4: off"), encoding="utf-8")
    monkeypatch.setenv("PRINTOPS_POLICY", str(pol))
    messages = [f.message for f in lint_calibrations.lint(lctx) if f.rule == "calibrations/policy"]
    assert any("unknown kind 'retracton'" in m for m in messages)
    assert any("unknown printer 'mk4'" in m for m in messages)

    monkeypatch.setenv("PRINTOPS_CALIBRATIONS", str(tmp_path / "missing.yaml"))
    assert [f.rule for f in lint_calibrations.lint(lctx)] == ["calibrations/catalog"]


def test_non_lot_sensitive_kind_supersedes_across_lots(ctx: cal.Ctx) -> None:
    """PA is not lot-sensitive: a new lot of the same filament supersedes the old record."""
    sm = ctx.spoolman
    first = spool(sm, "Elegoo", "PLA", "PLA Grey", lot="L1", slot="u1/T1")
    old = record(ctx, "pressure_advance", {"pressure_advance": 0.03}, printer="u1", head="T1")
    old_flow = record(ctx, "flow_ratio", {"flow_ratio": 0.96}, printer="u1", head="T1")
    unbind(sm, first)
    spool(sm, "Elegoo", "PLA", "PLA Grey", lot="L2", slot="u1/T1")
    new = record(ctx, "pressure_advance", {"pressure_advance": 0.035}, printer="u1", head="T1")
    record(ctx, "flow_ratio", {"flow_ratio": 0.95}, printer="u1", head="T1")
    assert cal.get(ctx.conn, old.id).superseded_by == new.id
    assert cal.get(ctx.conn, old_flow.id).superseded_by is None  # flow is lot-sensitive: both stay
    current = cal.list_calibrations(ctx.conn, kind="pressure_advance", current=True)
    assert [c.id for c in current] == [new.id]


def test_values_must_be_finite_numbers_and_numeric_text_is_accepted(ctx: cal.Ctx) -> None:
    spool(ctx.spoolman, "Elegoo", "PLA", "PLA Grey", slot="u1/T1")
    for bad in ("nan", "inf", float("nan"), float("-inf"), 10**400):
        with pytest.raises(cal.CalibrationError, match="finite number"):
            record(ctx, "flow_ratio", {"flow_ratio": bad}, printer="u1", head="T1")
    rec = record(ctx, "flow_ratio", {"flow_ratio": "0.97"}, printer="u1", head="T1")  # CLI sends text
    assert rec.values == {"flow_ratio": 0.97}
    with pytest.raises(CatalogError, match="finite numbers"):
        parse(_kind_body(values={"v": {"type": "number", "min": 0, "max": float("inf")}}))
    with pytest.raises(CatalogError, match="finite numbers"):
        parse(_kind_body(values={"v": {"type": "number", "min": 0, "max": 10**400}}))


def test_invalidation_in_the_same_millisecond_still_counts(ctx: cal.Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    spool(ctx.spoolman, "Elegoo", "PLA", "PLA Grey", slot="u1/T1")
    monkeypatch.setattr(db, "utc_now", lambda: "2026-10-16T10:00:00.000Z")
    record(ctx, "pressure_advance", {"pressure_advance": 0.03}, printer="u1", head="T1")
    invalidate(ctx.conn, "u1", "T1", "ptfe")
    k = by_kind(cal.requirements(ctx, "u1", head="T1", now="2026-10-16T10:00:00.000Z"))
    assert k["pressure_advance"].status == "invalidated"


def test_plate_scoped_kind_needs_the_plate_when_the_printer_has_several(ctx: cal.Ctx) -> None:
    k1 = ctx.fleet.printers["k1se"]
    fleet = dataclasses.replace(ctx.fleet, printers={**ctx.fleet.printers,
                                                     "k1se": dataclasses.replace(k1, plates=("smooth_pei", "textured_pei"))})
    c2 = dataclasses.replace(ctx, fleet=fleet)
    record(c2, "z_offset", {"z_offset": -0.05}, printer="k1se", plate="smooth_pei", method="manual")
    z = next(r for r in cal.requirements(c2, "k1se").items if r.kind is not None and r.kind.id == "z_offset")
    assert (z.status, z.level) == ("no_plate", "UNVERIFIED")
    z = next(r for r in cal.requirements(c2, "k1se", plate="smooth_pei").items
             if r.kind is not None and r.kind.id == "z_offset")
    assert z.status == "current"
    z = next(r for r in cal.requirements(c2, "k1se", plate="textured_pei").items
             if r.kind is not None and r.kind.id == "z_offset")
    assert z.status != "current"  # the smooth-PEI record says nothing about textured PEI


# -- colour kinds (#412, plan §20.4) ------------------------------------------------------------


def test_colour_values_are_normalised_hexes(repo_files: None) -> None:
    catalog = load_catalog()
    fc = catalog.kinds["filament_color"]
    assert fc.check_values({"hex": "c8902a", "spoolman_hex": "#F6C700", "delta_e": "11.5"}) == {
        "hex": "#C8902A", "spoolman_hex": "#F6C700", "delta_e": 11.5}
    with pytest.raises(CatalogError, match="hex must be a colour #RRGGBB"):
        fc.check_values({"hex": "yellow"})
    for typo in ("#C8902A0", "C8902AFF", "f00", "##C8902A"):  # device forms are not typed colours
        with pytest.raises(CatalogError, match="hex must be a colour #RRGGBB"):
            fc.check_values({"hex": typo})
    assert catalog.kinds["mix_color"].mix_scoped and not fc.mix_scoped
    assert policy.section("calibrations").disabled("u1", "filament_color")


def test_mix_color_records_are_keyed_by_the_canonical_mix(ctx: cal.Ctx) -> None:
    """431 and 314 on the same filaments are one mix: the second record supersedes the first;
    another mix and another printer keep their own records."""
    first = record(ctx, "mix_color", {"hex": "#b5654f"}, printer="u1", mix="h0.12:f12.f7.f3")
    assert first.mix == "h0.12:f3.f12.f7" and first.values == {"hex": "#B5654F"}
    assert first.head is None and first.filament is None and first.nozzle is None
    other = record(ctx, "mix_color", {"hex": "#9A9A9A"}, printer="u1", mix="h0.12:f1.f4")
    k1 = record(ctx, "mix_color", {"hex": "#B0604A"}, printer="k1se", mix="h0.12:f3.f12.f7")
    again = record(ctx, "mix_color", {"hex": "#B26250", "predicted_hex": "#A86A55",
                                      "delta_e_predicted": 4.2}, printer="u1", mix="h0.12:f7.f3.f12")
    assert cal.get(ctx.conn, first.id).superseded_by == again.id
    assert cal.get(ctx.conn, other.id).superseded_by is None
    assert cal.get(ctx.conn, k1.id).superseded_by is None
    current = cal.list_calibrations(ctx.conn, kind="mix_color", mix="h0.12:f12.f7.f3", current=True)
    assert [c.id for c in current] == [again.id, k1.id]  # newest first
    assert again.to_dict()["mix"] == "h0.12:f3.f12.f7"
    ev = json.loads(ctx.conn.execute(
        "SELECT payload FROM events WHERE kind = 'calibration.recorded' ORDER BY id DESC").fetchone()[0])
    assert ev["mix"] == "h0.12:f3.f12.f7" and ev["superseded"] == [first.id]


def test_mix_scope_is_required_exactly_for_mix_kinds(ctx: cal.Ctx) -> None:
    def err(**kw: Any) -> cal.CalibrationError:
        with pytest.raises(cal.CalibrationError) as e:
            cal.record(ctx, cal.RunRequest(**{"method": "manual", **kw}), {"values": {"hex": "#FFFFFF"}})
        return e.value

    assert "pass mix" in err(kind="mix_color", printer="u1").message
    assert "pass printer" in err(kind="mix_color", mix="h0.12:f1.f4").message
    spool(ctx.spoolman, "Elegoo", "PLA", "PLA Grey", slot="u1/T1")
    assert "drop mix" in err(kind="filament_color", printer="u1", head="T1", mix="h0.12:f1.f4").message
    # the key names the filaments: a head or spool on a mix record would be stored half
    assert "drop head" in err(kind="mix_color", printer="u1", head="T1", mix="h0.12:f1.f4").message
    assert "drop head" in err(kind="mix_color", printer="u1", spool_id=1, mix="h0.12:f1.f4").message
    for bad in ("431", "h0.12:f1.x4", "h2:f1.f4", "h0.12:lz:f1=50,f4=40"):
        with pytest.raises(cal.CalibrationError) as e:
            cal.RunRequest.from_dict({"kind": "mix_color", "printer": "u1", "mix": bad})
        assert e.value.status == 422, bad
    with pytest.raises(cal.CalibrationError, match="not a mix key"):
        cal.list_calibrations(ctx.conn, mix="431")


def test_colour_kinds_never_enter_the_requirement_lists(ctx: cal.Ctx) -> None:
    """filament_color is off by policy, mix-scoped kinds are skipped by the engine: a new spool
    with no measured colour and a printer with no measured mix add nothing to plan or tasks."""
    spool(ctx.spoolman, "Elegoo", "PLA", "PLA Grey", lot="L1", slot="u1/T1")
    record(ctx, "mix_color", {"hex": "#808080"}, printer="u1", mix="h0.12:f1.f4")
    kinds = {r.kind.id for r in cal.requirements(ctx, "u1").items if r.kind is not None}
    assert "flow_ratio" in kinds and "input_shaper" in kinds
    assert not kinds & {"filament_color", "mix_color"}


def test_filament_color_record_is_per_filament_and_lot(ctx: cal.Ctx) -> None:
    sid = spool(ctx.spoolman, "Elegoo", "PLA", "Snapdragon Yellow", lot="L1", slot="u1/T4")
    rec = record(ctx, "filament_color", {"hex": "#E8B400", "spoolman_hex": "#F6C700"},
                 spool_id=sid, method="mix_tester_photo")
    assert rec.printer is None and rec.head is None and rec.mix is None
    assert rec.filament is not None and rec.filament.lot_nr == "L1"
    lot2 = spool(ctx.spoolman, "Elegoo", "PLA", "Snapdragon Yellow", lot="L2")
    other_lot = record(ctx, "filament_color", {"hex": "#EDBA10"}, spool_id=lot2)
    assert cal.get(ctx.conn, rec.id).superseded_by is None  # lot-sensitive: lots keep their own colour
    assert other_lot.filament is not None and other_lot.filament.lot_nr == "L2"


def test_rest_records_and_filters_mix_color(po: ApiClient, printops_client: Any,
                                            printops_tokens: dict[str, str]) -> None:
    rec = po.post("/v1/calibrations", {"kind": "mix_color", "printer": "u1", "mix": "h0.12:f4.f3.f1",
                                       "method": "mix_tester_photo", "values": {"hex": "#b5654f"}})
    assert rec["mix"] == "h0.12:f1.f4.f3" and rec["values"] == {"hex": "#B5654F"}
    po.post("/v1/calibrations", {"kind": "mix_color", "printer": "u1", "mix": "h0.12:f1.f4",
                                 "values": {"hex": "#808080"}})
    listed = po.get("/v1/calibrations", kind="mix_color", mix="h0.12:f3.f1.f4")["calibrations"]
    assert [c["id"] for c in listed] == [rec["id"]]
    headers = {"Authorization": f"Bearer {printops_tokens['rw']}"}
    bad = printops_client.post("/v1/calibrations", headers=headers,
                               json={"kind": "mix_color", "printer": "u1", "mix": "431", "values": {"hex": "#FFFFFF"}})
    assert bad.status_code == 422 and "not a mix key" in bad.text
    assert printops_client.get("/v1/calibrations?mix=431", headers=headers).status_code == 422
