"""Settings-change journal (#264): migration, buffer location + parsing, import with the calibration mapping, REST."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fleet_fixtures import REPO_FLEET
from printops_fakes import FakeSpoolman

from printops import calibrations as cal
from printops import history_buffer as hb
from printops import policy
from printops import settings_history as sh
from printops.calibration_catalog import load_catalog
from printops.client import ApiClient, ApiError
from printops.fleet import load_fleet
from printops.spoolman import encode_extra, ensure_extra_fields

ORANGE, BLACK = "E8732A", "000000"


@pytest.fixture
def repo_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THREED_FLEET", str(REPO_FLEET))
    monkeypatch.delenv("PRINTOPS_POLICY", raising=False)
    monkeypatch.delenv("PRINTOPS_CALIBRATIONS", raising=False)


@pytest.fixture
def ctx(
    repo_files: None, printops_db: sqlite3.Connection, fake_spoolman: FakeSpoolman
) -> cal.Ctx:
    ensure_extra_fields(fake_spoolman)
    fleet = load_fleet()
    assert fleet is not None
    return cal.Ctx(
        conn=printops_db,
        spoolman=fake_spoolman,
        actor="importer",
        catalog=load_catalog(),
        policy=policy.section("calibrations"),
        fleet=fleet,
    )


def add_spool(sm: FakeSpoolman, name: str, color: str, slot: str | None = None) -> int:
    vendor = sm.create("vendor", {"name": f"v-{name}"})
    fil = sm.create(
        "filament",
        {
            "name": name,
            "vendor_id": vendor["id"],
            "material": "PLA",
            "density": 1.24,
            "diameter": 1.75,
            "weight": 1000,
            "color_hex": color,
        },
    )
    extra = {"printops_slot": encode_extra(slot)} if slot else {}
    return int(sm.create("spool", {"filament_id": fil["id"], "extra": extra})["id"])


def entry(
    uid: str,
    kind: str,
    key: str,
    status: str,
    new: Any,
    *,
    old: Any = None,
    head: str | None = "T1",
    at: str = "2026-10-15T20:03:08Z",
    source: str = "photo",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "uid": uid,
        "schema": hb.SCHEMA,
        "printer": "k1se",
        "at": at,
        "recorded_at": at,
        "actor": "cli:ultra (agent test)",
        "head": head,
        "plate": None,
        "nozzle": {"diameter_mm": 0.4},
        "spool_id": None,
        "filament_id": None,
        "lot_nr": None,
        "calibration_id": None,
        "maintenance_id": None,
        "reverts": None,
        "kind": kind,
        "key": key,
        "status": status,
        "source": source,
        "old": old,
        "new": new,
        **extra,
    }


def orange(**kw: Any) -> dict[str, Any]:
    return {"spool": {"filament": "orange PLA", "color_hex": f"#{ORANGE}"}, **kw}


def black(**kw: Any) -> dict[str, Any]:
    return {
        "spool": {"filament": "babushkin-dom black PLA", "color_hex": f"#{BLACK}"},
        **kw,
    }


def buffer(spool_id: int) -> list[dict[str, Any]]:
    """Shaped like the real k1se.jsonl of 2026-10-15/16 (one line per kind of case)."""
    return [
        entry(
            "u-shaper",
            "input_shaper",
            "input_shaper",
            "measured_not_applied",
            head=None,
            at="2026-10-15",
            source="accelerometer",
            old={
                "shaper_type_x": "ei",
                "shaper_freq_x": 70.6,
                "shaper_type_y": "ei",
                "shaper_freq_y": 70.6,
            },
            new={
                "shaper_type_x": "ei",
                "shaper_freq_x": 70.6,
                "shaper_type_y": "ei",
                "shaper_freq_y": 70.6,
            },
        ),
        entry(
            "u-black-pa",
            "pressure_advance",
            "pressure_advance",
            "measured_not_applied",
            at="2026-10-15",
            old={"pressure_advance": 0.04, "origin": "stock"},
            new={"pressure_advance": 0.04},
            method="pa_lines",
            **black(),
        ),
        entry(
            "u-pa",
            "pressure_advance",
            "pressure_advance",
            "applied",
            old={"pressure_advance": 0.04},
            new={"pressure_advance": 0.05},
            source="user_visual_choice",
            method="pa_lines",
            applied_to="repo: snapmaker-orca filament 'Orange PLA @K1SE'",
            evidence=[{"path": "/c/k1se_combo_orange_pla.gcode", "sha256": "3" * 64}],
            **orange(),
        ),
        entry(
            "u-flow",
            "flow_ratio",
            "filament_flow_ratio",
            "applied",
            old={"flow_ratio": 0.98},
            new={"flow_ratio": 0.931},
            source="user_visual_choice",
            method="flow_tiles_m221",
            **orange(),
        ),
        entry(
            "u-readout",
            "calibration_readout",
            "dense plate orange PLA",
            "measured_not_applied",
            head=None,
            at="2026-10-16T01:07:44Z",
            source="user_hand+photo",
            old={"filament_max_volumetric_speed": 18},
            new={"proposed_filament_max_volumetric_speed": 20},
        ),
        entry(
            "u-bind",
            "spool_binding",
            "slot k1se/T1",
            "applied",
            at="2026-10-16T02:45:24Z",
            source="user",
            old={"spool_id": None},
            new={
                "spool_id": spool_id,
                "name": "PLA оранжевый",
                "color_hex": f"#{ORANGE}",
            },
            spool_id=spool_id,
        ),
        entry(
            "u-mvs20",
            "slicer_profile",
            "snapmaker-orca filament: Orange PLA @K1SE / filament_max_volumetric_speed",
            "applied",
            at="2026-10-16T03:19:39Z",
            old={"filament_max_volumetric_speed": 18},
            new={"filament_max_volumetric_speed": 20},
            measured={"mvs_clean_up_to_mm3_s": 24},
            **orange(),
        ),
        entry(
            "u-accel",
            "slicer_profile",
            "snapmaker-orca: K1 SE accelerations / inner_wall_acceleration",
            "applied",
            head=None,
            at="2026-10-16T03:19:39Z",
            old={"inner_wall_acceleration": 6000},
            new={"inner_wall_acceleration": 8000},
            measured={"ringing": "none"},
        ),
        entry(
            "u-mvs22",
            "slicer_profile",
            "snapmaker-orca filament: Orange PLA @K1SE / filament_max_volumetric_speed",
            "applied",
            at="2026-10-16T14:28:52Z",
            old={"filament_max_volumetric_speed": 20},
            new={"filament_max_volumetric_speed": 22},
            measured={"mvs_last_clean_mm3_s": 26},
            **orange(),
        ),
        entry(
            "u-shaper-new",
            "input_shaper",
            "input_shaper",
            "measured_not_applied",
            head=None,
            at="2026-10-15T22:22:02+03:00",
            source="accelerometer",
            old={
                "shaper_type_x": "ei",
                "shaper_freq_x": 69.0,
                "shaper_type_y": "ei",
                "shaper_freq_y": 69.0,
            },
            new={
                "shaper_type_x": "ei",
                "shaper_freq_x": 72.0,
                "shaper_type_y": "ei",
                "shaper_freq_y": 72.0,
            },
        ),
    ]


def lines(entries: list[dict[str, Any]]) -> list[str]:
    return [json.dumps(e, ensure_ascii=False) for e in entries]


def outcomes(rep: sh.ImportReport) -> dict[str, tuple[str, int | None]]:
    return {r.uid: (r.outcome, r.calibration_id) for r in rep.results}


# -- migration and buffer ------------------------------------------------------------------------


def test_settings_change_rows_are_append_only(printops_db: sqlite3.Connection) -> None:
    printops_db.execute(
        "INSERT INTO settings_change (uid, at, recorded_at, printer, kind, key, status, source, actor, entry) "
        "VALUES ('u1', 'a', 'a', 'k1se', 'firmware', 'fw', 'applied', 'manual', 'me', '{}')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        printops_db.execute("UPDATE settings_change SET key = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        printops_db.execute("DELETE FROM settings_change")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        printops_db.execute(
            "INSERT INTO settings_change (uid, at, recorded_at, printer, kind, key, status, source, actor, entry) "
            "VALUES ('u1', 'a', 'a', 'k1se', 'firmware', 'fw', 'applied', 'manual', 'me', '{}')"
        )


def test_instant_normalises_dates_offsets_and_naive_times() -> None:
    assert sh.instant("2026-10-15") == "2026-10-15T00:00:00.000Z"
    assert sh.instant("2026-10-15T22:22:02+03:00") == "2026-10-15T19:22:02.000Z"
    assert sh.instant("2026-10-16T01:10:43+00:00") == "2026-10-16T01:10:43.000Z"
    assert sh.instant("2026-10-16T01:10:43") == "2026-10-16T01:10:43.000Z"
    with pytest.raises(ValueError):
        sh.instant("yesterday")


def test_parse_lines_stops_at_a_torn_line_with_its_number() -> None:
    good = json.dumps({"uid": "a", "schema": hb.SCHEMA})
    assert [ln.n for ln in hb.parse_lines([good, "", good.replace('"a"', '"b"')])] == [
        1,
        3,
    ]
    for bad, message in (
        (good[:20], "not JSON"),
        ("[1]", "not a JSON object"),
        (json.dumps({"uid": "a", "schema": "x/1"}), "schema must be"),
        (json.dumps({"schema": hb.SCHEMA}), "uid must be"),
        (
            '{"uid": "a", "schema": "' + hb.SCHEMA + '", "new": NaN}',
            "NaN is not a JSON number",
        ),
    ):
        with pytest.raises(hb.HistoryBufferError, match=message) as e:
            hb.parse_lines([good, bad])
        assert e.value.line == 2


def test_buffer_dir_moves_the_legacy_buffer_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    new, legacy = (
        tmp_path / "3d-cli" / "printer-history",
        tmp_path / "3d" / "printer-history",
    )
    assert (
        hb.buffer_dir() == new and not new.exists()
    )  # nothing anywhere: no dir is created

    legacy.mkdir(parents=True)
    (legacy / "k1se.jsonl").write_text('{"uid": "a"}\n', encoding="utf-8")
    new.mkdir(parents=True)  # an empty new dir is not a conflict
    assert hb.buffer_files() == (new, [new / "k1se.jsonl"])
    assert (
        not legacy.exists()
        and (new / "k1se.jsonl").read_text(encoding="utf-8") == '{"uid": "a"}\n'
    )
    assert hb.buffer_dir() == new  # idempotent

    legacy.mkdir(parents=True)
    (legacy / "u1.jsonl").write_text('{"uid": "b"}\n', encoding="utf-8")
    with pytest.raises(
        hb.HistoryBufferError, match="two settings-history buffers"
    ) as e:
        hb.buffer_dir()
    fix = "\n".join(e.value.remediation)
    assert (
        f"3d settings import {legacy / 'u1.jsonl'}" in fix
        and f"3d settings import {new / 'k1se.jsonl'}" in fix
    )
    assert (legacy / "u1.jsonl").exists() and (
        new / "k1se.jsonl"
    ).exists()  # nothing moved


# -- import ---------------------------------------------------------------------------------------


def test_import_maps_calibrations_binds_spools_and_is_idempotent(
    ctx: cal.Ctx, fake_spoolman: FakeSpoolman
) -> None:
    orange_spool = add_spool(fake_spoolman, "PLA оранжевый", ORANGE, slot="k1se/T1")
    add_spool(
        fake_spoolman, "оранжевый (Борис)", ORANGE
    )  # same colour, never bound to the K1
    rep = sh.import_lines(ctx, lines(buffer(orange_spool)), file="k1se.jsonl")
    got = outcomes(rep)

    assert got["u-black-pa"][0] == "refused"  # no binding of k1se/T1 with #000000
    black_detail = next(r.detail for r in rep.results if r.uid == "u-black-pa")
    assert (
        "pass --spool u-black-pa=<spool id>" in black_detail
        and "#000000" in black_detail
    )
    for uid in ("u-readout", "u-bind", "u-accel", "u-shaper-new"):
        assert got[uid] == ("imported", None), uid  # journal only
    assert "measured, not applied" in next(
        r.detail for r in rep.results if r.uid == "u-shaper-new"
    )
    shaper_detail = next(r.detail for r in rep.results if r.uid == "u-shaper")
    assert (
        got["u-shaper"][0] == "refused"
    )  # no method, and input_shaper has no `manual`
    assert (
        "pass --method u-shaper=<klipper_shaper_calibrate|k1_auto|u1_auto>"
        in shaper_detail
    )
    for uid in ("u-pa", "u-flow", "u-mvs20", "u-mvs22"):
        assert got[uid][0] == "imported" and got[uid][1] is not None, uid
    assert rep.to_dict()["counts"] == {
        "imported": 8,
        "calibrations": 4,
        "skipped": 0,
        "refused": 2,
    }

    pa = cal.get(ctx.conn, got["u-pa"][1] or 0)
    assert pa.values == {"pressure_advance": 0.05} and pa.method == "manual"
    assert pa.params == {"settings_change_uid": "u-pa", "buffer_method": "pa_lines"}
    assert (
        pa.recorded_at == "2026-10-15T20:03:08.000Z"
        and pa.created_by == "cli:ultra (agent test)"
    )
    assert (
        pa.filament is not None
        and pa.filament.spool_id == orange_spool
        and pa.filament.name == "PLA оранжевый"
    )
    assert (
        pa.nozzle is not None
        and pa.nozzle.install_id is None
        and pa.nozzle.diameter_mm == 0.4
    )
    assert (
        pa.gcode_path == "/c/k1se_combo_orange_pla.gcode" and pa.gcode_sha == "3" * 64
    )
    assert pa.applied_to[0]["target"] == "orca:pressure_advance"
    assert pa.applied_to[0]["before"] == {"pressure_advance": 0.04}
    mvs20, mvs22 = (cal.get(ctx.conn, got[u][1] or 0) for u in ("u-mvs20", "u-mvs22"))
    assert mvs20.kind == "max_volumetric_speed" and mvs20.superseded_by == mvs22.id
    assert (
        mvs22.values == {"max_volumetric_speed": 22.0} and mvs22.superseded_by is None
    )

    row = ctx.conn.execute(
        "SELECT * FROM settings_change WHERE uid = 'u-pa'"
    ).fetchone()
    assert (
        row["calibration_id"] == pa.id
        and row["spool_id"] == orange_spool
        and row["imported_by"] == "importer"
    )
    assert json.loads(row["entry"])["method"] == "pa_lines"

    need = {
        r.kind.id: r
        for r in cal.requirements(ctx, "k1se", head="T1").items
        if r.kind is not None
    }
    assert (
        need["pressure_advance"].status == "current"
        and need["pressure_advance"].record is not None
    )
    assert need["pressure_advance"].record.id == pa.id
    assert (
        need["flow_ratio"].status == "current"
        and need["max_volumetric_speed"].status == "current"
    )
    assert need["input_shaper"].status == "assumed"

    events_before = ctx.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    again = sh.import_lines(ctx, lines(buffer(orange_spool)), file="k1se.jsonl")
    assert again.to_dict()["counts"] == {
        "imported": 0,
        "calibrations": 0,
        "skipped": 8,
        "refused": 2,
    }
    assert (
        ctx.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events_before
    )

    black_spool = add_spool(fake_spoolman, "чёрный PLA", BLACK, slot="u1/T1")
    bad = sh.import_lines(
        ctx, lines(buffer(orange_spool)), methods={"u-shaper": "guess"}
    )
    assert "--method 'guess' is not one of" in next(
        r.detail for r in bad.results if r.uid == "u-shaper"
    )
    third = sh.import_lines(
        ctx,
        lines(buffer(orange_spool)),
        spools={"u-black-pa": black_spool, "u-typo": 1},
        methods={"u-shaper": "klipper_shaper_calibrate"},
    )
    assert third.to_dict()["counts"] == {
        "imported": 2,
        "calibrations": 2,
        "skipped": 8,
        "refused": 0,
    }
    assert third.unused_uids == ["u-typo"]
    black_pa = cal.get(ctx.conn, outcomes(third)["u-black-pa"][1] or 0)
    assert (
        black_pa.values == {"pressure_advance": 0.04} and black_pa.filament is not None
    )
    assert black_pa.filament.spool_id == black_spool and black_pa.applied_to == []
    assert cal.get(ctx.conn, pa.id).superseded_by is None  # another filament
    shaper = cal.get(ctx.conn, outcomes(third)["u-shaper"][1] or 0)
    assert (
        shaper.printer == "k1se"
        and shaper.head is None
        and shaper.method == "klipper_shaper_calibrate"
    )
    assert shaper.recorded_at == "2026-10-15T00:00:00.000Z"
    shaper_need = next(
        r
        for r in cal.requirements(ctx, "k1se", head="T1").items
        if r.kind is not None and r.kind.id == "input_shaper"
    )
    assert shaper_need.status == "current"


def test_import_dry_run_writes_nothing(
    ctx: cal.Ctx, fake_spoolman: FakeSpoolman
) -> None:
    s = add_spool(fake_spoolman, "PLA оранжевый", ORANGE, slot="k1se/T1")
    rep = sh.import_lines(ctx, lines(buffer(s)), dry_run=True)
    assert rep.dry_run and rep.to_dict()["counts"]["calibrations"] == 4
    for table in ("settings_change", "calibration", "events"):
        assert ctx.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, (
            table
        )


def test_import_refuses_conflicts_and_bad_lines(
    ctx: cal.Ctx, fake_spoolman: FakeSpoolman
) -> None:
    s = add_spool(fake_spoolman, "PLA оранжевый", ORANGE, slot="k1se/T1")
    base = buffer(s)
    with pytest.raises(sh.HistoryError, match=r"k1se.jsonl: line 3: not JSON") as e:
        sh.import_lines(
            ctx, [*lines(base[:2]), lines(base[2:3])[0][:40]], file="k1se.jsonl"
        )
    assert e.value.status == 422 and e.value.detail == {"line": 3}
    assert ctx.conn.execute("SELECT COUNT(*) FROM settings_change").fetchone()[0] == 0

    sh.import_lines(ctx, lines([base[5]]))  # the binding
    changed = {**base[5], "key": "slot k1se/T1 (edited)"}
    weird = [
        changed,
        entry("u-printer", "firmware", "fw", "applied", {}) | {"printer": "x1c"},
        entry("u-head", "firmware", "fw", "applied", {}, head="T9"),
        entry("u-status", "firmware", "fw", "done", {}),
        entry(
            "u-range",
            "flow_ratio",
            "filament_flow_ratio",
            "applied",
            {"flow_ratio": 3.0},
            **orange(),
        ),
        entry("u-reverts", "firmware", "fw", "rolled_back", {}) | {"reverts": "u-nope"},
        entry("u-dup", "firmware", "fw", "applied", {"v": 1}),
        entry("u-dup", "firmware", "fw", "applied", {"v": 2}),
    ]
    rep = sh.import_lines(ctx, lines(weird))
    detail = {r.uid + str(r.line): (r.outcome, r.detail) for r in rep.results}
    assert detail["u-bind1"] == (
        "refused",
        "uid already imported with a different entry: one uid, two changes",
    )
    assert (
        detail["u-printer2"][0] == "refused"
        and "unknown printer 'x1c'" in detail["u-printer2"][1]
    )
    assert detail["u-head3"] == ("refused", "k1se has no head 'T9'")
    assert (
        detail["u-status4"][0] == "refused"
        and "status must be one of" in detail["u-status4"][1]
    )
    assert (
        detail["u-range5"][0] == "refused"
        and "outside 0.5..1.5" in detail["u-range5"][1]
    )
    assert detail["u-reverts6"] == ("refused", "reverts u-nope, which is not imported")
    assert detail["u-dup7"][0] == "imported"
    assert detail["u-dup8"] == (
        "refused",
        "uid of an earlier line with a different entry",
    )


def test_a_refused_spool_binding_never_binds_a_calibration(
    ctx: cal.Ctx, fake_spoolman: FakeSpoolman
) -> None:
    s = add_spool(fake_spoolman, "PLA оранжевый", ORANGE, slot="k1se/T1")
    binding = {
        k: v for k, v in buffer(s)[5].items() if k != "actor"
    }  # refused: no actor
    got = outcomes(sh.import_lines(ctx, lines([binding, buffer(s)[2]])))
    assert got == {"u-bind": ("refused", None), "u-pa": ("refused", None)}
    assert ctx.conn.execute("SELECT COUNT(*) FROM calibration").fetchone()[0] == 0

    sh.import_lines(
        ctx, lines([buffer(s)[5]])
    )  # the binding alone, valid, in an earlier import
    assert (
        outcomes(sh.import_lines(ctx, lines([buffer(s)[2]])))["u-pa"][0] == "imported"
    )


def test_a_concurrent_import_of_the_same_uid_is_skipped_or_refused_by_content(
    ctx: cal.Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another import commits the uid between this import's plan and its transaction."""
    mine = entry("u-race", "firmware", "fw", "applied", {"v": 1})
    real_now = sh.db.utc_now

    def race(theirs: dict[str, Any]) -> Any:
        fired: list[bool] = []

        def now() -> str:
            if not fired:
                fired.append(True)
                other = sh.db.open_db(
                    ctx.conn.execute("PRAGMA database_list").fetchone()["file"]
                )
                assert (
                    sh.import_lines(
                        dataclasses.replace(ctx, conn=other), lines([theirs])
                    )
                    .results[0]
                    .outcome
                    == "imported"
                )
                other.close()
            return real_now()

        return now

    monkeypatch.setattr(sh.db, "utc_now", race(mine))
    same = sh.import_lines(ctx, lines([mine]))
    assert (same.results[0].outcome, same.results[0].detail) == (
        "skipped",
        "already imported (by a concurrent import)",
    )

    clash = {**mine, "uid": "u-race2"}
    monkeypatch.setattr(sh.db, "utc_now", race({**clash, "new": {"v": 2}}))
    res = sh.import_lines(ctx, lines([clash])).results[0]
    assert (
        res.outcome == "refused"
        and "concurrent import with a different entry" in res.detail
    )
    assert ctx.conn.execute("SELECT COUNT(*) FROM settings_change").fetchone()[0] == 2


def test_actor_source_are_trimmed_and_head_is_the_fleet_label(ctx: cal.Ctx) -> None:
    line = entry("u-t", "firmware", "fw", "applied", {"v": 1}, head="t1") | {
        "actor": "  me \n",
        "source": " manual ",
    }
    sh.import_lines(ctx, lines([line]))
    row = ctx.conn.execute(
        "SELECT actor, source, head FROM settings_change WHERE uid = 'u-t'"
    ).fetchone()
    assert (row["actor"], row["source"], row["head"]) == ("me", "manual", "T1")


def test_a_buffer_longer_than_the_sqlite_parameter_cap_imports(ctx: cal.Ctx) -> None:
    many = [entry(f"u-{i}", "firmware", "fw", "applied", {"v": i}) for i in range(1200)]
    assert sh.import_lines(ctx, lines(many)).to_dict()["counts"]["imported"] == 1200
    assert sh.import_lines(ctx, lines(many)).to_dict()["counts"]["skipped"] == 1200


def test_an_old_buffer_never_displaces_a_newer_calibration(
    ctx: cal.Ctx, fake_spoolman: FakeSpoolman
) -> None:
    s = add_spool(fake_spoolman, "PLA оранжевый", ORANGE, slot="k1se/T1")
    newer = cal.record(
        ctx,
        cal.RunRequest(
            kind="pressure_advance", method="manual", printer="k1se", head="T1"
        ),
        {"values": {"pressure_advance": 0.045}},
    )  # recorded now, after the buffer's 2026-10-15
    rep = sh.import_lines(ctx, lines([buffer(s)[5], buffer(s)[2]]))
    old = cal.get(ctx.conn, outcomes(rep)["u-pa"][1] or 0)
    assert (
        old.superseded_by == newer.id
        and cal.get(ctx.conn, newer.id).superseded_by is None
    )
    pa = next(
        r
        for r in cal.requirements(ctx, "k1se", head="T1").items
        if r.kind and r.kind.id == "pressure_advance"
    )
    assert pa.record is not None and pa.record.id == newer.id


def test_invalidation_after_the_measurement_makes_an_imported_record_stale(
    ctx: cal.Ctx, fake_spoolman: FakeSpoolman
) -> None:
    s = add_spool(fake_spoolman, "PLA оранжевый", ORANGE, slot="k1se/T1")
    from printops import events

    events.emit(
        ctx.conn,
        "calibration.invalidated",
        "k1se/T1",
        {
            "printer": "k1se",
            "head": "T1",
            "trigger": "nozzle_swap",
            "kinds": None,
            "reason": "swap",
            "maintenance_id": None,
            "nozzle_install_id": None,
        },
        "maintenance",
    )
    sh.import_lines(ctx, lines([buffer(s)[5], buffer(s)[2]]))
    pa = next(
        r
        for r in cal.requirements(ctx, "k1se", head="T1").items
        if r.kind and r.kind.id == "pressure_advance"
    )
    assert (
        pa.status == "invalidated"
    )  # the swap (now) is after the 2026-10-15 measurement


# -- REST -----------------------------------------------------------------------------------------


def test_rest_import_history_and_scopes(
    repo_files: None,
    api_client: ApiClient,
    fake_spoolman: FakeSpoolman,
    printops_client: Any,
    printops_tokens: dict[str, str],
) -> None:
    s = add_spool(fake_spoolman, "PLA оранжевый", ORANGE, slot="k1se/T1")
    body = {"file": "k1se.jsonl", "lines": lines(buffer(s))}
    dry = api_client.post("/v1/settings/import", {**body, "dry_run": True})
    assert dry["dry_run"] and dry["counts"] == {
        "imported": 8,
        "calibrations": 4,
        "skipped": 0,
        "refused": 2,
    }
    assert api_client.get("/v1/settings/history", printer="k1se")["changes"] == []
    done = api_client.post("/v1/settings/import", body)
    assert done["counts"]["calibrations"] == 4 and done["file"] == "k1se.jsonl"
    hist = api_client.get("/v1/settings/history", printer="k1se")["changes"]
    assert [c["uid"] for c in hist][:3] == ["u-mvs22", "u-accel", "u-mvs20"]
    assert (
        hist[0]["new"] == {"filament_max_volumetric_speed": 22}
        and hist[0]["calibration_id"] is not None
    )
    pa_only = api_client.get(
        "/v1/settings/history", printer="k1se", kind="pressure_advance"
    )["changes"]
    assert [c["uid"] for c in pa_only] == ["u-pa"]
    assert [
        c["uid"]
        for c in api_client.get(
            "/v1/settings/history", printer="k1se", since="2026-10-16T14:00Z"
        )["changes"]
    ] == ["u-mvs22"]
    need = {
        i["kind"]: i
        for i in api_client.get(
            "/v1/calibrations/required", printer="k1se", head="T1", all="true"
        )["items"]
    }
    assert (
        need["pressure_advance"]["status"] == "current"
        and need["flow_ratio"]["status"] == "current"
    )

    def status(method: str, path: str, token: str, **kw: Any) -> int:
        r = printops_client.request(
            method,
            path,
            headers={"Authorization": f"Bearer {printops_tokens[token]}"},
            **kw,
        )
        return int(r.status_code)

    assert status("POST", "/v1/settings/import", "read", json=body) == 403
    assert status("GET", "/v1/settings/history", "write") == 403
    for bad in (
        {"lines": "x"},
        {"lines": [], "spools": {"u": "2"}},
        {"lines": [], "extra": 1},
        {"lines": [], "methods": {"u": 1}},
        {"lines": [], "dry_run": "yes"},
    ):
        assert status("POST", "/v1/settings/import", "rw", json=bad) == 422, bad
    with pytest.raises(ApiError) as e:
        api_client.post("/v1/settings/import", {"lines": ["{"]})
    assert e.value.status == 422 and "line 1" in e.value.message
    with pytest.raises(ApiError) as e:
        api_client.get("/v1/settings/history", since="soon")
    assert e.value.status == 422


def test_a_respool_with_the_same_colour_binds_by_time(ctx: cal.Ctx, fake_spoolman: FakeSpoolman) -> None:
    first = add_spool(fake_spoolman, "PLA оранжевый", ORANGE)
    second = add_spool(fake_spoolman, "PLA оранжевый 2", ORANGE, slot="k1se/T1")
    bind = buffer(first)[5]
    rebind = {**bind, "uid": "u-bind2", "at": "2026-10-20T10:00:00Z", "recorded_at": "2026-10-20T10:00:00Z",
              "new": {**bind["new"], "spool_id": second}}
    late_pa = {**buffer(first)[2], "uid": "u-pa-late", "at": "2026-10-21T10:00:00Z",
               "recorded_at": "2026-10-21T10:00:00Z"}
    got = outcomes(sh.import_lines(ctx, lines([bind, rebind, buffer(first)[2], late_pa])))
    assert cal.get(ctx.conn, got["u-pa"][1] or 0).filament.spool_id == first  # type: ignore[union-attr]
    assert cal.get(ctx.conn, got["u-pa-late"][1] or 0).filament.spool_id == second  # type: ignore[union-attr]


def test_a_line_without_a_nozzle_records_an_unknown_nozzle(ctx: cal.Ctx, fake_spoolman: FakeSpoolman) -> None:
    s = add_spool(fake_spoolman, "PLA оранжевый", ORANGE, slot="k1se/T1")
    pa = {**buffer(s)[2], "nozzle": None}
    got = outcomes(sh.import_lines(ctx, lines([buffer(s)[5], pa])))
    assert cal.get(ctx.conn, got["u-pa"][1] or 0).nozzle is None
