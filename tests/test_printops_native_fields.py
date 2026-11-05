"""Behavior tests: PrintOps keeps no duplicate of a native Spoolman field. `native_fields.migrate_native`
moves the legacy extras (vendor tare, slot, location id, opened_at, tag_uids) to the native fields,
dry run by default, and deletes a legacy field definition only when no item holds a value; the tare
chain no longer reads the old extra; `/v1/fields/migrate-native` serves it and `/v1/health` reports
a leftover definition."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

import pytest
from printops_fakes import FakeSpoolman
from printops_fixtures import LiveApi

from printops import native_fields, tares
from printops.settings import Settings
from printops.spoolman import (
    LEGACY_EXTRA_FIELDS,
    HttpSpoolman,
    SpoolmanError,
    encode_extra,
    ensure_extra_fields,
    missing_extra_fields,
)


def no_locations(location_id: int) -> str | None:
    """No PrintOps locations exist: no recorded id resolves."""
    return None


LEGACY_PARAMS: dict[tuple[str, str], dict[str, Any]] = {
    ("vendor", "spool_tare_g"): {"name": "Tare", "field_type": "float", "unit": "g"},
    ("spool", "printops_slot"): {"name": "Slot", "field_type": "text"},
    ("spool", "printops_location_id"): {"name": "Loc id", "field_type": "integer"},
    ("spool", "opened_at"): {"name": "Opened", "field_type": "datetime"},
    ("spool", "tag_uids"): {"name": "Tags", "field_type": "text"},
}


@pytest.fixture
def sm() -> FakeSpoolman:
    """A Spoolman an old PrintOps registered: PrintOps fields plus every legacy field."""
    fake = FakeSpoolman()
    ensure_extra_fields(fake)
    for (entity, key), params in LEGACY_PARAMS.items():
        fake.set_field(entity, key, params)  # type: ignore[arg-type]
    return fake


def _vendor(
    sm: FakeSpoolman,
    name: str,
    *,
    extra: float | str | None = None,
    native: float | None = None,
) -> int:
    body: dict[str, Any] = {"name": name}
    if native is not None:
        body["empty_spool_weight"] = native
    vid = int(sm.create("vendor", body)["id"])
    if isinstance(
        extra, str
    ):  # garbage a real Spoolman would not accept on a float field
        sm.items["vendor"][vid]["extra"]["spool_tare_g"] = extra
    elif extra is not None:
        sm.update("vendor", vid, {"extra": {"spool_tare_g": encode_extra(extra)}})
    return vid


def _filament(sm: FakeSpoolman, **extra: Any) -> int:
    body: dict[str, Any] = {
        "material": "PLA",
        "density": 1.24,
        "diameter": 1.75,
        "weight": 1000,
        "name": "Galaxy",
    }
    body.update(extra)
    return int(sm.create("filament", body)["id"])


def _spool(
    sm: FakeSpoolman,
    fil: int | None = None,
    *,
    extra: Mapping[str, Any] | None = None,
    **native: Any,
) -> int:
    fid = fil if fil is not None else _filament(sm)
    ex = {
        k: encode_extra(v)
        for k, v in (extra or {}).items()
    }
    return int(sm.create("spool", {"filament_id": fid, **native, "extra": ex})["id"])


def _keys(sm: FakeSpoolman, entity: str) -> set[str]:
    return {str(f["key"]) for f in sm.list_fields(entity)}  # type: ignore[arg-type]


def _actions(report: native_fields.MigrationReport, field: str) -> list[str]:
    return [c.action for c in report.changes if c.field == field]


# ------------------------------------------------------------------------- fake field delete / tags


def test_fake_delete_field_destroys_stored_values_like_the_real_one(
    sm: FakeSpoolman,
) -> None:
    vid = _vendor(sm, "A", extra=135.0)
    remaining = sm.delete_field("vendor", "spool_tare_g")
    assert "spool_tare_g" not in {f["key"] for f in remaining}
    assert sm.get("vendor", vid)["extra"] == {}
    with pytest.raises(SpoolmanError, match="does not exist") as e:
        sm.delete_field("vendor", "spool_tare_g")
    assert e.value.status == 404


def test_http_delete_field_and_link_tag_round_trip(sm: FakeSpoolman) -> None:
    sid = _spool(sm)
    other = _spool(sm)
    with sm.serve() as url:
        http = HttpSpoolman(url)
        assert "spool_tare_g" not in {
            f["key"] for f in http.delete_field("vendor", "spool_tare_g")
        }
        assert http.link_tag(sid, "04:a2:b3")["uid"] == "04A2B3"
        http.link_tag(sid, "04A2B3")  # re-linking to the holder succeeds
        with pytest.raises(SpoolmanError) as taken:
            http.link_tag(other, "04a2b3")
        assert taken.value.status == 409
        with pytest.raises(SpoolmanError) as bad:
            http.link_tag(sid, "not hex")
        assert bad.value.status == 400
    assert [t["uid"] for t in sm.get("spool", sid)["tags"]] == ["04A2B3"]


# -------------------------------------------------------------------------------------- the chain


def test_vendor_with_only_the_legacy_extra_yields_no_tare_until_migrated(
    sm: FakeSpoolman, printops_db: sqlite3.Connection
) -> None:
    vid = _vendor(sm, "NoName", extra=135.0)
    fil = _filament(sm, vendor_id=vid)
    assert (
        tares.resolve_new(printops_db, sm.get("filament", fil)).tare is None
    )  # the extra is ignored

    native_fields.migrate_native(sm, location_path=no_locations, apply=True)

    tare = tares.resolve_new(printops_db, sm.get("filament", fil)).tare
    assert tare is not None
    assert (tare.grams, tare.origin, tare.source) == (
        135.0,
        "vendor",
        "Spoolman vendor.empty_spool_weight",
    )


# --------------------------------------------------------------------------- dry run vs apply


def _legacy_world(sm: FakeSpoolman) -> dict[str, int]:
    fil = _filament(sm, extra={})
    ids = {
        "tare-only": _vendor(sm, "tare-only", extra=135.0),
        "tare-equal": _vendor(sm, "tare-equal", extra=160.0, native=160.02),
        "tare-conflict": _vendor(sm, "tare-conflict", extra=140.0, native=150.0),
        "tare-garbage": _vendor(sm, "tare-garbage", extra="not json"),
        "tare-negative": _vendor(sm, "tare-negative", extra="-5"),
        "slot-free": _spool(
            sm, fil, extra={"printops_slot": "u1/T3", "printops_location_id": 7}
        ),
        "slot-same": _spool(
            sm, fil, location="slot:U1/T1", extra={"printops_slot": "u1/T1"}
        ),
        "slot-conflict": _spool(
            sm, fil, location="WH:A/S1", extra={"printops_slot": "u1/T2"}
        ),
        "slot-empty": _spool(sm, fil, location="WH:A/S1", extra={"printops_slot": ""}),
        "opened-new": _spool(sm, fil, extra={"opened_at": "2026-10-01T10:00:00Z"}),
        "opened-earlier": _spool(
            sm,
            fil,
            first_used="2026-10-20T00:00:00Z",
            extra={"opened_at": "2026-10-01T10:00:00Z"},
        ),
        "opened-later": _spool(
            sm,
            fil,
            first_used="2026-09-01T00:00:00Z",
            extra={"opened_at": "2026-10-01T10:00:00Z"},
        ),
        "opened-garbage": _spool(sm, fil, extra={"opened_at": "tomorrow"}),
        "tags": _spool(
            sm, fil, extra={"tag_uids": '["04:aa:bb", "CC-DD"]'}
        ),
    }
    return ids


def test_dry_run_plans_every_case_and_writes_nothing(sm: FakeSpoolman) -> None:
    ids = _legacy_world(sm)
    snapshot = {
        n: (
            sm.get("spool", i)
            if n.split("-")[0] in ("slot", "opened", "tags")
            else sm.get("vendor", i)
        )
        for n, i in ids.items()
    }
    writes_before = sm.writes()

    report = native_fields.migrate_native(sm, location_path=no_locations, apply=False)

    assert sm.writes() == writes_before  # GETs only
    assert {
        n: (
            sm.get("spool", i)
            if n.split("-")[0] in ("slot", "opened", "tags")
            else sm.get("vendor", i)
        )
        for n, i in ids.items()
    } == snapshot
    assert not report.apply and report.pending and report.blocked
    assert _actions(report, "vendor.spool_tare_g") == [
        "copy",
        "drop",
        "conflict",
        "invalid",
        "invalid",
    ]
    assert _actions(report, "spool.printops_slot") == [
        "copy",
        "drop",
        "conflict",
        "drop",
    ]
    assert _actions(report, "spool.opened_at") == ["copy", "adjust", "drop", "invalid"]
    assert _actions(report, "spool.tag_uids") == ["link"]
    states = {f.field: f.state for f in report.fields}
    assert (
        states["vendor.spool_tare_g"] == "kept"
    )  # a conflict and two invalid values would still hold it
    assert states["spool.printops_location_id"] == "would_delete"
    assert states["spool.printops_slot"] == "kept"
    assert all(
        f in _keys(sm, e)
        for e, f in [("vendor", "spool_tare_g"), ("spool", "tag_uids")]
    )


def test_apply_moves_vendor_tares_and_never_overwrites(sm: FakeSpoolman) -> None:
    ids = _legacy_world(sm)
    native_fields.migrate_native(sm, location_path=no_locations, apply=True)

    vendors = {n: sm.get("vendor", i) for n, i in ids.items() if n.startswith("tare")}
    assert (
        vendors["tare-only"]["empty_spool_weight"] == 135.0
        and vendors["tare-only"]["extra"] == {}
    )
    assert (
        vendors["tare-equal"]["empty_spool_weight"] == 160.02
        and vendors["tare-equal"]["extra"] == {}
    )
    assert vendors["tare-conflict"]["empty_spool_weight"] == 150.0
    assert vendors["tare-conflict"]["extra"] == {"spool_tare_g": encode_extra(140.0)}
    assert vendors["tare-garbage"]["extra"] == {"spool_tare_g": "not json"}


def test_apply_moves_spool_values_and_never_overwrites(sm: FakeSpoolman) -> None:
    ids = _legacy_world(sm)
    native_fields.migrate_native(sm, location_path=no_locations, apply=True)

    spools = {n: sm.get("spool", i) for n, i in ids.items() if not n.startswith("tare")}
    assert (
        spools["slot-free"]["location"] == "slot:u1/T3"
        and "printops_slot" not in spools["slot-free"]["extra"]
    )
    assert "printops_location_id" not in spools["slot-free"]["extra"]
    assert (
        spools["slot-same"]["location"] == "slot:U1/T1"
        and spools["slot-same"]["extra"] == {}
    )
    assert spools["slot-conflict"]["location"] == "WH:A/S1"  # never moved
    assert spools["slot-conflict"]["extra"] == {"printops_slot": encode_extra("u1/T2")}
    assert (
        spools["slot-empty"]["location"] == "WH:A/S1"
        and spools["slot-empty"]["extra"] == {}
    )
    assert (
        spools["opened-new"]["first_used"] == "2026-10-01T10:00:00Z"
        and spools["opened-new"]["extra"] == {}
    )
    assert (
        spools["opened-earlier"]["first_used"] == "2026-10-01T10:00:00Z"
    )  # opened before the first use: moved earlier
    assert spools["opened-later"]["first_used"] == "2026-09-01T00:00:00Z"
    assert spools["opened-garbage"]["extra"] == {"opened_at": encode_extra("tomorrow")}
    assert {t["uid"] for t in spools["tags"]["tags"]} == {"04AABB", "CCDD"} and spools[
        "tags"
    ]["extra"] == {}


def test_apply_keeps_a_definition_while_an_item_still_holds_a_value(sm: FakeSpoolman) -> None:
    _legacy_world(sm)
    report = native_fields.migrate_native(sm, location_path=no_locations, apply=True)

    states = {f.field: f.state for f in report.fields}
    assert states[
        "spool.printops_location_id"
    ] == "deleted" and "printops_location_id" not in _keys(sm, "spool")
    assert states["spool.tag_uids"] == "deleted" and "tag_uids" not in _keys(
        sm, "spool"
    )
    for kept in ("vendor.spool_tare_g", "spool.printops_slot", "spool.opened_at"):
        assert states[kept] == "kept"
    assert {"spool_tare_g"} <= _keys(sm, "vendor") and {
        "printops_slot",
        "opened_at",
    } <= _keys(sm, "spool")
    assert report.blocked and not report.pending


def test_opened_at_earlier_than_first_used_adjusts_first_used(sm: FakeSpoolman) -> None:
    fil = _filament(sm)
    early = _spool(
        sm,
        fil,
        first_used="2026-10-20T00:00:00Z",
        extra={"opened_at": "2026-10-01T10:00:00Z"},
    )
    late = _spool(
        sm,
        fil,
        first_used="2026-09-01T00:00:00Z",
        extra={"opened_at": "2026-10-01T10:00:00Z"},
    )
    report = native_fields.migrate_native(sm, location_path=no_locations, apply=True)
    assert _actions(report, "spool.opened_at") == ["adjust", "drop"]
    assert (
        sm.get("spool", early)["first_used"] == "2026-10-01T10:00:00Z"
    )  # opened earlier than first used
    assert (
        sm.get("spool", late)["first_used"] == "2026-09-01T00:00:00Z"
    )  # first use was earlier: kept


def test_fully_resolvable_world_finishes_deletes_every_definition_and_second_run_is_a_noop(
    sm: FakeSpoolman,
) -> None:
    vid = _vendor(sm, "a", extra=135.0)
    sid = _spool(
        sm,
        extra={
            "printops_slot": "u1/T1",
            "printops_location_id": 3,
            "opened_at": "2026-10-01T10:00:00Z",
            "tag_uids": '["AA11"]',
        },
    )
    first = native_fields.migrate_native(sm, location_path=no_locations, apply=True)
    assert not first.blocked and all(f.state == "deleted" for f in first.fields)
    assert not {k for _, k in LEGACY_EXTRA_FIELDS} & (
        _keys(sm, "vendor") | _keys(sm, "spool")
    )
    assert (
        sm.get("vendor", vid)["empty_spool_weight"] == 135.0
    )  # survived the field delete
    spool = sm.get("spool", sid)
    assert (
        spool["location"] == "slot:u1/T1"
        and spool["first_used"] == "2026-10-01T10:00:00Z"
    )
    assert [t["uid"] for t in spool["tags"]] == ["AA11"]

    writes = len(sm.writes())
    again = native_fields.migrate_native(sm, location_path=no_locations, apply=True)
    assert again.changes == [] and not again.blocked and not again.pending
    assert all(f.state == "absent" for f in again.fields)
    assert len(sm.writes()) == writes
    assert missing_extra_fields(sm) == []


def test_tag_held_by_another_spool_is_a_conflict_and_keeps_the_extra(
    sm: FakeSpoolman,
) -> None:
    holder = _spool(sm)
    sm.link_tag(holder, "04AA")
    sid = _spool(sm, extra={"tag_uids": '["04:aa", "BB22"]'})
    planned = native_fields.migrate_native(sm, location_path=no_locations, apply=False)  # the dry run already knows the holder
    assert [c.action for c in planned.changes if c.field == "spool.tag_uids"] == ["conflict"]
    report = native_fields.migrate_native(sm, location_path=no_locations, apply=True)
    change = next(c for c in report.changes if c.field == "spool.tag_uids")
    assert change.action == "conflict" and "another spool" in change.detail
    assert sm.get("spool", sid)["extra"] == {"tag_uids": encode_extra('["04:aa", "BB22"]')}
    assert next(f for f in report.fields if f.field == "spool.tag_uids").state == "kept"
    # the user unlinks it from the other spool (here: deletes that spool), the re-run finishes
    sm.delete("spool", holder)
    done = native_fields.migrate_native(sm, location_path=no_locations, apply=True)
    assert not done.blocked and {t["uid"] for t in sm.get("spool", sid)["tags"]} == {
        "04AA",
        "BB22",
    }


def test_temperature_midpoint_fills_only_empty_native_values(sm: FakeSpoolman) -> None:
    empty = _filament(
        sm,
        extra={
            "nozzle_temp_min": encode_extra(190),
            "nozzle_temp_max": encode_extra(220),
            "bed_temp_min": encode_extra(50),
            "bed_temp_max": encode_extra(61),
        },
    )
    set_already = _filament(
        sm,
        settings_extruder_temp=200,
        extra={
            "nozzle_temp_min": encode_extra(190),
            "nozzle_temp_max": encode_extra(220),
        },
    )
    half = _filament(sm, extra={"nozzle_temp_min": encode_extra(190)})
    dry = native_fields.migrate_native(sm, location_path=no_locations, apply=False)
    assert [(c.field, c.item_id, c.action) for c in dry.changes] == [
        ("filament.settings_extruder_temp", empty, "fill"),
        ("filament.settings_bed_temp", empty, "fill"),
    ]
    assert "settings_extruder_temp" not in sm.get("filament", empty)
    native_fields.migrate_native(sm, location_path=no_locations, apply=True)
    got = sm.get("filament", empty)
    assert (got["settings_extruder_temp"], got["settings_bed_temp"]) == (205, 56)
    assert sm.get("filament", set_already)["settings_extruder_temp"] == 200
    assert "settings_extruder_temp" not in sm.get("filament", half)
    assert (
        native_fields.migrate_native(sm, location_path=no_locations, apply=True).changes == []
    )  # ranges stay: nothing left to fill


def test_one_failed_write_does_not_abort_the_others_and_keeps_the_definition(
    sm: FakeSpoolman, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = _vendor(sm, "bad", extra=120.0)
    good = _vendor(sm, "good", extra=135.0)
    real_update = sm.update

    def flaky(entity: str, item_id: int, data: Mapping[str, Any]) -> dict[str, Any]:
        if entity == "vendor" and item_id == bad:
            raise SpoolmanError("Spoolman PATCH /vendor/1: HTTP 500: boom", status=500)
        return real_update(entity, item_id, data)  # type: ignore[arg-type]

    monkeypatch.setattr(sm, "update", flaky)
    report = native_fields.migrate_native(sm, location_path=no_locations, apply=True)
    assert _actions(report, "vendor.spool_tare_g") == ["failed", "copy"]
    assert "boom" in report.changes[0].detail
    assert sm.get("vendor", good)["empty_spool_weight"] == 135.0
    assert sm.get("vendor", bad)["extra"] == {"spool_tare_g": encode_extra(120.0)}
    assert "spool_tare_g" in _keys(sm, "vendor") and report.blocked


def test_a_write_the_reply_does_not_confirm_is_failed_not_trusted(
    sm: FakeSpoolman, monkeypatch: pytest.MonkeyPatch
) -> None:
    vid = _vendor(sm, "v", extra=135.0)
    stale = sm.get("vendor", vid)
    real_update = sm.update
    monkeypatch.setattr(
        sm, "update", lambda e, i, d: stale if e == "vendor" else real_update(e, i, d)
    )  # type: ignore[arg-type]
    report = native_fields.migrate_native(sm, location_path=no_locations, apply=True)
    assert _actions(report, "vendor.spool_tare_g") == [
        "failed"
    ] and "spool_tare_g" in _keys(sm, "vendor")


def test_a_failing_field_delete_is_reported_and_blocks(
    sm: FakeSpoolman, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(entity: str, key: str) -> list[dict[str, Any]]:
        raise SpoolmanError("Spoolman DELETE: HTTP 500: no", status=500)

    monkeypatch.setattr(sm, "delete_field", refuse)
    report = native_fields.migrate_native(sm, location_path=no_locations, apply=True)
    assert report.blocked and {f.state for f in report.fields} == {"failed"}


# ------------------------------------------------------------------------------- HTTP API / health


def _auth(tokens: dict[str, str], scope: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens[scope]}"}


def test_routes_dry_run_by_get_and_apply_by_post(
    printops_client: Any, printops_tokens: dict[str, str], fake_spoolman: FakeSpoolman
) -> None:
    for (entity, key), params in LEGACY_PARAMS.items():
        fake_spoolman.set_field(entity, key, params)  # type: ignore[arg-type]
    vid = _vendor(fake_spoolman, "Sunlu", extra=135.0)
    writes = len(fake_spoolman.writes())

    dry = printops_client.get(
        "/v1/fields/migrate-native", headers=_auth(printops_tokens, "read")
    )
    assert (
        dry.status_code == 200
        and dry.json()["apply"] is False
        and dry.json()["changes"][0]["action"] == "copy"
    )
    assert len(fake_spoolman.writes()) == writes

    assert (
        printops_client.post(
            "/v1/fields/migrate-native", headers=_auth(printops_tokens, "read")
        ).status_code
        == 403
    )
    assert (
        printops_client.get(
            "/v1/fields/migrate-native", headers=_auth(printops_tokens, "write")
        ).status_code
        == 403
    )
    assert printops_client.get("/v1/fields/migrate-native").status_code == 401
    assert len(fake_spoolman.writes()) == writes

    done = printops_client.post(
        "/v1/fields/migrate-native", headers=_auth(printops_tokens, "write")
    )
    assert done.status_code == 200 and done.json()["blocked"] is False
    assert fake_spoolman.get("vendor", vid)["empty_spool_weight"] == 135.0
    again = printops_client.post(
        "/v1/fields/migrate-native", json={}, headers=_auth(printops_tokens, "write")
    ).json()
    assert again["changes"] == [] and not again["pending"]


def test_route_answers_502_when_spoolman_fails_and_503_without_spoolman(
    printops_client: Any,
    printops_tokens: dict[str, str],
    fake_spoolman: FakeSpoolman,
    printops_settings: Settings,
) -> None:
    fake_spoolman.down = True
    r = printops_client.get(
        "/v1/fields/migrate-native", headers=_auth(printops_tokens, "read")
    )
    assert r.status_code == 502 and "unreachable" in r.json()["detail"]

    from fastapi.testclient import TestClient

    from printops.api.app import create_app

    with TestClient(create_app(printops_settings)) as bare:
        r = bare.post(
            "/v1/fields/migrate-native", headers=_auth(printops_tokens, "write")
        )
    assert r.status_code == 503


def test_health_lists_legacy_fields_as_a_warning_that_keeps_the_status_ok_until_migrated(
    printops_client: Any, printops_tokens: dict[str, str], fake_spoolman: FakeSpoolman
) -> None:
    """Leftover legacy definitions are information for the operator, not a failure of the stack: the
    status stays "ok" (a deploy waits for "ok"), while `legacy_fields` and `message` name them and the fix."""
    clean = printops_client.get("/v1/health").json()
    assert clean["status"] == "ok" and clean["spoolman"]["legacy_fields"] == []

    for (entity, key), params in LEGACY_PARAMS.items():
        fake_spoolman.set_field(entity, key, params)  # type: ignore[arg-type]
    calls = len(fake_spoolman.calls)
    warned = printops_client.get("/v1/health").json()
    assert (
        len(fake_spoolman.calls) - calls == 4
    )  # info + one field list per entity: no extra request
    assert warned["status"] == "ok" and warned["spoolman"]["ok"] is True
    assert warned["spoolman"]["legacy_fields"] == [
        f"{e}.{k}" for e, k in LEGACY_EXTRA_FIELDS
    ]
    assert "3d printops fields migrate-native" in warned["spoolman"]["message"]
    assert warned["spoolman"]["extra_fields"]["missing"] == []

    printops_client.post(
        "/v1/fields/migrate-native", headers=_auth(printops_tokens, "write")
    )
    healed = printops_client.get("/v1/health").json()
    assert healed["status"] == "ok" and healed["spoolman"]["legacy_fields"] == []
    assert "message" not in healed["spoolman"]


def test_a_stack_with_only_legacy_fields_is_healthy_for_deploy_and_init(live_api: LiveApi) -> None:
    """An upgraded install still has the old field definitions until `fields migrate-native --apply`
    ran against the new 3d-api: `wait_healthy` (deploy, init --env) must not wait 300 s for it, and
    `check_health` must call the stack ok."""
    from printops import deploy, env_setup

    for (entity, key), params in LEGACY_PARAMS.items():
        live_api.spoolman.set_field(entity, key, params)  # type: ignore[arg-type]

    body = deploy.wait_healthy(live_api.url, timeout_s=2, log=lambda _m: None, sleep=lambda _s: None)
    assert body["status"] == "ok" and body["spoolman"]["legacy_fields"]
    health = env_setup.check_health(live_api.url, None)
    assert (health.state, health.kind) == ("ok", "3d-api")


def test_a_degraded_wait_names_what_is_wrong_not_just_ok() -> None:
    """The wait/init message used to read `spoolman ok` when the real problem was a missing field or a
    DB error; the one formatter names the missing fields, the migrate hint, the DB or Spoolman error."""
    from printops import deploy

    missing = {
        "status": "degraded",
        "spoolman": {"ok": False, "extra_fields": {"status": "ok", "missing": ["spool.dry_state"]}},
    }
    assert "spool.dry_state" in deploy.degraded_reason(missing)
    legacy = {"status": "degraded", "spoolman": {"ok": True, "message": "legacy x: run migrate-native"}}
    assert "migrate-native" in deploy.degraded_reason(legacy)
    assert deploy.degraded_reason({"status": "degraded", "spoolman": {"error": "unreachable"}}) == "spoolman: unreachable"
    assert "locked" in deploy.degraded_reason({"status": "degraded", "db": {"ok": False, "error": "locked"}})


# ------------------------------------------- #712: a recorded location id is not lost with its field

LOCATION_ID = "printops_location_id"


def _location_by_id(paths: Mapping[int, str]) -> native_fields.LocationPath:
    def resolve(location_id: int) -> str | None:
        return paths.get(location_id)

    return resolve


def _location_changes(report: native_fields.MigrationReport) -> dict[int, native_fields.Change]:
    return {c.item_id: c for c in report.changes if c.field == f"spool.{LOCATION_ID}"}


def test_empty_location_text_takes_the_path_of_the_recorded_location_id(sm: FakeSpoolman) -> None:
    sid = _spool(sm, extra={LOCATION_ID: 3})
    paths = _location_by_id({3: "WH:A/S1"})

    planned = native_fields.migrate_native(sm, apply=False, location_path=paths)

    change = _location_changes(planned)[sid]
    assert (change.action, "WH:A/S1" in change.detail) == ("copy", True)
    assert not sm.get("spool", sid).get("location")  # a dry run writes nothing
    assert planned.pending

    done = native_fields.migrate_native(sm, apply=True, location_path=paths)

    assert _location_changes(done)[sid].action == "copy" and not done.blocked
    spool = sm.get("spool", sid)
    assert spool["location"] == "WH:A/S1" and LOCATION_ID not in spool["extra"]


def test_location_text_already_there_wins_over_the_recorded_id(sm: FakeSpoolman) -> None:
    sid = _spool(sm, location="WH:A", extra={LOCATION_ID: 3})

    done = native_fields.migrate_native(sm, apply=True, location_path=_location_by_id({3: "WH:A/S1"}))

    assert _location_changes(done)[sid].action == "drop"
    assert sm.get("spool", sid)["location"] == "WH:A"  # Spoolman's text is the truth, never overwritten


def test_an_id_of_no_location_is_dropped_and_says_so(sm: FakeSpoolman) -> None:
    sid = _spool(sm, extra={LOCATION_ID: 99})

    done = native_fields.migrate_native(sm, apply=True, location_path=_location_by_id({}))

    change = _location_changes(done)[sid]
    assert change.action == "drop" and "no PrintOps location" in change.detail
    assert not done.blocked and LOCATION_ID not in sm.get("spool", sid)["extra"]


def test_a_loaded_spool_keeps_its_slot_not_the_stored_location(sm: FakeSpoolman) -> None:
    """The slot is where the spool is now; the id only says where it was stored. The dry run already
    plans it that way (the slot's copy has not happened yet when the id is looked at)."""
    sid = _spool(sm, extra={"printops_slot": "u1/T3", LOCATION_ID: 3})
    paths = _location_by_id({3: "WH:A/S1"})

    planned = native_fields.migrate_native(sm, apply=False, location_path=paths)
    assert _location_changes(planned)[sid].action == "drop"

    native_fields.migrate_native(sm, apply=True, location_path=paths)
    assert sm.get("spool", sid)["location"] == "slot:u1/T3"


def test_a_slot_that_cannot_be_copied_leaves_the_stored_location_to_the_id(sm: FakeSpoolman) -> None:
    sid = _spool(sm, extra={"printops_slot": "x" * 80, LOCATION_ID: 3})  # too long for Spoolman's location
    paths = _location_by_id({3: "WH:A/S1"})

    planned = native_fields.migrate_native(sm, apply=False, location_path=paths)
    assert _location_changes(planned)[sid].action == "copy"

    native_fields.migrate_native(sm, apply=True, location_path=paths)
    assert sm.get("spool", sid)["location"] == "WH:A/S1"


def test_route_migrates_the_recorded_location_id_of_a_spool_without_location_text(
    printops_client: Any,
    printops_tokens: dict[str, str],
    fake_spoolman: FakeSpoolman,
    printops_db: sqlite3.Connection,
) -> None:
    """3d-api knows the PrintOps locations, so `fields migrate-native` (dry run, then apply) restores the
    placement the legacy extra recorded instead of dropping it with the field."""
    from printops import locations

    locations.add(printops_db, "WH:A", "zone", actor="test")
    shelf = locations.add(printops_db, "WH:A/S1", "shelf", actor="test")
    for (entity, key), params in LEGACY_PARAMS.items():
        fake_spoolman.set_field(entity, key, params)  # type: ignore[arg-type]
    placed = _spool(fake_spoolman, extra={LOCATION_ID: shelf.id})

    dry = printops_client.get("/v1/fields/migrate-native", headers=_auth(printops_tokens, "read")).json()
    change = next(c for c in dry["changes"] if c["field"] == f"spool.{LOCATION_ID}")
    assert change["action"] == "copy" and "WH:A/S1" in change["detail"]
    assert not fake_spoolman.get("spool", placed).get("location")

    done = printops_client.post("/v1/fields/migrate-native", headers=_auth(printops_tokens, "write")).json()
    assert done["blocked"] is False
    spool = fake_spoolman.get("spool", placed)
    assert spool["location"] == "WH:A/S1" and LOCATION_ID not in spool["extra"]
