"""`spool load` / `unload` and Moonraker's active spool: a printer whose Moonraker has `[spoolman]` (the
rooted K1 SE) books usage to ONE active spool itself, so loading a spool must make it the active one
(through Moonraker's own endpoint, read back) and unloading must clear it, only when it is that spool.
Only loopback fakes (FakeMoonraker, FakeSpoolman)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fleet_fixtures import k1se_moonraker_entry, u1_entry, write_fleet, write_policy_for_fleet
from printer_fakes import FakeMoonraker
from printops_fakes import FakeSpoolman

from printops import head_sync, locations, spools
from printops import reconcile as rc
from printops.api import routes_reconcile as routes
from printops.cli.spool import fmt_printer_write, printer_write_warnings
from printops.client import ApiClient
from printops.fleet import load_fleet

BLACK = {
    "vendor": "Elegoo",
    "material": "PLA+",
    "name": "RAPID PLA+ Black",
    "color_hex": "#000000",
    "net_g": 1000,
    "location": "WH:default",
}
WHITE = BLACK | {"name": "RAPID PLA+ White", "color_hex": "#FFFFFF"}
SPOOLMAN_PATHS = ("/server/spoolman/status", "/server/spoolman/spool_id")


@pytest.fixture
def k1() -> Iterator[FakeMoonraker]:
    """The rooted K1 SE: Moonraker with `[spoolman]`, spool #14 (white) selected, no print_task_config."""
    fake = FakeMoonraker(spoolman=True, active_spool=14)
    fake.status.pop("print_task_config")
    with fake.serve() as served:
        yield served


@pytest.fixture
def u1() -> Iterator[FakeMoonraker]:
    """The U1 with `[spoolman]` switched on, to prove a four-head printer's active spool is not touched."""
    fake = FakeMoonraker(spoolman=True, active_spool=14)
    with fake.serve() as served:
        yield served


@pytest.fixture
def use_fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    def apply(*entries: str) -> None:
        fleet = write_fleet(tmp_path / "fleet.yaml", *entries)
        monkeypatch.setenv("THREED_FLEET", str(fleet))
        monkeypatch.setenv("PRINTOPS_POLICY", str(write_policy_for_fleet(tmp_path / "policy.yaml", fleet)))

    monkeypatch.delenv("PRINTOPS_CALIBRATIONS", raising=False)
    return apply


@pytest.fixture
def po(
    use_fleet: Callable[..., None],
    k1: FakeMoonraker,
    u1: FakeMoonraker,
    printops_db: sqlite3.Connection,
    api_client: ApiClient,
) -> ApiClient:
    use_fleet(k1se_moonraker_entry(k1), u1_entry(u1))
    locations.add(printops_db, "WH:default", "warehouse", actor="test")
    return api_client


def _events(conn: sqlite3.Connection, kind: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT payload FROM events WHERE kind = ? ORDER BY id", (kind,))
    return [json.loads(r["payload"]) for r in rows]


def _spoolman_writes(moon: FakeMoonraker) -> list[str]:
    return [r.path for r in moon.writes() if r.path in SPOOLMAN_PATHS]


def _anomalies(conn: sqlite3.Connection, spoolman: FakeSpoolman) -> list[rc.Finding]:
    fleet = load_fleet()
    assert fleet is not None
    binds = rc.bindings(spools.all_spools(spoolman))
    found = rc.compare_printer(fleet.printers["k1se"], None, binds, failures=rc.write_failures(conn))
    return [f for f in found if f.kind == "printer_write_failed"]


# ---- load -------------------------------------------------------------------------------------


def test_load_makes_the_spool_the_moonraker_active_spool_on_a_k1(
    po: ApiClient, k1: FakeMoonraker, u1: FakeMoonraker, printops_db: sqlite3.Connection
) -> None:
    sid = po.post("/v1/spools", BLACK)["id"]
    got = po.post("/v1/printers/k1se/slots/T1/load", {"spool_id": sid})

    assert got["slot"] == "k1se/T1"
    assert k1.active_spool == sid  # was #14 (white)
    pw = got["printer_write"]
    assert pw["active_spool"] == {
        "status": "written",
        "reason": None,
        "spool_id": sid,
        "before": 14,
        "after": sid,
    }
    assert pw["status"] == "skipped" and "print_task_config" in pw["reason"]  # no head to write on a K1
    assert k1.scripts == [] and _spoolman_writes(k1) == ["/server/spoolman/spool_id"]
    assert u1.requests == []
    written = _events(printops_db, "slot.printer_written")
    assert [(e["printer"], e["head"], e["spool_id"], e["active_spool"]["after"]) for e in written] == [
        ("k1se", "T1", sid, sid)
    ]
    assert _events(printops_db, "slot.printer_write_failed") == []


def test_replacing_the_occupant_makes_the_new_spool_active(po: ApiClient, k1: FakeMoonraker) -> None:
    black, white = po.post("/v1/spools", BLACK)["id"], po.post("/v1/spools", WHITE)["id"]
    po.post("/v1/printers/k1se/slots/T1/load", {"spool_id": black})
    po.post("/v1/printers/k1se/slots/T1/load", {"spool_id": white, "replace_to": "WH:default"})
    assert k1.active_spool == white


def test_printer_write_false_leaves_the_active_spool_alone_on_load_and_unload(
    po: ApiClient, k1: FakeMoonraker
) -> None:
    sid = po.post("/v1/spools", BLACK)["id"]
    got = po.post("/v1/printers/k1se/slots/T1/load", {"spool_id": sid, "printer_write": False})
    assert got["printer_write"]["status"] == "skipped" and got["printer_write"]["active_spool"] is None
    k1.active_spool = sid
    po.post("/v1/printers/k1se/slots/T1/unload", {"printer_write": False})
    assert k1.active_spool == sid and k1.requests == []


def test_a_multi_head_printer_is_skipped_without_contacting_its_spoolman(
    po: ApiClient, u1: FakeMoonraker
) -> None:
    """Moonraker has one active spool and no idea which of the U1's four heads extrudes."""
    sid = po.post("/v1/spools", BLACK)["id"]
    got = po.post("/v1/printers/u1/slots/T3/load", {"spool_id": sid})
    active = got["printer_write"]["active_spool"]
    assert active["status"] == "skipped" and "u1 has 4 heads" in active["reason"]
    assert got["printer_write"]["status"] == "written"  # the head itself is still written
    assert u1.active_spool == 14
    assert all(not r.path.startswith("/server/spoolman") for r in u1.requests)
    unloaded = po.post("/v1/printers/u1/slots/T3/unload", {})
    assert unloaded["printer_write"]["active_spool"]["status"] == "skipped"
    assert all(not r.path.startswith("/server/spoolman") for r in u1.requests)


def test_a_moonraker_without_spoolman_is_skipped_quietly(
    po: ApiClient, k1: FakeMoonraker, printops_db: sqlite3.Connection
) -> None:
    k1.spoolman = False
    sid = po.post("/v1/spools", BLACK)["id"]
    got = po.post("/v1/printers/k1se/slots/T1/load", {"spool_id": sid})
    active = got["printer_write"]["active_spool"]
    assert got["slot"] == "k1se/T1"
    assert active["status"] == "skipped" and "no [spoolman] section" in active["reason"]
    assert _events(printops_db, "slot.printer_write_failed") == []


def test_a_single_head_printer_with_head_filament_writes_the_head_then_the_active_spool(
    use_fleet: Callable[..., None],
    po: ApiClient,
    u1: FakeMoonraker,
    printops_db: sqlite3.Connection,
) -> None:
    use_fleet(u1_entry(u1, heads=1))
    sid = po.post("/v1/spools", BLACK)["id"]
    got = po.post("/v1/printers/u1/slots/T1/load", {"spool_id": sid})
    assert got["printer_write"]["status"] == "written"
    assert got["printer_write"]["active_spool"]["status"] == "written"
    assert u1.active_spool == sid and len(u1.scripts) == 1
    assert len(_events(printops_db, "slot.printer_written")) == 2  # head, then active spool


def test_a_failed_head_write_does_not_stop_the_active_spool_and_stays_the_failure_reported(
    use_fleet: Callable[..., None],
    po: ApiClient,
    u1: FakeMoonraker,
    printops_db: sqlite3.Connection,
) -> None:
    """The head's filament and Moonraker's active spool are separate writes: the good one neither
    waits for the failed one nor hides it from the reconciler."""
    use_fleet(u1_entry(u1, heads=1))
    u1.script_errors["SET_PRINT_FILAMENT_CONFIG"] = "extruder busy"
    sid = po.post("/v1/spools", BLACK)["id"]
    got = po.post("/v1/printers/u1/slots/T1/load", {"spool_id": sid})
    assert got["printer_write"]["status"] == "failed"
    assert got["printer_write"]["active_spool"]["status"] == "written" and u1.active_spool == sid
    assert [e["stage"] for e in _events(printops_db, "slot.printer_write_failed")] == ["filament"]
    assert {k: f.stage for k, f in rc.write_failures(printops_db).items()} == {"u1/T1": "filament"}


def test_a_failed_active_spool_is_not_hidden_by_a_good_head_write(
    use_fleet: Callable[..., None], po: ApiClient, u1: FakeMoonraker, printops_db: sqlite3.Connection
) -> None:
    use_fleet(u1_entry(u1, heads=1))
    u1.spoolman_set_error = 500
    sid = po.post("/v1/spools", BLACK)["id"]
    got = po.post("/v1/printers/u1/slots/T1/load", {"spool_id": sid})
    assert got["printer_write"]["status"] == "written"
    assert got["printer_write"]["active_spool"]["status"] == "failed"
    assert {k: f.stage for k, f in rc.write_failures(printops_db).items()} == {
        rc.active_key("u1/T1"): "active_spool"
    }

    u1.spoolman_set_error = 0  # the same load again heals the active spool channel only
    po.post("/v1/printers/u1/slots/T1/load", {"spool_id": sid})
    assert rc.write_failures(printops_db) == {}


def test_when_both_writes_failed_both_are_reported_and_a_live_head_reading_clears_only_the_head(
    use_fleet: Callable[..., None],
    po: ApiClient,
    u1: FakeMoonraker,
    printops_db: sqlite3.Connection,
    fake_spoolman: FakeSpoolman,
) -> None:
    use_fleet(u1_entry(u1, heads=1))
    u1.script_errors["SET_PRINT_FILAMENT_CONFIG"] = "extruder busy"
    u1.spoolman_set_error = 500
    sid = po.post("/v1/spools", BLACK)["id"]
    po.post("/v1/printers/u1/slots/T1/load", {"spool_id": sid})
    assert {k: f.stage for k, f in rc.write_failures(printops_db).items()} == {
        "u1/T1": "filament",
        rc.active_key("u1/T1"): "active_spool",
    }
    fleet = load_fleet()
    assert fleet is not None
    binds = rc.bindings(spools.all_spools(fake_spoolman))

    def stages(reading: Any) -> list[str]:
        found = rc.compare_printer(fleet.printers["u1"], reading, binds, failures=rc.write_failures(printops_db))
        return sorted(f.evidence["stage"] for f in found if f.kind == "printer_write_failed")

    assert stages(None) == ["active_spool", "filament"]  # printer unreadable: both stay open
    cfg = u1.status["print_task_config"]  # the owner sets the head on the touchscreen
    cfg["filament_type"][0], cfg["filament_color_rgba"][0] = "PLA+", "000000FF"
    live = routes.fleet_reader(fleet, listen_s=0.5)(fleet.printers["u1"])
    assert stages(live) == ["active_spool"]  # the head matches; Moonraker still books to the old spool


@pytest.mark.parametrize(
    ("setup", "needle"),
    [
        (lambda m: setattr(m, "spoolman_set_error", 500), "HTTP 500"),
        (lambda m: setattr(m, "spoolman_ignores_set", True), "Moonraker reports 14"),
    ],
    ids=["moonraker-refuses", "read-back-mismatch"],
)
def test_a_failed_active_spool_write_keeps_the_binding_opens_an_anomaly_and_the_retry_heals_it(
    po: ApiClient,
    k1: FakeMoonraker,
    printops_db: sqlite3.Connection,
    fake_spoolman: FakeSpoolman,
    setup: Callable[[FakeMoonraker], None],
    needle: str,
) -> None:
    sid = po.post("/v1/spools", BLACK)["id"]
    setup(k1)
    got = po.post("/v1/printers/k1se/slots/T1/load", {"spool_id": sid})

    assert got["slot"] == "k1se/T1"  # the binding stays
    active = got["printer_write"]["active_spool"]
    assert active["status"] == "failed" and needle in active["reason"]
    assert [(e["spool_id"], e["stage"]) for e in _events(printops_db, "slot.printer_write_failed")] == [
        (sid, "active_spool")
    ]
    found = _anomalies(printops_db, fake_spoolman)
    assert [(f.subject, f.evidence["stage"]) for f in found] == [("slot:k1se/T1", "active_spool")]

    k1.spoolman_set_error, k1.spoolman_ignores_set = 0, False
    retried = po.post("/v1/printers/k1se/slots/T1/load", {"spool_id": sid})  # the same load again
    assert retried["printer_write"]["active_spool"]["status"] == "written" and k1.active_spool == sid
    assert _anomalies(printops_db, fake_spoolman) == []


def test_load_and_unload_work_while_moonraker_cannot_reach_spoolman_and_warn(
    po: ApiClient, k1: FakeMoonraker, printops_db: sqlite3.Connection
) -> None:
    """Moonraker keeps the active spool in its own database and queues usage per spool id, so the
    change is right even with its Spoolman link down; the answer warns that the booking waits."""
    k1.spoolman_connected = False
    k1.spoolman_pending = [{"spool_id": 14, "filament_used": 800.0}]
    sid = po.post("/v1/spools", BLACK)["id"]
    loaded = po.post("/v1/printers/k1se/slots/T1/load", {"spool_id": sid})
    assert loaded["printer_write"]["active_spool"]["status"] == "written" and k1.active_spool == sid
    assert len(loaded["printer_write"]["warnings"]) == 1
    assert "k1se's Moonraker cannot reach Spoolman" in loaded["printer_write"]["warnings"][0]
    assert "(1 spool(s) waiting)" in loaded["printer_write"]["warnings"][0]
    unloaded = po.post("/v1/printers/k1se/slots/T1/unload", {})
    assert unloaded["printer_write"]["active_spool"]["status"] == "written" and k1.active_spool is None
    assert "cannot reach Spoolman right now: the active spool is cleared" in unloaded["printer_write"]["warnings"][0]
    assert _events(printops_db, "slot.printer_write_failed") == []


# ---- unload -----------------------------------------------------------------------------------


def test_unload_clears_the_active_spool_when_it_is_the_unloaded_spool(
    po: ApiClient, k1: FakeMoonraker, printops_db: sqlite3.Connection
) -> None:
    sid = po.post("/v1/spools", BLACK)["id"]
    po.post("/v1/printers/k1se/slots/T1/load", {"spool_id": sid})
    got = po.post("/v1/printers/k1se/slots/T1/unload", {"to": "WH:default"})

    assert got["slot"] is None
    assert k1.active_spool is None
    assert got["printer_write"]["active_spool"] == {
        "status": "written",
        "reason": None,
        "spool_id": sid,
        "before": sid,
        "after": None,
    }
    assert len(_events(printops_db, "slot.printer_written")) == 1  # the load's; an unload writes no event


def test_unload_leaves_a_different_active_spool_alone(po: ApiClient, k1: FakeMoonraker) -> None:
    sid = po.post("/v1/spools", BLACK)["id"]
    po.post("/v1/printers/k1se/slots/T1/load", {"spool_id": sid})
    k1.active_spool = 99  # someone picked another spool in Fluidd
    before = len(k1.writes())
    got = po.post("/v1/printers/k1se/slots/T1/unload", {})
    active = got["printer_write"]["active_spool"]
    assert active["status"] == "skipped" and "spool #99" in active["reason"]
    assert k1.active_spool == 99 and len(k1.writes()) == before


def test_a_clear_does_not_undo_a_load_that_committed_after_the_unload(
    po: ApiClient, k1: FakeMoonraker, printops_db: sqlite3.Connection, fake_spoolman: FakeSpoolman
) -> None:
    """unload commits, a load of the same spool commits and sets the active spool, and only then the
    unload's clear gets the slot lock: the spool is bound again, so the clear leaves it alone."""
    sid = po.post("/v1/spools", BLACK)["id"]
    po.post("/v1/printers/k1se/slots/T1/load", {"spool_id": sid})
    ctx = spools.Ctx(conn=printops_db, spoolman=fake_spoolman, actor="test")
    got = head_sync.release_active_spool(ctx, spools.SlotRef("k1se", "T1"), [sid])
    assert got.active_spool is not None and got.active_spool["status"] == "skipped"
    assert f"spool #{sid} is bound to k1se/T1 again" in got.active_spool["reason"]
    assert k1.active_spool == sid


def test_a_printing_printer_keeps_its_active_spool_on_load_and_unload(
    po: ApiClient, k1: FakeMoonraker, printops_db: sqlite3.Connection, fake_spoolman: FakeSpoolman
) -> None:
    """Changing the active spool mid-job would book the rest of the job to the other spool."""
    black, white = po.post("/v1/spools", BLACK)["id"], po.post("/v1/spools", WHITE)["id"]
    po.post("/v1/printers/k1se/slots/T1/load", {"spool_id": black})
    k1.status["print_stats"]["state"] = "printing"
    got = po.post("/v1/printers/k1se/slots/T1/load", {"spool_id": white, "replace_to": "WH:default"})
    assert got["slot"] == "k1se/T1"  # bound, as always
    assert got["printer_write"]["active_spool"]["status"] == "failed"
    assert "is printing" in got["printer_write"]["active_spool"]["reason"] and k1.active_spool == black
    found = _anomalies(printops_db, fake_spoolman)
    assert [(f.subject, f.evidence["stage"]) for f in found] == [("slot:k1se/T1", "active_spool")]
    k1.active_spool = white  # the owner picked it in Fluidd; clearing it mid-job is refused too
    unloaded = po.post("/v1/printers/k1se/slots/T1/unload", {})
    assert unloaded["printer_write"]["active_spool"]["status"] == "failed" and k1.active_spool == white

    k1.status["print_stats"]["state"] = "paused"  # a spool is swapped while paused: allowed
    k1.active_spool = black
    po.post("/v1/printers/k1se/slots/T1/load", {"spool_id": white})
    assert k1.active_spool == white
    assert _anomalies(printops_db, fake_spoolman) == []


def test_unload_with_no_active_spool_writes_nothing(po: ApiClient, k1: FakeMoonraker) -> None:
    sid = po.post("/v1/spools", BLACK)["id"]
    po.post("/v1/printers/k1se/slots/T1/load", {"spool_id": sid})
    k1.active_spool = None
    before = len(k1.writes())
    got = po.post("/v1/printers/k1se/slots/T1/unload", {})
    assert got["printer_write"]["active_spool"]["status"] == "skipped"
    assert len(k1.writes()) == before


def test_a_failed_clear_is_reported_and_recorded_but_the_unload_stands(
    po: ApiClient, k1: FakeMoonraker, printops_db: sqlite3.Connection
) -> None:
    sid = po.post("/v1/spools", BLACK)["id"]
    po.post("/v1/printers/k1se/slots/T1/load", {"spool_id": sid})
    k1.spoolman_ignores_set = True
    got = po.post("/v1/printers/k1se/slots/T1/unload", {})
    active = got["printer_write"]["active_spool"]
    assert active["status"] == "failed" and f"Moonraker reports {sid}" in active["reason"]
    assert k1.active_spool == sid  # still named; the CLI warns
    assert _events(printops_db, "slot.printer_write_failed") == []  # nothing is bound for an anomaly to name
    assert po.get(f"/v1/spools/{sid}")["slot"] is None  # the spool did leave the slot


# ---- what the CLI prints ----------------------------------------------------------------------


def _active(status: str, **kw: Any) -> dict[str, Any]:
    return {"status": status, "reason": None, "spool_id": 2, "before": 14, "after": 2} | kw


def test_the_k1_load_line_reports_the_active_spool_and_not_the_missing_head_write() -> None:
    pw: dict[str, Any] = {
        "status": "skipped",
        "reason": "adapter 'moonraker' does not support writing a head's filament",
        "after": None,
        "pressure_advance": None,
        "warnings": [],
        "active_spool": _active("written"),
    }
    assert fmt_printer_write(pw) == "\n  printer: Moonraker active spool #2 (read back)"
    assert printer_write_warnings(pw) == []


def test_the_cli_lines_cover_head_and_active_spool_outcomes() -> None:
    head: dict[str, Any] = {
        "status": "written",
        "reason": None,
        "warnings": [],
        "pressure_advance": {"k": 0.02},
        "after": {"color": "#000000", "vendor": "Elegoo", "material": "PLA+", "sub_type": "Basic"},
    }
    quiet = fmt_printer_write(head | {"active_spool": _active("skipped", reason="u1 has 4 heads")})
    assert "printer head: #000000 Elegoo PLA+ Basic (read back)" in quiet and "Moonraker" not in quiet
    failed = head | {"active_spool": _active("failed", reason="HTTP 500")}
    assert "Moonraker active spool failed — HTTP 500" in fmt_printer_write(failed)
    assert any("active spool was NOT set" in w for w in printer_write_warnings(failed))
    assert any("may still be #2" in w for w in printer_write_warnings(failed, unloading=True))
    head_failed = fmt_printer_write({"status": "failed", "reason": "extruder busy", "active_spool": _active("written")})
    assert head_failed.splitlines()[1:] == [
        "  printer head: failed — extruder busy",
        "  printer: Moonraker active spool #2 (read back)",
    ]
    cleared = fmt_printer_write({"status": "skipped", "active_spool": _active("written", after=None)})
    assert cleared == "\n  printer: Moonraker active spool cleared (was #14) (read back)"
