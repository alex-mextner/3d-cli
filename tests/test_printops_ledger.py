"""Unit tests: the spool ledger (intervals, anchors, atomic Spoolman /use), the tare DB/chain,
the plates.json importer and the spool data-lint rules (issues #160, #163)."""

from __future__ import annotations

import datetime
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from printops_fakes import FakeSpoolman

from printops import datalint, db, ledger, lint_spools, tares
from printops.importers import ImportFormatError, parse_bought, plates_owned
from printops.spoolman import SpoolmanError, ensure_extra_fields, encode_extra


@pytest.fixture
def sm() -> FakeSpoolman:
    fake = FakeSpoolman()
    ensure_extra_fields(fake)
    return fake


def _spool(
    sm: FakeSpoolman,
    *,
    vendor: dict[str, Any] | None = None,
    weight: float | None = 1000.0,
    **spool: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "Galaxy Black",
        "material": "PLA",
        "density": 1.24,
        "diameter": 1.75,
    }
    if weight is not None:
        body["weight"] = weight
    if vendor is not None:
        body["vendor_id"] = sm.create("vendor", vendor)["id"]
    fil = sm.create("filament", body)
    return sm.create("spool", {"filament_id": fil["id"], **spool})


def _anchor(
    conn: sqlite3.Connection,
    spool_id: int,
    grams: float,
    source: str = "weigh",
    sigma: float = 2.0,
) -> None:
    with db.transaction(conn):
        ledger.anchor(conn, spool_id, grams, source, sigma, actor="test")


# -- ledger --------------------------------------------------------------------------------------


def test_interval_grows_with_each_estimate_and_a_weighing_resets_it(
    printops_db: sqlite3.Connection, sm: FakeSpoolman
) -> None:
    sid = _spool(sm)["id"]
    _anchor(printops_db, sid, 1000.0, "receive", 20.0)
    ledger.book(printops_db, sm, sid, 100.0, "gcode_estimate", actor="test")  # ±10
    ledger.book(printops_db, sm, sid, 50.0, "voice", actor="test")  # ±10 (20 %)
    est = ledger.estimate(printops_db, sid)
    assert est.mid == pytest.approx(850.0)
    assert est.sigma == pytest.approx((20**2 + 10**2 + 10**2) ** 0.5)
    assert est.text() == "850 ± 24 g (826–874)" and est.deltas == 2

    _anchor(printops_db, sid, 830.0, "weigh", 2.2)
    after = ledger.estimate(printops_db, sid)
    assert (after.mid, after.sigma, after.deltas, after.anchor_source) == (
        830.0,
        2.2,
        0,
        "weigh",
    )
    assert after.last_weighed_at is not None
    jump = printops_db.execute(
        "SELECT delta_g FROM ledger WHERE source = 'weigh'"
    ).fetchone()["delta_g"]
    assert jump == pytest.approx(-20.0)  # the drift the estimates had accumulated


def test_book_writes_spoolman_use_and_caches_sigma(
    printops_db: sqlite3.Connection, sm: FakeSpoolman
) -> None:
    sid = _spool(sm)["id"]
    _anchor(printops_db, sid, 1000.0, "receive", 20.0)
    row, spool = ledger.book(
        printops_db,
        sm,
        sid,
        12.5,
        "moonraker_live",
        0.5,
        job_id="j1",
        note="plate 3",
        actor="cli",
    )
    assert (row.delta_g, row.confidence_g, row.job_id, row.actor) == (
        -12.5,
        0.5,
        "j1",
        "cli",
    )
    assert spool["remaining_weight"] == pytest.approx(987.5)
    assert json.loads(spool["extra"]["remaining_confidence_g"]) == pytest.approx(
        20.0, abs=0.1
    )
    kinds = [r["kind"] for r in printops_db.execute("SELECT kind FROM events")]
    assert kinds == ["spool.used"]


def test_book_rolls_back_when_spoolman_fails(
    printops_db: sqlite3.Connection, sm: FakeSpoolman
) -> None:
    sid = _spool(sm)["id"]
    sm.down = True
    with pytest.raises(SpoolmanError):
        ledger.book(printops_db, sm, sid, 10.0, "manual", actor="test")
    assert printops_db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0
    assert printops_db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("grams", "source", "conf", "msg"),
    [
        (10.0, "weigh", None, "usage source"),
        (0.0, "manual", None, "non-zero"),
        (float("nan"), "manual", None, "non-zero"),
        (5.0, "manual", -1.0, "confidence_g"),
    ],
)
def test_book_rejects_bad_input(
    printops_db: sqlite3.Connection,
    sm: FakeSpoolman,
    grams: float,
    source: str,
    conf: float | None,
    msg: str,
) -> None:
    before = len(sm.calls)
    with pytest.raises(ledger.LedgerError, match=msg):
        ledger.book(printops_db, sm, 1, grams, source, conf, actor="test")
    assert sm.calls[before:] == []
    assert printops_db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0


def test_unanchored_spool_is_bounded_by_spoolman_and_notes_change_nothing(
    printops_db: sqlite3.Connection, sm: FakeSpoolman
) -> None:
    sp = _spool(sm)
    with db.transaction(printops_db):
        ledger.note(
            printops_db, sp["id"], "dried 6 h, lost 9 g of water", actor="drying"
        )
    est = ledger.remaining(printops_db, sm, sp["id"])
    assert not est.anchored and (est.lo, est.hi) == (0.0, 1000.0)
    assert est.text() == "≤1000 g (never weighed)"
    with pytest.raises(ledger.LedgerError):
        ledger.note(printops_db, sp["id"], "  ", actor="drying")


def test_ledger_is_append_only(
    printops_db: sqlite3.Connection, sm: FakeSpoolman
) -> None:
    _anchor(printops_db, 1, 500.0)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        printops_db.execute("UPDATE ledger SET anchor_g = 1")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        printops_db.execute("DELETE FROM ledger")


# -- tares ---------------------------------------------------------------------------------------


def test_tare_db_is_seeded_from_spoolmandb_with_sources(
    printops_db: sqlite3.Connection,
) -> None:
    rows = tares.list_tares(printops_db, "Bambu Lab")
    assert {r.tare_g for r in rows} == {250.0}
    assert all("github.com/Donkie/SpoolmanDB" in r.source for r in rows)
    assert len(tares.list_tares(printops_db)) >= 150
    elegoo = tares.lookup(printops_db, "ELEGOO", net_g=1000.0)
    assert (
        elegoo.tare is not None and elegoo.tare.grams == 154.0
    )  # cardboard and plastic agree


def test_tare_lookup_narrows_by_kind_and_net_and_reports_ambiguity(
    printops_db: sqlite3.Connection,
) -> None:
    amb = tares.lookup(printops_db, "Polymaker")
    assert amb.tare is None and "ambiguous" in amb.reason and len(amb.candidates) > 2
    one = tares.lookup(printops_db, "polymaker", spool_kind="plastic", net_g=1000.0)
    assert one.tare is not None and one.tare.grams == 250.0
    unknown = tares.lookup(printops_db, "Snapmaker")
    assert unknown.tare is None and "no tare known" in unknown.reason


def test_own_weighing_beats_the_catalogue(printops_db: sqlite3.Connection) -> None:
    tares.add_tare(
        printops_db,
        vendor="Polymaker",
        tare_g=137.0,
        source="weighed empty spool",
        actor="t",
        spool_kind="cardboard",
        net_g=1000.0,
    )
    res = tares.lookup(printops_db, "Polymaker", spool_kind="cardboard", net_g=1000.0)
    assert res.tare is not None and (res.tare.grams, res.tare.sigma) == (137.0, 2.0)
    with pytest.raises(tares.TareError, match="source"):
        tares.add_tare(printops_db, vendor="X", tare_g=100.0, source=" ", actor="t")
    with pytest.raises(tares.TareError, match="spool_kind"):
        tares.add_tare(
            printops_db,
            vendor="X",
            tare_g=100.0,
            source="s",
            actor="t",
            spool_kind="metal",
        )


def test_tare_chain_order(printops_db: sqlite3.Connection, sm: FakeSpoolman) -> None:
    by_db = _spool(sm, vendor={"name": "Bambu Lab"})
    res = tares.resolve(printops_db, by_db)
    assert res.tare is not None and res.tare.origin == "db" and res.tare.grams == 250.0

    vendor_field = _spool(
        sm, vendor={"name": "NoName", "extra": {"spool_tare_g": encode_extra(180.0)}}
    )
    res = tares.resolve(printops_db, vendor_field)
    assert res.tare is not None and (res.tare.origin, res.tare.grams) == (
        "vendor",
        180.0,
    )

    by_hand = _spool(sm, vendor={"name": "Bambu Lab"}, spool_weight=240.0)
    res = tares.resolve(printops_db, by_hand)
    assert res.tare is not None and (res.tare.origin, res.tare.sigma) == (
        "spool",
        tares.UNSOURCED_SIGMA,
    )

    tares.record_choice(
        printops_db,
        by_hand["id"],
        tares.Tare(240.0, 2.0, "weighed", "given"),
        actor="t",
    )
    res = tares.resolve(printops_db, by_hand)
    assert res.tare is not None and (
        res.tare.origin,
        res.tare.sigma,
        res.tare.source,
    ) == ("choice", 2.0, "weighed")

    no_vendor = _spool(sm)
    assert tares.resolve(printops_db, no_vendor).tare is None


# -- importer ------------------------------------------------------------------------------------

PLATES = {
    "_doc": "…",
    "owned": {
        "белый": "#FFFFFF",
        "зелёный Elegoo": "#4FE44D",
        "оранжевый (Борис)": "#E8732A",
    },
    "bought": {"зелёный Elegoo": "Elegoo RAPID PLA+ Green (Game Centar, 1499 дин)"},
}


def test_plates_owned_items(tmp_path: Path) -> None:
    path = tmp_path / "babushkin-dom" / "gen" / "plates.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(PLATES, ensure_ascii=False), encoding="utf-8")
    items = plates_owned(path, location="WH:default")
    assert [i["external_key"] for i in items] == [
        "babushkin-dom:owned:белый",
        "babushkin-dom:owned:зелёный Elegoo",
        "babushkin-dom:owned:оранжевый (Борис)",
    ]
    white, green = items[0], items[1]
    assert (
        white["material"],
        white["name"],
        white["color_hex"],
        white["location"],
    ) == ("PLA", "белый", "#FFFFFF", "WH:default")
    assert "vendor" not in white and "price" not in white
    assert (green["vendor"], green["material"], green["name"], green["price"]) == (
        "Elegoo",
        "PLA+",
        "RAPID PLA+ Green",
        1499.0,
    )


def test_parse_bought_and_bad_files(tmp_path: Path) -> None:
    assert (
        parse_bought("eSun PETG Black (3dfilamenti, 2490,5 дин)")["material"] == "PETG"
    )
    assert parse_bought("some free text") == {"product": "some free text"}
    bad = tmp_path / "plates.json"
    bad.write_text('{"owned": {"x": "red"}}', encoding="utf-8")
    with pytest.raises(ImportFormatError, match="#RRGGBB"):
        plates_owned(bad)
    bad.write_text('{"plates": []}', encoding="utf-8")
    with pytest.raises(ImportFormatError, match="owned"):
        plates_owned(bad)


# -- lint rules ----------------------------------------------------------------------------------


def _lint(
    conn: sqlite3.Connection,
    sm: FakeSpoolman,
    days: int = 30,
    now: datetime.datetime | None = None,
) -> list[datalint.Finding]:
    ctx = datalint.LintContext(
        conn=conn,
        spoolman=sm,
        spools=sm.find("spool"),
        now=now or datetime.datetime.now(datetime.timezone.utc),
        weigh_days=days,
    )
    return lint_spools.lint(ctx)


def test_lint_spools_rules(printops_db: sqlite3.Connection, sm: FakeSpoolman) -> None:
    sealed = _spool(sm, vendor={"name": "Bambu Lab"})["id"]
    _anchor(printops_db, sealed, 1000.0, "receive", 20.0)
    over = _spool(sm, vendor={"name": "Bambu Lab"})["id"]
    _anchor(printops_db, over, 30.0, "weigh", 2.0)
    ledger.book(printops_db, sm, over, 45.0, "gcode_estimate", actor="t")
    no_tare = _spool(sm)["id"]

    found = {(f.rule, f.subject) for f in _lint(printops_db, sm)}
    assert ("spools/remaining-negative", f"spool:{over}") in found
    assert ("spools/no-tare", f"spool:{no_tare}") in found
    assert ("spools/not-weighed", f"spool:{no_tare}") in found  # never weighed
    assert not any(s == f"spool:{sealed}" for _, s in found)  # sealed, unused: exempt
    assert ("spools/not-weighed", f"spool:{over}") not in found  # weighed today

    later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=45)
    stale = [
        f for f in _lint(printops_db, sm, 30, later) if f.subject == f"spool:{over}"
    ]
    assert any(
        f.rule == "spools/not-weighed" and "45 days ago" in f.message for f in stale
    )
    levels = {f.rule: f.level for f in _lint(printops_db, sm)}
    assert (
        levels["spools/remaining-negative"] == "error"
        and levels["spools/no-tare"] == "warn"
    )


def test_datalint_runs_every_rule_module_and_survives_a_crash(
    printops_db: sqlite3.Connection, sm: FakeSpoolman, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "printops.lint_spools" in datalint.rule_modules()

    def boom(ctx: datalint.LintContext) -> list[datalint.Finding]:
        raise RuntimeError("bad rule")

    monkeypatch.setattr(lint_spools, "lint", boom)
    ctx = datalint.LintContext(
        conn=printops_db,
        spoolman=sm,
        spools=[],
        now=datetime.datetime.now(datetime.timezone.utc),
    )
    found = datalint.run(ctx)
    assert any(f.rule == "spools/crashed" and "bad rule" in f.message for f in found)


def test_spool_without_any_weight_is_unknown_not_zero(printops_db: sqlite3.Connection, sm: FakeSpoolman) -> None:
    """An imported spool with no label net: Spoolman has no remaining, so nothing bounds it."""
    sp = _spool(sm, weight=None)
    est = ledger.remaining(printops_db, sm, sp["id"])
    assert "remaining_weight" not in sp and not est.bounded
    assert est.text() == "unknown (never weighed)" and est.to_dict()["bounded"] is False


# -- review round 3 (PR #216) ---------------------------------------------------------------------


def test_lint_spools_survives_a_malformed_opened_at(
    printops_db: sqlite3.Connection, sm: FakeSpoolman
) -> None:
    sp = _spool(sm)
    bad = {**sp, "extra": {**(sp.get("extra") or {}), "opened_at": "2026-10-14 not json"}}
    ctx = datalint.LintContext(
        conn=printops_db,
        spoolman=sm,
        spools=[bad],
        now=datetime.datetime.now(datetime.timezone.utc),
    )
    found = {(f.rule, f.subject) for f in lint_spools.lint(ctx)}
    assert ("spools/not-weighed", f"spool:{sp['id']}") in found


def _vm_steps(conn: sqlite3.Connection, spool_id: int) -> int:
    steps = 0

    def tick() -> int:
        nonlocal steps
        steps += 1
        return 0

    conn.set_progress_handler(tick, 1)
    try:
        ledger.estimate(conn, spool_id)
    finally:
        conn.set_progress_handler(None, 1)
    return steps


def test_estimate_does_not_scan_history_older_than_the_anchor(
    printops_db: sqlite3.Connection,
) -> None:
    """A never-weighed spool: the rows before its newest anchor are not read."""

    def history(spool_id: int, old_rows: int) -> None:
        with db.transaction(printops_db):
            for _ in range(old_rows):
                ledger.anchor(printops_db, spool_id, 500.0, "manual", 20.0, actor="t")
            ledger.anchor(printops_db, spool_id, 400.0, "manual", 20.0, actor="t")
            for source, delta in (("gcode_estimate", -10.0), ("purge", -2.0)):
                ledger._insert(
                    printops_db,
                    spool_id=spool_id,
                    kind="delta",
                    delta_g=delta,
                    anchor_g=None,
                    source=source,
                    confidence_g=1.0,
                    actor="t",
                )

    history(1, 5)
    history(2, 3000)
    short, long = _vm_steps(printops_db, 1), _vm_steps(printops_db, 2)
    assert long < short * 2, (short, long)
    est = ledger.estimate(printops_db, 2)
    assert (est.mid, est.deltas, est.anchor_source, est.last_weighed_at) == (
        388.0,
        2,
        "manual",
        None,
    )


def test_parse_bought_knows_multi_word_vendors() -> None:
    vendors = ["Bambu Lab", "Das Filament", "eSun"]
    got = parse_bought("Bambu Lab PLA Basic Black (Game Centar, 2990 дин)", vendors)
    assert (got["vendor"], got["product"], got["material"]) == (
        "Bambu Lab",
        "PLA Basic Black",
        "PLA",
    )
    # matched by vendor key, kept as spelled in the source
    assert parse_bought("BAMBU-LAB PETG HF (x, 1 дин)", vendors)["vendor"] == "BAMBU-LAB"
    # an unknown vendor: the first word, as before
    assert parse_bought("Elegoo RAPID PLA+ Green (Game Centar, 1499 дин)", vendors)[
        "vendor"
    ] == "Elegoo"
    # a known vendor needs a word boundary: "eSunny" is not eSun
    assert parse_bought("eSunny PLA (x, 1 дин)", vendors)["vendor"] == "eSunny"
