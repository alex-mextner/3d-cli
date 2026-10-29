"""The records `3d mix record` writes, against a test 3d-api: POST /v1/calibrations, the visible
GET /v1/calibrations, idempotency, and the settings-history import of the offline buffer (#417)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fleet_fixtures import REPO_FLEET
from mix_fit_fixtures import simulated_readout
from printops_fakes import FakeSpoolman
from test_printops_settings_history import add_spool

from printops import calibrations as cal
from printops import policy
from printops import settings_history as sh
from printops.calibration_catalog import load_catalog
from printops.client import ApiClient
from printops.fleet import load_fleet
from printops.spoolman import ensure_extra_fields
from slicing import mix_record

SHA = "f" * 64
AT = "2026-10-24T12:00:00Z"


@pytest.fixture(autouse=True)
def repo_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THREED_FLEET", str(REPO_FLEET))
    monkeypatch.delenv("PRINTOPS_POLICY", raising=False)
    monkeypatch.delenv("PRINTOPS_CALIBRATIONS", raising=False)


def _spools(sm: FakeSpoolman) -> tuple[int, int, int]:
    ids = []
    for name, color in (("black", "000000"), ("green", "1B5E20"), ("white", "FFFFFF")):
        spool = add_spool(sm, f"PLA {name}", color)
        ids.append(int(sm.get("spool", spool)["filament"]["id"]))
    return ids[0], ids[1], ids[2]


def _plan(tmp_path: Path, sm: FakeSpoolman) -> mix_record.Plan:
    readout = simulated_readout(tmp_path, filament_ids=_spools(sm))
    return mix_record.plan(
        readout, SHA, "u1", readout_name="wall.readout.json", at=AT, actor="cli:test"
    )


def test_the_records_land_in_3d_api_and_get_calibrations_shows_them(
    api_client: ApiClient, fake_spoolman: FakeSpoolman, tmp_path: Path
) -> None:
    pl = _plan(tmp_path, fake_spoolman)
    made = [api_client.post("/v1/calibrations", item.body) for item in pl.items]
    assert [r["kind"] for r in made] == ["filament_color"] * 3 + ["mix_color"] * 4
    assert all(
        r["status"] == "recorded" and r["method"] == "mix_tester_photo" for r in made
    )

    fc = api_client.get(
        "/v1/calibrations", kind="filament_color", current="true", limit=100
    )["calibrations"]
    assert {r["filament"]["name"] for r in fc} == {
        "PLA black",
        "PLA green",
        "PLA white",
    }
    white = next(r for r in fc if r["filament"]["name"] == "PLA white")
    assert white["values"]["hex"] == pl.items[2].body["values"]["hex"].upper()
    assert white["values"]["spoolman_hex"] == "#FFFFFF"
    assert (
        white["params"]["readout_sha256"] == SHA
        and white["evidence"] == pl.items[2].body["evidence"]
    )

    mc = api_client.get(
        "/v1/calibrations", kind="mix_color", printer="u1", current="true", limit=100
    )["calibrations"]
    assert (
        len(mc) == 4
        and {r["printer"] for r in mc} == {"u1"}
        and all(r["mix"].startswith("h0.12:f") for r in mc)
    )
    one = mc[0]
    assert one["values"].keys() == {"hex", "predicted_hex", "delta_e_predicted"}
    assert one["params"]["swatch"] in {i.number for i in pl.items[3:]}
    # filtered by the mix key, any rotation of the cycle finds it
    by_key = api_client.get("/v1/calibrations", kind="mix_color", mix=one["mix"])[
        "calibrations"
    ]
    assert [r["id"] for r in by_key] == [one["id"]]


def test_repeating_the_same_readout_is_idempotent(
    api_client: ApiClient, fake_spoolman: FakeSpoolman, tmp_path: Path
) -> None:
    pl = _plan(tmp_path, fake_spoolman)

    def write() -> int:
        stored = {
            k: api_client.get("/v1/calibrations", kind=k, limit=1000)["calibrations"]
            for k in ("filament_color", "mix_color")
        }
        n = 0
        for item in pl.items:
            if mix_record.existing(item, stored[item.kind], SHA) is None:
                api_client.post("/v1/calibrations", item.body)
                n += 1
        return n

    assert write() == 7
    assert write() == 0
    allrows = api_client.get("/v1/calibrations", limit=1000)["calibrations"]
    assert (
        len([r for r in allrows if r["kind"] in ("filament_color", "mix_color")]) == 7
    )
    assert not [
        r for r in allrows if r["superseded_by"]
    ]  # a re-write would have superseded


def test_a_later_readout_supersedes_the_earlier_record_of_the_same_scope(
    api_client: ApiClient, fake_spoolman: FakeSpoolman, tmp_path: Path
) -> None:
    ids = _spools(fake_spoolman)
    first = mix_record.plan(
        simulated_readout(tmp_path / "a", filament_ids=ids),
        "1" * 64,
        "u1",
        readout_name="a",
        at=AT,
    )
    second = mix_record.plan(
        simulated_readout(tmp_path / "b", filament_ids=ids),
        "2" * 64,
        "u1",
        readout_name="b",
        at=AT,
    )
    for item in first.items + second.items:
        api_client.post("/v1/calibrations", item.body)
    current = api_client.get("/v1/calibrations", kind="mix_color", current="true")[
        "calibrations"
    ]
    assert len(current) == 4 and {r["params"]["readout_sha256"] for r in current} == {
        "2" * 64
    }


def test_the_api_refuses_a_filament_it_does_not_know_with_the_reason(
    api_client: ApiClient, fake_spoolman: FakeSpoolman, tmp_path: Path
) -> None:
    from printops.client import ApiError

    readout = simulated_readout(tmp_path, filament_ids=(901, 902, 903))
    pl = mix_record.plan(readout, SHA, "u1", readout_name="x", at=AT)
    with pytest.raises(ApiError) as e:
        api_client.post("/v1/calibrations", pl.items[0].body)
    assert e.value.status in (404, 422, 502)


@pytest.fixture
def ctx(printops_db: sqlite3.Connection, fake_spoolman: FakeSpoolman) -> cal.Ctx:
    ensure_extra_fields(fake_spoolman)
    fleet = load_fleet()
    assert fleet is not None
    return cal.Ctx(
        conn=printops_db,
        spoolman=fake_spoolman,
        actor="test",
        catalog=load_catalog(),
        policy=policy.section("calibrations"),
        fleet=fleet,
    )


def test_the_buffer_lines_import_as_the_same_records_and_a_second_import_adds_nothing(
    ctx: cal.Ctx, fake_spoolman: FakeSpoolman, tmp_path: Path
) -> None:
    pl = _plan(tmp_path, fake_spoolman)
    lines = [json.dumps(i.line) for i in pl.items]
    first = sh.import_lines(ctx, lines)
    assert [r.outcome for r in first.results] == ["imported"] * 7, [
        r.detail for r in first.results
    ]
    rows: list[Any] = cal.list_calibrations(ctx.conn, limit=100)
    assert sorted(c.kind for c in rows) == ["filament_color"] * 3 + ["mix_color"] * 4
    mixes = [c for c in rows if c.kind == "mix_color"]
    assert all(
        c.mix and c.printer == "u1" and c.method == "mix_tester_photo" for c in mixes
    )
    assert all(
        c.params["readout_sha256"] == SHA and c.params["settings_change_uid"]
        for c in rows
    )
    assert {c.mix for c in mixes} == {i.body["mix"] for i in pl.items[3:]}
    second = sh.import_lines(ctx, lines)
    assert [r.outcome for r in second.results] == ["skipped"] * 7
    assert len(cal.list_calibrations(ctx.conn, limit=100)) == 7
