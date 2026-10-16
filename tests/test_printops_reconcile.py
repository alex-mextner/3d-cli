"""Slot reconciliation (#161): CIEDE2000, slot readers, compare rules, anomaly lifecycle, poller, API.

The printers are the in-process fakes of printer_fakes.py driven through the real adapters
(printer_ops.moonraker / creality), so "swap a spool" is a change of what the fake printer
reports — and every fake records that it only ever received reads.
"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fleet_fixtures import k1_entry, u1_entry, write_fleet
from printer_fakes import FakeCreality, FakeMoonraker, k1_state
from printops_fakes import FakeSpoolman

from printops import events as ev
from printops import reconcile as rc
from printops.api import routes_reconcile as routes
from printops.color import delta_e2000, hex_delta_e, parse_hex, srgb_to_lab
from printops.db import migrate, open_db
from printops.fleet import load_fleet
from printops.slots import LiveSlot, Reading, k1_slots, switch_slots, u1_slots
from printops.spoolman import encode_extra, ensure_extra_fields

# Sharma, Wu, Dalal (2005), Table 1: all 34 CIEDE2000 test pairs (L1, a1, b1), (L2, a2, b2), ΔE00.
SHARMA = [
    ((50.0000, 2.6772, -79.7751), (50.0000, 0.0000, -82.7485), 2.0425),
    ((50.0000, 3.1571, -77.2803), (50.0000, 0.0000, -82.7485), 2.8615),
    ((50.0000, 2.8361, -74.0200), (50.0000, 0.0000, -82.7485), 3.4412),
    ((50.0000, -1.3802, -84.2814), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, -1.1848, -84.8006), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, -0.9009, -85.5211), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, 0.0000, 0.0000), (50.0000, -1.0000, 2.0000), 2.3669),
    ((50.0000, -1.0000, 2.0000), (50.0000, 0.0000, 0.0000), 2.3669),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0009), 7.1792),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0010), 7.1792),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0011), 7.2195),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0012), 7.2195),
    ((50.0000, -0.0010, 2.4900), (50.0000, 0.0009, -2.4900), 4.8045),
    ((50.0000, -0.0010, 2.4900), (50.0000, 0.0010, -2.4900), 4.8045),
    ((50.0000, -0.0010, 2.4900), (50.0000, 0.0011, -2.4900), 4.7461),
    ((50.0000, 2.5000, 0.0000), (50.0000, 0.0000, -2.5000), 4.3065),
    ((50.0000, 2.5000, 0.0000), (73.0000, 25.0000, -18.0000), 27.1492),
    ((50.0000, 2.5000, 0.0000), (61.0000, -5.0000, 29.0000), 22.8977),
    ((50.0000, 2.5000, 0.0000), (56.0000, -27.0000, -3.0000), 31.9030),
    ((50.0000, 2.5000, 0.0000), (58.0000, 24.0000, 15.0000), 19.4535),
    ((50.0000, 2.5000, 0.0000), (50.0000, 3.1736, 0.5854), 1.0000),
    ((50.0000, 2.5000, 0.0000), (50.0000, 3.2972, 0.0000), 1.0000),
    ((50.0000, 2.5000, 0.0000), (50.0000, 1.8634, 0.5757), 1.0000),
    ((50.0000, 2.5000, 0.0000), (50.0000, 3.2592, 0.3350), 1.0000),
    ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
    ((63.0109, -31.0961, -5.8663), (62.8187, -29.7946, -4.0864), 1.2630),
    ((61.2901, 3.7196, -5.3901), (61.4292, 2.2480, -4.9620), 1.8731),
    ((35.0831, -44.1164, 3.7933), (35.0232, -40.0716, 1.5901), 1.8645),
    ((22.7233, 20.0904, -46.6940), (23.0331, 14.9730, -42.5619), 2.0373),
    ((36.4612, 47.8580, 18.3852), (36.2715, 50.5065, 21.2231), 1.4146),
    ((90.8027, -2.0831, 1.4410), (91.1528, -1.6435, 0.0447), 1.4441),
    ((90.9257, -0.5406, -0.9208), (88.6381, -0.8985, -0.7239), 1.5381),
    ((6.7747, -0.2908, -2.4247), (5.8714, -0.0985, -2.2286), 0.6377),
    ((2.0776, 0.0795, -1.1350), (0.9033, -0.0636, -0.5514), 0.9082),
]


# ---- colour -----------------------------------------------------------------------------------


@pytest.mark.parametrize(("lab1", "lab2", "expected"), SHARMA)
def test_delta_e2000_matches_sharma_reference_pairs(
    lab1: Any, lab2: Any, expected: float
) -> None:
    assert delta_e2000(lab1, lab2) == pytest.approx(expected, abs=5e-5)
    assert delta_e2000(lab2, lab1) == pytest.approx(expected, abs=5e-5)  # symmetric


def test_srgb_to_lab_reference_colours() -> None:
    # CIELAB of sRGB primaries under D65 (e.g. Bruce Lindbloom's calculator).
    assert srgb_to_lab("#FFFFFF") == pytest.approx((100.0, 0.0, 0.0), abs=1e-3)
    assert srgb_to_lab("#000000") == pytest.approx((0.0, 0.0, 0.0), abs=1e-9)
    assert srgb_to_lab("#FF0000") == pytest.approx(
        (53.2408, 80.0925, 67.2032), abs=1e-3
    )
    assert srgb_to_lab("#00FF00") == pytest.approx(
        (87.7347, -86.1827, 83.1793), abs=1e-3
    )
    assert srgb_to_lab("#0000FF") == pytest.approx(
        (32.2970, 79.1875, -107.8602), abs=1e-3
    )
    assert hex_delta_e("#123456", "#123456") == 0.0
    assert hex_delta_e("#000000", "#FFFFFF") == pytest.approx(100.0, abs=1e-3)


@pytest.mark.parametrize(
    ("raw", "hex_"),
    [
        ("#1a2b3c", "#1A2B3C"),
        ("1A2B3C", "#1A2B3C"),
        ("000000FF", "#000000"),  # U1 RGBA
        ("#0ffa800", "#FFA800"),
        ("#fff", "#FFFFFF"),  # Creality padded, short
        ("", None),
        ("#12345", None),
        ("red", None),
        (None, None),
        (0xFFFFFF, None),
    ],
)
def test_parse_hex_accepts_printer_and_spoolman_forms(
    raw: Any, hex_: str | None
) -> None:
    assert parse_hex(raw) == hex_


# ---- slot readers -----------------------------------------------------------------------------


def test_u1_slots_drop_none_values_and_default_white_of_unknown_filament() -> None:
    """Live U1 shape (2026-10-14): T3 holds filament the printer knows nothing about (type NONE,
    colour FFFFFFFF) — that white is a default, not a colour reading."""
    status = {
        "print_task_config": {
            "filament_exist": [True, False, True, True],
            "filament_type": ["PLA", "NONE", "NONE", "PLA"],
            "filament_vendor": ["Snapmaker", "NONE", "NONE", "Snapmaker"],
            "filament_sub_type": ["SnapSpeed", "NONE", "NONE", "SnapSpeed"],
            "filament_color_rgba": ["000000FF", "FFFFFFFF", "FFFFFFFF", "FFFFFFFF"],
        },
        "filament_detect": {
            "info": [{"CARD_UID": 0, "MAIN_TYPE": "NONE"}] * 4,
            "state": [0, 0, 0, 0],
        },
    }
    t1, t2, t3, t4 = u1_slots(status)
    assert (t1.head, t1.present, t1.material, t1.color, t1.vendor, t1.tag_uid) == (
        "T1",
        True,
        "PLA",
        "#000000",
        "Snapmaker",
        None,
    )
    assert (t2.present, t2.material, t2.color) == (False, None, None)
    assert (t3.present, t3.material, t3.color, t3.vendor) == (True, None, None, None)
    assert (t4.material, t4.color) == ("PLA", "#FFFFFF")


def test_u1_slots_read_the_rfid_tag_of_filament_detect() -> None:
    status = {
        "print_task_config": {
            "filament_exist": [True],
            "filament_type": ["NONE"],
            "filament_color_rgba": ["FFFFFFFF"],
        },
        "filament_detect": {
            "info": [
                {
                    "CARD_UID": 2864434397,
                    "MAIN_TYPE": "PETG",
                    "RGB_1": 0x1B5E20,
                    "OFFICIAL": True,
                    "CARD_TYPE": 1,
                }
            ]
        },
    }
    (t1,) = u1_slots(status)
    assert (t1.tag_uid, t1.material, t1.color, t1.source) == (
        "2864434397",
        "PETG",
        "#1B5E20",
        "print_task_config+filament_detect",
    )


@pytest.mark.parametrize(
    ("pushed", "present"),
    [
        ({"materialDetect": 1, "materialStatus": 0}, True),
        ({"materialDetect": 1, "materialStatus": 1}, False),
        (
            {"materialDetect": 0, "materialStatus": 0},
            None,
        ),  # runout switch off: cannot tell
        ({}, None),
    ],
)
def test_k1_slot_presence_comes_from_the_runout_switch(
    pushed: dict[str, Any], present: bool | None
) -> None:
    (t1,) = k1_slots(pushed, ("T1",))
    assert (t1.head, t1.present, t1.material, t1.color) == ("T1", present, None, None)


_S1, _S2 = (
    "filament_switch_sensor filament_sensor",
    "filament_switch_sensor filament_sensor_2",
)


@pytest.mark.parametrize(
    ("status", "present"),
    [
        # live K1 SE on Moonraker, 2026-10-16: both switches enabled and detecting
        (
            {
                _S1: {"filament_detected": True, "enabled": True},
                _S2: {"filament_detected": True, "enabled": True},
            },
            True,
        ),
        ({_S1: {"filament_detected": False, "enabled": True}, _S2: {}}, False),
        ({_S1: {"filament_detected": True, "enabled": False}}, None),
        ({"print_task_config": {}, _S1: {}}, None),
    ],
)
def test_moonraker_switch_sensor_gives_the_single_heads_presence(
    status: dict[str, Any], present: bool | None
) -> None:
    (t1,) = switch_slots(status, ("T1",))
    assert (t1.head, t1.present, t1.material) == ("T1", present, None)
    assert switch_slots(status, ()) == ()


# ---- fixtures ---------------------------------------------------------------------------------


@pytest.fixture
def spoolman() -> FakeSpoolman:
    fake = FakeSpoolman()
    ensure_extra_fields(fake)
    return fake


def add_spool(
    sp: FakeSpoolman,
    *,
    material: str = "PLA",
    color: str = "1A1A1A",
    slot: str | None = "u1/T1",
    location: str | None = None,
    tags: list[str] | None = None,
    measured: str | None = None,
) -> int:
    vendor = sp.find("vendor", {"name": "Elegoo"}) or [
        sp.create("vendor", {"name": "Elegoo"})
    ]
    fil_extra = {"measured_hex": encode_extra(measured)} if measured else {}
    fil = sp.create(
        "filament",
        {
            "name": f"{material} {color}",
            "vendor_id": vendor[0]["id"],
            "material": material,
            "color_hex": color,
            "density": 1.24,
            "diameter": 1.75,
            "weight": 1000,
            "extra": fil_extra,
        },
    )
    extra: dict[str, str] = {}
    if slot is not None:
        extra["printops_slot"] = encode_extra(slot)
    if tags is not None:
        extra["tag_uids"] = encode_extra(
            json.dumps(tags)
        )  # a text field holding a JSON list
    spool = sp.create(
        "spool", {"filament_id": fil["id"], "location": location, "extra": extra}
    )
    return int(spool["id"])


@pytest.fixture
def moon() -> Iterator[FakeMoonraker]:
    """A fake U1 with filament only in T1 (black PLA): every other head reports empty."""
    fake = FakeMoonraker()
    cfg = copy.deepcopy(fake.status["print_task_config"])
    cfg["filament_exist"] = [True, False, False, False]
    cfg["filament_color_rgba"][0] = "000000FF"
    fake.status["print_task_config"] = cfg
    with fake.serve() as served:
        yield served


@pytest.fixture
def fleet_file(
    tmp_path: Path, moon: FakeMoonraker, monkeypatch: pytest.MonkeyPatch
) -> Path:
    path = write_fleet(tmp_path / "fleet.yaml", u1_entry(moon))
    monkeypatch.setenv("THREED_FLEET", str(path))
    return path


def make_reconciler(
    tmp_path: Path, spoolman: FakeSpoolman, fleet_file: Path
) -> rc.Reconciler:
    db = tmp_path / "po.db"
    conn = open_db(db)
    migrate(conn)
    conn.close()
    fleet = load_fleet(fleet_file)
    return rc.Reconciler(db, spoolman, fleet, routes.fleet_reader(fleet, listen_s=0.5))


def open_anomalies(rec: rc.Reconciler) -> list[dict[str, Any]]:
    conn = open_db(rec.db_path)
    try:
        return rc.list_anomalies(conn)
    finally:
        conn.close()


def all_events(rec: rc.Reconciler) -> list[ev.Event]:
    conn = open_db(rec.db_path)
    try:
        return ev.since(conn, 0, ev.MAX_PAGE)
    finally:
        conn.close()


def set_u1_head(
    moon: FakeMoonraker,
    index: int,
    *,
    exist: bool = True,
    material: str = "PLA",
    rgba: str = "1A1A1AFF",
) -> None:
    cfg = copy.deepcopy(moon.status["print_task_config"])
    cfg["filament_exist"][index] = exist
    cfg["filament_type"][index] = material
    cfg["filament_color_rgba"][index] = rgba
    moon.status["print_task_config"] = cfg


# ---- the acceptance story: a swapped spool ----------------------------------------------------


def test_swapping_the_spool_in_a_slot_opens_slot_mismatch_and_resolves_when_swapped_back(
    tmp_path: Path, spoolman: FakeSpoolman, moon: FakeMoonraker, fleet_file: Path
) -> None:
    """u1/T1 is bound to a black PLA spool. Someone loads red PETG into T1 instead: the next poll
    opens type + colour anomalies and emits slot.mismatch; putting the black PLA back resolves
    both. The printer only ever sees reads."""
    for i in range(4):
        set_u1_head(moon, i, exist=False)
    set_u1_head(moon, 0, material="PLA", rgba="000000FF")
    spool = add_spool(
        spoolman, material="PLA", color="1A1A1A", slot="u1/T1", location="slot:u1/T1"
    )
    rec = make_reconciler(tmp_path, spoolman, fleet_file)

    reading = rec.poll("u1")
    assert reading.ok and reading.slot("T1") == LiveSlot(
        "T1",
        True,
        "PLA",
        "#000000",
        "Snapmaker",
        "SnapSpeed",
        None,
        "print_task_config",
    )
    assert open_anomalies(rec) == []  # #000000 vs #1A1A1A is well under ΔE 15

    set_u1_head(moon, 0, material="PETG", rgba="C62828FF")
    rec.poll("u1")
    found = {a["kind"]: a for a in open_anomalies(rec)}
    assert set(found) == {"type_mismatch", "color_mismatch"}
    assert found["type_mismatch"]["subject"] == "slot:u1/T1"
    assert found["type_mismatch"]["evidence"]["printer"]["material"] == "PETG"
    assert found["type_mismatch"]["evidence"]["spool"]["spool_id"] == spool
    assert found["color_mismatch"]["evidence"]["delta_e2000"] > 15
    kinds = [(e.kind, e.subject, e.payload.get("kind")) for e in all_events(rec)]
    assert ("slot.mismatch", "slot:u1/T1", "type_mismatch") in kinds
    assert ("slot.mismatch", "slot:u1/T1", "color_mismatch") in kinds
    assert [k for k in kinds if k[0] == "anomaly.opened"] == [
        ("anomaly.opened", "slot:u1/T1", "type_mismatch"),
        ("anomaly.opened", "slot:u1/T1", "color_mismatch"),
    ]

    rec.poll("u1")  # the same condition again: no duplicate anomaly, no new events
    assert len(open_anomalies(rec)) == 2 and len(all_events(rec)) == len(kinds)

    set_u1_head(moon, 0, material="PLA", rgba="000000FF")
    rec.poll("u1")
    assert open_anomalies(rec) == []
    resolved = [e for e in all_events(rec) if e.kind == "anomaly.resolved"]
    assert {e.payload["kind"] for e in resolved} == {"type_mismatch", "color_mismatch"}
    assert all(e.payload["by"] == "reconcile" for e in resolved)
    assert moon.writes() == [] and {r.path for r in moon.requests} == {
        "/printer/objects/query"
    }


def test_a_second_swap_while_mismatched_is_a_new_anomaly(
    tmp_path: Path, spoolman: FakeSpoolman, moon: FakeMoonraker, fleet_file: Path
) -> None:
    add_spool(spoolman, material="PLA", color="000000", slot="u1/T1")
    rec = make_reconciler(tmp_path, spoolman, fleet_file)
    set_u1_head(moon, 0, material="PETG", rgba="000000FF")
    rec.poll("u1")
    set_u1_head(moon, 0, material="ABS", rgba="000000FF")
    rec.poll("u1")
    conn = open_db(rec.db_path)
    rows = rc.list_anomalies(conn, statuses=rc.STATUSES)
    conn.close()
    types = [
        (r["status"], r["evidence"]["printer"]["material"])
        for r in rows
        if r["kind"] == "type_mismatch"
    ]
    assert types == [("open", "ABS"), ("resolved", "PETG")]


def test_unreadable_printer_keeps_live_anomalies_open(
    tmp_path: Path, spoolman: FakeSpoolman, moon: FakeMoonraker, fleet_file: Path
) -> None:
    add_spool(spoolman, material="PLA", color="000000", slot="u1/T1")
    rec = make_reconciler(tmp_path, spoolman, fleet_file)
    set_u1_head(moon, 0, material="PETG", rgba="000000FF")
    rec.poll("u1")
    assert [a["kind"] for a in open_anomalies(rec)] == ["type_mismatch"]

    def offline(_printer: Any) -> Reading:
        return Reading(
            "u1", "2026-10-14T00:00:00.000Z", False, error="connection refused"
        )

    rec.read = offline
    rec.poll("u1")
    assert [a["kind"] for a in open_anomalies(rec)] == ["type_mismatch"]
    last = rec.reading("u1")
    assert last is not None and last.error == "connection refused"


def test_a_pass_that_fails_on_one_scope_commits_no_scope(
    tmp_path: Path,
    spoolman: FakeSpoolman,
    moon: FakeMoonraker,
    fleet_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """u1 has a type mismatch and a spool is bound to a printer outside the fleet. When writing
    the fleet scope fails, the u1 anomaly written earlier in the same pass is rolled back too."""
    add_spool(spoolman, material="PLA", color="000000", slot="u1/T1")
    add_spool(spoolman, slot="x1c/T1")
    rec = make_reconciler(tmp_path, spoolman, fleet_file)
    set_u1_head(moon, 0, material="PETG", rgba="000000FF")
    emit = ev.emit

    def failing(conn: Any, kind: str, subject: str, payload: Any, actor: str) -> Any:
        if payload.get("scope") == rc.FLEET_SCOPE:
            raise RuntimeError("disk full")
        return emit(conn, kind, subject, payload, actor)

    monkeypatch.setattr(rc.ev, "emit", failing)
    with pytest.raises(RuntimeError):
        rec.poll("u1")
    assert open_anomalies(rec) == [] and all_events(rec) == []

    monkeypatch.setattr(rc.ev, "emit", emit)
    rec.reconcile()
    assert {(a["scope"], a["kind"]) for a in open_anomalies(rec)} >= {
        ("u1", "type_mismatch")
    }
    assert rc.FLEET_SCOPE in {a["scope"] for a in open_anomalies(rec)}


def test_k1_runout_switch_reports_empty_slot_with_a_bound_spool(
    tmp_path: Path, spoolman: FakeSpoolman, monkeypatch: pytest.MonkeyPatch
) -> None:
    k1 = FakeCreality(state={**k1_state(), "materialDetect": 1, "materialStatus": 1})
    with k1.serve() as served:
        path = write_fleet(tmp_path / "fleet.yaml", k1_entry(served))
        add_spool(spoolman, slot="k1se/T1")
        rec = make_reconciler(tmp_path, spoolman, path)
        reading = rec.poll("k1se")
        assert reading.ok and reading.slot("T1") == LiveSlot(
            "T1", False, source="materialStatus"
        )
        assert [a["kind"] for a in open_anomalies(rec)] == ["empty_with_spool"]
        assert served.writes() == []


# ---- compare rules ----------------------------------------------------------------------------


def _reading(*slots: LiveSlot) -> Reading:
    return Reading("u1", "2026-10-14T00:00:00.000Z", True, state="idle", slots=slots)


def _binds(spoolman: FakeSpoolman) -> list[rc.Binding]:
    return rc.bindings(spoolman.find("spool"))


def test_compare_rules_cover_every_kind(
    spoolman: FakeSpoolman, fleet_file: Path
) -> None:
    fleet = load_fleet(fleet_file)
    assert fleet is not None
    u1 = fleet.printers["u1"]
    tagged = add_spool(
        spoolman, slot="u1/T1", color="000000", tags=["AA01"], location="slot:u1/T1"
    )
    broken = add_spool(spoolman, slot="u1/T2")  # T2 is unusable
    add_spool(spoolman, slot="u1/T3")
    add_spool(spoolman, slot="u1/T3")  # two in one slot
    ghost = add_spool(spoolman, slot="u1/T9")  # no such head
    moved = add_spool(spoolman, slot="u1/T4", location="WH:A/S1")  # also on a shelf
    stranger = add_spool(spoolman, slot="x1c/T1")  # not in the fleet
    add_spool(spoolman, slot=None)  # unbound: ignored
    archived = add_spool(spoolman, slot="u1/T4")
    spoolman.update("spool", archived, {"archived": True})
    binds = _binds(spoolman)
    assert {b.spool_id for b in binds} == {
        tagged,
        broken,
        ghost,
        moved,
        stranger,
        tagged + 2,
        tagged + 3,
    }

    reading = _reading(
        LiveSlot("T1", True, "PLA", "#000000", tag_uid="BB02"),
        LiveSlot("T2", False),
        LiveSlot("T3", True, "PLA", "#000000"),
        LiveSlot("T4", None),
    )
    found = {(f.kind, f.subject) for f in rc.compare_printer(u1, reading, binds)}
    assert found == {
        ("tag_mismatch", "slot:u1/T1"),
        ("unusable_head", "slot:u1/T2"),
        ("empty_with_spool", "slot:u1/T2"),
        ("multiple_spools", "slot:u1/T3"),
        ("unknown_slot", f"spool:{ghost}"),
        ("location_conflict", f"spool:{moved}"),
    }
    assert [(f.kind, f.scope, f.subject) for f in rc.compare_unknown(fleet, binds)] == [
        ("unknown_slot", "fleet", f"spool:{stranger}")
    ]
    # no reading: only the binding checks
    assert {f.kind for f in rc.compare_printer(u1, None, binds)} == {
        "unusable_head",
        "multiple_spools",
        "unknown_slot",
        "location_conflict",
    }


def test_unbound_filament_and_colour_rules(
    spoolman: FakeSpoolman, fleet_file: Path
) -> None:
    fleet = load_fleet(fleet_file)
    assert fleet is not None
    u1 = fleet.printers["u1"]
    add_spool(
        spoolman, slot="u1/T1", material="pla", color="2E7D32"
    )  # green, lower-case material
    add_spool(
        spoolman, slot="u1/T3", material="PLA", color="FFFFFF", measured="E53935"
    )  # measured red wins
    reading = _reading(
        LiveSlot(
            "T1", True, "PLA", "#388E3C"
        ),  # a slightly different green: same colour
        LiveSlot("T3", True, "PLA", "#FFFFFF"),  # white vs measured red
        LiveSlot("T4", True, "PETG", "#000000"),  # filament, no spool
    )
    findings = rc.compare_printer(u1, reading, _binds(spoolman))
    assert [(f.kind, f.subject) for f in findings] == [
        ("color_mismatch", "slot:u1/T3"),
        ("unbound_filament", "slot:u1/T4"),
    ]
    assert findings[0].evidence["delta_e2000"] > 40
    assert hex_delta_e("#388E3C", "#2E7D32") < rc.DEFAULT_MAX_DELTA_E
    strict = rc.compare_printer(u1, reading, _binds(spoolman), max_delta_e=1.0)
    assert ("color_mismatch", "slot:u1/T1") in [(f.kind, f.subject) for f in strict]


def test_binding_reads_multi_colour_and_garbage_extras(spoolman: FakeSpoolman) -> None:
    spool = {
        "id": 7,
        "archived": False,
        "location": "",
        "extra": {"printops_slot": '"u1/T3"', "tag_uids": "not json"},
        "filament": {
            "name": "Silk",
            "material": "PLA",
            "multi_color_hexes": "FF0000,00ff00",
            "extra": {},
            "vendor": {"name": "Sunlu"},
        },
    }
    b = rc.binding_of(spool)
    assert b is not None
    assert (b.slot, b.printer, b.head, b.name, b.colors, b.tag_uids, b.location) == (
        "u1/T3",
        "u1",
        "T3",
        "Sunlu Silk",
        ("#FF0000", "#00FF00"),
        (),
        None,
    )
    assert rc.binding_of({**spool, "extra": {"printops_slot": '""'}}) is None


# ---- ack / resolve ----------------------------------------------------------------------------


def test_ack_then_manual_resolve_and_reopen(
    tmp_path: Path, spoolman: FakeSpoolman, moon: FakeMoonraker, fleet_file: Path
) -> None:
    add_spool(spoolman, material="PLA", color="000000", slot="u1/T1")
    rec = make_reconciler(tmp_path, spoolman, fleet_file)
    set_u1_head(moon, 0, material="PETG", rgba="000000FF")
    rec.poll("u1")
    (a,) = open_anomalies(rec)
    conn = open_db(rec.db_path)
    try:
        acked = rc.set_status(conn, a["id"], "ack", "cli", "CF on purpose")
        assert acked is not None and (
            acked["status"],
            acked["acked_by"],
            acked["note"],
        ) == ("ack", "cli", "CF on purpose")
        with pytest.raises(rc.TransitionError):
            rc.set_status(conn, a["id"], "ack", "cli")
        rec.poll("u1")  # still mismatched: the acked row stays, nothing new opens
        assert [(r["id"], r["status"]) for r in rc.list_anomalies(conn)] == [
            (a["id"], "ack")
        ]
        done = rc.set_status(conn, a["id"], "resolved", "cli")
        assert done is not None and (done["status"], done["resolved_by"]) == (
            "resolved",
            "cli",
        )
        assert rc.set_status(conn, 999, "ack", "cli") is None
        rec.poll("u1")  # resolved by hand but still there → a new one
        (again,) = rc.list_anomalies(conn)
        assert again["id"] != a["id"] and again["status"] == "open"
        kinds = [e.kind for e in ev.since(conn, 0, ev.MAX_PAGE)]
        assert kinds.count("anomaly.acked") == 1 and kinds.count("anomaly.opened") == 2
    finally:
        conn.close()


# ---- settings + background tasks --------------------------------------------------------------


def test_non_finite_poll_interval_falls_back_to_default() -> None:
    for raw in ("nan", "inf", "-inf", "fast"):
        assert (
            routes.ReconcileSettings.from_env({"PRINTOPS_POLL_S": raw}).poll_s
            == routes.DEFAULT_POLL_S
        )


def test_malformed_printer_colour_is_dropped_not_compared() -> None:
    status = {
        "print_task_config": {
            "filament_exist": [True],
            "filament_type": ["PLA"],
            "filament_color_rgba": ["GGGGGGFF"],
        }
    }
    (t1,) = u1_slots(status)
    assert (t1.material, t1.color) == ("PLA", None)


def test_poll_interval_has_a_floor_and_zero_disables() -> None:
    assert routes.ReconcileSettings.from_env({}).poll_s == routes.DEFAULT_POLL_S
    assert (
        routes.ReconcileSettings.from_env({"PRINTOPS_POLL_S": "2"}).poll_s
        == routes.MIN_POLL_S
    )
    assert routes.ReconcileSettings.from_env({"PRINTOPS_POLL_S": "0"}).poll_s == 0
    assert (
        routes.ReconcileSettings.from_env({"PRINTOPS_COLOR_DE_MAX": "7.5"}).max_delta_e
        == 7.5
    )


@pytest.mark.parametrize("raw", ["oops", "nan", "inf", "-1"])
def test_bad_colour_threshold_falls_back_to_default(raw: str) -> None:
    assert (
        routes.ReconcileSettings.from_env({"PRINTOPS_COLOR_DE_MAX": raw}).max_delta_e
        == rc.DEFAULT_MAX_DELTA_E
    )


def test_poller_reads_once_per_interval_and_events_trigger_a_reconcile(
    tmp_path: Path, spoolman: FakeSpoolman, moon: FakeMoonraker, fleet_file: Path
) -> None:
    rec = make_reconciler(tmp_path, spoolman, fleet_file)
    set_u1_head(moon, 0, material="PLA", rgba="000000FF")

    async def scenario() -> None:
        poll = asyncio.create_task(routes.poll_printer(rec, "u1", interval=0.3))
        follow = asyncio.create_task(routes.follow_events(rec, rec.db_path))
        await asyncio.sleep(0.5)
        reads = len(moon.requests)
        assert 1 <= reads <= 2
        assert [a["kind"] for a in open_anomalies(rec)] == ["unbound_filament"]
        # Spools binds a spool to u1/T1 and records slot.loaded: reconcile without a printer read
        add_spool(spoolman, material="PLA", color="000000", slot="u1/T1")
        before = len(moon.requests)
        conn = open_db(rec.db_path)
        ev.emit(
            conn, "slot.loaded", "slot:u1/T1", {"printer": "u1", "head": "T1"}, "test"
        )
        conn.close()
        for _ in range(100):
            if not open_anomalies(rec):
                break
            await asyncio.sleep(0.01)
        assert open_anomalies(rec) == []
        assert (
            len(moon.requests) - before <= 1
        )  # at most the regular poll, not the event
        for t in (poll, follow):
            t.cancel()
        await asyncio.gather(poll, follow, return_exceptions=True)

    asyncio.run(scenario())
    assert moon.writes() == []


# ---- API --------------------------------------------------------------------------------------


def _auth(tokens: dict[str, str], name: str = "rw") -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens[name]}"}


def test_api_printers_slots_and_anomalies(
    tmp_path: Path,
    fleet_file: Path,
    moon: FakeMoonraker,
    fake_spoolman: FakeSpoolman,
    printops_settings: Any,
    printops_tokens: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from printops.api.app import create_app

    monkeypatch.setenv(
        "PRINTOPS_POLL_S", "10"
    )  # the lifespan poller reads each printer at once, then every 10 s
    set_u1_head(moon, 0, material="PETG", rgba="000000FF")
    set_u1_head(
        moon, 3, material="PLA", rgba="FFFFFFFF"
    )  # white PLA in T4, no spool bound
    ensure_extra_fields(fake_spoolman)
    spool = add_spool(
        fake_spoolman,
        material="PLA",
        color="000000",
        slot="u1/T1",
        location="slot:u1/T1",
    )
    app = create_app(printops_settings, spoolman=fake_spoolman)
    with TestClient(app) as client:
        rec = app.state.reconcile
        deadline = time.monotonic() + 10
        while not open_anomalies(rec) and time.monotonic() < deadline:
            time.sleep(0.02)
        h = _auth(printops_tokens)
        body = client.get("/v1/printers", headers=h).json()
        by_name = {p["name"]: p for p in body["printers"]}
        assert set(by_name) == {"u1", "p1s", "boris"}
        u1 = by_name["u1"]
        assert (u1["polled"], u1["live"]["ok"], u1["live"]["state"]) == (
            True,
            True,
            "idle",
        )
        t1, t2 = u1["slots"][0], u1["slots"][1]
        assert t1["printer"]["material"] == "PETG" and [
            s["spool_id"] for s in t1["spools"]
        ] == [spool]
        assert [a["kind"] for a in t1["anomalies"]] == ["type_mismatch"]
        assert (t2["usable"], t2["reason"]) == (False, "T2 is broken")
        assert by_name["p1s"]["live"] is None and by_name["p1s"]["polled"] is False

        slots = client.get("/v1/printers/u1/slots", headers=h).json()
        assert [s["slot"] for s in slots["slots"]] == [
            "u1/T1",
            "u1/T2",
            "u1/T3",
            "u1/T4",
        ]
        assert client.get("/v1/printers/nope", headers=h).status_code == 404

        listed = client.get("/v1/anomalies", headers=h).json()["anomalies"]
        kinds = {a["kind"]: a for a in listed}
        assert (
            "type_mismatch" in kinds and "unbound_filament" in kinds
        )  # T4 holds PLA with no spool bound
        aid = kinds["type_mismatch"]["id"]
        assert client.get("/v1/anomalies?status=bogus", headers=h).status_code == 422
        assert client.get("/v1/anomalies?printer=p1s", headers=h).json() == {
            "anomalies": []
        }

        assert (
            client.post(
                f"/v1/anomalies/{aid}/ack", headers=_auth(printops_tokens, "read")
            ).status_code
            == 403
        )
        acked = client.post(
            f"/v1/anomalies/{aid}/ack", headers=h, json={"note": "known"}
        )
        assert (
            acked.status_code == 200
            and acked.json()["status"] == "ack"
            and acked.json()["acked_by"] == "test-rw"
        )
        assert client.post(f"/v1/anomalies/{aid}/ack", headers=h).status_code == 409
        assert (
            client.post(f"/v1/anomalies/{aid}/resolve", headers=h).json()["status"]
            == "resolved"
        )
        assert client.post("/v1/anomalies/99999/resolve", headers=h).status_code == 404
        assert client.get("/v1/printers").status_code == 401
    assert moon.writes() == []
    assert len(moon.requests) == 1  # one read at startup; the next is 10 s later


def test_api_reports_a_broken_fleet_file_without_failing_startup(
    tmp_path: Path,
    printops_settings: Any,
    fake_spoolman: FakeSpoolman,
    printops_tokens: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from printops.api.app import create_app

    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "u1: {model: Snapmaker U1, owner: martian, access: offline, heads: []}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("THREED_FLEET", str(bad))
    with TestClient(create_app(printops_settings, spoolman=fake_spoolman)) as client:
        r = client.get("/v1/printers", headers=_auth(printops_tokens))
        assert (
            r.status_code == 503
            and "fleet/printers.yaml unusable" in r.json()["detail"]
        )
        assert client.get("/v1/live").status_code == 200
