"""3d-api spool lifecycle through HTTP (ApiClient → TestClient → FakeSpoolman): receive → store →
load → use → unload (+weigh) → empty, the slot rules, weighing with the tare chain, the
idempotent import and GET /v1/lint (issues #160, #163)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fleet_fixtures import write_policy_for_fleet
from printops_fakes import FakeSpoolman

from printops import locations
from printops.client import ApiClient, ApiError

FLEET = """\
u1:
  model: Snapmaker U1
  owner: own
  access: full
  adapter: moonraker
  host: 127.0.0.1
  port: 9
  heads:
    - {label: T1, index: 0, usable: true}
    - {label: T2, index: 1, usable: false, reason: T2 is broken (babushkin-dom queue.json)}
    - {label: T3, index: 2, usable: true}
    - {label: T4, index: 3, usable: true}
p1s:
  model: Bambu Lab P1S
  owner: neighbour
  access: offline
  heads: [{label: T1, index: 0, usable: true}]
  ams: [{id: ams1, kind: ams, slots: 4}]
"""

GREEN = {
    "vendor": "Elegoo",
    "material": "PLA+",
    "name": "RAPID PLA+ Green",
    "color_hex": "#4FE44D",
    "net_g": 1000,
    "price": 1499,
    "location": "WH:default",
    "lot_nr": "L2409",
}


@pytest.fixture
def po(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    printops_db: sqlite3.Connection,
    api_client: ApiClient,
) -> ApiClient:
    """api_client with a fleet (u1 with broken T2, offline p1s with an AMS) and two shelves."""
    fleet = tmp_path / "fleet.yaml"
    fleet.write_text(FLEET, encoding="utf-8")
    monkeypatch.setenv("THREED_FLEET", str(fleet))
    monkeypatch.setenv("PRINTOPS_POLICY", str(write_policy_for_fleet(tmp_path / "policy.yaml", fleet)))
    locations.add(printops_db, "WH:default", "warehouse", actor="test")
    locations.add(printops_db, "WH:A", "zone", actor="test")
    locations.add(printops_db, "WH:A/S1", "shelf", actor="test")
    return api_client


def _events(
    conn: sqlite3.Connection, prefix: str = ""
) -> list[tuple[str, str, dict[str, Any]]]:
    rows = conn.execute(
        "SELECT kind, subject, payload FROM events WHERE kind LIKE ? ORDER BY id",
        (prefix + "%",),
    )
    return [(r["kind"], r["subject"], json.loads(r["payload"])) for r in rows]


def _err(fn: Any, *args: Any) -> ApiError:
    with pytest.raises(ApiError) as e:
        fn(*args)
    return e.value


def test_full_lifecycle_receive_store_load_use_unload_weigh_empty(
    po: ApiClient, fake_spoolman: FakeSpoolman, printops_db: sqlite3.Connection
) -> None:
    got = po.post("/v1/spools", GREEN)
    sid = got["id"]
    assert (got["vendor"], got["material"], got["color_hex"], got["location"]) == (
        "Elegoo",
        "PLA+",
        "4FE44D",
        "WH:default",
    )
    assert (
        got["filament_created"] is True
        and got["lot_nr"] == "L2409"
        and got["price"] == 1499
    )
    assert (
        got["remaining"]["anchored"] and got["remaining"]["anchor_source"] == "receive"
    )
    assert got["remaining_text"] == "1000 ± 20 g (980–1020)"
    assert (
        got["tare"]["tare_g"] == 154.0 and "SpoolmanDB" in got["tare"]["source"]
    )  # sourced tare DB
    sp = fake_spoolman.get("spool", sid)
    assert sp["spool_weight"] == 154.0 and sp["initial_weight"] == 1000
    assert (
        json.loads(sp["extra"]["printops_location_id"])
        == locations.get(printops_db, "WH:default").id
    )

    # same filament again → matched, not duplicated
    again = po.post("/v1/spools", {**GREEN, "lot_nr": "L2410"})
    assert (
        again["filament_id"] == got["filament_id"]
        and again["filament_created"] is False
    )

    moved = po.post(f"/v1/spools/{sid}/move", {"location": "WH:A/S1"})
    assert moved["location"] == "WH:A/S1"

    loaded = po.post("/v1/printers/u1/slots/T3/load", {"spool_id": sid})
    assert (
        loaded["slot"] == "u1/T3"
        and loaded["location"] == "slot:u1/T3"
        and loaded["opened_at"]
    )
    assert (
        json.loads(fake_spoolman.get("spool", sid)["extra"]["printops_slot"]) == "u1/T3"
    )

    used = po.post(
        f"/v1/spools/{sid}/use",
        {"grams": 100, "source": "gcode_estimate", "job_id": "plate-3"},
    )
    assert used["remaining"]["mid_g"] == 900.0 and used["remaining"][
        "sigma_g"
    ] == pytest.approx(22.4)
    assert (
        used["ledger_row"]["job_id"] == "plate-3"
        and used["spoolman_remaining_g"] == 900.0
    )

    # unload to the shelf and weigh on the way: 154 g tare + 820 g of filament
    out = po.post("/v1/printers/u1/slots/T3/unload", {"to": "WH:A/S1", "gross_g": 974})
    assert out["slot"] is None and out["location"] == "WH:A/S1"
    assert (
        out["remaining"]["mid_g"] == 820.0
        and out["remaining"]["anchor_source"] == "weigh"
    )
    assert out["remaining"]["sigma_g"] == pytest.approx(
        (1 + 10**2) ** 0.5, abs=0.1
    )  # scale ±1, SpoolmanDB tare ±10
    assert out["drift_g"] == -80.0 and fake_spoolman.get("spool", sid)[
        "remaining_weight"
    ] == pytest.approx(820.0)

    gone = po.post(f"/v1/spools/{sid}/empty", {"note": "ran out on plate 7"})
    assert (
        gone["archived"]
        and gone["location"] is None
        and gone["remaining"]["mid_g"] == 0.0
    )
    assert sid not in [s["id"] for s in po.get("/v1/spools")["spools"]]
    assert sid in [s["id"] for s in po.get("/v1/spools", archived="true")["spools"]]

    kinds = [k for k, _, _ in _events(printops_db) if k.startswith(("spool.", "slot."))]
    assert kinds == [
        "spool.received",
        "spool.received",
        "spool.moved",
        "slot.loaded",
        "spool.used",
        "spool.measured",  # unload --gross weighs first, then unloads
        "slot.unloaded",
        "spool.empty",
    ]
    _, subject, payload = next(e for e in _events(printops_db, "slot.loaded"))
    assert (
        subject == "slot:u1/T3"
        and payload["printer"] == "u1"
        and payload["head"] == "T3"
    )
    show = po.get(f"/v1/spools/{sid}")
    assert [r["source"] for r in show["ledger"]] == [
        "receive",
        "gcode_estimate",
        "weigh",
        "manual",
    ]
    assert show["ledger"][2]["gross_g"] == 974 and show["ledger"][2]["tare_g"] == 154


def test_load_refuses_a_head_with_open_faults_until_all_are_closed(po: ApiClient) -> None:
    """A fault opened from the CLI/voice stops loads into that head without editing the fleet file (#233)."""
    spool = po.post("/v1/spools", GREEN)["id"]
    loaded = po.post("/v1/spools", {**GREEN, "name": "RAPID PLA+ White", "color_hex": "#FFFFFF"})["id"]
    po.post("/v1/printers/u1/slots/t3/load", {"spool_id": loaded})
    clicks = po.post("/v1/printers/u1/faults", {"symptom": "extruder clicks", "head": "t3"})  # stored as T3
    heater = po.post("/v1/printers/u1/faults", {"symptom": "heater fluctuates", "head": "T3"})

    # a spool already in the head can still come out to service it
    assert po.post("/v1/printers/u1/slots/T3/unload", {})["slot"] is None

    refused = _err(po.post, "/v1/printers/u1/slots/t3/load", {"spool_id": spool})
    assert refused.status == 409
    assert f"#{clicks['id']} extruder clicks" in refused.message
    assert f"#{heater['id']} heater fluctuates" in refused.message
    assert po.get(f"/v1/spools/{spool}")["slot"] is None  # nothing was written to Spoolman
    assert _err(po.post, "/v1/printers/u1/slots/T3/load", {"spool_id": 999}).status == 404  # spool first

    replace = _err(po.post, "/v1/printers/u1/slots/T3/load", {"spool_id": spool, "replace_to": "WH:A/S1"})
    assert replace.status == 409 and "open fault(s)" in replace.message  # the fault wins over replace_to
    po.post("/v1/printers/u1/slots/T1/load", {"spool_id": spool})  # other heads stay usable
    po.post("/v1/printers/u1/slots/T1/unload", {})
    printer_wide = po.post("/v1/printers/u1/faults", {"symptom": "enclosure fan noisy"})
    po.post("/v1/printers/u1/slots/T4/load", {"spool_id": spool})  # a printer-wide fault gates no head
    po.post("/v1/printers/u1/slots/T4/unload", {})
    po.post(f"/v1/faults/{printer_wide['id']}/close", {"status": "wontfix"})

    po.post(f"/v1/faults/{clicks['id']}/close", {"status": "fixed", "root_cause": "gear cleaned"})
    still = _err(po.post, "/v1/printers/u1/slots/T3/load", {"spool_id": spool})
    assert still.status == 409 and f"#{heater['id']}" in still.message and f"#{clicks['id']} " not in still.message
    po.post(f"/v1/faults/{heater['id']}/close", {"status": "wontfix", "root_cause": "within tolerance"})
    assert po.post("/v1/printers/u1/slots/T3/load", {"spool_id": spool})["slot"] == "u1/T3"


def test_an_ams_slot_loads_while_a_head_of_the_same_printer_has_a_fault(po: ApiClient) -> None:
    """AMS slots are not heads: a fault on p1s T1 does not block p1s/ams1/2 (#233)."""
    spool = po.post("/v1/spools", GREEN)["id"]
    po.post("/v1/printers/p1s/faults", {"symptom": "nozzle clogged", "head": "T1"})
    assert _err(po.post, "/v1/printers/p1s/slots/T1/load", {"spool_id": spool}).status == 409
    assert po.post("/v1/printers/p1s/slots/ams1/2/load", {"spool_id": spool})["slot"] == "p1s/ams1/2"


def test_a_load_refusal_names_the_oldest_faults_only(po: ApiClient) -> None:
    """Four open faults: the refusal names three, shortened, and counts the rest."""
    spool = po.post("/v1/spools", GREEN)["id"]
    ids = [po.post("/v1/printers/u1/faults", {"symptom": f"problem {i} " + "x" * 200, "head": "T4"})["id"]
           for i in range(4)]
    refused = _err(po.post, "/v1/printers/u1/slots/T4/load", {"spool_id": spool})
    assert "has 4 open fault(s)" in refused.message and "(+1 more)" in refused.message
    assert f"#{ids[2]} problem 2" in refused.message and f"#{ids[3]} " not in refused.message
    assert "x" * 81 not in refused.message


def test_a_fault_symptom_with_control_characters_is_refused(po: ApiClient) -> None:
    """Symptoms are echoed to terminals in refusals and `fault ls`: no escape sequences."""
    bad = _err(po.post, "/v1/printers/u1/faults", {"symptom": "clicks\x1b]52;c;cGF3bmVk\x07", "head": "T3"})
    assert bad.status == 422 and "control characters" in bad.message
    c1 = _err(po.post, "/v1/printers/u1/faults", {"symptom": "clicks\x9b31m", "head": "T3"})  # C1 CSI
    assert c1.status == 422
    bidi = _err(po.post, "/v1/printers/u1/faults", {"symptom": "clicks \u202eskcilc", "head": "T3"})
    assert bidi.status == 422
    lone_cr = _err(po.post, "/v1/printers/u1/faults", {"symptom": "clicks\r#99  FIXED  u1/T3", "head": "T3"})
    assert lone_cr.status == 422
    fault = po.post("/v1/printers/u1/faults", {"symptom": "clicks\r\nthen stops", "head": "T3"})
    assert fault["symptom"] == "clicks\nthen stops"  # CRLF stored as LF
    closing = _err(po.post, f"/v1/faults/{fault['id']}/close", {"root_cause": "gear\x1b[2J"})
    assert closing.status == 422 and "root_cause: control characters" in closing.message


def test_a_load_refusal_prints_fault_text_on_one_line_without_escapes(
    po: ApiClient, printops_db: sqlite3.Connection
) -> None:
    """A multi-line symptom, and a row stored before control characters were refused, still give a
    one-line, escape-free refusal."""
    spool = po.post("/v1/spools", GREEN)["id"]
    multi = po.post("/v1/printers/u1/faults", {"symptom": "clicks\nthen\tstops", "head": "T3"})
    old = po.post("/v1/printers/u1/faults", {"symptom": "placeholder", "head": "T3"})
    printops_db.execute("UPDATE fault SET symptom = ? WHERE id = ?", ("old\x1b]52;c;cGF3bmVk\x07row", old["id"]))
    printops_db.commit()
    refused = _err(po.post, "/v1/printers/u1/slots/T3/load", {"spool_id": spool})
    assert "\x1b" not in refused.message and "\x07" not in refused.message and "\n" not in refused.message
    assert f"#{multi['id']} clicks then stops" in refused.message
    assert f"#{old['id']} old ]52;c;cGF3bmVk row" in refused.message


def test_load_refuses_unusable_heads_unknown_slots_and_occupied_slots(
    po: ApiClient, fake_spoolman: FakeSpoolman
) -> None:
    a = po.post("/v1/spools", GREEN)["id"]
    b = po.post(
        "/v1/spools", {**GREEN, "name": "RAPID PLA+ White", "color_hex": "#FFFFFF"}
    )["id"]

    broken = _err(po.post, "/v1/printers/u1/slots/T2/load", {"spool_id": a})
    assert broken.status == 409 and "T2 is broken" in broken.message
    assert _err(po.post, "/v1/printers/u1/slots/T9/load", {"spool_id": a}).status == 404
    assert (
        _err(po.post, "/v1/printers/x1c/slots/T1/load", {"spool_id": a}).status == 404
    )

    po.post("/v1/printers/u1/slots/T1/load", {"spool_id": a})
    busy = _err(po.post, "/v1/printers/u1/slots/T1/load", {"spool_id": b})
    assert busy.status == 409 and f"spool #{a}" in busy.message

    swapped = po.post(
        "/v1/printers/u1/slots/T1/load", {"spool_id": b, "replace_to": "WH:A/S1"}
    )
    assert swapped["slot"] == "u1/T1"
    old = po.get(f"/v1/spools/{a}")
    assert old["slot"] is None and old["location"] == "WH:A/S1"

    # a spool moves between slots; the old slot is released
    po.post("/v1/printers/u1/slots/T4/load", {"spool_id": b})
    assert [s["id"] for s in po.get("/v1/spools", loaded="true")["spools"]] == [b]
    assert po.get(f"/v1/spools/{b}")["slot"] == "u1/T4"

    # AMS slot of an offline (manual-only) printer
    ams = po.post("/v1/printers/p1s/slots/ams1/2/load", {"spool_id": a})
    assert ams["slot"] == "p1s/ams1/2"
    assert (
        _err(po.post, "/v1/printers/p1s/slots/ams1/5/load", {"spool_id": a}).status
        == 404
    )
    assert _err(po.post, "/v1/printers/u1/slots/T3/unload", {}).status == 404


def test_measure_needs_a_tare_and_reports_the_chain(
    po: ApiClient, fake_spoolman: FakeSpoolman
) -> None:
    sid = po.post(
        "/v1/spools",
        {
            "material": "PLA",
            "name": "белый",
            "color_hex": "#FFFFFF",
            "location": "WH:default",
        },
    )["id"]
    fresh = po.get(f"/v1/spools/{sid}")
    assert fresh["tare"] is None and not fresh["remaining"]["anchored"]
    assert fresh["remaining_text"].endswith("(never weighed)")

    no_tare = _err(po.post, f"/v1/spools/{sid}/measure", {"gross_g": 900})
    assert no_tare.status == 409 and "no tare" in no_tare.message

    weighed = po.post(
        f"/v1/spools/{sid}/measure",
        {
            "gross_g": 900,
            "tare_g": 180,
            "tare_source": "weighed an identical empty spool",
        },
    )
    assert weighed["remaining"]["mid_g"] == 720.0 and weighed["remaining"][
        "sigma_g"
    ] == pytest.approx(2.2, abs=0.1)
    assert weighed["drift_g"] is None  # nothing to compare with: first anchor
    sp = fake_spoolman.get("spool", sid)
    assert sp["spool_weight"] == 180 and sp["remaining_weight"] == pytest.approx(720.0)
    chain = po.get(f"/v1/spools/{sid}/tare")
    assert (
        chain["tare"]["origin"] == "choice"
        and chain["tare"]["source"] == "weighed an identical empty spool"
    )

    # second weighing re-uses the recorded tare; gross below tare is refused
    again = po.post(f"/v1/spools/{sid}/measure", {"gross_g": 850})
    assert again["remaining"]["mid_g"] == 670.0 and again["drift_g"] == -50.0
    assert _err(po.post, f"/v1/spools/{sid}/measure", {"gross_g": 100}).status == 422


def test_tares_api(po: ApiClient) -> None:
    added = po.post(
        "/v1/tares",
        {
            "vendor": "Snapmaker",
            "tare_g": 165,
            "spool_kind": "plastic",
            "net_g": 1000,
            "source": "weighed empty Snapmaker spool",
        },
    )
    assert added["sigma_g"] == 2.0
    assert [t["tare_g"] for t in po.get("/v1/tares", vendor="snapmaker")["tares"]] == [
        165.0
    ]
    assert _err(po.post, "/v1/tares", {"vendor": "X", "tare_g": 10}).status == 422
    sid = po.post(
        "/v1/spools",
        {
            "vendor": "Snapmaker",
            "material": "PLA",
            "name": "SnapSpeed White",
            "net_g": 1000,
            "spool_kind": "plastic",
        },
    )["id"]
    assert po.get(f"/v1/spools/{sid}")["tare"]["tare_g"] == 165.0


def test_use_validation_and_scopes(
    po: ApiClient, printops_client: Any, printops_tokens: dict[str, str]
) -> None:
    sid = po.post("/v1/spools", GREEN)["id"]
    assert (
        _err(po.post, f"/v1/spools/{sid}/use", {"grams": 5, "source": "guess"}).status
        == 422
    )
    assert _err(po.post, f"/v1/spools/{sid}/use", {"grams": "5"}).status == 422
    assert _err(po.post, "/v1/spools/999/use", {"grams": 5}).status == 404
    r = printops_client.post(
        f"/v1/spools/{sid}/use",
        json={"grams": 5},
        headers={"Authorization": f"Bearer {printops_tokens['read']}"},
    )
    assert r.status_code == 403
    r = printops_client.get(
        "/v1/spools", headers={"Authorization": f"Bearer {printops_tokens['write']}"}
    )
    assert r.status_code == 403


def test_import_is_a_dry_run_first_then_idempotent(
    po: ApiClient, fake_spoolman: FakeSpoolman
) -> None:
    items: list[dict[str, Any]] = [
        {
            "external_key": "bd:owned:белый",
            "material": "PLA",
            "name": "белый",
            "color_hex": "#FFFFFF",
            "location": "WH:default",
        },
        {
            "external_key": "bd:owned:зелёный Elegoo",
            "vendor": "Elegoo",
            "material": "PLA+",
            "name": "RAPID PLA+ Green",
            "color_hex": "#4FE44D",
            "price": 1499,
            "location": "WH:default",
        },
    ]
    dry = po.post("/v1/spools/import", {"items": items, "dry_run": True})
    assert dry["changes"] == 2 and [a["action"] for a in dry["actions"]] == [
        "create",
        "create",
    ]
    assert fake_spoolman.writes() == [
        w for w in fake_spoolman.writes() if w[1].startswith("/field")
    ]

    applied = po.post("/v1/spools/import", {"items": items, "dry_run": False})
    assert applied["changes"] == 2 and applied["counts"] == {"created": 2}
    spools = po.get("/v1/spools")["spools"]
    assert sorted(s["external_key"] for s in spools) == [
        "bd:owned:белый",
        "bd:owned:зелёный Elegoo",
    ]

    rerun = po.post("/v1/spools/import", {"items": items, "dry_run": False})
    assert rerun["changes"] == 0 and rerun["counts"] == {"exists": 2}
    drift = po.post(
        "/v1/spools/import",
        {"items": [{**items[0], "color_hex": "#FAFAFA"}], "dry_run": True},
    )
    assert drift["changes"] == 0 and "color_hex" in drift["actions"][0]["differs"][0]
    assert len(po.get("/v1/spools")["spools"]) == 2

    dup = _err(po.post, "/v1/spools", items[0])
    assert dup.status == 409 and "external key" in dup.message


def test_lint_endpoint(po: ApiClient) -> None:
    po.post("/v1/spools", {"material": "PLA", "name": "чёрный", "color_hex": "#000000"})
    sealed = po.post("/v1/spools", GREEN)["id"]
    res = po.get("/v1/lint")
    rules = {(f["rule"], f["subject"]) for f in res["findings"]}
    assert ("spools/no-tare", "spool:1") in rules and (
        "spools/not-weighed",
        "spool:1",
    ) in rules
    assert not any(s == f"spool:{sealed}" for _, s in rules)
    assert (
        res["errors"] == 0
        and res["warnings"] == len(res["findings"])
        and "lint_spools" in res["rules"]
    )


def test_import_validates_every_item_before_writing(
    po: ApiClient, fake_spoolman: FakeSpoolman
) -> None:
    good = {
        "external_key": "bd:owned:белый",
        "material": "PLA",
        "name": "белый",
        "color_hex": "#FFFFFF",
        "location": "WH:default",
    }
    for bad, status in (
        ({**good, "external_key": "bd:x", "location": "WH:nowhere"}, 404),
        ({**good, "external_key": "bd:y", "material": "Unobtainium"}, 422),
        (good, 422),
    ):  # duplicate key in one batch
        before = len(fake_spoolman.writes())
        err = _err(
            po.post, "/v1/spools/import", {"items": [good, bad], "dry_run": False}
        )
        assert err.status == status, err.message
        assert len(fake_spoolman.writes()) == before
    assert po.get("/v1/spools")["spools"] == []


# -- slot spelling, validation before writes, concurrency -----------------------------------------


def test_unload_canonicalises_the_slot_like_load(
    po: ApiClient, fake_spoolman: FakeSpoolman
) -> None:
    a = po.post("/v1/spools", GREEN)["id"]
    po.post("/v1/printers/u1/slots/T3/load", {"spool_id": a})
    assert po.post("/v1/printers/u1/slots/t3/unload", {})["slot"] is None
    po.post("/v1/printers/p1s/slots/ams1/2/load", {"spool_id": a})
    assert po.post("/v1/printers/p1s/slots/ams1/02/unload", {})["slot"] is None
    # an unusable head can still be emptied (the spool got there by hand in Spoolman)
    fake_spoolman.update("spool", a, {"extra": {"printops_slot": json.dumps("u1/T2")}})
    assert po.post("/v1/printers/u1/slots/T2/unload", {})["slot"] is None


def test_import_checks_numbers_before_the_first_write(
    po: ApiClient, fake_spoolman: FakeSpoolman
) -> None:
    good = {"external_key": "bd:a", "material": "PLA", "name": "белый"}
    for bad in (
        {"external_key": "bd:b", "material": "PLA", "name": "x", "price": -5},
        {"external_key": "bd:c", "material": "PLA", "name": "x", "net_g": -1},
        {"external_key": "bd:d", "material": "PLA", "name": "x", "spool_kind": "glass"},
    ):
        before = len(fake_spoolman.writes())
        err = _err(
            po.post, "/v1/spools/import", {"items": [good, bad], "dry_run": False}
        )
        assert err.status == 422, err.message
        assert len(fake_spoolman.writes()) == before
    assert po.get("/v1/spools")["spools"] == []


def test_ledger_of_an_unknown_spool_is_404(po: ApiClient) -> None:
    assert _err(po.get, "/v1/spools/999/ledger").status == 404


@pytest.mark.parametrize(
    "field, value",
    [
        ("material", 7),
        ("name", ["x"]),
        ("net_g", "1000"),
        ("price", True),
        ("opened", "yes"),
        ("filament_id", True),
        ("filament_id", 1.5),
    ],
)
def test_receive_rejects_wrong_field_types_with_422(
    po: ApiClient, field: str, value: Any
) -> None:
    body = {"material": "PLA", "name": "белый", field: value}
    err = _err(po.post, "/v1/spools", body)
    assert err.status == 422 and field in err.message
    imp = _err(po.post, "/v1/spools/import", {"items": [{**body, "external_key": "k"}]})
    assert imp.status == 422 and field in imp.message


def test_unload_with_weighing_leaves_the_slot_when_spoolman_fails(
    po: ApiClient,
    fake_spoolman: FakeSpoolman,
    printops_db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from printops.spoolman import SpoolmanError

    sid = po.post("/v1/spools", GREEN)["id"]
    po.post("/v1/printers/u1/slots/T3/load", {"spool_id": sid})

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise SpoolmanError("Spoolman 500", status=500)

    monkeypatch.setattr(fake_spoolman, "measure", boom)  # the weighing's Spoolman write
    err = _err(
        po.post, "/v1/printers/u1/slots/T3/unload", {"to": "WH:A/S1", "gross_g": 974}
    )
    assert err.status == 502
    monkeypatch.undo()
    still = po.get(f"/v1/spools/{sid}")
    assert still["slot"] == "u1/T3" and still["location"] == "slot:u1/T3"
    assert [r["source"] for r in still["ledger"]] == ["receive"]
    assert not _events(printops_db, "slot.unloaded")


@pytest.mark.parametrize("action", ["empty", "move"])
def test_empty_and_move_recheck_archived_under_the_write_lock(
    po: ApiClient,
    fake_spoolman: FakeSpoolman,
    printops_db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    """Another writer empties the spool while this request waits for the write lock."""
    from printops import db

    sid = po.post("/v1/spools", GREEN)["id"]
    real = db.transaction

    def racing(conn: sqlite3.Connection) -> Any:
        fake_spoolman.update("spool", sid, {"archived": True})
        return real(conn)

    monkeypatch.setattr(db, "transaction", racing)
    body = {"location": "WH:A/S1"} if action == "move" else {}
    err = _err(po.post, f"/v1/spools/{sid}/{action}", body)
    assert err.status == 409 and "archived" in err.message
    monkeypatch.undo()
    rows = po.get(f"/v1/spools/{sid}/ledger")["rows"]
    assert [r["source"] for r in rows] == ["receive"]
    assert not _events(printops_db, "spool.empty") and not _events(
        printops_db, "spool.moved"
    )


@pytest.mark.parametrize(
    "field, value",
    [("remaining_confidence_g", -1), ("density", 0), ("diameter", -1.75)],
)
def test_import_rejects_bad_confidence_density_diameter_before_writing(
    po: ApiClient, fake_spoolman: FakeSpoolman, field: str, value: int | float
) -> None:
    good = {"external_key": "bd:a", "material": "PLA", "name": "белый"}
    bad = {
        "external_key": "bd:b",
        "material": "PLA",
        "name": "x",
        "remaining_g": 100,
        field: value,
    }
    before = len(fake_spoolman.writes())
    err = _err(po.post, "/v1/spools/import", {"items": [good, bad], "dry_run": False})
    assert err.status == 422 and field in err.message, err.message
    assert "finite number" in err.message  # the value check, not "unknown field"
    assert len(fake_spoolman.writes()) == before


def test_unload_from_a_printer_that_left_the_fleet_ignores_case(
    po: ApiClient, fake_spoolman: FakeSpoolman
) -> None:
    sid = po.post("/v1/spools", GREEN)["id"]
    fake_spoolman.update(
        "spool", sid, {"extra": {"printops_slot": json.dumps("k2/T1")}}
    )
    assert po.post("/v1/printers/k2/slots/t1/unload", {})["slot"] is None


def test_load_sees_an_occupant_whose_slot_differs_in_case(
    po: ApiClient, fake_spoolman: FakeSpoolman
) -> None:
    """A spool hand-set to `u1/t1` in Spoolman still occupies u1/T1."""
    a = po.post("/v1/spools", GREEN)["id"]
    b = po.post(
        "/v1/spools", {**GREEN, "name": "RAPID PLA+ White", "color_hex": "#FFFFFF"}
    )["id"]
    fake_spoolman.update("spool", a, {"extra": {"printops_slot": json.dumps("u1/t1")}})
    busy = _err(po.post, "/v1/printers/u1/slots/T1/load", {"spool_id": b})
    assert busy.status == 409 and f"spool #{a}" in busy.message
    assert po.get(f"/v1/spools/{b}")["slot"] is None
