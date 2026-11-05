"""Behavior tests: every filament carries a max print speed (extra `max_speed_mm_s`, mm/s): 300, PLA+
600. Filaments PrintOps creates get it, `fill_defaults` / `/v1/fields/fill-defaults` set it on
existing filaments only where it is empty, and a value typed by a person always survives."""

from __future__ import annotations

from typing import Any

import pytest
from printops_fakes import FakeSpoolman

from printops import filament_defaults
from printops.client import ApiClient
from printops.spoolman import decode_extra, encode_extra, ensure_extra_fields, missing_extra_fields

FIELD = filament_defaults.FIELD


@pytest.fixture
def sm() -> FakeSpoolman:
    fake = FakeSpoolman()
    ensure_extra_fields(fake)
    return fake


def _fil(sm: FakeSpoolman, material: str, name: str, **extra: Any) -> int:
    vendor = sm.find("vendor", {"name": "Elegoo"}) or [sm.create("vendor", {"name": "Elegoo"})]
    body = {"name": name, "vendor_id": vendor[0]["id"], "material": material, "density": 1.24, "diameter": 1.75,
            "weight": 1000, "extra": {k: encode_extra(v) for k, v in extra.items()}}
    return int(sm.create("filament", body)["id"])


def _speed(sm: FakeSpoolman, fid: int) -> Any:
    raw = sm.get("filament", fid)["extra"].get(FIELD)
    return None if raw is None else decode_extra(raw)


def test_field_is_registered_and_distinct_from_the_volumetric_one(sm: FakeSpoolman) -> None:
    fields = {f["key"]: f for f in sm.list_fields("filament")}
    assert (fields[FIELD]["field_type"], fields[FIELD]["unit"]) == ("integer", "mm/s")
    assert fields["max_volumetric_mm3_s"]["unit"] == "mm³/s"
    assert missing_extra_fields(sm) == []


@pytest.mark.parametrize(
    ("material", "name", "speed"),
    [
        ("PLA+", "Green", 600),
        (" pla + ", "Green", 600),  # case and spaces do not matter
        ("PLA", "Elegoo RAPID PLA+", 600),  # by name
        ("PLA", "Basic PLA + Silk", 600),
        ("PLA", "Galaxy Black", 300),
        ("PETG", "Grey", 300),
        ("PLA-CF", "Carbon", 300),
        ("Mystery", "Thing", 300),
        ("", "", 300),
    ],
)
def test_the_rule_table(material: str, name: str, speed: int) -> None:
    assert filament_defaults.max_speed_default(material, name) == speed


def test_dry_run_shows_the_plan_and_writes_nothing(sm: FakeSpoolman) -> None:
    plus, pla, petg = _fil(sm, "PLA+", "A"), _fil(sm, "PLA", "B"), _fil(sm, "PETG", "C")
    writes = sm.writes()
    report = filament_defaults.fill_defaults(sm, apply=False)
    assert sm.writes() == writes
    assert [(r.filament_id, r.old, r.new, r.action) for r in report.rows] == [
        (plus, None, 600, "set"), (pla, None, 300, "set"), (petg, None, 300, "set")]
    assert report.pending and not report.failed
    assert all(_speed(sm, f) is None for f in (plus, pla, petg))


def test_apply_sets_defaults_keeps_user_values_and_second_run_is_a_noop(sm: FakeSpoolman) -> None:
    plus = _fil(sm, "PLA+", "A")
    by_name = _fil(sm, "PLA", "RAPID PLA+ Green")
    pla, petg, odd = _fil(sm, "PLA", "B"), _fil(sm, "PETG", "C"), _fil(sm, "Mystery", "D")
    typed = _fil(sm, "PLA+", "E", **{FIELD: 450})  # a person chose a different number
    low = _fil(sm, "PETG", "F", **{FIELD: 120})

    first = filament_defaults.fill_defaults(sm, apply=True)

    assert {f: _speed(sm, f) for f in (plus, by_name, pla, petg, odd, typed, low)} == {
        plus: 600, by_name: 600, pla: 300, petg: 300, odd: 300, typed: 450, low: 120}
    assert first.to_dict()["summary"] == {"set": 5, "keep": 2, "failed": 0}
    assert not first.failed and not first.pending
    writes = sm.writes()
    again = filament_defaults.fill_defaults(sm, apply=True)
    assert sm.writes() == writes  # idempotent: not one write
    assert again.to_dict()["summary"] == {"set": 0, "keep": 7, "failed": 0}


def test_a_value_that_is_not_json_is_kept_not_overwritten(sm: FakeSpoolman) -> None:
    fid = _fil(sm, "PLA", "X")
    sm.items["filament"][fid]["extra"][FIELD] = "fast"  # typed by hand
    row = filament_defaults.fill_defaults(sm, apply=True).rows[0]
    assert row.action == "keep" and sm.get("filament", fid)["extra"][FIELD] == "fast"


def test_a_failed_write_is_reported_and_the_others_still_run(sm: FakeSpoolman, monkeypatch: pytest.MonkeyPatch) -> None:
    from printops.spoolman import SpoolmanError

    bad, good = _fil(sm, "PLA", "bad"), _fil(sm, "PLA", "good")
    real = sm.update

    def flaky(entity: str, item_id: int, data: Any) -> dict[str, Any]:
        if item_id == bad:
            raise SpoolmanError("HTTP 500: boom", status=500)
        return real(entity, item_id, data)  # type: ignore[arg-type]

    monkeypatch.setattr(sm, "update", flaky)
    report = filament_defaults.fill_defaults(sm, apply=True)
    assert [r.action for r in report.rows] == ["failed", "set"] and report.failed
    assert _speed(sm, good) == 300 and _speed(sm, bad) is None


# ------------------------------------------------------------------------------ receive / routes

GREEN = {"vendor": "Elegoo", "material": "PLA+", "name": "RAPID PLA+ Green", "color_hex": "#4FE44D", "net_g": 1000}


def test_receive_creates_the_filament_with_the_default(api_client: ApiClient, fake_spoolman: FakeSpoolman) -> None:
    got = api_client.post("/v1/spools", GREEN)
    assert _speed(fake_spoolman, got["filament_id"]) == 600
    petg = api_client.post("/v1/spools", {**GREEN, "material": "PETG", "name": "Grey", "color_hex": "#888888"})
    assert _speed(fake_spoolman, petg["filament_id"]) == 300


def test_a_given_value_wins_and_an_existing_filament_is_not_changed(
    api_client: ApiClient, fake_spoolman: FakeSpoolman
) -> None:
    given = api_client.post("/v1/spools", {**GREEN, "max_speed_mm_s": 450})
    assert _speed(fake_spoolman, given["filament_id"]) == 450
    fake_spoolman.update("filament", given["filament_id"], {"extra": {FIELD: encode_extra(333)}})
    again = api_client.post("/v1/spools", GREEN)  # matches the existing filament
    assert again["filament_id"] == given["filament_id"] and _speed(fake_spoolman, again["filament_id"]) == 333


def test_receive_refuses_a_non_positive_speed(api_client: ApiClient) -> None:
    from printops.client import ApiError

    with pytest.raises(ApiError) as e:
        api_client.post("/v1/spools", {**GREEN, "name": "Z", "max_speed_mm_s": 0})
    assert e.value.status == 422 and "max_speed_mm_s" in e.value.message
    with pytest.raises(ApiError) as e2:
        api_client.post("/v1/spools", {**GREEN, "name": "Z", "max_speed_mm_s": "fast"})
    assert e2.value.status == 422


def test_routes_dry_run_by_get_apply_by_post_and_scopes(
    printops_client: Any, printops_tokens: dict[str, str], fake_spoolman: FakeSpoolman
) -> None:
    fid = _fil(fake_spoolman, "PLA+", "A")
    read = {"Authorization": f"Bearer {printops_tokens['read']}"}
    write = {"Authorization": f"Bearer {printops_tokens['write']}"}
    writes = fake_spoolman.writes()
    dry = printops_client.get("/v1/fields/fill-defaults", headers=read)
    assert dry.status_code == 200 and dry.json()["rows"][0]["new"] == 600 and dry.json()["pending"] is True
    assert fake_spoolman.writes() == writes
    assert printops_client.post("/v1/fields/fill-defaults", headers=read).status_code == 403
    assert printops_client.get("/v1/fields/fill-defaults").status_code == 401
    done = printops_client.post("/v1/fields/fill-defaults", headers=write).json()
    assert done["summary"]["set"] == 1 and _speed(fake_spoolman, fid) == 600
    n = len(fake_spoolman.writes())
    assert printops_client.post("/v1/fields/fill-defaults", headers=write).json()["summary"]["set"] == 0
    assert len(fake_spoolman.writes()) == n
    fake_spoolman.down = True
    assert printops_client.get("/v1/fields/fill-defaults", headers=read).status_code == 502


def test_health_is_unaffected_by_the_new_field(printops_client: Any) -> None:
    body = printops_client.get("/v1/health").json()
    assert body["status"] == "ok" and body["spoolman"]["legacy_fields"] == []
    assert body["spoolman"]["extra_fields"]["missing"] == []
