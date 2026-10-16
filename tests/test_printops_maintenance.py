"""PrintOps maintenance (#166): fleet seeding (nozzles, plates, the U1 T2 fault), nozzle swaps and
the calibration.invalidated contract with #167, the maintenance log, faults, maintenance rules
(schema + due evaluation + maintenance.due), Moonraker history counters, lint, and the routes."""
from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from printops import events
from printops import maintenance as mt
from printops import maintenance_rules as mr
from printops.api import routes_maintenance
from printops.client import ApiClient, ApiError
from printops.counters import FleetCounters, MoonrakerHistory, Usage
from printops.datalint import LintContext
from printops.db import open_db
from printops.fleet import load_fleet
from printops.lint_maintenance import lint

REPO = Path(__file__).resolve().parents[1]

FLEET = """\
u1:
  model: Snapmaker U1
  owner: own
  access: offline
  heads:
    - {label: T1, index: 0, nozzle: {diameter_mm: 0.4, material: stainless, type: std}}
    - label: T2
      index: 1
      nozzle: {diameter_mm: 0.4, material: stainless, type: std}
      usable: false
      reason: "head T2 does not work (gen/queue.json)"
    - {label: T3, index: 2, nozzle: {diameter_mm: 0.4, material: stainless, type: std}}
  plates: [textured_pei]
k1se:
  model: Creality K1 SE
  owner: own
  access: offline
  heads:
    - {label: T1, index: 0, nozzle: {diameter_mm: 0.4, material: hardened, type: std}}
"""

RULES = """\
check_every_min: 30
invalidates:
  - {component: ptfe, when: [replace], trigger: ptfe}
  - {component: belts, when: [replace, tighten], trigger: belts}
rules:
  - id: u1-rails
    title: lube the rails
    printers: [u1]
    component: kinematics
    task: lube
    every: {days: 30, filament_m: 1000}
    source: vendor wiki
  - id: u1-hours
    title: inspect after hours
    printers: [u1]
    component: printer
    task: inspect
    every: {print_hours: 100}
    source: vendor wiki
  - id: u1-wiper
    title: clean each head's wiper
    printers: [u1]
    component: head
    task: clean
    scope: head
    every: {print_hours: 50}
    source: vendor wiki
"""


class FakeCounters:
    """CounterProvider with fixed usage per (printer, since); records the calls."""

    def __init__(self, usage: dict[str | None, Usage] | None = None) -> None:
        self.by_since = usage or {}
        self.calls: list[tuple[str, str | None, str | None]] = []

    def usage(self, printer: str, head: str | None, since: str | None) -> Usage:
        self.calls.append((printer, head, since))
        if head is not None:
            return Usage(source="fake")
        return self.by_since.get(since, self.by_since.get("*", Usage(source="fake")))


@pytest.fixture
def fleet_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    f = tmp_path / "printers.yaml"
    f.write_text(FLEET, encoding="utf-8")
    monkeypatch.setenv("THREED_FLEET", str(f))
    return f


@pytest.fixture
def rules_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    f = tmp_path / "maintenance-rules.yaml"
    f.write_text(RULES, encoding="utf-8")
    monkeypatch.setenv("THREED_MAINTENANCE_RULES", str(f))
    return f


@pytest.fixture
def fleet(fleet_file: Path) -> Any:
    return load_fleet(fleet_file)


@pytest.fixture
def rules(rules_file: Path) -> mr.Rules:
    return mr.load_rules(rules_file)


@pytest.fixture
def conn(printops_db: sqlite3.Connection, fleet: Any) -> sqlite3.Connection:
    mt.seed_from_fleet(printops_db, fleet)
    return printops_db


def _events(conn: sqlite3.Connection, kind: str) -> list[events.Event]:
    return [e for e in events.since(conn, 0, 1000) if e.kind == kind]


# -- seeding ----------------------------------------------------------------------------------
def test_fleet_seed_records_nozzles_plates_and_the_u1_t2_fault_once(printops_db: sqlite3.Connection, fleet: Any) -> None:
    report = mt.seed_from_fleet(printops_db, fleet)
    assert report.installed == ["k1se/T1 nozzle 0.4 hardened std", "u1/T1 nozzle 0.4 stainless std",
                                "u1/T2 nozzle 0.4 stainless std", "u1/T3 nozzle 0.4 stainless std",
                                "u1 plate textured_pei"]
    assert report.faults_opened == ["u1/T2 #1"]
    fault = mt.get_fault(printops_db, 1)
    assert (fault["printer"], fault["head"], fault["component"], fault["status"], fault["source"],
            fault["external_key"]) == ("u1", "T2", "head", "open", "fleet", "fleet:u1/T2")
    assert fault["symptom"] == "head T2 does not work (gen/queue.json)"
    nozzle = mt.current_nozzle_install(printops_db, "u1", "T3")
    assert nozzle is not None and nozzle["installed_at"] is None  # "unknown date"
    assert (nozzle["diameter_mm"], nozzle["material"], nozzle["type"]) == (0.4, "stainless", "std")
    view = printops_db.execute("SELECT * FROM nozzle_install WHERE printer = 'u1' AND head = 'T3'").fetchone()
    assert view["id"] == nozzle["id"] and view["reason"].startswith("fleet/printers.yaml")

    again = mt.seed_from_fleet(printops_db, fleet)
    assert not again.changed and again.drift == []
    heads = {h["label"]: h for h in mt.heads(printops_db, fleet, "u1")}
    assert heads["T2"]["usable"] is False and heads["T2"]["open_faults"] == [1]
    assert heads["T1"]["usable"] is True and heads["T1"]["nozzle"]["material"] == "stainless"


def test_seed_never_overrides_recorded_history_and_reports_drift(conn: sqlite3.Connection, fleet: Any) -> None:
    mt.swap_nozzle(conn, fleet, "u1", "T3", reason="abrasive CF", material="hardened", actor="t")
    mt.close_fault(conn, 1, actor="t", root_cause="reseated the head")
    report = mt.seed_from_fleet(conn, fleet)
    assert report.installed == [] and report.faults_opened == []
    assert {(d["subject"], d["what"]) for d in report.drift} == {("u1/T3", "nozzle"), ("u1/T2", "usable")}
    assert mt.current_nozzle_install(conn, "u1", "T3")["material"] == "hardened"  # type: ignore[index]


def test_a_head_the_fleet_calls_usable_with_its_seeded_fault_open_is_drift(conn: sqlite3.Connection,
                                                                            fleet_file: Path) -> None:
    fleet_file.write_text(FLEET.replace("usable: false", "usable: true"), encoding="utf-8")
    report = mt.seed_from_fleet(conn, load_fleet(fleet_file))
    assert {(d["subject"], d["what"], d["fleet"]) for d in report.drift} == {("u1/T2", "usable", True)}
    assert mt.get_fault(conn, 1)["status"] == "open"  # closing needs a cause: the fleet file does not close it


def test_ensure_seeded_reseeds_only_when_the_fleet_file_changes(printops_db: sqlite3.Connection,
                                                                fleet_file: Path) -> None:
    first = mt.ensure_seeded(printops_db)
    assert first is not None and first.changed
    assert mt.ensure_seeded(printops_db) == mt.SeedReport()
    fleet_file.write_text(FLEET.replace("plates: [textured_pei]", "plates: [textured_pei, smooth_pei]"), encoding="utf-8")
    later = mt.ensure_seeded(printops_db)
    assert later is not None and later.installed == ["u1 plate smooth_pei"]


def test_concurrent_cold_seeding_inserts_once(printops_db: sqlite3.Connection, fleet_file: Path) -> None:
    """Two threadpool requests after a restart must not race the same seed INSERTs."""
    import threading

    db_file = str(printops_db.execute("PRAGMA database_list").fetchone()["file"])
    errors: list[BaseException] = []
    start = threading.Barrier(4)

    def seed() -> None:
        conn = open_db(db_file)
        try:
            start.wait()
            mt.ensure_seeded(conn)
        except BaseException as e:  # collected and asserted below
            errors.append(e)
        finally:
            conn.close()

    threads = [threading.Thread(target=seed) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert printops_db.execute("SELECT COUNT(*) FROM fault WHERE external_key = 'fleet:u1/T2'").fetchone()[0] == 1


# -- nozzle swaps and the #167 contract -----------------------------------------------------
def test_nozzle_swap_records_the_install_and_invalidates_that_heads_calibrations(conn: sqlite3.Connection,
                                                                                 fleet: Any) -> None:
    old = mt.current_nozzle_install(conn, "u1", "T3")
    res = mt.swap_nozzle(conn, fleet, "u1", "t3", reason="clogged", material="hardened", actor="cli",
                         at="2026-10-14T10:00:00+02:00", parts=["0.4 hardened steel nozzle"])
    new = mt.current_nozzle_install(conn, "u1", "T3")
    assert new is not None and old is not None and new["id"] != old["id"]
    assert (new["diameter_mm"], new["material"], new["type"], new["installed_at"]) == \
        (0.4, "hardened", "std", "2026-10-14T08:00:00.000Z")
    assert res["inherited"] == ["diameter_mm", "type"]
    assert res["removed"]["removed_at"] == "2026-10-14T08:00:00.000Z" and res["removed"]["removal_reason"] == "clogged"
    m = res["maintenance"]
    assert (m["component"], m["kind"], m["head"], m["parts_used"]) == ("nozzle", "replace", "T3",
                                                                        ["0.4 hardened steel nozzle"])
    assert m["triggers_calibrations"] == [{"trigger": "nozzle_swap", "kinds": None}]
    [ev] = _events(conn, "calibration.invalidated")
    assert ev.subject == "u1/T3" and ev.actor == "cli"
    assert ev.payload == {"printer": "u1", "head": "T3", "trigger": "nozzle_swap", "kinds": None,
                          "reason": "nozzle swap on u1/T3: clogged", "maintenance_id": m["id"],
                          "nozzle_install_id": new["id"]}
    assert [e.subject for e in _events(conn, "component.installed")][-1] == "u1/T3"
    assert _events(conn, "component.removed")[0].payload["id"] == old["id"]
    assert mt.current_nozzle_install(conn, "u1", "T1")["id"] != new["id"]  # type: ignore[index]  # other heads untouched


def test_a_diameter_change_also_fires_nozzle_diameter(conn: sqlite3.Connection, fleet: Any) -> None:
    res = mt.swap_nozzle(conn, fleet, "k1se", "T1", reason="detail prints", diameter_mm=0.2, actor="t")
    assert [i["trigger"] for i in res["invalidated"]] == ["nozzle_swap", "nozzle_diameter"]
    assert {e.subject for e in _events(conn, "calibration.invalidated")} == {"k1se/T1"}


@pytest.mark.parametrize(("printer", "head", "kwargs", "status", "message"), [
    ("u1", "T9", {}, 404, "u1 has no head 'T9'"),
    ("x1", "T1", {}, 404, "unknown printer 'x1'"),
    ("u1", "T1", {"reason": " "}, 422, "reason: required"),
    ("u1", "T1", {"material": "gold"}, 422, "material: 'gold' is not one of"),
    ("u1", "T1", {"type_": "turbo"}, 422, "type: 'turbo' is not one of"),
    ("u1", "T1", {"diameter_mm": 3}, 422, "is not a nozzle diameter"),
    ("u1", "T1", {"diameter_mm": float("nan")}, 422, "is not a nozzle diameter"),
    ("u1", "T1", {"at": "2999-01-01"}, 422, "is in the future"),
    ("u1", "T1", {"at": "yesterday"}, 422, "is not an ISO-8601"),
])
def test_nozzle_swap_refusals(conn: sqlite3.Connection, fleet: Any, printer: str, head: str,
                              kwargs: dict[str, Any], status: int, message: str) -> None:
    args: dict[str, Any] = {"reason": "worn", **kwargs}
    with pytest.raises(mt.MaintError) as e:
        mt.swap_nozzle(conn, fleet, printer, head, actor="t", **args)
    assert e.value.status == status and message in e.value.message
    assert _events(conn, "calibration.invalidated") == []


def test_a_swap_dated_before_the_mounted_nozzle_is_refused_and_rolled_back(conn: sqlite3.Connection,
                                                                           fleet: Any) -> None:
    mt.swap_nozzle(conn, fleet, "u1", "T1", reason="worn", at="2026-10-10", actor="t")
    with pytest.raises(mt.MaintError) as e:
        mt.swap_nozzle(conn, fleet, "u1", "T1", reason="worn", at="2026-10-01", actor="t")
    assert e.value.status == 409 and "installed at 2026-10-10" in e.value.message
    assert conn.execute("SELECT COUNT(*) FROM maintenance").fetchone()[0] == 1
    assert len(_events(conn, "calibration.invalidated")) == 1


def test_a_head_without_a_recorded_nozzle_needs_the_diameter(printops_db: sqlite3.Connection, tmp_path: Path) -> None:
    f = tmp_path / "bare.yaml"
    f.write_text("p:\n  model: Snapmaker U1\n  owner: own\n  access: offline\n  heads: [{label: T1, index: 0}]\n",
                 encoding="utf-8")
    bare = load_fleet(f)
    with pytest.raises(mt.MaintError, match="give its diameter"):
        mt.swap_nozzle(printops_db, bare, "p", "T1", reason="first record", actor="t")
    res = mt.swap_nozzle(printops_db, bare, "p", "T1", reason="first record", diameter_mm=0.6, actor="t")
    assert res["removed"] is None and res["inherited"] == []


# -- maintenance log --------------------------------------------------------------------------
def test_replacing_a_ptfe_records_the_part_and_fires_the_mapped_trigger(conn: sqlite3.Connection, fleet: Any,
                                                                        rules: mr.Rules) -> None:
    first = mt.log_maintenance(conn, fleet, rules, "u1", component="ptfe", kind="replace", head="T3",
                               label="stock", actor="t", at="2026-09-01")
    assert first["removed"] is None and first["installed"]["label"] == "stock"
    res = mt.log_maintenance(conn, fleet, rules, "u1", component="ptfe", kind="replace", head="T3",
                             label="Capricorn XS", notes="kinked", actor="t")
    assert res["removed"]["id"] == first["installed"]["id"] and res["installed"]["label"] == "Capricorn XS"
    assert res["invalidated"][-1]["trigger"] == "ptfe" and res["invalidated"][-1]["kinds"] is None
    assert res["invalidated"][-1]["head"] == "T3" and res["invalidated"][-1]["nozzle_install_id"] is None
    assert [c["label"] for c in mt.components(conn, "u1") if c["kind"] == "ptfe"] == ["Capricorn XS"]
    assert len([c for c in mt.components(conn, "u1", include_removed=True) if c["kind"] == "ptfe"]) == 2


def test_explicit_kinds_override_and_no_invalidate_suppresses(conn: sqlite3.Connection, fleet: Any,
                                                              rules: mr.Rules) -> None:
    res = mt.log_maintenance(conn, fleet, rules, "u1", component="belts", kind="tighten", invalidates=["input_shaper"],
                             actor="t")
    assert res["invalidated"][0]["trigger"] == "belts" and res["invalidated"][0]["kinds"] == ["input_shaper"]
    assert res["invalidated"][0]["head"] is None and res["invalidated"][0]["reason"] == "belts tighten on u1"
    res = mt.log_maintenance(conn, fleet, rules, "u1", component="extruder", kind="repair", head="T1",
                             invalidates=["rotation_distance"], actor="t")
    assert res["invalidated"][0]["trigger"] == "repair"  # unmapped: generic repair
    res = mt.log_maintenance(conn, fleet, rules, "u1", component="belts", kind="tighten", no_invalidate=True, actor="t")
    assert res["invalidated"] == [] and res["maintenance"]["triggers_calibrations"] == []
    res = mt.log_maintenance(conn, fleet, rules, "u1", component="kinematics", kind="lube", actor="t")
    assert res["invalidated"] == []  # lubing invalidates nothing


def test_rule_logging_fills_component_and_kind_and_refuses_contradictions(conn: sqlite3.Connection, fleet: Any,
                                                                          rules: mr.Rules) -> None:
    res = mt.log_maintenance(conn, fleet, rules, "u1", rule_id="u1-rails", actor="t")
    m = res["maintenance"]
    assert (m["component"], m["kind"], m["rule_id"]) == ("kinematics", "lube", "u1-rails")
    with pytest.raises(mt.MaintError, match="contradicts rule u1-rails"):
        mt.log_maintenance(conn, fleet, rules, "u1", rule_id="u1-rails", kind="clean", actor="t")
    with pytest.raises(mt.MaintError, match="no maintenance rule 'u1-rails' for k1se"):
        mt.log_maintenance(conn, fleet, rules, "k1se", rule_id="u1-rails", actor="t")
    with pytest.raises(mt.MaintError, match="is per head"):
        mt.log_maintenance(conn, fleet, rules, "u1", rule_id="u1-wiper", actor="t")


@pytest.mark.parametrize(("kwargs", "message"), [
    ({"component": "nozzle", "kind": "replace"}, "nozzle swap"),
    ({"component": "teapot", "kind": "clean"}, "component: 'teapot'"),
    ({"component": "fan", "kind": "polish"}, "kind: 'polish'"),
    ({"component": "printer", "kind": "repair", "head": "T1"}, "whole printer"),
    ({"component": "fan", "kind": "clean", "label": "Noctua"}, "label names the new part"),
    ({"component": "fan", "kind": "clean", "invalidates": ["flow"], "no_invalidate": True}, "exclude each other"),
    ({"component": "fan", "kind": "clean", "parts": ["ok", " "]}, "parts_used"),
    ({"rule_id": "u1-rails", "head": "T1"}, "for the whole printer"),
    ({"component": "plate", "kind": "replace"}, "names the new plate"),
    ({"component": "plate", "kind": "replace", "label": "pei", "head": "T1"}, "whole printer: no head"),
])
def test_maintenance_refusals(conn: sqlite3.Connection, fleet: Any, rules: mr.Rules, kwargs: dict[str, Any],
                              message: str) -> None:
    with pytest.raises(mt.MaintError, match=message):
        mt.log_maintenance(conn, fleet, rules, "u1", actor="t", **kwargs)
    assert conn.execute("SELECT COUNT(*) FROM maintenance").fetchone()[0] == 0


# -- faults -----------------------------------------------------------------------------------
def test_fault_lifecycle_fixed_by_maintenance(conn: sqlite3.Connection, fleet: Any, rules: mr.Rules) -> None:
    f = mt.open_fault(conn, fleet, "u1", head="T3", component="extruder", symptom="under-extrusion", actor="t")
    assert [h["usable"] for h in mt.heads(conn, fleet, "u1")] == [True, False, False]
    res = mt.log_maintenance(conn, fleet, rules, "u1", component="ptfe", kind="replace", head="T3",
                             fixes=[f["id"]], actor="t")
    [fixed] = res["fixed"]
    assert (fixed["status"], fixed["fixed_by_maintenance_id"]) == ("fixed", res["maintenance"]["id"])
    assert fixed["closed_at"] is not None
    assert [e.payload["status"] for e in _events(conn, "fault.closed")] == ["fixed"]
    with pytest.raises(mt.MaintError) as e:
        mt.close_fault(conn, f["id"], actor="t")
    assert e.value.status == 409 and "already fixed" in e.value.message
    assert [x["id"] for x in mt.list_faults(conn, "u1")] == [1]  # only the T2 fault stays open
    assert [x["id"] for x in mt.list_faults(conn, None, status=None)] == [1, f["id"]]


def test_only_that_heads_or_printer_wide_work_fixes_a_heads_fault(conn: sqlite3.Connection, fleet: Any,
                                                                    rules: mr.Rules) -> None:
    f = mt.open_fault(conn, fleet, "u1", head="T3", component="extruder", symptom="grinding", actor="t")
    with pytest.raises(mt.MaintError, match="is on head T1, fault #2 on head T3"):
        mt.log_maintenance(conn, fleet, rules, "u1", component="ptfe", kind="replace", head="T1",
                           fixes=[f["id"]], actor="t")
    assert conn.execute("SELECT COUNT(*) FROM maintenance").fetchone()[0] == 0  # rolled back
    res = mt.log_maintenance(conn, fleet, rules, "u1", component="printer", kind="repair", fixes=[f["id"]], actor="t")
    assert res["fixed"][0]["status"] == "fixed"


def test_maintenance_done_before_a_fault_was_noticed_cannot_fix_it(conn: sqlite3.Connection, fleet: Any,
                                                                     rules: mr.Rules) -> None:
    old = mt.log_maintenance(conn, fleet, rules, "u1", component="ptfe", kind="clean", head="T3", at="2026-09-01",
                             actor="t")
    f = mt.open_fault(conn, fleet, "u1", head="T3", symptom="clicks", actor="t")
    with pytest.raises(mt.MaintError, match="predates fault"):
        mt.close_fault(conn, f["id"], actor="t", fixed_by=old["maintenance"]["id"])
    with pytest.raises(mt.MaintError, match="predates fault"):
        mt.log_maintenance(conn, fleet, rules, "u1", component="ptfe", kind="clean", head="T3", at="2026-09-02",
                           fixes=[f["id"]], actor="t")


def test_fault_close_wontfix_and_cross_printer_refusals(conn: sqlite3.Connection, fleet: Any,
                                                        rules: mr.Rules) -> None:
    k1 = mt.log_maintenance(conn, fleet, rules, "k1se", component="fan", kind="clean", actor="t")
    with pytest.raises(mt.MaintError, match="is on k1se, fault #1 on u1"):
        mt.close_fault(conn, 1, actor="t", fixed_by=k1["maintenance"]["id"])
    with pytest.raises(mt.MaintError, match="fixed_by needs status fixed"):
        mt.close_fault(conn, 1, actor="t", status="wontfix", fixed_by=k1["maintenance"]["id"])
    with pytest.raises(mt.MaintError, match="fault #1 is on u1, not k1se"):
        mt.close_fault(conn, 1, actor="t", printer="k1se")
    closed = mt.close_fault(conn, 1, actor="t", status="wontfix", root_cause="  board dead, head retired ")
    assert (closed["status"], closed["root_cause"]) == ("wontfix", "board dead, head retired")
    with pytest.raises(mt.MaintError, match="symptom: required"):
        mt.open_fault(conn, fleet, "u1", symptom="", actor="t")
    with pytest.raises(mt.MaintError, match="status: 'broken'"):
        mt.list_faults(conn, status="broken")


# -- rules file -------------------------------------------------------------------------------
def test_the_repo_rules_file_validates_against_the_repo_fleet() -> None:
    from printops.fleet import read_yaml

    fleet = load_fleet(REPO / "fleet" / "printers.yaml")
    assert fleet is not None
    rules, problems = mr.validate(read_yaml(REPO / "fleet" / "maintenance-rules.yaml"),
                                  REPO / "fleet" / "maintenance-rules.yaml", fleet.printers)
    assert problems == []
    assert rules is not None and {r.printers[0] for r in rules.rules} == {"u1", "k1se"}
    assert all(r.source.startswith("https://") for r in rules.rules)
    assert rules.trigger_for("ptfe", "replace") == "ptfe" and rules.trigger_for("kinematics", "lube") is None


def test_the_k1se_x_rods_rule_is_due_at_200_hours_or_90_days_whichever_first(conn: sqlite3.Connection,
                                                                               fleet: Any) -> None:
    """The K1 SE X axis (graphite bushings, IPA only) comes due by print hours or by the calendar."""
    rules = mr.load_rules(REPO / "fleet" / "maintenance-rules.yaml")
    rule = rules.get("k1se-x-rods-bushings")
    assert rule is not None and rule.every == {"print_hours": 200.0, "days": 90.0}
    assert (rule.component, rule.task, rule.scope) == ("kinematics", "clean", "printer")
    assert rule.note is not None and "bd1be79b" in rule.note  # the 2026-10-16 stiff-X record

    def status(hours: float, at: str) -> tuple[str, list[str]]:
        conn.execute("DELETE FROM maintenance")
        conn.commit()
        mt.log_maintenance(conn, fleet, rules, "k1se", rule_id=rule.id, at=at, actor="t")
        provider = FakeCounters({"*": Usage(print_hours=hours, source="fake")})
        [item] = [i for i in mr.evaluate(conn, rules, fleet, provider, printer="k1se", now=NOW) if i.rule == rule.id]
        return item.status, item.reasons

    assert status(50, "2026-10-01") == ("ok", [])
    assert status(205, "2026-10-01") == ("due", ["print_hours 205 ≥ 200"])  # hours first
    assert status(10, "2026-07-01") == ("due", ["days 106.5 ≥ 90"])  # barely printed: the calendar first
    assert status(185, "2026-10-01")[0] == "soon"  # 0.925 of 200 h

    # logged with --rule (what `due` prints as log_command), Y/Z rod work leaves the X clock alone
    conn.execute("DELETE FROM maintenance")
    conn.commit()
    mt.log_maintenance(conn, fleet, rules, "k1se", rule_id="k1se-rods", actor="t")
    assert mr.baseline(conn, rule, "k1se", None) is None
    # without --rule the shared component × kind restarts both clocks (documented: log with --rule)
    plain = mt.log_maintenance(conn, fleet, rules, "k1se", component="kinematics", kind="clean", actor="t")
    rods = rules.get("k1se-rods")
    assert rods is not None
    for r in (rule, rods):
        assert mr.baseline(conn, r, "k1se", None)["id"] == plain["maintenance"]["id"]  # type: ignore[index]


def test_rules_schema_reports_every_problem(tmp_path: Path) -> None:
    data = {
        "check_every_min": 0, "soon_fraction": 2, "extra": 1,
        "invalidates": [{"component": "ptfe", "when": ["replace"], "trigger": "ptfe"},
                        {"component": "ptfe", "when": ["replace"], "trigger": "warp"},
                        {"component": "laser", "when": [], "trigger": "ptfe", "on": True}],
        "rules": [
            {"id": "a", "title": "t", "printers": ["u1"], "component": "fan", "task": "clean",
             "every": {"days": 1}, "source": "s"},
            {"id": "a", "title": "", "printers": [], "component": "fan", "task": "polish", "scope": "bed",
             "every": {"hours": 5, "days": -1}, "typo": 1},
            {"id": "b", "title": "t", "printers": ["ghost"], "component": "fan", "task": "clean",
             "every": {}, "source": "s"},
            {"id": "c", "title": "t", "printers": ["u1", "u1"], "component": "fan", "task": "clean",
             "every": {"days": 1}, "source": "s"},
            {"id": "d", "title": "t", "printers": ["u1"], "component": "printer", "task": "inspect", "scope": "head",
             "every": {"days": 1}, "source": "s"},
            {"id": "e", "title": "t", "printers": ["u1"], "component": "nozzle", "task": "replace",
             "every": {"days": 1}, "source": "s"},
            {"id": "f", "title": "t", "printers": ["u1"], "component": "fan", "task": "clean",
             "every": {"days": float("inf")}, "source": "s"},
        ],
    }
    rules, problems = mr.validate(data, tmp_path / "r.yaml", ["u1"])
    text = "\n".join(problems)
    for needle in ("extra: unknown key", "check_every_min: 0", "soon_fraction: 2",
                   "invalidates[1]: ptfe × replace is mapped twice", "invalidates[1].trigger: 'warp'",
                   "invalidates[2].on: unknown key", "invalidates[2].component: 'laser'", "invalidates[2].when",
                   "rules[1].id: 'a' is used twice", "rules[1].typo: unknown key", "rules[1].title: required text",
                   "rules[1].source: required text (where the interval comes from)", "rules[1].printers: required",
                   "rules[1].task: 'polish'", "rules[1].scope: 'bed'", "rules[1].every.hours: unknown threshold",
                   "rules[1].every.days: -1 is not a number > 0", "rules[b].printers: 'ghost' is not in",
                   "rules[b].every: required", "rules[c].printers: a printer is listed twice",
                   "rules[d].scope: a printer rule is printer-wide", "rules[e]: a nozzle replacement is logged by",
                   "rules[f].every.days: inf is not a number > 0"):
        assert needle in text, needle
    assert rules is not None and [r.id for r in rules.rules] == ["a"]


def test_load_rules_layer_switch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THREED_MAINTENANCE_RULES", "")
    assert mr.load_rules() is mr.EMPTY
    monkeypatch.setenv("THREED_MAINTENANCE_RULES", str(tmp_path / "missing.yaml"))
    with pytest.raises(mr.RulesError, match="not found"):
        mr.load_rules()
    bad = tmp_path / "bad.yaml"
    bad.write_text("rules: 3\n", encoding="utf-8")
    with pytest.raises(mr.RulesError) as e:
        mr.load_rules(bad)
    assert e.value.exit_code == 2 and "rules: must be a list" in e.value.remediation


# -- due evaluation ---------------------------------------------------------------------------
NOW = datetime.datetime(2026, 10, 15, 12, 0, tzinfo=datetime.timezone.utc)


def _by_key(items: list[mr.DueItem]) -> dict[tuple[str, str | None], mr.DueItem]:
    return {(i.rule, i.head): i for i in items}


def test_never_logged_rules_are_due_and_counters_count_from_the_first_job(conn: sqlite3.Connection, fleet: Any,
                                                                          rules: mr.Rules) -> None:
    provider = FakeCounters({None: Usage(print_hours=330.9, filament_m=2240.3, jobs=88, source="fake")})
    items = _by_key(mr.evaluate(conn, rules, fleet, provider, now=NOW))
    rails = items[("u1-rails", None)]
    assert rails.status == "due" and rails.baseline is None
    assert rails.used == {"days": None, "filament_m": 2240.3}
    assert rails.reasons == ["filament_m 2240.3 ≥ 1000", "never logged"]
    assert rails.log_command == "3d printops maint log u1 --rule u1-rails"
    hours = items[("u1-hours", None)]
    assert hours.status == "due" and hours.progress == {"print_hours": 3.309}
    wiper = items[("u1-wiper", "T2")]
    assert wiper.status == "no_counter" and wiper.subject == "u1/T2"
    assert wiper.reasons == ["unknown: print_hours (fake does not count print_hours)"]
    assert wiper.log_command == "3d printops maint log u1/T2 --rule u1-wiper"
    assert {k for k in items if k[0] == "u1-wiper"} == {("u1-wiper", "T1"), ("u1-wiper", "T2"), ("u1-wiper", "T3")}


def test_calendar_rules_never_ask_the_printer_and_unknown_history_is_named(conn: sqlite3.Connection,
                                                                            fleet: Any) -> None:
    rules, problems = mr.validate({"rules": [
        {"id": "k1-belts", "title": "belts", "printers": ["k1se"], "component": "belts", "task": "inspect",
         "every": {"days": 30}, "source": "s"},
        {"id": "k1-rods", "title": "rods", "printers": ["k1se"], "component": "kinematics", "task": "lube",
         "every": {"print_hours": 300}, "source": "s"}]}, None)
    assert problems == [] and rules is not None

    class NoHistory:
        def __init__(self) -> None:
            self.asked: list[str] = []

        def usage(self, printer: str, head: str | None, since: str | None) -> Usage:
            self.asked.append(printer)
            return Usage()  # e.g. Creality stock firmware: no readable job history

    provider = NoHistory()
    items = _by_key(mr.evaluate(conn, rules, fleet, provider, now=NOW))
    assert provider.asked == ["k1se"]  # only for k1-rods
    assert items[("k1-belts", None)].status == "due"
    rods = items[("k1-rods", None)]
    assert rods.status == "no_counter" and rods.counter_source == ""
    assert rods.reasons == ["unknown: print_hours (no job history of k1se is readable; the job ledger (#170) "
                            "will count it)"]



def test_logging_a_rule_restarts_its_counters_from_that_time(conn: sqlite3.Connection, fleet: Any,
                                                             rules: mr.Rules) -> None:
    m = mt.log_maintenance(conn, fleet, rules, "u1", rule_id="u1-rails", at="2026-10-01T12:00:00Z", actor="t")
    since = m["maintenance"]["at"]
    provider = FakeCounters({None: Usage(filament_m=5000, print_hours=500, source="fake"),
                             since: Usage(filament_m=950, print_hours=20, source="fake")})
    rails = _by_key(mr.evaluate(conn, rules, fleet, provider, now=NOW))[("u1-rails", None)]
    assert rails.baseline == {"maintenance_id": m["maintenance"]["id"], "at": "2026-10-01T12:00:00.000Z"}
    assert rails.used == {"days": 14.0, "filament_m": 950.0}
    assert rails.status == "soon" and rails.progress == {"days": 0.467, "filament_m": 0.95}
    later = _by_key(mr.evaluate(conn, rules, fleet, provider, now=NOW + datetime.timedelta(days=17)))
    assert later[("u1-rails", None)].status == "due" and later[("u1-rails", None)].reasons == ["days 31 ≥ 30"]
    # A plain (rule-less) kinematics lube also counts as doing it; a clean does not.
    mt.log_maintenance(conn, fleet, rules, "u1", component="kinematics", kind="clean", actor="t")
    assert mr.baseline(conn, rules.get("u1-rails"), "u1", None)["id"] == m["maintenance"]["id"]  # type: ignore[arg-type,index]
    plain = mt.log_maintenance(conn, fleet, rules, "u1", component="kinematics", kind="lube", actor="t")
    assert mr.baseline(conn, rules.get("u1-rails"), "u1", None)["id"] == plain["maintenance"]["id"]  # type: ignore[arg-type,index]


def test_check_announces_maintenance_due_once_per_baseline(conn: sqlite3.Connection, fleet: Any,
                                                           rules: mr.Rules) -> None:
    provider = FakeCounters({"*": Usage(print_hours=10, filament_m=10, source="fake"),
                             None: Usage(print_hours=150, filament_m=10, source="fake")})
    first = mr.check(conn, rules, fleet, provider, now=NOW)
    assert sorted(a["rule"] for a in first.announced) == ["u1-hours", "u1-rails"]
    [hours] = [e for e in _events(conn, "maintenance.due") if e.payload["rule"] == "u1-hours"]
    assert hours.subject == "u1" and hours.payload["reasons"] == ["print_hours 150 ≥ 100"]
    assert hours.payload["log_command"] == "3d printops maint log u1 --rule u1-hours"
    assert mr.check(conn, rules, fleet, provider, now=NOW).announced == []  # still due: not repeated
    mt.log_maintenance(conn, fleet, rules, "u1", rule_id="u1-hours", actor="t")
    assert mr.check(conn, rules, fleet, provider, now=NOW).announced == []  # done: 10 h since
    mt.log_maintenance(conn, fleet, rules, "u1", rule_id="u1-hours", at="2026-10-01", actor="t")
    assert first.to_dict()["due"] == 2


def test_head_work_does_not_service_a_printer_wide_rule(conn: sqlite3.Connection, fleet: Any,
                                                         rules: mr.Rules) -> None:
    mt.log_maintenance(conn, fleet, rules, "u1", component="kinematics", kind="lube", head="T1", actor="t")
    assert mr.baseline(conn, rules.get("u1-rails"), "u1", None) is None  # type: ignore[arg-type]
    mt.log_maintenance(conn, fleet, rules, "u1", component="kinematics", kind="lube", actor="t")
    assert mr.baseline(conn, rules.get("u1-rails"), "u1", None) is not None  # type: ignore[arg-type]


def test_due_is_decided_on_raw_usage_not_the_rounded_progress(conn: sqlite3.Connection, fleet: Any,
                                                               rules: mr.Rules) -> None:
    provider = FakeCounters({"*": Usage(print_hours=99.96, source="fake"), None: Usage(print_hours=99.96, source="fake")})
    [hours] = [i for i in mr.evaluate(conn, rules, fleet, provider, printer="u1", now=NOW) if i.rule == "u1-hours"]
    assert (hours.status, hours.reasons, hours.progress) == ("soon", [], {"print_hours": 1.0})


def test_concurrent_checks_announce_each_due_item_once(printops_db: sqlite3.Connection, fleet: Any,
                                                       rules: mr.Rules) -> None:
    """The background loop and POST /v1/maintenance/check may run at once on two connections."""
    import threading

    mt.ensure_seeded(printops_db)
    db_file = str(printops_db.execute("PRAGMA database_list").fetchone()["file"])
    provider = FakeCounters({"*": Usage(print_hours=150, filament_m=10, source="fake"),
                             None: Usage(print_hours=150, filament_m=10, source="fake")})
    errors: list[BaseException] = []
    start = threading.Barrier(4)

    def run() -> None:
        conn = open_db(db_file)
        try:
            start.wait()
            mr.check(conn, rules, fleet, provider, now=NOW)
        except BaseException as e:  # collected and asserted below
            errors.append(e)
        finally:
            conn.close()

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sorted(e.payload["rule"] for e in _events(printops_db, "maintenance.due")) == ["u1-hours", "u1-rails"]


def test_check_skips_an_item_logged_after_it_was_evaluated(conn: sqlite3.Connection, fleet: Any, rules: mr.Rules,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """The user logs the rule between the check's evaluate() and its announcement: no stale alert."""
    provider = FakeCounters({"*": Usage(print_hours=150, filament_m=10, source="fake"),
                             None: Usage(print_hours=150, filament_m=10, source="fake")})
    real = mr.evaluate

    def evaluate_then_log(*args: Any, **kw: Any) -> list[mr.DueItem]:
        items = real(*args, **kw)
        mt.log_maintenance(conn, fleet, rules, "u1", rule_id="u1-hours", actor="user")
        return items

    monkeypatch.setattr(mr, "evaluate", evaluate_then_log)
    report = mr.check(conn, rules, fleet, provider, now=NOW)
    assert [a["rule"] for a in report.announced] == ["u1-rails"]


def test_evaluate_asks_the_provider_earliest_baseline_first(conn: sqlite3.Connection, fleet: Any,
                                                            rules: mr.Rules) -> None:
    mt.log_maintenance(conn, fleet, rules, "u1", rule_id="u1-hours", at="2026-10-10", actor="t")
    mt.log_maintenance(conn, fleet, rules, "u1", rule_id="u1-rails", at="2026-10-01", actor="t")
    provider = FakeCounters()
    mr.evaluate(conn, rules, fleet, provider, printer="u1", now=NOW)
    printer_wide = [c[2] for c in provider.calls if c[1] is None]
    assert printer_wide == ["2026-10-01T00:00:00.000Z", "2026-10-10T00:00:00.000Z"]
    assert mr.evaluate(conn, rules, fleet, provider, printer="k1se", now=NOW) == []


# -- counters ---------------------------------------------------------------------------------
class FakeMoonraker:
    """GET-only fake of Moonraker's history API; records every URL asked."""

    def __init__(self, jobs: list[dict[str, Any]]) -> None:
        self.jobs = jobs
        self.urls: list[str] = []

    def __call__(self, url: str) -> Any:
        from urllib.parse import parse_qs, urlsplit

        self.urls.append(url)
        parts = urlsplit(url)
        if parts.path == "/server/history/totals":
            return {"result": {"job_totals": {
                "total_jobs": float(len(self.jobs)),
                "total_print_time": sum(j["print_duration"] for j in self.jobs),
                "total_filament_used": sum(j["filament_used"] for j in self.jobs)}, "auxiliary_totals": []}}
        assert parts.path == "/server/history/list"
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        assert q["order"] == "asc"
        since, start, limit = float(q["since"]), int(q["start"]), int(q["limit"])
        # a malformed start (not a number) passes the server's filter, as a corrupt history row would
        hits = [j for j in self.jobs if not isinstance(j["start_time"], (int, float)) or j["start_time"] >= since]
        return {"result": {"count": len(hits[start:start + limit]), "jobs": hits[start:start + limit]}}


def _jobs(n: int) -> list[dict[str, Any]]:
    base = datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc).timestamp()
    return [{"job_id": f"{i:06X}", "start_time": base + i * 3600, "print_duration": 1800.0, "filament_used": 500.0,
             "status": "completed"} for i in range(n)]


def test_moonraker_history_totals_and_paged_usage_since() -> None:
    fake = FakeMoonraker(_jobs(250))
    hist = MoonrakerHistory("http://u1:7125/", fake)
    total = hist.usage(None)
    assert (total.print_hours, total.filament_m, total.jobs) == (125.0, 125.0, 250.0)
    assert total.tool_changes is None and total.source == "moonraker http://u1:7125/server/history"
    since = hist.usage("2026-10-01T10:00:00Z")  # jobs 10..249
    assert (since.print_hours, since.filament_m, since.jobs) == (120.0, 120.0, 240.0)
    list_calls = [u for u in fake.urls if "/list?" in u]
    assert len(list_calls) == 3  # 100 + 100 + 40
    later = hist.usage("2026-10-08T00:00:00Z")  # served from the cached read: jobs 168..249
    assert later.jobs == 82.0 and len([u for u in fake.urls if "/list?" in u]) == 3
    hist.usage(None)
    assert len([u for u in fake.urls if "/totals" in u]) == 1


def test_a_history_longer_than_the_page_cap_is_unknown_not_short(monkeypatch: pytest.MonkeyPatch) -> None:
    from printops import counters

    monkeypatch.setattr(counters, "MAX_PAGES", 2)
    hist = MoonrakerHistory("http://u1:7125/", FakeMoonraker(_jobs(250)))
    since = hist.usage("2026-10-01T10:00:00Z")
    assert since.filament_m is None and since.jobs is None
    assert since.error is not None and "longer than 200 jobs" in since.error
    later = hist.usage("2026-10-08T00:00:00Z")  # a window that fits is read anew, not marked unknown
    assert later.error is None and later.jobs == 82.0


def test_fleet_counters_route_by_adapter_and_never_guess() -> None:
    fake = FakeMoonraker(_jobs(3))
    devices = {"u1": ("moonraker", "http://u1:7125"), "k1se": ("creality-stock", "http://k1:80")}
    fc = FleetCounters(get_json=fake, resolve=devices.get)
    assert fc.usage("u1", None, None).jobs == 3.0
    head = fc.usage("u1", "T1", None)
    assert head.print_hours is None and head.source.startswith("moonraker")  # per-head needs the job ledger
    assert fc.usage("k1se", None, None) == Usage()
    assert fc.usage("p1s", None, None) == Usage()

    def down(url: str) -> Any:
        raise OSError("connection refused")

    broken = FleetCounters(get_json=down, resolve=devices.get).usage("u1", None, None)
    assert broken.print_hours is None and broken.error is not None and "connection refused" in broken.error
    odd = FleetCounters(get_json=lambda url: {"result": ["not", "a", "mapping"]}, resolve=devices.get).usage("u1", None, None)
    assert odd.print_hours is None and odd.error is not None and "AttributeError" in odd.error  # not a 500
    nan = FleetCounters(get_json=lambda url: {"result": {"job_totals": {
        "total_print_time": "nan", "total_filament_used": 1.0, "total_jobs": 1.0}}}, resolve=devices.get)
    bad = nan.usage("u1", None, None)
    assert bad.print_hours is None and bad.error is not None and "not a finite number" in bad.error  # never "ok"
    jobs = FakeMoonraker(_jobs(3))
    jobs.jobs[1]["start_time"] = "nan"
    since = FleetCounters(get_json=jobs, resolve=devices.get).usage("u1", None, "2026-10-01T00:00:00Z")
    assert since.jobs is None and since.error is not None and "not a finite number" in since.error  # not dropped


def test_an_unreachable_counter_source_is_reported_not_guessed(conn: sqlite3.Connection, fleet: Any,
                                                               rules: mr.Rules) -> None:
    class Down:
        def usage(self, printer: str, head: str | None, since: str | None) -> Usage:
            return Usage(source="moonraker x", error="moonraker x: OSError: refused")

    hours = _by_key(mr.evaluate(conn, rules, fleet, Down(), now=NOW))[("u1-hours", None)]
    assert hours.status == "no_counter" and hours.errors == ["moonraker x: OSError: refused"]
    assert hours.reasons == ["unknown: print_hours (moonraker x: OSError: refused)"]


# -- lint -------------------------------------------------------------------------------------
def test_lint_flags_open_faults_and_fleet_drift(conn: sqlite3.Connection, fleet: Any, fake_spoolman: Any) -> None:
    ctx = LintContext(conn=conn, spoolman=fake_spoolman, spools=[], now=NOW)
    assert [(f.rule, f.subject) for f in lint(ctx)] == [("maintenance/open-fault", "fault:1")]
    mt.open_fault(conn, fleet, "u1", head="T3", symptom="clicks", actor="t")
    mt.swap_nozzle(conn, fleet, "u1", "T1", reason="worn", material="hardened", actor="t")
    mt.close_fault(conn, 1, actor="t", root_cause="reseated")
    found = {(f.rule, f.subject) for f in lint(ctx)}
    assert found == {("maintenance/open-fault", "fault:2"), ("maintenance/fleet-drift", "u1/T3"),
                     ("maintenance/fleet-drift", "u1/T2"), ("maintenance/fleet-drift", "u1/T1")}


def test_lint_reads_each_printers_faults_once(conn: sqlite3.Connection, fake_spoolman: Any,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str | None] = []
    real = mt.list_faults

    def counting(c: sqlite3.Connection, printer: str | None = None, **kw: Any) -> list[dict[str, Any]]:
        calls.append(printer)
        return real(c, printer, **kw)

    monkeypatch.setattr(mt, "list_faults", counting)
    lint(LintContext(conn=conn, spoolman=fake_spoolman, spools=[], now=NOW))
    assert sorted(calls, key=str) == sorted([None, "k1se", "u1"], key=str)


def test_lint_rules_file_checks_printer_names_against_the_fleet(tmp_path: Path, fleet_file: Path) -> None:
    f = tmp_path / "r.yaml"
    f.write_text(RULES.replace("printers: [u1]\n    component: printer", "printers: [x9]\n    component: printer"),
                 encoding="utf-8")
    path, findings = mr.lint_rules_file(f, fleet=fleet_file)
    assert path == f and [(x.rule, x.level) for x in findings] == [("maintenance/rules-schema", "error")]
    assert "'x9' is not in fleet/printers.yaml" in findings[0].message
    assert mr.lint_rules_file(tmp_path / "nope.yaml")[1][0].message == "maintenance rules file not found"


# -- REST -------------------------------------------------------------------------------------
@pytest.fixture
def api(api_client: ApiClient, fleet_file: Path, rules_file: Path, monkeypatch: pytest.MonkeyPatch) -> ApiClient:
    provider = FakeCounters({"*": Usage(print_hours=1, filament_m=1, source="fake"),
                             None: Usage(print_hours=150, filament_m=10, source="fake")})
    monkeypatch.setattr(routes_maintenance, "counter_provider", lambda: provider)
    return api_client


def test_routes_cover_components_swaps_maintenance_faults_and_due(api: ApiClient) -> None:
    comps = api.get("/v1/printers/u1/components")
    assert [h["usable"] for h in comps["heads"]] == [True, False, True]  # seeded on first request
    assert comps["heads"][1]["open_faults"] == [1]
    swap = api.post("/v1/printers/u1/components/nozzle-swap", {"head": "T3", "reason": "clog", "material": "hardened"})
    assert swap["installed"]["material"] == "hardened" and swap["invalidated"][0]["trigger"] == "nozzle_swap"
    logged = api.post("/v1/printers/u1/maintenance", {"component": "ptfe", "kind": "replace", "head": "T3",
                                                       "label": "Capricorn", "fixes": []})
    assert logged["invalidated"][0]["trigger"] == "ptfe"
    rows = api.get("/v1/printers/u1/maintenance")["maintenance"]
    assert [r["component"] for r in rows] == ["ptfe", "nozzle"]  # before the fault: cannot fix it (tested apart)
    fault = api.post("/v1/printers/u1/faults", {"symptom": "grinding", "head": "T3", "component": "extruder"})
    assert api.get("/v1/faults")["faults"][-1]["id"] == fault["id"]
    repair = api.post("/v1/printers/u1/maintenance", {"component": "extruder", "kind": "repair", "head": "T3"})
    closed = api.post(f"/v1/faults/{fault['id']}/close",
                      {"root_cause": "gear", "fixed_by": repair["maintenance"]["id"]})
    assert closed["status"] == "fixed"
    assert [f["id"] for f in api.get("/v1/printers/u1/faults", status="all")["faults"]] == [1, fault["id"]]
    due = api.get("/v1/printers/u1/maintenance/due")["items"]
    assert {(i["rule"], i["status"]) for i in due if i["head"] is None} == {("u1-rails", "due"), ("u1-hours", "due")}
    assert api.get("/v1/maintenance/due", status="no_counter")["items"][0]["rule"] == "u1-wiper"
    check = api.post("/v1/maintenance/check")
    assert check["due"] == 2 and len(check["announced"]) == 2
    assert api.post("/v1/maintenance/check")["announced"] == []
    assert api.post("/v1/maintenance/sync-fleet")["installed"] == []
    kinds = {e["kind"] for e in api.events(last=100)["events"]}
    assert {"calibration.invalidated", "maintenance.due", "fault.opened", "fault.closed", "component.installed",
            "maintenance.logged"} <= kinds


@pytest.mark.parametrize(("method", "path", "body", "status", "detail"), [
    ("GET", "/v1/printers/x9/components", None, 404, "unknown printer 'x9'"),
    ("GET", "/v1/printers/x9/maintenance/due", None, 404, "unknown printer"),
    ("POST", "/v1/printers/u1/components/nozzle-swap", {"head": "T9", "reason": "x"}, 404, "no head 'T9'"),
    ("POST", "/v1/printers/u1/components/nozzle-swap", {"head": "T1", "reason": "x", "colour": "red"}, 422, "colour"),
    ("POST", "/v1/printers/u1/maintenance", {"component": "nozzle", "kind": "replace"}, 422, "nozzle swap"),
    ("POST", "/v1/faults/1/close", {"status": "broken"}, 422, "status"),
    ("POST", "/v1/faults/99/close", {}, 404, "no fault #99"),
    ("GET", "/v1/faults/99", None, 404, "no fault #99"),
    ("POST", "/v1/maintenance/check?printer=x9", {}, 404, "unknown printer"),
])
def test_route_errors(api: ApiClient, method: str, path: str, body: Any, status: int, detail: str) -> None:
    with pytest.raises(ApiError) as e:
        api.request(method, path, json_body=body)
    assert e.value.status == status and detail in e.value.message


def test_no_token_or_a_wrong_scope_is_refused_before_fleet_seeding(api: ApiClient, printops_client: Any,
                                                                    printops_tokens: dict[str, str],
                                                                    printops_db: sqlite3.Connection) -> None:
    from printops_fixtures import TestClientTransport

    assert printops_client.get("/v1/printers/u1/components").status_code == 401
    reader = ApiClient("http://testserver", printops_tokens["read"], TestClientTransport(printops_client))
    with pytest.raises(ApiError) as e:
        reader.post("/v1/printers/u1/faults", {"symptom": "x"})
    assert e.value.status == 403
    assert printops_db.execute("SELECT COUNT(*) FROM component").fetchone()[0] == 0
    assert printops_db.execute("SELECT COUNT(*) FROM fault").fetchone()[0] == 0


def test_rules_for_a_printer_not_in_the_fleet_and_a_broken_fleet_are_503(api: ApiClient, rules_file: Path,
                                                                         fleet_file: Path) -> None:
    rules_file.write_text(RULES.replace("printers: [u1]", "printers: [ghost]", 1), encoding="utf-8")
    with pytest.raises(ApiError) as e:
        api.get("/v1/maintenance/due")
    assert e.value.status == 503 and "ghost" in e.value.message
    fleet_file.write_text("u1: [not, a, printer]\n", encoding="utf-8")
    for path in ("/v1/faults", "/v1/faults/1"):
        with pytest.raises(ApiError) as e:
            api.get(path)
        assert e.value.status == 503, path


def test_a_write_only_token_closes_a_fault_by_id(api: ApiClient, printops_client: Any,
                                                 printops_tokens: dict[str, str]) -> None:
    from printops_fixtures import TestClientTransport

    writer = ApiClient("http://testserver", printops_tokens["write"], TestClientTransport(printops_client))
    fault = writer.post("/v1/printers/u1/faults", {"symptom": "clicks", "head": "T1"})
    assert writer.post(f"/v1/faults/{fault['id']}/close", {"status": "wontfix"})["status"] == "wontfix"


def test_the_app_starts_the_periodic_due_check(printops_app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from fastapi.testclient import TestClient

    started = threading.Event()

    async def fake_loop(state: Any) -> None:
        started.set()

    monkeypatch.setattr(routes_maintenance, "_loop", fake_loop)
    with TestClient(printops_app):
        assert started.wait(5), "the router lifespan did not run inside the app's lifespan"


def test_routes_need_scopes_and_report_a_broken_rules_file(api: ApiClient, printops_client: Any,
                                                           printops_tokens: dict[str, str], rules_file: Path) -> None:
    from printops_fixtures import TestClientTransport

    reader = ApiClient("http://testserver", printops_tokens["read"], TestClientTransport(printops_client))
    assert reader.get("/v1/printers/u1/maintenance")["maintenance"] == []
    with pytest.raises(ApiError) as e:
        reader.post("/v1/printers/u1/faults", {"symptom": "x"})
    assert e.value.status == 403
    rules_file.write_text("rules: {}\n", encoding="utf-8")
    with pytest.raises(ApiError) as e:
        api.get("/v1/maintenance/due")
    assert e.value.status == 503 and "rules: must be a list" in e.value.message
