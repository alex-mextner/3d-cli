"""Spoolman ↔ PrintOps sync (issue #704): what the Spoolman web UI and Moonraker change in Spoolman
reaches the ledger, events and bindings exactly once; PrintOps' own writes never look like drift;
a mutation behind the system's back is detected and healed.

Everything runs on FakeSpoolman (same wire behaviour as Spoolman 0.27.0: PATCH — the UI edit
form — does not move `last_used`, `/use` and `/measure` do) through the real engine
(printops.spool_sync) and the one write path (printops.spool_mirror.SyncedSpoolman)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fleet_fixtures import write_policy_for_fleet
from printops_fakes import FakeSpoolman

from printops import ledger, locations, spool_sync, spools
from printops import reconcile as rc
from printops.spool_mirror import SyncedSpoolman
from printops.spoolman import Entity, SpoolmanError, ensure_extra_fields

FLEET = """\
k1se:
  model: Creality K1 SE
  owner: own
  access: full
  adapter: moonraker
  host: 127.0.0.1
  port: 9
  heads: [{label: T1, index: 0, usable: true}]
u1:
  model: Snapmaker U1
  owner: own
  access: full
  adapter: moonraker
  host: 127.0.0.1
  port: 9
  heads:
    - {label: T1, index: 0, usable: true}
    - {label: T2, index: 1, usable: true}
    - {label: T3, index: 2, usable: true}
    - {label: T4, index: 3, usable: true}
"""

GREEN = {
    "vendor": "Elegoo",
    "material": "PLA+",
    "name": "RAPID PLA+ Green",
    "color_hex": "#4FE44D",
    "net_g": 1000,
}


@dataclass
class World:
    conn: sqlite3.Connection
    fake: FakeSpoolman
    sm: SyncedSpoolman
    ctx: spools.Ctx

    def receive(self, location: str = "WH:default", **fields: Any) -> int:
        out = spools.receive(
            self.ctx,
            spools.Receive.from_dict({**GREEN, "location": location, **fields}),
        )
        return int(out["id"])

    def detect(self) -> list[spool_sync.Drift]:
        return spool_sync.detect(self.conn, self.sm).drifts

    def kinds(self) -> list[str]:
        return sorted(d.kind for d in self.detect())

    def heal(self, only: int | None = None) -> spool_sync.HealReport:
        with spool_sync.PASS_LOCK:
            return spool_sync.heal(self.conn, self.sm, only=only)

    def rows(self, sid: int) -> list[ledger.Row]:
        return ledger.history(self.conn, sid)

    def events(self, prefix: str = "") -> list[tuple[str, str, dict[str, Any]]]:
        rows = self.conn.execute(
            "SELECT kind, subject, payload FROM events WHERE kind LIKE ? ORDER BY id",
            (prefix + "%",),
        )
        return [(r["kind"], r["subject"], json.loads(r["payload"])) for r in rows]

    def state(self) -> tuple[int, int, int]:
        """(ledger rows, events, Spoolman writes): what a no-op pass must leave untouched."""
        n_rows = self.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
        n_events = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return n_rows, n_events, len(self.fake.writes())

    def remaining(self, sid: int) -> tuple[float, float | None]:
        """(ledger mid, Spoolman remaining)."""
        sp = self.fake.get("spool", sid)
        return ledger.remaining(self.conn, self.fake, sid, spool=sp).mid, sp.get(
            "remaining_weight"
        )


@pytest.fixture
def world(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    printops_db: sqlite3.Connection,
    fake_spoolman: FakeSpoolman,
) -> World:
    fleet = tmp_path / "fleet.yaml"
    fleet.write_text(FLEET, encoding="utf-8")
    monkeypatch.setenv("THREED_FLEET", str(fleet))
    monkeypatch.setenv(
        "PRINTOPS_POLICY", str(write_policy_for_fleet(tmp_path / "policy.yaml", fleet))
    )
    ensure_extra_fields(fake_spoolman)
    sm = SyncedSpoolman(fake_spoolman, printops_db)
    for path, kind in (
        ("WH:default", "warehouse"),
        ("WH:A", "zone"),
        ("WH:A/S1", "shelf"),
    ):
        locations.add(printops_db, path, kind, actor="test")
    locations.ensure_fleet_slots(printops_db, sm, actor="test", force=True)
    return World(printops_db, fake_spoolman, sm, spools.Ctx(printops_db, sm, "test"))


# -- PrintOps' own writes are never drift; the gateway is what makes that true ----------------------


def test_every_own_write_keeps_the_two_sides_in_step(world: World) -> None:
    sid = world.receive()
    steps = [
        lambda: spools.use(world.ctx, sid, 12.5, "gcode_estimate", job_id="j1"),
        lambda: spools.measure(world.ctx, sid, 900.0),
        lambda: spools.move(world.ctx, sid, "WH:A/S1"),
        lambda: spools.load(world.ctx, "u1", "T3", sid, printer_write=False),
        lambda: spools.use(world.ctx, sid, -2.0, "manual", note="correction"),
        lambda: spools.unload(
            world.ctx, "u1", "T3", to="WH:default", gross_g=840.0, printer_write=False
        ),
        lambda: spools.empty(world.ctx, sid, note="ran out"),
    ]
    assert world.detect() == []
    for step in steps:
        step()
        assert world.detect() == [], f"own write left drift after {step}"


def test_a_write_that_bypasses_the_gateway_is_detected(world: World) -> None:
    """Teeth: the same /use that SyncedSpoolman keeps in step is drift when it goes around it."""
    sid = world.receive()
    world.fake.use(
        sid, use_weight=10.0
    )  # not through the gateway: the ledger never hears of it
    [d] = world.detect()
    assert (d.kind, d.spool_id, d.healable) == ("remaining", sid, True)
    assert d.detail["ledger_g"] == 1000.0 and d.detail["spoolman_g"] == 990.0


def test_the_gateway_follows_only_the_fields_a_write_sets(world: World) -> None:
    """A UI edit of the weight that lands before PrintOps' own PATCH of the location stays pending."""
    sid = world.receive()
    world.fake.update("spool", sid, {"remaining_weight": 700})  # the UI
    spools.move(world.ctx, sid, "WH:A/S1")  # PrintOps' write, through the gateway
    assert [d.kind for d in world.detect()] == [
        "remaining"
    ]  # the move is processed, the weight is not


# -- the Spoolman web UI ----------------------------------------------------------------------------


def test_ui_weight_edit_becomes_a_ledger_anchor_and_spoolman_is_not_written_back(
    world: World,
) -> None:
    sid = world.receive()
    world.fake.update(
        "spool", sid, {"remaining_weight": 412}
    )  # the UI edit form: PATCH
    assert world.remaining(sid) == (1000.0, 412.0)
    report = world.heal()
    assert [d.kind for d in report.healed] == ["remaining"] and report.ok
    last = world.rows(sid)[-1]
    assert (last.kind, last.source, last.actor, last.anchor_g) == (
        "anchor",
        "manual",
        "spool-sync",
        412.0,
    )  # a typed number is an estimate: it is not a weighing
    assert last.delta_g == -588.0 and "weight changed in Spoolman" in (last.note or "")
    assert last.confidence_g == pytest.approx(
        spools.MANUAL_REMAINING_FRACTION * 412.0, abs=0.01
    )  # a hand estimate, as `receive --remaining`
    assert ledger.estimate(world.conn, sid).last_weighed_at is None
    assert world.remaining(sid) == (412.0, 412.0)
    assert spools.view(world.conn, world.fake.get("spool", sid))[
        "remaining_text"
    ].startswith("412")
    # nothing weight-shaped was ever sent to Spoolman by the heal
    assert [w for w in world.fake.writes() if w[1].endswith(("/use", "/measure"))] == []
    assert any(
        e[0] == "spool.synced" and e[2]["cause"] == "edit"
        for e in world.events("spool.synced")
    )


def test_ui_measure_dialog_is_a_weighing_too(world: World) -> None:
    sid = world.receive()
    world.fake.measure(
        sid, 600.0
    )  # gross 600 − tare 154 = 446 net, Spoolman's own maths
    world.heal()
    assert world.remaining(sid) == (446.0, 446.0)
    assert world.rows(sid)[-1].source == "weigh"
    assert ledger.estimate(world.conn, sid).last_weighed_at is not None


def test_sync_twice_changes_nothing(world: World) -> None:
    sid = world.receive()
    world.fake.update("spool", sid, {"remaining_weight": 412})
    world.fake.update("spool", sid, {"location": "WH:A/S1"})
    world.heal()
    before = world.state()
    again = world.heal()
    assert again.healed == [] and again.before.drifts == [] and world.state() == before


def test_heal_never_flips_back_a_value_the_user_set(world: World) -> None:
    """The UI move is kept: the heal records it, it does not put the spool back where PrintOps had it."""
    sid = world.receive(location="WH:default")
    world.fake.update("spool", sid, {"location": "WH:A/S1"})
    world.heal()
    assert world.fake.get("spool", sid)["location"] == "WH:A/S1"
    [(kind, _, payload)] = world.events("spool.moved")[-1:]
    assert (kind, payload["from"], payload["to"], payload["via"]) == (
        "spool.moved",
        "WH:default",
        "WH:A/S1",
        "spoolman",
    )


# -- Moonraker books usage into Spoolman -----------------------------------------------------------


def _in_k1se(world: World, sid: int) -> None:
    spools.load(world.ctx, "k1se", "T1", sid, printer_write=False)


def test_moonraker_usage_is_booked_once_as_moonraker_live(world: World) -> None:
    sid = world.receive()
    _in_k1se(world, sid)
    world.fake.use(sid, use_weight=11.8)  # Moonraker [spoolman]: PUT /spool/{id}/use
    [d] = world.detect()
    assert d.detail["cause"] == "usage"
    world.heal()
    row = world.rows(sid)[-1]
    assert (row.kind, row.source, row.delta_g, row.actor) == (
        "delta",
        "moonraker_live",
        pytest.approx(-11.8),
        "spool-sync",
    )
    assert world.remaining(sid) == (pytest.approx(988.2), pytest.approx(988.2))
    n = len(world.rows(sid))
    world.heal()
    assert len(world.rows(sid)) == n  # second pass: not booked again
    assert [e for e in world.events("spool.used") if e[2].get("observed")][-1][2][
        "grams"
    ] == pytest.approx(11.8)


def test_moonraker_usage_and_ledger_usage_are_not_double_counted(world: World) -> None:
    sid = world.receive()
    _in_k1se(world, sid)
    world.fake.use(sid, use_weight=20.0)  # Moonraker
    spools.use(
        world.ctx, sid, 5.0, "manual"
    )  # PrintOps books a correction through the gateway
    world.heal()
    world.fake.use(sid, use_weight=3.0)  # Moonraker again
    world.heal()
    mid, spoolman = world.remaining(sid)
    assert spoolman == pytest.approx(972.0)  # 1000 − 20 − 5 − 3, counted once each
    assert mid == pytest.approx(972.0)
    assert world.detect() == []


def test_ledger_only_booking_then_moonraker_landing_is_not_double_counted(
    world: World,
) -> None:
    """The job ledger (print-queue spec) books a Moonraker printer's usage ledger-only; when Moonraker's
    own /use lands in Spoolman the two agree and nothing is booked twice."""
    sid = world.receive()
    _in_k1se(world, sid)
    ledger.observe_usage(
        world.conn, sid, -11.8, "gcode_estimate", actor="jobs", note="job:1"
    )
    assert [d.kind for d in world.detect()] == [
        "remaining"
    ]  # Spoolman has not heard yet
    world.fake.use(sid, use_weight=11.8)  # Moonraker
    assert world.detect() == []
    assert len(world.rows(sid)) == 2  # the receive anchor and the one job row


def test_usage_on_a_spool_outside_a_moonraker_printer_is_a_weighing_not_moonraker(
    world: World,
) -> None:
    sid = world.receive()  # in storage: nothing books usage to it
    world.fake.use(sid, use_weight=30.0)  # the UI's "use weight" dialog
    [d] = world.detect()
    assert d.detail["cause"] == "edit"
    world.heal()
    assert world.rows(sid)[-1].source == "weigh"
    assert world.remaining(sid) == (pytest.approx(970.0), pytest.approx(970.0))


# -- create / archive / delete -----------------------------------------------------------------------


def _ui_create(world: World, **fields: Any) -> int:
    fil = world.fake.create(
        "filament",
        {
            "name": "Black",
            "material": "PLA",
            "density": 1.24,
            "diameter": 1.75,
            "weight": 1000,
            "spool_weight": 200,
        },
    )
    body = {
        "filament_id": fil["id"],
        "initial_weight": 1000,
        "spool_weight": 200,
        "location": "WH:default",
        **fields,
    }
    return int(world.fake.create("spool", body)["id"])


def test_a_spool_created_in_the_ui_is_adopted_and_anchored(world: World) -> None:
    sid = _ui_create(world)
    assert sorted(d.kind for d in world.detect()) == ["remaining", "unknown_spool"]
    world.heal()
    assert world.detect() == []
    first = world.rows(sid)[0]
    assert (first.kind, first.source, first.anchor_g, first.actor) == (
        "anchor",
        "receive",
        1000.0,
        "spool-sync",
    )  # sealed
    assert ("spool.adopted", f"spool:{sid}") in [
        (k, s) for k, s, _ in world.events("spool.adopted")
    ]
    # a spool that arrives already used is a weighing, not a sealed one
    used = _ui_create(world, used_weight=300.0)
    world.heal()
    assert world.rows(used)[0].source == "manual" and world.remaining(used) == (
        700.0,
        700.0,
    )  # nobody weighed it: an estimate
    assert ledger.estimate(world.conn, used).last_weighed_at is None


def test_archiving_in_the_ui_is_recorded_and_unbinds_a_loaded_spool(
    world: World,
) -> None:
    sid = world.receive()
    spools.load(world.ctx, "u1", "T3", sid, printer_write=False)
    world.fake.update("spool", sid, {"archived": True})
    assert sorted(d.kind for d in world.detect()) == [
        "archived",
        "location",
    ]  # still in the slot
    world.heal()
    assert world.detect() == []
    sp = world.fake.get("spool", sid)
    assert sp["archived"] is True and not sp.get("location")  # taken out of the slot
    kinds = [e[0] for e in world.events()]
    assert "spool.archived" in kinds and "slot.unloaded" in kinds
    assert "archived in Spoolman" in (world.rows(sid)[-1].note or "")
    world.fake.update("spool", sid, {"archived": False})
    world.heal()
    assert "spool.unarchived" in [e[0] for e in world.events()]


def test_deleting_in_the_ui_is_recorded_after_a_404_and_frees_the_slot(
    world: World,
) -> None:
    sid, keep = world.receive(), world.receive()
    spools.load(world.ctx, "u1", "T3", sid, printer_write=False)
    world.fake.delete("spool", sid)
    [d] = world.detect()
    assert (d.kind, d.spool_id) == ("spool_deleted", sid)
    world.heal()
    assert world.detect() == []
    assert "deleted in Spoolman" in (world.rows(sid)[-1].note or "")
    unloaded = [e for e in world.events("slot.unloaded") if e[2]["spool_id"] == sid]
    assert unloaded and unloaded[-1][2]["why"] == "spool deleted in Spoolman"
    assert world.rows(keep)  # the other spool is untouched
    world.heal()  # and it stays recorded: not reported again
    assert world.detect() == []


def test_a_spoolman_that_lost_most_spools_is_not_taken_for_a_mass_delete(
    world: World,
) -> None:
    ids = [world.receive() for _ in range(5)]
    for sid in ids[:4]:
        world.fake.delete(
            "spool", sid
        )  # a wiped Spoolman, or SPOOLMAN_URL pointing at another one
    [d] = world.detect()
    assert d.kind == "spoolman_empty" and d.healable is False and d.spool_id is None
    before = world.state()
    assert (
        world.heal().ok is False and world.state() == before
    )  # nothing recorded as deleted
    assert (
        world.conn.execute(
            "SELECT COUNT(*) FROM spool_sync WHERE state = 'deleted'"
        ).fetchone()[0]
        == 0
    )


def test_a_deleted_spool_that_comes_back_is_adopted_again(world: World) -> None:
    sid, _other = world.receive(), world.receive()
    saved = dict(world.fake.items["spool"][sid])
    world.fake.delete("spool", sid)
    world.heal()
    world.fake.items["spool"][sid] = (
        saved  # Spoolman restored from a backup that has it
    )
    assert "spool_returned" in world.kinds()
    world.heal()
    assert world.detect() == [] and "spool.returned" in [e[0] for e in world.events()]


def test_spoolman_restored_from_a_backup_is_never_silently_reanchored(
    world: World,
) -> None:
    sid = world.receive()
    world.fake.use(sid, use_weight=50.0)
    world.heal()  # PrintOps has seen last_used = now
    row = world.fake.items["spool"][sid]
    row["used_weight"] = 0.0  # the backup: more filament …
    row["last_used"] = "2020-01-01T00:00:00Z"  # … and an older last use
    [d] = world.detect()
    assert (d.kind, d.healable) == ("spoolman_rolled_back", False)
    before = world.state()
    world.heal()
    assert (
        world.state() == before
    )  # PrintOps does not follow Spoolman backwards by itself


# -- location and slot ------------------------------------------------------------------------------


def test_ui_move_of_a_loaded_spool_unbinds_it_and_keeps_the_users_place(
    world: World,
) -> None:
    sid = world.receive()
    spools.load(world.ctx, "u1", "T3", sid, printer_write=False)
    world.fake.update("spool", sid, {"location": "WH:A/S1"})
    [d] = world.detect()
    assert d.kind == "location" and "moved" in d.detail["change"]
    world.heal()
    assert (
        world.fake.get("spool", sid)["location"] == "WH:A/S1"
    )  # PrintOps did not move it back
    unloaded = [e for e in world.events("slot.unloaded") if e[2]["spool_id"] == sid]
    assert (
        unloaded[-1][2]["why"] == "moved in Spoolman"
        and unloaded[-1][1] == "slot:u1/T3"
    )
    assert world.detect() == []


def test_typing_a_slot_location_in_the_ui_loads_the_slot(world: World) -> None:
    sid = world.receive()
    world.fake.update("spool", sid, {"location": "slot:k1se/T1"})
    world.heal()
    loaded = [e for e in world.events("slot.loaded") if e[2]["spool_id"] == sid]
    assert loaded[-1][1] == "slot:k1se/T1" and loaded[-1][2]["via"] == "spoolman"
    assert world.detect() == []


def test_two_spools_in_one_slot_is_a_conflict_only_a_person_resolves(
    world: World,
) -> None:
    a, b = world.receive(), world.receive()
    world.fake.update("spool", a, {"location": "slot:u1/T3"})
    world.fake.update("spool", b, {"location": "slot:u1/T3"})
    drifts = [d for d in world.detect() if d.kind == "slot_conflict"]
    assert sorted(d.spool_id for d in drifts if d.spool_id) == [a, b] and not any(
        d.healable for d in drifts
    )
    assert world.heal().ok is False


def test_a_location_typed_in_the_ui_that_printops_does_not_know_is_unhealable_until_it_does(
    world: World,
) -> None:
    sid = world.receive()
    world.fake.update("spool", sid, {"location": "Shelf 3"})
    [d] = world.detect()
    assert (d.kind, d.healable) == ("location_unknown", False)
    report, _ = spool_sync.run_pass(world.conn, world.sm)
    assert not report.ok
    [anomaly] = rc.list_anomalies(world.conn, scope=spool_sync.SCOPE)
    assert (anomaly["kind"], anomaly["subject"], anomaly["status"]) == (
        "location_unknown",
        f"spool:{sid}",
        "open",
    )
    locations.add(world.conn, "WH:Shelf3", "shelf", actor="test")
    world.fake.update("spool", sid, {"location": "WH:Shelf3"})
    spool_sync.run_pass(world.conn, world.sm)
    assert (
        rc.list_anomalies(world.conn, scope=spool_sync.SCOPE) == []
    )  # cleared by itself
    assert (
        rc.list_anomalies(world.conn, statuses=("resolved",), scope=spool_sync.SCOPE)[
            0
        ]["kind"]
        == "location_unknown"
    )


def test_a_failed_spoolman_move_repair_is_healed_by_the_pass(world: World) -> None:
    """A PrintOps move whose Spoolman writes failed leaves the spools on the former path."""
    sid = world.receive(location="WH:A/S1")
    shelf = locations.get(world.conn, "WH:A/S1")
    world.fake.down = True
    locations.move(world.conn, world.sm, shelf, "WH:A/S2", actor="test")
    world.fake.down = False
    [d] = world.detect()
    assert d.kind == "location" and "former_path" in d.detail["change"]
    world.heal()
    assert (
        world.fake.get("spool", sid)["location"] == "WH:A/S2" and world.detect() == []
    )


# -- derived values ---------------------------------------------------------------------------------


def test_a_spool_without_a_native_tare_gets_the_one_printops_knows(
    world: World,
) -> None:
    sid = _ui_create(world)
    world.fake.update("spool", sid, {"spool_weight": None})
    world.fake.items["filament"][world.fake.get("spool", sid)["filament"]["id"]][
        "spool_weight"
    ] = None
    vendor = world.fake.create("vendor", {"name": "Elegoo"})
    fil = world.fake.items["filament"][world.fake.get("spool", sid)["filament"]["id"]]
    fil["vendor_id"] = vendor["id"]
    fil["weight"] = 1000
    assert "tare" in world.kinds()
    world.heal()
    sp = world.fake.get("spool", sid)
    assert (
        sp["spool_weight"] == 154.0
        and "SpoolmanDB" in spools.view(world.conn, sp)["tare"]["source"]
    )
    assert world.detect() == []


def test_a_hand_edited_confidence_cache_is_rewritten_from_the_ledger(
    world: World,
) -> None:
    sid = world.receive()
    ledger.sync_confidence(world.conn, world.sm, sid, world.fake.get("spool", sid))
    world.fake.update("spool", sid, {"extra": {"remaining_confidence_g": "999.0"}})
    assert world.kinds() == ["confidence"]
    world.heal()
    assert (
        json.loads(world.fake.get("spool", sid)["extra"]["remaining_confidence_g"])
        == 20.0
    )
    assert world.detect() == []


# -- the ledger side mutated behind the system's back ---------------------------------------------------


def test_a_ledger_row_that_never_reached_spoolman_is_detected_and_healed(
    world: World,
) -> None:
    sid = world.receive()
    ledger.observe_usage(
        world.conn, sid, -40.0, "manual", actor="intruder"
    )  # a row Spoolman never heard of
    [d] = [x for x in world.detect() if x.kind == "remaining"]
    assert (d.kind, d.detail["cause"]) == ("remaining", "ledger_moved")
    world.heal()
    assert (
        world.remaining(sid) == (1000.0, 1000.0) and world.detect() == []
    )  # Spoolman is the authority


# -- a pass that fails or races ----------------------------------------------------------------------


def test_a_refused_spoolman_write_rolls_the_spool_back_and_opens_an_anomaly(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = world.receive()
    world.fake.update("spool", sid, {"remaining_weight": 500})
    real = world.fake.update

    def refuse(entity: Entity, item_id: int, data: Any) -> Any:
        if "extra" in data:
            raise SpoolmanError("Spoolman is busy", status=503)
        return real(entity, item_id, data)

    monkeypatch.setattr(world.fake, "update", refuse)
    before = len(world.rows(sid))
    report, _ = spool_sync.run_pass(world.conn, world.sm)
    assert [f["spool_id"] for f in report.failed] == [sid] and len(
        world.rows(sid)
    ) == before  # the anchor rolled back with it
    [anomaly] = rc.list_anomalies(world.conn, scope=spool_sync.SCOPE)
    assert anomaly["evidence"]["heal_error"] == "Spoolman is busy"
    monkeypatch.setattr(world.fake, "update", real)
    spool_sync.run_pass(
        world.conn, world.sm
    )  # retried by the next pass; the anomaly clears
    assert (
        world.detect() == []
        and rc.list_anomalies(world.conn, scope=spool_sync.SCOPE) == []
    )


def test_a_change_that_lands_while_a_spool_is_healed_is_the_next_passes_not_lost(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = world.receive()
    world.fake.update("spool", sid, {"remaining_weight": 800})
    real_get, landed = world.fake.get, list[bool]()

    def get_then_somebody_edits(entity: Entity, item_id: int) -> Any:
        out = real_get(entity, item_id)
        if entity == "spool" and not landed:
            landed.append(True)
            world.fake.update(
                "spool", sid, {"remaining_weight": 600}
            )  # lands right after the read
        return out

    monkeypatch.setattr(world.fake, "get", get_then_somebody_edits)
    first = world.heal()
    assert [d.kind for d in first.after.drifts] == [
        "remaining"
    ]  # the late edit is visible, not swallowed
    monkeypatch.setattr(world.fake, "get", real_get)
    world.heal()
    assert world.remaining(sid) == (600.0, 600.0) and world.detect() == []
    anchors = [r.anchor_g for r in world.rows(sid) if r.kind == "anchor"]
    assert anchors == [1000.0, 800.0, 600.0]


def test_gateway_use_does_not_swallow_a_usage_that_landed_just_before_it(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = world.receive()
    _in_k1se(world, sid)
    real = world.fake.use
    landed = list[bool]()

    def moonraker_first(spool_id: int, **kw: Any) -> Any:
        if not landed:
            landed.append(True)
            real(
                spool_id, use_weight=3.0
            )  # Moonraker's 5 s report lands just before PrintOps' /use
        return real(spool_id, **kw)

    monkeypatch.setattr(world.fake, "use", moonraker_first)
    spools.use(world.ctx, sid, 10.0, "manual")
    assert world.remaining(sid) == (990.0, 987.0)
    [d] = world.detect()
    assert (
        d.detail["delta_g"] == -3.0
    )  # the 3 g are a pending difference, not part of PrintOps' booking
    world.heal()
    assert world.remaining(sid) == (987.0, 987.0)


def test_the_scheduled_pass_status_and_anomalies_survive_spoolman_down(
    world: World,
) -> None:
    sid = world.receive()
    world.fake.update("spool", sid, {"location": "Nowhere"})
    spool_sync.run_pass(world.conn, world.sm)
    assert len(rc.list_anomalies(world.conn, scope=spool_sync.SCOPE)) == 1
    world.fake.down = True
    with pytest.raises(SpoolmanError):
        spool_sync.run_pass(world.conn, world.sm)
    assert (
        len(rc.list_anomalies(world.conn, scope=spool_sync.SCOPE)) == 1
    )  # not "resolved" because we could not look


# -- review fixes: the bookmark never moves past what was not healed -----------------------------------


def _slot_events(world: World, sid: int) -> list[tuple[str, str]]:
    return [
        (kind, subject)
        for kind, subject, payload in world.events("slot.")
        if payload["spool_id"] == sid
    ]


def test_a_weight_edit_with_an_unknown_location_keeps_the_unload_until_the_location_exists(
    world: World,
) -> None:
    """One UI save types a new weight and a shelf PrintOps does not know, on a spool that was in a slot:
    the weight is healed, the move is not, and once the shelf exists the unload of the slot is recorded."""
    sid = world.receive()
    spools.load(world.ctx, "u1", "T3", sid, printer_write=False)
    world.fake.update("spool", sid, {"remaining_weight": 412, "location": "WH:Q/S9"})
    report = world.heal()
    assert [d.kind for d in report.healed] == ["remaining"]
    assert [d.kind for d in world.detect()] == ["location_unknown"]
    assert ("slot.unloaded", "slot:u1/T3") not in _slot_events(world, sid)[-1:]

    locations.add(world.conn, "WH:Q", "zone", actor="test")
    locations.add(world.conn, "WH:Q/S9", "shelf", actor="test")
    world.heal()

    assert world.detect() == []
    assert _slot_events(world, sid)[-1] == ("slot.unloaded", "slot:u1/T3")
    moved = [e for e in world.events("spool.moved") if e[1] == f"spool:{sid}"][-1][2]
    assert (moved["from"], moved["to"]) == ("slot:u1/T3", "WH:Q/S9")


def test_a_slot_clash_records_nothing_and_the_load_is_recorded_once_the_clash_clears(
    world: World,
) -> None:
    """B is typed into a slot A holds: no move or load of B is booked while two spools claim it; when A
    leaves, B's move and its slot.loaded come, once."""
    a, b = world.receive(), world.receive()
    spools.load(world.ctx, "u1", "T3", a, printer_write=False)
    world.fake.update("spool", b, {"location": "slot:u1/T3"})
    moves_before = len(world.events("spool.moved"))
    assert [d.kind for d in world.detect() if d.spool_id == b] == ["slot_conflict"]
    world.heal()
    assert len(world.events("spool.moved")) == moves_before
    assert ("slot.loaded", "slot:u1/T3") not in _slot_events(world, b)

    world.fake.update("spool", a, {"location": "WH:A/S1"})  # A leaves the slot in the UI
    world.heal()

    assert world.detect() == []
    assert _slot_events(world, b)[-1] == ("slot.loaded", "slot:u1/T3")
    assert _slot_events(world, a)[-1] == ("slot.unloaded", "slot:u1/T3")
    world.heal()
    assert len([e for e in _slot_events(world, b) if e[0] == "slot.loaded"]) == 1


def test_an_archived_spool_with_a_location_printops_does_not_know_is_not_drift(
    world: World,
) -> None:
    """Spoolman keeps the free-text location of every archived spool; they must not keep `spool drift`
    and `health` failing for good."""
    sid = world.receive()
    world.fake.update("spool", sid, {"archived": True, "location": "old box 7"})
    world.heal()
    assert world.detect() == []

    other = world.receive(location="WH:A/S1")
    world.fake.update("spool", other, {"archived": True})
    world.heal()
    locations.archive(world.conn, world.sm, locations.get(world.conn, "WH:A/S1"), actor="test")
    assert [d for d in world.detect() if d.spool_id == other] == []
    assert rc.list_anomalies(world.conn, scope=spool_sync.SCOPE) == []


def test_a_spool_that_is_not_archived_is_still_reported_for_an_unknown_location(world: World) -> None:
    sid = world.receive()
    world.fake.update("spool", sid, {"location": "old box 7"})
    assert [d.kind for d in world.detect()] == ["location_unknown"]


def _wipe_most(world: World) -> tuple[list[int], int]:
    ids = [world.receive() for _ in range(5)]
    for sid in ids[:4]:
        world.fake.delete("spool", sid)
    return ids[:4], ids[4]


def test_a_spoolman_that_looks_wiped_heals_no_spool_at_all(world: World) -> None:
    """The guard is for a wrong SPOOLMAN_URL or a wiped Spoolman: the survivor shares its id with
    a foreign spool, so nothing may be anchored, emitted or written until a person confirms."""
    gone, alive = _wipe_most(world)
    world.fake.update("spool", alive, {"remaining_weight": 412})
    before = world.state()

    report = world.heal()
    assert report.healed == [] and report.failed == []
    assert world.state() == before
    assert "spoolman_empty" in [d.kind for d in report.after.drifts]
    only = world.heal(only=alive)  # a single-spool heal is no way around the guard
    assert only.healed == [] and world.state() == before
    spool_sync.run_pass(world.conn, world.sm)  # the scheduled pass opens the anomaly, nothing else
    rows, _, writes = world.state()
    assert (rows, writes) == (before[0], before[2])
    assert "spoolman_empty" in world.kinds()


def test_confirming_the_deletions_records_them_and_lifts_the_guard(world: World) -> None:
    """The way out of `spoolman_empty`: the owner confirms; each id is checked with a 404 and recorded
    as deleted, after which the survivor heals again."""
    gone, alive = _wipe_most(world)
    world.fake.update("spool", alive, {"remaining_weight": 412})

    with spool_sync.PASS_LOCK:
        result = spool_sync.confirm_deleted(world.conn, world.sm)

    assert sorted(result.confirmed) == gone and result.still_present == []
    assert result.may_be_other_spools == [alive]  # Spoolman still lists it: check it, ids can collide
    deleted = world.events("spool.deleted")
    assert sorted(int(e[1].split(":")[1]) for e in deleted) == gone
    assert "spoolman_empty" not in world.kinds()
    assert [d.kind for d in world.heal().healed] == ["remaining"]
    assert world.detect() == []


def test_confirming_leaves_a_spool_that_spoolman_still_has(world: World) -> None:
    gone, alive = _wipe_most(world)
    with spool_sync.PASS_LOCK:
        result = spool_sync.confirm_deleted(world.conn, world.sm)
    assert alive not in result.confirmed
    assert world.conn.execute(
        "SELECT COUNT(*) FROM spool_sync WHERE state = 'deleted'"
    ).fetchone()[0] == len(gone)


# -- review fixes, round 3 ----------------------------------------------------------------------------------------


def test_an_archived_spool_that_leaves_its_slot_for_free_text_books_the_move_and_the_unload(
    world: World,
) -> None:
    """In one UI save a spool in a slot gets the text "bin" and is archived: the bookmark moves to "bin",
    so the unload of the slot must be booked then, or exposure charges the slot's jobs to it for good."""
    sid = world.receive()
    spools.load(world.ctx, "u1", "T3", sid, printer_write=False)
    world.fake.update("spool", sid, {"archived": True, "location": "bin"})

    world.heal()

    assert world.detect() == []
    assert _slot_events(world, sid)[-1] == ("slot.unloaded", "slot:u1/T3")
    moved = [e for e in world.events("spool.moved") if e[1] == f"spool:{sid}"][-1][2]
    assert moved["from"] == "slot:u1/T3" and moved["via"] == "spoolman"
    world.heal()
    assert len([e for e in _slot_events(world, sid) if e[0] == "slot.unloaded"]) == 1  # booked once


@pytest.mark.parametrize(
    ("anchored", "was", "now", "weighing"),
    [
        (True, "2026-11-01T10:00:00Z", "2026-11-02T10:00:00Z", True),  # /measure moved last_used forward
        (True, None, "2026-11-02T10:00:00Z", True),
        (True, "2026-11-01T10:00:00Z", "2026-11-01T10:00:00Z", False),  # PATCH of the edit form
        (True, "2026-11-01T10:00:00Z", None, False),  # last_used cleared: not a weighing
        (False, "2026-11-01T10:00:00Z", "2026-11-02T10:00:00Z", False),  # never anchored: nothing to compare
    ],
)
def test_only_a_forward_move_of_last_used_on_an_anchored_spool_is_a_weighing(
    anchored: bool, was: str | None, now: str | None, weighing: bool
) -> None:
    from printops.spool_mirror import PRESENT, Seen

    spool = {"id": 1, "last_used": now}
    seen = Seen(1, PRESENT, 500.0, was, False, "")
    assert spool_sync._is_weighing(spool, seen, "edit", anchored) is weighing
    assert spool_sync._is_weighing(spool, seen, "first_sync", anchored) is False
    assert spool_sync._is_weighing(spool, None, "edit", anchored) is False
