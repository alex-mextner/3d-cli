"""PrintOps printer tasks (#281): the printer_task model, derivation from faults / due rules /
calibration requirements, the manual lifecycle, the JSONL buffer and its import, and the routes."""

from __future__ import annotations

import datetime
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fleet_fixtures import REPO_FLEET
from printops_fakes import FakeSpoolman

from printops import calibrations as cal
from printops import events, policy
from printops import maintenance as mt
from printops import maintenance_rules as mr
from printops import printer_task_buffer as buf
from printops import printer_tasks as pt
from printops.calibration_catalog import load_catalog
from printops.client import ApiClient, ApiError
from printops.fleet import load_fleet
from printops.spoolman import Spoolman, SpoolmanError, encode_extra, ensure_extra_fields

RULES = """\
rules:
  - id: hotend-wipe
    title: wipe the hotend and check the nozzle seal
    printers: [u1, k1se]
    component: hotend
    task: clean
    scope: head
    every: {days: 30}
    source: vendor wiki
"""


def _line(**over: Any) -> str:
    base: dict[str, Any] = {
        "schema": buf.TASK_SCHEMA,
        "op": "open",
        "printer": "k1se",
        "printer_model": "Creality K1 SE",
        "head": None,
        "slot": None,
        "component": None,
        "type": "inspect",
        "title": "t",
        "why": None,
        "calibration_kinds": [],
        "spools": [],
        "blocks": {"scope": "none", "head": None, "slot": None},
        "when": "idle",
        "when_note": None,
        "duration_min": None,
        "needs_human": True,
        "source": "manual",
        "source_key": None,
        "depends_on": [],
        "refs": [],
        "status": "open",
        "at": "2026-10-15",
        "recorded_at": "2026-10-15T20:45:01Z",
        "actor": "cli:ultra",
    }
    base.update(over)
    if base["op"] == "open":
        base.setdefault("uid", base.get("task_uid"))
        base["task_uid"] = base["uid"]
    return json.dumps(base, ensure_ascii=False)


U_X = "01a1414f-a5c7-7458-a04e-9d8ed6502fca"
U_SHAPER = "01a1414f-a5c7-7458-a04e-9d8fae81f569"
U_BORDEAUX = "01a1414f-a5c7-7458-a04e-9d90a71de89c"
U_STIFF = "8b5a9ff8-005c-4402-8c86-573c9a03cf07"
U_T2 = "01a1414f-a5c7-7458-a04e-9d95ee795639"
U_T3 = "01a1414f-a5c7-7458-a04e-9d967eed2692"

# The shapes of the 2026-10-15/16 buffers (~/.local/share/3d/printer-todo/{k1se,u1}.jsonl).
K1SE = [
    _line(uid=U_X, component="kinematics", title="X mechanics check", duration_min=20),
    _line(
        uid=U_SHAPER,
        component="kinematics",
        type="calibration",
        title="Cold SHAPER_CALIBRATE AXIS=X",
        calibration_kinds=["input_shaper"],
        depends_on=[U_X],
        needs_human=False,
    ),
    _line(
        uid=U_BORDEAUX,
        head="T1",
        type="calibration",
        title="Calibrate PA + flow for spool 'bordeaux'",
        calibration_kinds=["pressure_advance", "flow_ratio"],
        spools=[{"name": "bordeaux", "spool_id": None}],
    ),
    json.dumps(
        {
            "uid": "4d47cb65-53a7-4c83-acd1-837f61e3e506",
            "schema": buf.TASK_SCHEMA,
            "op": "snooze",
            "task_uid": U_BORDEAUX,
            "printer": "k1se",
            "status": "snoozed",
            "snoozed_until": None,
            "reason": "deferred by user tg#22362",
            "at": "2026-10-16T10:58:01Z",
            "actor": "PrintOpsLead2",
        }
    ),
    _line(
        uid="c6e2731c-cb3c-4d4e-b984-c7b8aab43853",
        title="X: scale vs backlash check",
        blocks=False,
        duration_min=5,
        source="calibration_readout",
        source_key="k1se:x_scale_backlash",
        depends_on=[U_X],
    ),
    json.dumps(
        {
            "uid": "2078dcba-e442-4e77-a480-3282934b28b1",
            "schema": buf.TASK_SCHEMA,
            "op": "done",
            "task_uid": U_X,
            "printer": "k1se",
            "type": None,
            "blocks": None,
            "source": "user_hand",
            "status": "done",
            "at": "2026-10-16T14:06:17+00:00",
            "actor": "PrintOpsLead2",
        }
    ),
    _line(
        uid=U_STIFF,
        title="X: fix stiff motion",
        duration_min=30,
        source="user_hand",
        source_key="k1se:x_stiff_motion",
        blocks=False,
        depends_on=[U_X],
    ),
    json.dumps(
        {
            "uid": "8b629b35-b1e2-4874-95ef-090f885ae91e",
            "schema": buf.TASK_SCHEMA,
            "op": "update",
            "task_uid": U_SHAPER,
            "printer": "k1se",
            "type": None,
            "depends_on": [U_STIFF],
            "status": "open",
            "at": "2026-10-16T14:06:17+00:00",
            "actor": "PrintOpsLead2",
        }
    ),
]
U1 = [
    _line(
        uid=U_T2,
        printer="u1",
        head="T2",
        component="head",
        type="repair",
        title="T2 head faulty",
        blocks={"scope": "head", "head": "T2", "slot": None},
        source="fault",
        source_key="fault:fleet:u1/T2",
    ),
    _line(
        uid=U_T3,
        printer="u1",
        head="T3",
        slot="T3",
        type="setup",
        title="Fix slot T3 filament label",
        blocks={"scope": "slot", "head": "T3", "slot": "T3"},
    ),
]


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    rules = tmp_path / "maintenance-rules.yaml"
    rules.write_text(RULES, encoding="utf-8")
    monkeypatch.setenv("THREED_FLEET", str(REPO_FLEET))
    monkeypatch.setenv("THREED_MAINTENANCE_RULES", str(rules))
    monkeypatch.delenv("PRINTOPS_POLICY", raising=False)
    monkeypatch.delenv("PRINTOPS_CALIBRATIONS", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return tmp_path


@pytest.fixture
def fleet(env: Path) -> Any:
    return load_fleet()


@pytest.fixture
def conn(printops_db: sqlite3.Connection, fleet: Any) -> sqlite3.Connection:
    mt.seed_from_fleet(printops_db, fleet)
    return printops_db


@pytest.fixture
def ctx(conn: sqlite3.Connection, fleet: Any, fake_spoolman: FakeSpoolman) -> cal.Ctx:
    ensure_extra_fields(fake_spoolman)
    return cal.Ctx(
        conn=conn,
        spoolman=fake_spoolman,
        actor="test",
        catalog=load_catalog(),
        policy=policy.section("calibrations"),
        fleet=fleet,
    )


@pytest.fixture
def rules(fleet: Any) -> mr.Rules:
    return mr.load_rules(printers=fleet.printers)


@pytest.fixture
def src(fleet: Any, ctx: cal.Ctx, rules: mr.Rules) -> pt.Sources:
    return pt.Sources(fleet=fleet, rules=rules, calibration=ctx)


def spool(
    sm: Spoolman | None, name: str, *, slot: str | None, lot: str | None = None
) -> int:
    assert sm is not None
    vendors = [v for v in sm.find("vendor") if v["name"] == "Creality"]
    vid = (
        vendors[0]["id"] if vendors else sm.create("vendor", {"name": "Creality"})["id"]
    )
    fils = [f for f in sm.find("filament") if f["name"] == name]
    fid = (
        fils[0]["id"]
        if fils
        else sm.create(
            "filament",
            {
                "name": name,
                "vendor_id": vid,
                "material": "PLA",
                "density": 1.24,
                "diameter": 1.75,
                "weight": 1000,
            },
        )["id"]
    )
    extra = {"printops_slot": encode_extra(slot)} if slot else {}
    return int(
        sm.create("spool", {"filament_id": fid, "lot_nr": lot, "extra": extra})["id"]
    )


def by_key(
    conn: sqlite3.Connection, prefix: str, *, status: str | None = None
) -> dict[str, dict[str, Any]]:
    return {
        t["source_key"]: t
        for t in pt.list_tasks(conn, status=status or "all")
        if (t["source_key"] or "").startswith(prefix)
    }


def kinds_open(
    conn: sqlite3.Connection, printer: str, head: str
) -> dict[str, dict[str, Any]]:
    return {
        t["calibration_kinds"][0]: t
        for t in pt.list_tasks(conn, printer)
        if t["source"] == "calibration" and t["head"] == head
    }


def ev(conn: sqlite3.Connection, kind: str) -> list[dict[str, Any]]:
    return [
        json.loads(r["payload"])
        for r in conn.execute(
            "SELECT payload FROM events WHERE kind = ? ORDER BY id", (kind,)
        )
    ]


# -- schema --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("cols", "vals"),
    [
        ("blocks", "'head'"),  # blocks head without a head
        ("blocks, head", "'slot', 'T1'"),  # blocks slot without a slot
        ("status", "'done'"),  # closed without closed_at
        ("closed_at", "'2026-10-16T00:00:00.000Z'"),  # closed_at on an open task
        (
            "snoozed_until",
            "'2026-10-20T00:00:00.000Z'",
        ),  # snoozed_until on an open task
        ("source", "'chat'"),
    ],
)
def test_schema_checks_are_the_last_line_of_defence(
    conn: sqlite3.Connection, cols: str, vals: str
) -> None:
    base = "uid, printer, type, title, source, created_by, created_at, updated_at"
    vbase = "'u', 'k1se', 'other', 't', 'manual', 'x', 'now', 'now'"
    if "source" in cols:
        base, vbase = base.replace("source, ", ""), vbase.replace("'manual', ", "")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            f"INSERT INTO printer_task ({base}, {cols}) VALUES ({vbase}, {vals})"
        )  # noqa: S608


def test_an_open_source_key_is_unique_but_a_closed_one_repeats(
    conn: sqlite3.Connection, fleet: Any
) -> None:
    sql = (
        "INSERT INTO printer_task (uid, printer, type, title, source, source_key, status, closed_at, created_by, "
        "created_at, updated_at) VALUES (?, 'k1se', 'other', 't', 'manual', 'k', ?, ?, 'x', 'now', 'now')"
    )
    conn.execute(sql, ("a", "done", "2026-10-16"))
    conn.execute(sql, ("b", "open", None))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, ("c", "open", None))


# -- derivation: faults --------------------------------------------------------------------------
def test_the_seeded_u1_t2_fault_is_a_repair_task_blocking_t2_and_sync_is_idempotent(
    conn: sqlite3.Connection, src: pt.Sources
) -> None:
    first = pt.sync(conn, src)
    [t2] = pt.list_tasks(conn, "u1")
    assert (t2["type"], t2["head"], t2["blocks"], t2["source"]) == (
        "repair",
        "T2",
        "head",
        "fault",
    )
    assert t2["source_key"] == "fault:fleet:u1/T2" and t2["id"] in first.opened
    assert t2["done_hint"].startswith(
        f"3d printops maint fault close {t2['fault_id']} --fixed-by"
    )
    again = pt.sync(conn, src)
    assert again.changed == 0 and again.errors == []
    assert [e["id"] for e in ev(conn, "printer_task.opened")] == [t2["id"]]


def test_closing_the_fault_after_a_logged_repair_closes_its_task(
    conn: sqlite3.Connection,
    src: pt.Sources,
    fleet: Any,
    rules: mr.Rules,
    fake_spoolman: FakeSpoolman,
) -> None:
    pt.sync(conn, src)
    [t2] = pt.list_tasks(conn, "u1")
    m = mt.log_maintenance(
        conn,
        fleet,
        rules,
        "u1",
        actor="me",
        head="T2",
        component="head",
        kind="repair",
        notes="new hotend",
        fixes=[t2["fault_id"]],
    )
    assert pt.sync(conn, src).closed == [t2["id"]]
    done = pt.get(conn, t2["id"])
    assert (done["status"], done["closed_by"], done["maintenance_id"]) == (
        "done",
        "derive",
        m["maintenance"]["id"],
    )
    assert pt.list_tasks(conn, "u1") == []
    assert ev(conn, "printer_task.closed")[-1]["status"] == "done"
    # the head prints again by the closed fault; the fleet file's stale `usable: false` is lint's
    from printops.datalint import LintContext
    from printops.lint_maintenance import lint

    found = {
        (f.rule, f.subject)
        for f in lint(
            LintContext(
                conn=conn,
                spoolman=fake_spoolman,
                spools=[],
                now=datetime.datetime.now(datetime.timezone.utc),
            )
        )
    }
    assert ("maintenance/fleet-drift", "u1/T2") in found


def test_a_printer_wide_fault_blocks_the_printer_and_wontfix_dismisses(
    conn: sqlite3.Connection, src: pt.Sources, fleet: Any
) -> None:
    f = mt.open_fault(conn, fleet, "k1se", symptom="bed wobbles", actor="me")
    pt.sync(conn, src)
    [t] = pt.list_tasks(conn, "k1se")
    assert (t["blocks"], t["head"], t["source_key"]) == (
        "printer",
        None,
        f"fault:{f['id']}",
    )
    mt.close_fault(conn, f["id"], actor="me", status="wontfix", root_cause="cosmetic")
    pt.sync(conn, src)
    assert (
        pt.get(conn, t["id"])["status"] == "dismissed"
        and "wontfix: cosmetic" in pt.get(conn, t["id"])["note"]
    )


# -- derivation: maintenance rules ---------------------------------------------------------------
def test_a_due_rule_is_one_task_per_printer_head_and_logging_it_closes_that_one(
    conn: sqlite3.Connection, src: pt.Sources, fleet: Any, rules: mr.Rules
) -> None:
    mr.check(conn, rules, fleet, provider=None)  # type: ignore[arg-type]  # calendar rule: no counters read
    pt.sync(conn, src)
    tasks = by_key(conn, "rule:")
    assert {"rule:u1/hotend-wipe/T1/0", "rule:k1se/hotend-wipe/T1/0"} <= set(tasks)
    assert (
        tasks["rule:k1se/hotend-wipe/T1/0"]["title"]
        == "wipe the hotend and check the nozzle seal"
    )
    k1 = tasks["rule:k1se/hotend-wipe/T1/0"]
    assert k1["done_hint"] == "3d printops maint log k1se/T1 --rule hotend-wipe"
    m = mt.log_maintenance(
        conn, fleet, rules, "k1se", actor="me", head="T1", rule_id="hotend-wipe"
    )
    rep = pt.sync(conn, src)
    assert rep.closed == [k1["id"]] and rep.opened == []
    assert pt.get(conn, k1["id"])["maintenance_id"] == m["maintenance"]["id"]
    assert pt.get(conn, tasks["rule:u1/hotend-wipe/T1/0"]["id"])["status"] == "open"


# -- derivation: calibrations --------------------------------------------------------------------
def test_a_nozzle_swap_opens_flow_and_pa_tasks_recording_closes_them_and_a_new_swap_opens_new_ones(
    conn: sqlite3.Connection, src: pt.Sources, ctx: cal.Ctx, fleet: Any
) -> None:
    sid = spool(ctx.spoolman, "Hyper PLA", slot="k1se/T1", lot="A1")
    pt.sync(conn, src, "k1se")
    first = kinds_open(conn, "k1se", "T1")
    assert {"flow_ratio", "pressure_advance"} <= set(first)
    flow = first["flow_ratio"]
    assert flow["spool_id"] == sid and flow["blocks"] == "none" and flow["duration_min"]
    assert re.fullmatch(
        r"calib:k1se/T1/flow_ratio/nozzle:\d+/filament:\d+/lot:A1", flow["source_key"]
    )
    assert (
        flow["done_hint"]
        == f"3d calibrate record flow_ratio --printer k1se --head T1 --spool {sid}"
    )
    for kind, values in (
        ("flow_ratio", {"flow_ratio": 0.97}),
        ("pressure_advance", {"pressure_advance": 0.03}),
    ):
        rec = cal.record(
            ctx,
            cal.RunRequest(
                kind=kind, method="manual", printer="k1se", head="T1", spool_id=sid
            ),
            {"values": values},
        )
        assert rec.status == "recorded"
    rep = pt.sync(conn, src, "k1se")
    assert {first["flow_ratio"]["id"], first["pressure_advance"]["id"]} <= set(
        rep.closed
    )
    closed = pt.get(conn, flow["id"])
    assert closed["status"] == "done" and closed["calibration_id"] is not None
    assert "flow_ratio" not in kinds_open(conn, "k1se", "T1")

    mt.swap_nozzle(conn, fleet, "k1se", "T1", reason="worn", actor="me")
    pt.sync(conn, src, "k1se")
    after = kinds_open(conn, "k1se", "T1")
    assert {"flow_ratio", "pressure_advance"} <= set(after)
    assert (
        after["flow_ratio"]["id"] != flow["id"]
        and after["flow_ratio"]["source_key"] != flow["source_key"]
    )
    assert (
        pt.get(conn, flow["id"])["status"] == "done"
    )  # the closed task is history, never reopened


def test_another_spool_dismisses_the_old_spools_tasks_and_an_unknown_spool_leaves_them(
    conn: sqlite3.Connection,
    src: pt.Sources,
    ctx: cal.Ctx,
    fake_spoolman: FakeSpoolman,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = spool(ctx.spoolman, "Hyper PLA", slot="k1se/T1")
    pt.sync(conn, src, "k1se")
    old = kinds_open(conn, "k1se", "T1")["pressure_advance"]

    def down(*_: Any, **__: Any) -> Any:
        raise SpoolmanError("connection refused")

    monkeypatch.setattr(fake_spoolman, "find", down)
    assert pt.sync(conn, src, "k1se").changed == 0  # Spoolman down: nothing closes
    monkeypatch.undo()
    fake_spoolman.update("spool", a, {"extra": {"printops_slot": encode_extra("")}})
    assert (
        pt.sync(conn, src, "k1se").changed == 0
    )  # nothing bound: unknown, not "no longer needed"
    spool(ctx.spoolman, "Matte PLA", slot="k1se/T1")
    pt.sync(conn, src, "k1se")
    gone = pt.get(conn, old["id"])
    assert (gone["status"], gone["closed_by"]) == ("dismissed", "derive")
    assert kinds_open(conn, "k1se", "T1")["pressure_advance"]["id"] != old["id"]


# -- done / dismiss / snooze / dependencies -------------------------------------------------------
def test_a_derived_task_refuses_done_and_a_persons_dismissal_sticks(
    conn: sqlite3.Connection, src: pt.Sources
) -> None:
    pt.sync(conn, src)
    [t2] = pt.list_tasks(conn, "u1")
    with pytest.raises(pt.TaskError) as e:
        pt.done(conn, t2["id"], actor="me")
    assert (
        e.value.status == 409
        and "3d printops maint fault close" in e.value.message
        and "dismiss" in e.value.message
    )
    with pytest.raises(pt.TaskError, match="reason: required"):
        pt.dismiss(conn, t2["id"], actor="me", reason=" ")
    pt.dismiss(
        conn, t2["id"], actor="me", reason="T2 parked until the new head arrives"
    )
    assert (
        pt.sync(conn, src).opened == []
    )  # the fault is still open, the person said not now
    assert pt.list_tasks(conn, "u1") == []
    reopened = pt.reopen(conn, t2["id"], actor="me")
    assert reopened["status"] == "open" and reopened["closed_at"] is None


def test_manual_task_lifecycle_dependencies_and_snoozes(
    conn: sqlite3.Connection, fleet: Any, src: pt.Sources
) -> None:
    x = pt.create(
        conn,
        fleet,
        "k1se",
        actor="me",
        title="X mechanics check",
        type_="inspect",
        when="idle",
        duration_min=20,
    )
    shaper = pt.create(
        conn,
        fleet,
        "k1se",
        actor="me",
        title="Cold SHAPER_CALIBRATE X",
        type_="calibration",
        depends_on=[x["id"]],
        needs_human=False,
    )
    assert pt.get(conn, shaper["id"])["waiting_on"] == [x["id"]]
    s = pt.summary(pt.list_tasks(conn, "k1se"))
    assert (s["open"], s["waiting"], s["next_idle"]["id"]) == (2, 1, x["id"])
    done = pt.done(conn, x["id"], actor="me", note="feet tightened")
    assert (done["status"], done["note"], done["closed_by"]) == (
        "done",
        "feet tightened",
        "me",
    )
    assert pt.get(conn, shaper["id"])["waiting_on"] == []
    with pytest.raises(pt.TaskError, match="already done"):
        pt.done(conn, x["id"], actor="me")

    sn = pt.snooze(
        conn,
        shaper["id"],
        actor="me",
        until="2026-10-20T09:00+02:00",
        reason="after the house queue",
    )
    assert (
        sn["status"] == "snoozed" and sn["snoozed_until"] == "2026-10-20T07:00:00.000Z"
    )
    with pytest.raises(pt.TaskError, match="not in the future"):
        pt.snooze(conn, shaper["id"], actor="me", until="2020-01-01")
    rep = pt.sync(
        conn,
        src,
        "k1se",
        now=datetime.datetime(2026, 10, 21, tzinfo=datetime.timezone.utc),
    )
    assert (
        shaper["id"] in rep.updated and pt.get(conn, shaper["id"])["status"] == "open"
    )
    forever = pt.snooze(conn, shaper["id"], actor="me")
    assert (
        forever["snoozed_until"] is None
        and pt.reopen(conn, shaper["id"], actor="me")["status"] == "open"
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"blocks": "head"}, "blocks head: name the head"),
        ({"blocks": "slot", "head": "T1"}, "blocks slot: name the slot"),
        ({"head": "T9"}, "has no head 'T9'"),
        ({"component": "wheel"}, "component: 'wheel'"),
        ({"when": "never"}, "when: 'never'"),
        ({"duration_min": 0}, "duration_min"),
        ({"depends_on": [999]}, "no printer task '999'"),
        ({"source": "fault"}, "cannot be created directly"),
    ],
)
def test_create_refusals(
    conn: sqlite3.Connection, fleet: Any, kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(pt.TaskError, match=re.escape(message)):
        pt.create(conn, fleet, "k1se", actor="me", title="t", **kwargs)


def test_dependencies_stay_on_one_printer(conn: sqlite3.Connection, fleet: Any) -> None:
    u = pt.create(conn, fleet, "u1", actor="me", title="u1 thing")
    with pytest.raises(pt.TaskError, match="dependencies stay on one printer"):
        pt.create(conn, fleet, "k1se", actor="me", title="t", depends_on=[u["id"]])
    with pytest.raises(pt.TaskError, match="unknown printer 'mk4'"):
        pt.create(conn, fleet, "mk4", actor="me", title="t")


# -- the buffer ------------------------------------------------------------------------------------
def test_uuid7_is_time_ordered_and_rfc_shaped() -> None:
    a, b = buf.uuid7(), buf.uuid7()
    assert re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", a
    )
    assert a[:13] <= b[:13]


def test_append_writes_whole_lines_and_replay_follows_the_ops(env: Path) -> None:
    for raw in K1SE:
        buf.append("k1se", json.loads(raw))
    path = buf.buffer_path("k1se")
    assert path == env / "data" / "3d" / "printer-todo" / "k1se.jsonl"
    tasks = buf.replay(buf.read(path))
    assert tasks[U_X]["status"] == "done" and tasks[U_BORDEAUX]["status"] == "snoozed"
    assert tasks[U_SHAPER]["depends_on"] == [U_STIFF]
    with pytest.raises(buf.BufferError, match="not a fleet printer name"):
        buf.buffer_path("../etc")


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        (
            [K1SE[0], '{"uid": "x", "schema": "printops.printer_task/1", "op": "op'],
            "line 2: not a JSON object",
        ),
        (
            [json.dumps({"schema": "printops.calendar/1"})],
            "line 1: unknown schema 'printops.calendar/1'",
        ),
        (
            [K1SE[3]],
            "line 1: op snooze for task " + U_BORDEAUX + " that no earlier line opens",
        ),
        ([_line(uid="a", type="chores")], "line 1: type: 'chores'"),
        (
            [_line(uid="a", blocks={"scope": "head"})],
            "line 1: blocks: scope head needs the head",
        ),
        ([_line(uid="a", printer="mk4")], "line 1: printer 'mk4' is not in the fleet"),
        (
            [_line(uid="a", depends_on=["nope"])],
            "line 1: depends_on nope: no task with that uid",
        ),
    ],
)
def test_import_stops_at_the_first_bad_line_and_imports_nothing(
    conn: sqlite3.Connection, fleet: Any, lines: list[str], message: str
) -> None:
    with pytest.raises(pt.TaskError, match=re.escape(message)) as e:
        pt.import_lines(conn, fleet, lines, actor="me")
    assert "nothing imported" in e.value.message
    assert conn.execute("SELECT COUNT(*) FROM printer_task_op").fetchone()[0] == 0


def test_import_replays_the_buffers_merges_the_t2_fault_and_a_second_import_adds_nothing(
    conn: sqlite3.Connection, fleet: Any, src: pt.Sources, fake_spoolman: FakeSpoolman
) -> None:
    pt.sync(conn, src)  # the derived U1 T2 repair task exists first
    [derived] = pt.list_tasks(conn, "u1")
    bordeaux = spool(fake_spoolman, "bordeaux", slot=None)

    dry = pt.import_lines(
        conn, fleet, K1SE, actor="me", spoolman=fake_spoolman, dry_run=True
    )
    assert dry.dry_run and dry.created == 5 and dry.applied == 3
    assert pt.list_tasks(conn, "k1se") == []  # rolled back

    k1 = pt.import_lines(conn, fleet, K1SE, actor="me", spoolman=fake_spoolman)
    u1 = pt.import_lines(conn, fleet, U1, actor="me", spoolman=fake_spoolman)
    assert (k1.created, k1.applied, k1.refused, k1.added) == (5, 3, 0, 8)
    assert (u1.created, u1.merged) == (1, 1)
    merged = pt.get(conn, derived["id"])
    assert U_T2 in merged["refs"] and merged["source"] == "fault"
    t3 = pt.get(conn, U_T3)
    assert (t3["blocks"], t3["slot"], t3["source"]) == ("slot", "T3", "import")

    x, shaper, bo = pt.get(conn, U_X), pt.get(conn, U_SHAPER), pt.get(conn, U_BORDEAUX)
    assert x["status"] == "done" and x["closed_by"] == "PrintOpsLead2"
    assert (
        shaper["depends_on"] == [pt.get(conn, U_STIFF)["id"]]
        and shaper["waiting_on"] == shaper["depends_on"]
    )
    assert (bo["status"], bo["snoozed_until"], bo["note"]) == (
        "snoozed",
        None,
        "deferred by user tg#22362",
    )
    assert bo["spool_id"] == bordeaux and bo["spools"] == [
        {"name": "bordeaux", "spool_id": bordeaux}
    ]
    backlash = pt.get(conn, "c6e2731c-cb3c-4d4e-b984-c7b8aab43853")
    assert (backlash["blocks"], backlash["source_key"], backlash["source"]) == (
        "none",
        "k1se:x_scale_backlash",
        "import",
    )
    assert x["created_at"] == "2026-10-15T20:45:01.000Z"

    for lines in (K1SE, U1):
        again = pt.import_lines(conn, fleet, lines, actor="me", spoolman=fake_spoolman)
        assert again.added == 0 and again.skipped == len(lines)
    assert pt.sync(conn, src).changed == 0


def test_a_spool_name_binds_exactly_or_by_one_partial_match_else_warns(
    conn: sqlite3.Connection, fleet: Any, fake_spoolman: FakeSpoolman
) -> None:
    yellow = spool(fake_spoolman, "жёлтый Snapdragon", slot=None)
    spool(fake_spoolman, "синий", slot=None)
    spool(fake_spoolman, "синий матовый", slot=None)

    def task(uid: str, name: str) -> str:
        return _line(
            uid=uid,
            head="T1",
            type="calibration",
            title=f"PA for {name}",
            spools=[{"name": name, "spool_id": None}],
        )

    rep = pt.import_lines(
        conn,
        fleet,
        [
            task("s1", "Snapdragon"),
            task("s2", "синий"),
            task("s3", "матов"),
            task("s4", "bordeaux"),
        ],
        actor="me",
        spoolman=fake_spoolman,
    )
    assert pt.get(conn, "s1")["spool_id"] == yellow
    assert (
        pt.get(conn, "s2")["spool_id"] is not None
    )  # exact beats the longer partial name
    assert (
        pt.get(conn, "s3")["spool_id"] is not None
        and pt.get(conn, "s4")["spool_id"] is None
    )
    assert rep.warnings == [
        f"line 1: spool 'Snapdragon' bound to #{yellow} by partial filament name 'жёлтый Snapdragon'",
        f"line 3: spool 'матов' bound to #{pt.get(conn, 's3')['spool_id']} by partial filament name 'синий матовый'",
        "line 4: spool 'bordeaux' not bound: 0 Spoolman spools of that filament name (none)",
    ]


def test_a_buffered_done_of_a_derived_task_is_refused_and_recorded(
    conn: sqlite3.Connection, fleet: Any, src: pt.Sources
) -> None:
    pt.sync(conn, src)
    done = json.dumps(
        {
            "uid": "d1",
            "schema": buf.TASK_SCHEMA,
            "op": "done",
            "task_uid": U_T2,
            "printer": "u1",
        }
    )
    rep = pt.import_lines(conn, fleet, [U1[0], done], actor="me")
    assert rep.merged == 1 and rep.refused == 1
    assert (
        rep.items[1]["outcome"].startswith("refused: task #")
        and "fault close" in rep.items[1]["outcome"]
    )
    assert pt.import_lines(conn, fleet, [U1[0], done], actor="me").added == 0


def test_an_unmerged_buffer_task_with_a_derived_key_is_adopted_by_the_derivation(
    conn: sqlite3.Connection, fleet: Any, src: pt.Sources
) -> None:
    pt.import_lines(
        conn, fleet, U1[:1], actor="me"
    )  # before any sync: no derived task to merge into
    t = pt.get(conn, U_T2)
    assert t["source"] == "import"
    rep = pt.sync(conn, src)
    assert rep.opened == [] and t["id"] in rep.updated
    assert pt.get(conn, U_T2)["source"] == "fault"


def test_idle_notices_keep_the_last_state_of_a_period(
    conn: sqlite3.Connection, fleet: Any
) -> None:
    def notice(state: str, **over: Any) -> str:
        return json.dumps(
            {
                "uid": buf.uuid7(),
                "schema": buf.NOTICE_SCHEMA,
                "printer": "k1se",
                "idle_since": "2026-10-16T10:00:00Z",
                "state": state,
                "claimed_at": "2026-10-16T10:20:00Z",
                "task_uids": [U_X],
                "task_set_sha": "ab",
                "host": "ws",
                **over,
            }
        )

    lines = [
        notice("pending"),
        notice("sent", sent_at="2026-10-16T10:20:02Z", tg_message_id=22400),
    ]
    assert pt.import_lines(conn, fleet, lines, actor="me").notices == 2
    row = conn.execute("SELECT * FROM printer_idle_notice").fetchone()
    assert (row["state"], row["tg_message_id"]) == ("sent", 22400)
    assert pt.import_lines(conn, fleet, lines[1:], actor="me").added == 0


# -- REST ------------------------------------------------------------------------------------------
@pytest.fixture
def api(env: Path, api_client: ApiClient) -> ApiClient:
    return api_client


def test_rest_list_add_done_refusal_import_and_events(
    api: ApiClient, printops_db: sqlite3.Connection
) -> None:
    data = api.get("/v1/printers/u1/tasks")
    [t2] = data["tasks"]
    assert (
        t2["blocks"] == "head"
        and data["summary"]["blocking"] == 1
        and data["sync"]["errors"] == []
    )
    with pytest.raises(ApiError) as e:
        api.post(f"/v1/tasks/{t2['id']}/done", {})
    assert e.value.status == 409 and "fault close" in e.value.message

    x = api.post(
        "/v1/printers/k1se/tasks",
        {
            "title": "X mechanics check",
            "type": "inspect",
            "when": "idle",
            "duration_min": 20,
        },
    )
    shaper = api.post(
        "/v1/printers/k1se/tasks", {"title": "shaper", "depends_on": [x["id"]]}
    )
    assert shaper["waiting_on"] == [x["id"]] and shaper["source"] == "manual"
    with pytest.raises(ApiError) as e:
        api.post("/v1/printers/k1se/tasks", {"title": "t", "blocks": "head"})
    assert e.value.status == 422
    assert api.post(f"/v1/tasks/{x['uid']}/done", {"note": "ok"})["status"] == "done"
    assert (
        api.post(f"/v1/tasks/{shaper['id']}/snooze", {"reason": "later"})["status"]
        == "snoozed"
    )
    assert api.get(f"/v1/tasks/{shaper['id']}")["note"] == "later"
    assert [
        t["id"] for t in api.get("/v1/tasks", printer="k1se", status="all")["tasks"]
    ] == [shaper["id"], x["id"]]

    rep = api.post("/v1/tasks/import?dry_run=true", {"lines": U1})
    assert rep["dry_run"] and rep["merged"] == 1 and rep["created"] == 1
    assert api.post("/v1/tasks/import", {"lines": U1})["added"] == 2
    assert api.post("/v1/tasks/import", {"lines": U1})["added"] == 0
    with pytest.raises(ApiError) as e:
        api.post("/v1/tasks/import", {"lines": ["{"]})
    assert e.value.status == 422 and "line 1" in e.value.message
    kinds = {e.kind for e in events.since(printops_db, 0, 1000)}
    assert {
        "printer_task.opened",
        "printer_task.updated",
        "printer_task.closed",
    } <= kinds
    with pytest.raises(ApiError) as e:
        api.get("/v1/printers/mk4/tasks")
    assert e.value.status == 404


def test_rest_needs_scopes(
    env: Path, printops_client: Any, printops_tokens: dict[str, str]
) -> None:
    from printops_fixtures import TestClientTransport

    reader = ApiClient(
        "http://testserver",
        printops_tokens["read"],
        TestClientTransport(printops_client),
    )
    assert reader.get("/v1/tasks")["tasks"]
    with pytest.raises(ApiError) as e:
        reader.post("/v1/printers/k1se/tasks", {"title": "t"})
    assert e.value.status == 403
