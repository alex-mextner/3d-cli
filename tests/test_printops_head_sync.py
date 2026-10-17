"""`spool load` writes the printer head (issue #400): the U1 `SET_PRINT_FILAMENT_CONFIG` write and its
read-back, the pressure-advance restore after the firmware's FLOW_RESET_K, the refusals, the opt-out,
and the `printer_write_failed` reconcile anomaly. Only loopback fakes (FakeMoonraker, FakeSpoolman)."""

from __future__ import annotations

import copy
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fleet_fixtures import u1_entry, write_fleet, write_policy_for_fleet
from printer_fakes import FACTORY_K, FakeMoonraker
from printops_fakes import FakeSpoolman

from printer_ops.base import (
    HeadFilament,
    PrinterCommError,
    PrinterRefused,
    UnsupportedOperation,
)
from printer_ops.moonraker import MoonrakerAdapter, filament_config_gcode
from printops import events as ev
from printops import head_sync, locations, spools
from printops import reconcile as rc
from printops.api import routes_reconcile as routes
from printops.client import ApiClient, ApiError
from printops.fleet import load_fleet
from printops.settings import Settings

GREEN = {
    "vendor": "Elegoo",
    "material": "PLA+",
    "name": "RAPID PLA+ Green",
    "color_hex": "#4FE44D",
    "net_g": 1000,
    "location": "WH:default",
    "lot_nr": "L2409",
}
GREEN_LINE = (
    'SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=2 VENDOR="Elegoo" FILAMENT_TYPE="PLA+" '
    'FILAMENT_SUBTYPE="Basic" FILAMENT_COLOR_RGBA=4FE44DFF'
)


@pytest.fixture
def moon() -> Iterator[FakeMoonraker]:
    fake = FakeMoonraker()
    with fake.serve() as served:
        yield served


@pytest.fixture
def po(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    moon: FakeMoonraker,
    printops_db: sqlite3.Connection,
    api_client: ApiClient,
) -> ApiClient:
    """3d-api whose fleet U1 is the FakeMoonraker (T2 broken) plus offline p1s; the committed
    calibration catalog and the repo policy cut down to that fleet."""
    fleet = write_fleet(tmp_path / "fleet.yaml", u1_entry(moon))
    monkeypatch.setenv("THREED_FLEET", str(fleet))
    monkeypatch.setenv(
        "PRINTOPS_POLICY", str(write_policy_for_fleet(tmp_path / "policy.yaml", fleet))
    )
    monkeypatch.delenv("PRINTOPS_CALIBRATIONS", raising=False)
    locations.add(printops_db, "WH:default", "warehouse", actor="test")
    return api_client


def _gcode(moon: FakeMoonraker, command: str) -> list[str]:
    return [s for s in moon.scripts if s.startswith(command)]


def _events(conn: sqlite3.Connection, kind: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT payload FROM events WHERE kind = ? ORDER BY id", (kind,)
    )
    return [json.loads(r["payload"]) for r in rows]


def _head(moon: FakeMoonraker, i: int) -> tuple[str, str, str, str]:
    cfg = moon.status["print_task_config"]
    return (
        cfg["filament_color_rgba"][i],
        cfg["filament_vendor"][i],
        cfg["filament_type"][i],
        cfg["filament_sub_type"][i],
    )


# ---- the load story ---------------------------------------------------------------------------


def test_load_writes_the_head_once_reads_it_back_and_keeps_factory_k_without_a_calibration(
    po: ApiClient, moon: FakeMoonraker, printops_db: sqlite3.Connection
) -> None:
    sid = po.post("/v1/spools", GREEN)["id"]
    got = po.post("/v1/printers/u1/slots/T3/load", {"spool_id": sid})

    assert got["slot"] == "u1/T3"
    assert moon.scripts == [GREEN_LINE]  # exactly one write, for head index 2, no FORCE
    assert _head(moon, 2) == ("4FE44DFF", "Elegoo", "PLA+", "Basic")
    pw = got["printer_write"]
    assert pw["status"] == "written" and pw["reason"] is None
    assert pw["before"] == {
        "color": "#1B5E20",
        "material": "PLA",
        "vendor": "Snapmaker",
        "sub_type": "Basic",
    }
    assert pw["after"] == {
        "color": "#4FE44D",
        "material": "PLA+",
        "vendor": "Elegoo",
        "sub_type": "Basic",
    }
    assert pw["pressure_advance"] == {
        "action": "factory",
        "k": FACTORY_K,
        "calibration_id": None,
    }
    assert (
        len(pw["warnings"]) == 1
        and f"factory pressure advance K={FACTORY_K}" in pw["warnings"][0]
    )
    assert "new_material_type" in pw["warnings"][0]
    written = _events(printops_db, "slot.printer_written")
    assert [(e["printer"], e["head"], e["spool_id"]) for e in written] == [
        ("u1", "T3", sid)
    ]
    assert _events(printops_db, "slot.printer_write_failed") == []


def test_load_puts_the_current_pressure_advance_calibration_back_after_the_firmware_reset(
    po: ApiClient, moon: FakeMoonraker
) -> None:
    sid = po.post("/v1/spools", GREEN)["id"]
    po.post("/v1/printers/u1/slots/T3/load", {"spool_id": sid, "printer_write": False})
    cal = po.post(
        "/v1/calibrations",
        {
            "kind": "pressure_advance",
            "printer": "u1",
            "head": "T3",
            "spool_id": sid,
            "values": {"pressure_advance": 0.035},
        },
    )
    assert moon.scripts == []

    got = po.post(
        "/v1/printers/u1/slots/T3/load", {"spool_id": sid}
    )  # same slot: writes again
    assert moon.scripts == [
        GREEN_LINE,
        "SET_PRESSURE_ADVANCE EXTRUDER=extruder2 ADVANCE=0.035",
    ]
    assert (
        moon.status["extruder2"]["pressure_advance"] == 0.035
    )  # read back by the adapter
    pw = got["printer_write"]
    assert pw["status"] == "written" and pw["warnings"] == []
    assert pw["pressure_advance"] == {
        "action": "restored",
        "k": 0.035,
        "factory_k": FACTORY_K,
        "calibration_id": cal["id"],
    }


def test_a_calibration_of_another_head_is_not_applied(
    po: ApiClient, moon: FakeMoonraker
) -> None:
    sid = po.post("/v1/spools", GREEN)["id"]
    po.post("/v1/printers/u1/slots/T1/load", {"spool_id": sid, "printer_write": False})
    po.post(
        "/v1/calibrations",
        {
            "kind": "pressure_advance",
            "printer": "u1",
            "head": "T1",
            "spool_id": sid,
            "values": {"pressure_advance": 0.035},
        },
    )
    got = po.post("/v1/printers/u1/slots/T3/load", {"spool_id": sid})
    assert _gcode(moon, "SET_PRESSURE_ADVANCE") == []
    assert got["printer_write"]["pressure_advance"]["action"] == "factory"


def test_official_rfid_head_is_refused_unless_forced(
    po: ApiClient, moon: FakeMoonraker, printops_db: sqlite3.Connection
) -> None:
    moon.status["print_task_config"]["filament_official"][2] = True
    sid = po.post("/v1/spools", GREEN)["id"]

    refused = po.post("/v1/printers/u1/slots/T3/load", {"spool_id": sid})
    assert refused["slot"] == "u1/T3"  # the binding stays
    assert refused["printer_write"]["status"] == "failed"
    assert "official RFID filament" in refused["printer_write"]["reason"]
    assert moon.scripts == [] and moon.writes() == []  # nothing sent to the printer
    assert [e["stage"] for e in _events(printops_db, "slot.printer_write_failed")] == [
        "filament"
    ]

    forced = po.post(
        "/v1/printers/u1/slots/T3/load", {"spool_id": sid, "force_official": True}
    )
    assert moon.scripts == [GREEN_LINE + " FORCE=1"]
    assert forced["printer_write"]["status"] == "written"
    assert moon.status["print_task_config"]["filament_official"][2] is False


@pytest.mark.parametrize("state", ["printing", "paused"])
def test_a_running_job_is_never_touched(
    po: ApiClient, moon: FakeMoonraker, state: str
) -> None:
    moon.status["print_stats"]["state"] = state
    sid = po.post("/v1/spools", GREEN)["id"]
    got = po.post("/v1/printers/u1/slots/T3/load", {"spool_id": sid})
    assert (
        got["printer_write"]["status"] == "failed"
        and "FLOW_RESET_K" in got["printer_write"]["reason"]
    )
    assert moon.writes() == []


def test_printer_write_false_sends_nothing(po: ApiClient, moon: FakeMoonraker) -> None:
    sid = po.post("/v1/spools", GREEN)["id"]
    got = po.post(
        "/v1/printers/u1/slots/T3/load", {"spool_id": sid, "printer_write": False}
    )
    assert got["printer_write"]["status"] == "skipped" and moon.requests == []


def test_ams_slots_and_offline_printers_are_skipped_without_contact(
    po: ApiClient, moon: FakeMoonraker
) -> None:
    sid = po.post("/v1/spools", GREEN)["id"]
    got = po.post("/v1/printers/p1s/slots/T1/load", {"spool_id": sid})
    assert (
        got["printer_write"]["status"] == "skipped"
        and "offline" in got["printer_write"]["reason"]
    )
    po.post("/v1/printers/p1s/slots/T1/unload", {})
    got = po.post("/v1/printers/boris/slots/lite/2/load", {"spool_id": sid})
    assert (
        got["printer_write"]["status"] == "skipped"
        and "AMS" in got["printer_write"]["reason"]
    )
    assert moon.requests == []


def test_a_spool_that_left_the_slot_before_the_write_is_not_written(
    po: ApiClient, moon: FakeMoonraker, printops_db: sqlite3.Connection, fake_spoolman: FakeSpoolman
) -> None:
    """A concurrent replace/unload committed between this load's binding and its printer write."""
    sid = po.post("/v1/spools", GREEN)["id"]
    po.post("/v1/printers/u1/slots/T3/load", {"spool_id": sid, "printer_write": False})
    stale = fake_spoolman.get("spool", sid)  # what the load saw when it committed
    po.post("/v1/printers/u1/slots/T3/unload", {})
    ctx = spools.Ctx(conn=printops_db, spoolman=fake_spoolman, actor="test")
    got = head_sync.sync_head(ctx, spools.SlotRef("u1", "T3"), stale)
    assert got.status == "skipped" and "left u1/T3" in (got.reason or "")
    assert moon.requests == []


def test_bad_printer_write_flag_is_422(po: ApiClient) -> None:
    sid = po.post("/v1/spools", GREEN)["id"]
    with pytest.raises(ApiError) as e:
        po.post(
            "/v1/printers/u1/slots/T3/load", {"spool_id": sid, "printer_write": "no"}
        )
    assert e.value.status == 422


@pytest.mark.parametrize(
    ("setup", "stage", "needle"),
    [
        (
            lambda m: m.script_errors.update(SET_PRINT_FILAMENT_CONFIG="extruder busy"),
            "filament",
            "extruder busy",
        ),
        (
            lambda m: m.ignored_commands.add("SET_PRINT_FILAMENT_CONFIG"),
            "filament",
            "after SET_PRINT_FILAMENT_CONFIG shows",
        ),
        (
            lambda m: m.script_errors.update(SET_PRESSURE_ADVANCE="no such extruder"),
            "pressure_advance",
            "restoring pressure advance",
        ),
    ],
    ids=["klipper-error", "read-back-mismatch", "pa-restore"],
)
def test_printer_failures_keep_the_binding_and_record_a_failure(
    po: ApiClient,
    moon: FakeMoonraker,
    printops_db: sqlite3.Connection,
    setup: Any,
    stage: str,
    needle: str,
) -> None:
    sid = po.post("/v1/spools", GREEN)["id"]
    if stage == "pressure_advance":
        po.post(
            "/v1/printers/u1/slots/T3/load", {"spool_id": sid, "printer_write": False}
        )
        po.post(
            "/v1/calibrations",
            {
                "kind": "pressure_advance",
                "printer": "u1",
                "head": "T3",
                "spool_id": sid,
                "values": {"pressure_advance": 0.04},
            },
        )
    setup(moon)
    got = po.post("/v1/printers/u1/slots/T3/load", {"spool_id": sid})
    assert got["slot"] == "u1/T3"
    assert (
        got["printer_write"]["status"] == "failed"
        and needle in got["printer_write"]["reason"]
    )
    failed = _events(printops_db, "slot.printer_write_failed")
    assert [(e["head"], e["spool_id"], e["stage"]) for e in failed] == [
        ("T3", sid, stage)
    ]


# ---- the anomaly ------------------------------------------------------------------------------


def test_a_failed_write_is_an_anomaly_until_a_write_succeeds(
    po: ApiClient,
    moon: FakeMoonraker,
    printops_settings: Settings,
    fake_spoolman: FakeSpoolman,
) -> None:
    sid = po.post("/v1/spools", GREEN)["id"]
    moon.script_errors["SET_PRINT_FILAMENT_CONFIG"] = "extruder busy"
    po.post("/v1/printers/u1/slots/T3/load", {"spool_id": sid})
    fleet = load_fleet()
    rec = rc.Reconciler(
        printops_settings.db_path,
        fake_spoolman,
        fleet,
        routes.fleet_reader(fleet, listen_s=0.5),
    )

    rec.poll("u1")
    found = [
        a
        for a in po.get("/v1/anomalies")["anomalies"]
        if a["kind"] == "printer_write_failed"
    ]
    assert [
        (a["subject"], a["evidence"]["stage"], a["evidence"]["spool"]["spool_id"])
        for a in found
    ] == [("slot:u1/T3", "filament", sid)]
    assert "extruder busy" in found[0]["evidence"]["error"]

    moon.script_errors.clear()
    po.post(
        "/v1/printers/u1/slots/T3/load", {"spool_id": sid}
    )  # the retry the WARNING suggests
    rec.poll("u1")
    assert [
        a
        for a in po.get("/v1/anomalies")["anomalies"]
        if a["kind"] == "printer_write_failed"
    ] == []


def _fleet_u1() -> Any:
    fleet = load_fleet()
    assert fleet is not None
    return fleet.printers["u1"]


def _binding(spool_id: int, slot: str = "u1/T3") -> rc.Binding:
    printer, _, head = slot.partition("/")
    return rc.Binding(
        spool_id,
        slot,
        printer,
        head,
        "Elegoo RAPID PLA+ Green",
        "PLA+",
        ("#4FE44D",),
        (),
        f"slot:{slot}",
        900.0,
    )


def _failure(spool_id: int, stage: str = "filament") -> dict[str, rc.WriteFailure]:
    return {
        "u1/T3": rc.WriteFailure(
            "u1/T3", spool_id, "boom", stage, "2026-10-17T00:00:00Z", 9
        )
    }


def _reading(moon: FakeMoonraker) -> Any:
    fleet = load_fleet()
    return routes.fleet_reader(fleet, listen_s=0.5)(_fleet_u1())


def test_write_failure_heals_when_the_printer_shows_the_spool_or_the_binding_changes(
    po: ApiClient, moon: FakeMoonraker
) -> None:
    u1 = _fleet_u1()
    stale = _reading(moon)  # T3 shows Snapmaker PLA #1B5E20

    def kinds(
        reading: Any, binds: list[rc.Binding], failures: dict[str, rc.WriteFailure]
    ) -> list[str]:
        return [
            f.kind
            for f in rc.compare_printer(u1, reading, binds, failures=failures)
            if f.kind == "printer_write_failed"
        ]

    assert kinds(stale, [_binding(7)], _failure(7)) == ["printer_write_failed"]
    assert kinds(None, [_binding(7)], _failure(7)) == [
        "printer_write_failed"
    ]  # unreadable: still open
    assert kinds(stale, [_binding(8)], _failure(7)) == []  # another spool is bound now
    assert kinds(stale, [], _failure(7)) == []  # unloaded

    cfg = copy.deepcopy(moon.status["print_task_config"])
    cfg["filament_type"][2], cfg["filament_color_rgba"][2] = (
        "PLA+",
        "50E34EFF",
    )  # set on the screen
    moon.status["print_task_config"] = cfg
    fixed = _reading(moon)
    assert kinds(fixed, [_binding(7)], _failure(7)) == []
    assert kinds(fixed, [_binding(7)], _failure(7, "pressure_advance")) == [
        "printer_write_failed"
    ]


def test_write_failures_reads_only_the_latest_write_per_slot(
    printops_db: sqlite3.Connection,
) -> None:
    def emit(kind: str, slot: str, spool_id: int) -> None:
        ev.emit(
            printops_db,
            kind,
            f"slot:{slot}",
            {"spool_id": spool_id, "error": "e", "stage": "filament"},
            "t",
        )

    emit(head_sync.EVENT_FAILED, "u1/T1", 1)
    emit(head_sync.EVENT_WRITTEN, "u1/T1", 1)
    emit(head_sync.EVENT_WRITTEN, "u1/T3", 2)
    emit(head_sync.EVENT_FAILED, "u1/T3", 2)
    printops_db.commit()
    got = rc.write_failures(printops_db)
    assert (
        list(got) == ["u1/T3"]
        and got["u1/T3"].spool_id == 2
        and got["u1/T3"].stage == "filament"
    )


# ---- the adapter and the values ---------------------------------------------------------------


def test_adapter_without_print_task_config_is_unsupported(moon: FakeMoonraker) -> None:
    del moon.status["print_task_config"]
    adapter = MoonrakerAdapter("k1", "127.0.0.1", moon.port)
    with pytest.raises(UnsupportedOperation):
        adapter.set_head_filament(0, HeadFilament("#FFFFFF", "PLA", "Generic", "Basic"))
    assert moon.writes() == []


def test_adapter_pressure_advance_is_read_back(moon: FakeMoonraker) -> None:
    adapter = MoonrakerAdapter("u1", "127.0.0.1", moon.port)
    assert adapter.set_pressure_advance(3, 0.041, smooth_time=0.03) == 0.041
    assert moon.scripts == [
        "SET_PRESSURE_ADVANCE EXTRUDER=extruder3 ADVANCE=0.041 SMOOTH_TIME=0.03"
    ]
    moon.ignored_commands.add("SET_PRESSURE_ADVANCE")
    with pytest.raises(PrinterCommError, match="pressure_advance 0.041, sent 0.05"):
        adapter.set_pressure_advance(3, 0.05)


@pytest.mark.parametrize(
    ("filament", "needle"),
    [
        (HeadFilament("#12345", "PLA", "Generic", "Basic"), "#RRGGBB"),
        (HeadFilament("#123456", "PLA", "Acme; G28", "Basic"), "cannot carry"),
        (HeadFilament("#123456", 'PLA"', "Generic", "Basic"), "cannot carry"),
        (HeadFilament("#123456", "PLA", "Generic", "Matte #2"), "cannot carry"),
        (HeadFilament("#123456", "", "Generic", "Basic"), "material is empty"),
        (HeadFilament("#123456", "PLA", "x" * 65, "Basic"), "longer than 64"),
    ],
)
def test_values_klipper_cannot_carry_are_refused_before_contact(
    moon: FakeMoonraker, filament: HeadFilament, needle: str
) -> None:
    with pytest.raises(ValueError, match=needle):
        filament_config_gcode(0, filament)
    with pytest.raises(PrinterRefused, match=needle):
        MoonrakerAdapter("u1", "127.0.0.1", moon.port).set_head_filament(0, filament)
    assert moon.requests == []


def test_gcode_line_quotes_spaces_and_upper_cases_the_colour() -> None:
    line = filament_config_gcode(
        1, HeadFilament("#a1b2c3", "PETG", "Bambu Lab", "HF Basic"), force_official=True
    )
    assert line == (
        'SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=1 VENDOR="Bambu Lab" FILAMENT_TYPE="PETG" '
        'FILAMENT_SUBTYPE="HF Basic" FILAMENT_COLOR_RGBA=A1B2C3FF FORCE=1'
    )


def test_wanted_filament_defaults_vendor_and_takes_the_first_multi_colour() -> None:
    spool = {
        "id": 4,
        "filament": {"material": " pla ", "multi_color_hexes": "ff0000,00ff00"},
    }
    assert head_sync.wanted_filament(spool) == HeadFilament(
        "#FF0000", "PLA", "Generic", "Basic"
    )
    with pytest.raises(ValueError, match="no colour"):
        head_sync.wanted_filament({"id": 5, "filament": {"material": "PLA"}})
    with pytest.raises(ValueError, match="no material"):
        head_sync.wanted_filament({"id": 6, "filament": {"color_hex": "FFFFFF"}})
