"""The spool-sync invariant in the print gates (issue #704): a spool Spoolman and PrintOps disagree
about is never trusted by `3d print-preflight` — `spool_in_sync` grades the disagreement by policy and
`filament_sufficient` refuses to pick one of two weights.

Three layers: the gates state the engine builds from a FakeSpoolman (`gates.printer_state` puts `sync`
on every bound spool), the checks over a hand-built state (every grade, policy level, `--accept`), and
the whole chain with teeth: a UI edit behind the system's back → FAIL → heal → PASS."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml
from fleet_fixtures import write_policy_for_fleet
from printops_fakes import FakeSpoolman
from test_printops_gates import NOW, REPO_POLICY, gcode, spool, state
from test_printops_spool_sync import FLEET, GREEN

from printer_ops import preflight
from printer_ops.printops_checks import PrintopsContext, run_checks
from printops import gates, locations, spool_sync, spools
from printops.fleet import load_fleet
from printops.gates_policy import CHECKS
from printops.gates_policy import parse as parse_gates
from printops.policy import PolicyError
from printops.spool_mirror import SyncedSpoolman
from printops.spoolman import ensure_extra_fields


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fleet = tmp_path / "fleet.yaml"
    fleet.write_text(FLEET, encoding="utf-8")
    monkeypatch.setenv("THREED_FLEET", str(fleet))
    monkeypatch.setenv(
        "PRINTOPS_POLICY", str(write_policy_for_fleet(tmp_path / "policy.yaml", fleet))
    )
    for var in (
        "PRINTOPS_API",
        "PRINTOPS_TOKEN",
        "PRINTOPS_SNAPSHOT",
        "PRINTOPS_HA_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    return fleet


def set_levels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    levels: dict[str, str] | None = None,
    printers: dict[str, dict[str, str]] | None = None,
) -> None:
    data = yaml.safe_load(REPO_POLICY.read_text(encoding="utf-8"))
    data["gates"]["levels"].update(levels or {})
    if printers:
        data["gates"]["printers"] = printers
    path = tmp_path / "policy-override.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("PRINTOPS_POLICY", str(path))


# -- the state the engine builds ---------------------------------------------------------------------


class Plant:
    """FakeSpoolman + the one write path + the fleet, as 3d-api has them (no HTTP)."""

    def __init__(self, conn: sqlite3.Connection, fake: FakeSpoolman) -> None:
        ensure_extra_fields(fake)
        self.conn, self.fake = conn, fake
        self.sm = SyncedSpoolman(fake, conn)
        self.ctx = spools.Ctx(conn, self.sm, "test")
        for path, kind in (
            ("WH:default", "warehouse"),
            ("WH:A", "zone"),
            ("WH:A/S1", "shelf"),
        ):
            locations.add(conn, path, kind, actor="test")
        locations.ensure_fleet_slots(conn, self.sm, actor="test", force=True)

    def loaded(self, head: str, **fields: Any) -> int:
        out = spools.receive(
            self.ctx,
            spools.Receive.from_dict({**GREEN, "location": "WH:default", **fields}),
        )
        sid = int(out["id"])
        spools.load(self.ctx, "u1", head, sid, printer_write=False)
        return sid

    def loaded_in(self, printer: str, head: str) -> int:
        out = spools.receive(
            self.ctx, spools.Receive.from_dict({**GREEN, "location": "WH:default"})
        )
        sid = int(out["id"])
        spools.load(self.ctx, printer, head, sid, printer_write=False)
        return sid

    def state(self) -> dict[str, Any]:
        fleet = load_fleet()
        assert fleet is not None
        return gates.printer_state(
            self.conn,
            self.fake,
            fleet.printers["u1"],
            drying_policy=None,
            drying_error="not under test",
            now=NOW,
        )

    def bound(self, st: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
        st = st or self.state()
        return {t["slot"]: t["spool"] for t in st["tools"] if t["spool"] is not None}


@pytest.fixture
def plant(printops_db: sqlite3.Connection, fake_spoolman: FakeSpoolman) -> Plant:
    return Plant(printops_db, fake_spoolman)


def test_every_bound_spool_carries_a_sync_verdict_ok_when_in_step(plant: Plant) -> None:
    plant.loaded("T1")
    plant.loaded("T3")
    bound = plant.bound()
    assert sorted(bound) == ["T1", "T3"]
    for slot, sp in bound.items():
        assert sp["sync"] == {"status": "ok", "findings": []}, slot


def test_a_ui_weight_edit_is_a_blocking_drift_finding_on_that_spool_only(
    plant: Plant,
) -> None:
    edited, other = plant.loaded("T1"), plant.loaded("T3")
    plant.fake.update("spool", edited, {"remaining_weight": 412})  # the UI
    bound = plant.bound()
    assert bound["T3"]["id"] == other and bound["T3"]["sync"]["status"] == "ok"
    sync = bound["T1"]["sync"]
    assert bound["T1"]["id"] == edited and sync["status"] == "drift"
    [finding] = sync["findings"]
    assert (finding["kind"], finding["healable"], finding["blocks"]) == (
        "remaining",
        True,
        True,
    )
    assert "412" in finding["summary"]


def test_a_ui_move_without_an_event_is_a_non_blocking_location_finding(
    plant: Plant,
) -> None:
    """The UI moves a loaded spool to a shelf (location text only): PrintOps has no spool.moved
    event for it. Spoolman has the right answer; the bookkeeping is late: the verdict the gates
    would carry for it is non-blocking. (Whether such a spool still counts as bound in the gates
    state depends on the binding source, so only the engine's finding is asserted.)"""
    sid = plant.loaded("T3")
    assert spool_sync.detect(plant.conn, plant.fake).drifts == []
    plant.fake.update("spool", sid, {"location": "WH:A/S1"})
    drifts = spool_sync.detect(plant.conn, plant.fake).drifts
    sync = spool_sync.gate_state(drifts)
    assert sync["status"] == "drift"
    [finding] = sync["findings"]
    assert (finding["kind"], finding["blocks"], finding["healable"]) == (
        "location",
        False,
        True,
    )


def test_building_the_state_writes_nothing(plant: Plant) -> None:
    sid = plant.loaded("T3")
    plant.fake.update("spool", sid, {"remaining_weight": 412})
    rows = plant.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
    events = plant.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    writes = len(plant.fake.writes())
    for _ in range(2):
        assert plant.bound()["T3"]["sync"]["status"] == "drift"
    assert plant.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == rows
    assert plant.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events
    assert len(plant.fake.writes()) == writes


def test_the_snapshot_carries_the_sync_verdict_too(plant: Plant) -> None:
    sid = plant.loaded("T3")
    plant.fake.update("spool", sid, {"remaining_weight": 412})
    fleet = load_fleet()
    assert fleet is not None
    snap = gates.snapshot(plant.conn, plant.fake, fleet, drying_policy=None, now=NOW)
    t3 = {t["slot"]: t for t in snap["printers"]["u1"]["tools"]}["T3"]["spool"]
    assert t3["sync"]["status"] == "drift"


# -- the checks over a hand-built state ---------------------------------------------------------------


REMAINING = {
    "kind": "remaining",
    "summary": "#3 Elegoo PLA: ledger 600.0 g, Spoolman 412.0 g",
    "healable": True,
    "blocks": True,
}
LOCATION = {
    "kind": "location",
    "summary": "#3 Elegoo PLA: moved in Spoolman, no event",
    "healable": True,
    "blocks": False,
}
SLOT_CLASH = {
    "kind": "slot_conflict",
    "summary": "#3 Elegoo PLA: slot u1/T3 also holds #9",
    "healable": False,
    "blocks": True,
}


def drifted(*findings: dict[str, Any], **kw: Any) -> dict[str, Any]:
    sp = spool(3, color="1B5E20", **kw)
    sp["sync"] = {"status": "drift" if findings else "ok", "findings": list(findings)}
    return sp


def checks_of(
    tmp_path: Path,
    st: dict[str, Any],
    *,
    accept: frozenset[str] = frozenset(),
    reason: str = "",
) -> dict[str, preflight.Check]:
    ctx = PrintopsContext(printer="u1", state=st, accept=accept, reason=reason)
    return {c.name: c for c in run_checks(gcode(tmp_path), ctx, now=NOW).checks}


def sync_check(tmp_path: Path, sp: dict[str, Any], **kw: Any) -> preflight.Check:
    return checks_of(tmp_path, state(t0=spool(1), t2=sp), **kw)["spool_in_sync"]


def test_a_blocking_finding_fails_under_block_with_the_fix_in_the_message(
    tmp_path: Path,
) -> None:
    c = sync_check(tmp_path, drifted(REMAINING))
    assert c.status == "FAIL"
    assert "T3" in c.detail and "ledger 600.0 g, Spoolman 412.0 g" in c.detail
    assert "`3d printops spool drift --heal`" in c.detail  # healable: --heal is the fix


def test_an_unhealable_finding_points_at_the_listing_not_at_heal(
    tmp_path: Path,
) -> None:
    c = sync_check(tmp_path, drifted(SLOT_CLASH))
    assert c.status == "FAIL"
    assert "`3d printops spool drift`" in c.detail and "--heal" not in c.detail


def test_every_finding_is_named_but_the_list_is_cut_after_three(tmp_path: Path) -> None:
    many = [{**REMAINING, "summary": f"difference {i}"} for i in range(5)]
    c = sync_check(tmp_path, drifted(*many))
    assert "difference 0; difference 1; difference 2" in c.detail
    assert "difference 3" not in c.detail and "(+2 more)" in c.detail


def test_the_policy_level_decides_between_fail_and_warn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sp = drifted(REMAINING)
    assert sync_check(tmp_path, sp).status == "FAIL"  # fleet/policy.yaml: block
    set_levels(tmp_path, monkeypatch, levels={"spool_in_sync": "warn"})
    assert sync_check(tmp_path, sp).status == "WARN"
    set_levels(tmp_path, monkeypatch, levels={"spool_in_sync": "off"})
    assert "spool_in_sync" not in checks_of(tmp_path, state(t0=spool(1), t2=sp))


def test_a_per_printer_override_wins_over_the_global_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sp = drifted(REMAINING)
    set_levels(tmp_path, monkeypatch, printers={"u1": {"spool_in_sync": "warn"}})
    assert sync_check(tmp_path, sp).status == "WARN"
    set_levels(
        tmp_path,
        monkeypatch,
        levels={"spool_in_sync": "warn"},
        printers={"u1": {"spool_in_sync": "block"}},
    )
    assert sync_check(tmp_path, sp).status == "FAIL"


def test_in_step_passes_and_a_state_without_the_key_is_unverified_never_pass(
    tmp_path: Path,
) -> None:
    assert sync_check(tmp_path, drifted()).status == "PASS"
    old = spool(3, color="1B5E20")
    del old["sync"]  # an older 3d-api does not say
    c = sync_check(tmp_path, old)
    assert c.status == "UNVERIFIED" and "older 3d-api" in c.detail
    broken = spool(3, color="1B5E20")
    broken["sync"] = {"status": "ok"}  # no findings list: not a verdict either
    assert sync_check(tmp_path, broken).status == "UNVERIFIED"


def test_bookkeeping_only_findings_warn_even_under_block(tmp_path: Path) -> None:
    c = sync_check(tmp_path, drifted(LOCATION))
    assert c.status == "WARN" and "moved in Spoolman, no event" in c.detail
    mixed = sync_check(tmp_path, drifted(LOCATION, REMAINING))
    assert mixed.status == "FAIL"  # one blocking finding is enough


def test_an_unbound_slot_adds_no_sync_verdict(tmp_path: Path) -> None:
    got = checks_of(tmp_path, state(t0=spool(1)))
    assert got["spool_in_sync"].status == "PASS"  # T1 is bound and in step
    assert (
        "T3" not in got["spool_in_sync"].detail
    )  # nothing is claimed about the empty slot


def test_filament_sufficient_never_picks_a_side_when_the_weight_drifts(
    tmp_path: Path,
) -> None:
    """600 g would pass the 57 g the plate needs, 30 g would fail it: with a `remaining` finding the
    gate does not know which number is true, so it is UNVERIFIED — never PASS, never FAIL."""
    for grams, plain in ((600.0, "PASS"), (30.0, "FAIL")):
        sp = spool(3, color="1B5E20", remaining=(grams, 10.0, True))
        sp["sync"] = {"status": "ok", "findings": []}
        base = checks_of(tmp_path, state(t0=spool(1), t2=sp))["filament_sufficient"]
        assert base.status == plain, (
            grams,
            base.detail,
        )  # the unchanged verdict without drift

        d = drifted(REMAINING, remaining=(grams, 10.0, True))
        got = checks_of(tmp_path, state(t0=spool(1), t2=d))
        sufficient = got["filament_sufficient"]
        assert sufficient.status == "UNVERIFIED", (grams, sufficient.detail)
        assert "remaining not trustworthy" in sufficient.detail
        assert "ledger 600.0 g, Spoolman 412.0 g" in sufficient.detail
        assert got["spool_in_sync"].status == "FAIL"  # the one place that grades it


def test_a_rolled_back_spoolman_also_leaves_the_weight_unverified(
    tmp_path: Path,
) -> None:
    rolled = {
        "kind": "spoolman_rolled_back",
        "summary": "#3: Spoolman went back in time",
        "healable": False,
        "blocks": True,
    }
    got = checks_of(tmp_path, state(t0=spool(1), t2=drifted(rolled)))
    assert got["filament_sufficient"].status == "UNVERIFIED"
    assert got["spool_in_sync"].status == "FAIL"


def test_a_non_weight_finding_leaves_filament_sufficient_alone(tmp_path: Path) -> None:
    for finding in (LOCATION, SLOT_CLASH):
        got = checks_of(tmp_path, state(t0=spool(1), t2=drifted(finding)))
        assert got["filament_sufficient"].status == "PASS", finding["kind"]


def test_accepting_the_drift_turns_the_hard_finding_into_a_warn_with_the_reason(
    tmp_path: Path,
) -> None:
    sp = drifted(REMAINING)
    assert sync_check(tmp_path, sp).status == "FAIL"  # without --accept: the teeth
    c = sync_check(
        tmp_path,
        sp,
        accept=frozenset({"spool_in_sync"}),
        reason="weighed it on the scale, Spoolman is wrong",
    )
    assert c.status == "WARN"
    assert "accepted with --accept spool_in_sync: weighed it on the scale" in " ".join(
        [c.detail, *c.lines]
    )
    # accepting another check does not accept this one
    other = sync_check(tmp_path, sp, accept=frozenset({"preset_match"}), reason="other")
    assert other.status == "FAIL"


# -- the policy file ------------------------------------------------------------------------------------------


def test_the_committed_policy_grades_spool_in_sync_and_every_check_has_a_level() -> (
    None
):
    body = yaml.safe_load(REPO_POLICY.read_text(encoding="utf-8"))["gates"]
    assert "spool_in_sync" in CHECKS
    pol = parse_gates(body)
    assert pol.level("spool_in_sync", "u1") == "block"
    assert set(body["levels"]) == set(CHECKS)


@pytest.mark.parametrize("level", ["block", "warn", "off"])
def test_policy_accepts_each_level_for_the_new_check(level: str) -> None:
    body = yaml.safe_load(REPO_POLICY.read_text(encoding="utf-8"))["gates"]
    body["levels"]["spool_in_sync"] = level
    body["printers"] = {"u1": {"spool_in_sync": level}}
    pol = parse_gates(body)
    assert pol.level("spool_in_sync", "u1") == level


def test_a_policy_without_spool_in_sync_is_refused_by_name() -> None:
    body = yaml.safe_load(REPO_POLICY.read_text(encoding="utf-8"))["gates"]
    del body["levels"]["spool_in_sync"]
    with pytest.raises(PolicyError, match=r"missing check\(s\) spool_in_sync"):
        parse_gates(body)


def test_a_bad_level_for_the_new_check_is_refused() -> None:
    body = yaml.safe_load(REPO_POLICY.read_text(encoding="utf-8"))["gates"]
    body["levels"]["spool_in_sync"] = "maybe"
    with pytest.raises(PolicyError, match=r"levels\.spool_in_sync must be one of"):
        parse_gates(body)


# -- the whole chain, with teeth ---------------------------------------------------------------------------------


def test_ui_edit_fails_the_gate_and_a_heal_clears_it_end_to_end(
    tmp_path: Path, plant: Plant
) -> None:
    plant.loaded("T1")
    sid = plant.loaded("T3")
    path = gcode(tmp_path)  # needs 57 g from T3 (50 g + purge + margin)

    def verdicts() -> tuple[dict[str, preflight.Check], dict[str, Any]]:
        st = plant.state()
        ctx = PrintopsContext(printer="u1", state=st)
        return {c.name: c for c in run_checks(path, ctx, now=NOW).checks}, st

    clean, st = verdicts()
    assert st["tools"][2]["spool"]["sync"]["status"] == "ok"
    assert clean["spool_in_sync"].status == "PASS"
    assert clean["filament_sufficient"].status == "PASS"

    plant.fake.update(
        "spool", sid, {"remaining_weight": 30}
    )  # the UI says 30 g are left
    drift, st = verdicts()
    assert st["tools"][2]["spool"]["sync"]["status"] == "drift"
    assert drift["spool_in_sync"].status == "FAIL"
    assert "30" in drift["spool_in_sync"].detail
    assert drift["filament_sufficient"].status == "UNVERIFIED"  # no side is picked

    with spool_sync.PASS_LOCK:
        report = spool_sync.heal(plant.conn, plant.sm)
    assert report.ok and [d.kind for d in report.healed] == ["remaining"]

    healed, st = verdicts()
    assert st["tools"][2]["spool"]["sync"] == {"status": "ok", "findings": []}
    assert healed["spool_in_sync"].status == "PASS"
    # the ledger now agrees with Spoolman, so the numbers can be judged again: 30 g cannot print 57 g
    assert healed["filament_sufficient"].status == "FAIL"


# -- bookkeeping lag does not block a print; a real divergence still does ----------------------------------------


def _remaining_drift(cause: str) -> spool_sync.Drift:
    return spool_sync.Drift(3, spool_sync.KIND_REMAINING, "x", True, "", {"cause": cause})


@pytest.mark.parametrize(
    ("cause", "blocks"),
    [
        ("edit", True),  # somebody typed another number: two numbers for one spool
        ("ledger_moved", True),  # PrintOps' own ledger moved without Spoolman
        ("usage", False),  # Moonraker booked a job into Spoolman: it is right, the ledger is late
        ("adopt", False),
        ("first_sync", False),  # a spool that existed before the first pass
    ],
)
def test_only_a_real_weight_divergence_blocks(cause: str, blocks: bool) -> None:
    assert _remaining_drift(cause).blocks is blocks


def test_a_spool_without_a_bookmark_yet_does_not_fail_the_gate(tmp_path: Path, plant: Plant) -> None:
    """Right after the 0105 upgrade no spool has a bookmark until the first pass: a loaded spool whose
    ledger and Spoolman agree must not FAIL `3d printer start` for that."""
    plant.loaded("T1")
    plant.loaded("T3")
    plant.conn.execute("DELETE FROM spool_sync")  # the upgrade: nothing processed yet
    plant.conn.commit()

    st = plant.state()
    for slot, sp in plant.bound(st).items():
        kinds = {f["kind"]: f["blocks"] for f in sp["sync"]["findings"]}
        assert kinds == {"unknown_spool": False, "location": False}, slot  # nothing blocks
    check = checks_of(tmp_path, st)["spool_in_sync"]
    assert check.status == "WARN", check.detail


def test_moonraker_usage_waiting_for_the_next_pass_is_a_warning_not_a_refusal(
    tmp_path: Path, plant: Plant
) -> None:
    """A K1 SE (single head, Moonraker `[spoolman]`) books a job into Spoolman; until the next pass the
    ledger is late. Spoolman is right, so the loaded spool must not FAIL the next start."""
    k1 = plant.loaded_in("k1se", "T1")
    plant.fake.use(k1, use_weight=11.8)
    fleet = load_fleet()
    assert fleet is not None
    st = gates.printer_state(
        plant.conn, plant.fake, fleet.printers["k1se"], drying_policy=None, now=NOW
    )
    [finding] = st["tools"][0]["spool"]["sync"]["findings"]
    assert (finding["kind"], finding["blocks"]) == ("remaining", False)
