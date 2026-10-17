"""The Jinja of packages/printops_tasks.yaml (#285), rendered the way Home Assistant renders it:
script/automation `variables` and `for_each` are parsed back to native values (HA's
render-to-native), with HA's own filters/tests used here (regex_findall, regex_replace, match,
state_attr, states, has_value, tojson). Proves which items the sync adds, closes and removes for
a given list and sensor, without a running HA (the live check is in docs/printops-tasks.md)."""

from __future__ import annotations

import ast
import datetime
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

jinja2 = pytest.importorskip("jinja2")

PACKAGE = Path(__file__).resolve().parents[2] / "packages" / "printops_tasks.yaml"


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_constructor(
    "!secret", lambda loader, node: f"<secret {loader.construct_scalar(node)}>"
)


@pytest.fixture(scope="module")
def package() -> dict[str, Any]:
    data = yaml.load(PACKAGE.read_text(encoding="utf-8"), Loader=_Loader)  # noqa: S506 — SafeLoader subclass
    assert isinstance(data, dict)
    return data


def _env(states: dict[str, dict[str, Any]]) -> Any:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["regex_findall"] = lambda value, find="", ignorecase=False: re.findall(
        find, value
    )
    env.filters["regex_replace"] = (
        lambda value="", find="", replace="", ignorecase=False: re.sub(
            find, replace, value
        )
    )
    env.filters["tojson"] = lambda value: json.dumps(value, ensure_ascii=False)
    env.tests["match"] = lambda value, find="": bool(re.match(find, str(value)))
    env.filters["regex_match"] = lambda value, find="", ignorecase=False: bool(
        re.match(find, str(value))
    )
    env.globals["states"] = lambda eid: states.get(eid, {}).get("state", "unknown")
    env.globals["state_attr"] = lambda eid, attr: (
        states.get(eid, {}).get("attributes", {}).get(attr)
    )
    env.globals["has_value"] = lambda eid: (
        states.get(eid, {}).get("state", "unknown") not in ("unknown", "unavailable")
    )
    return env


def _native(text: str) -> Any:
    """HA's render-to-native: a rendered string that is a Python literal becomes that value."""
    try:
        return ast.literal_eval(text.strip())
    except (ValueError, SyntaxError):
        return text


def render(env: Any, template: str, variables: dict[str, Any]) -> Any:
    return _native(env.from_string(template).render(**variables))


TASKS = [
    {
        "id": 1,
        "title": "Repair u1/T2: head T2 does not work",
        "status": "open",
        "blocks": "head",
        "head": "T2",
        "slot": None,
        "when": "any",
        "duration_min": None,
        "waiting_on": [],
        "why": "open fault #1",
        "done_hint": "3d printops maint fault close 1 --fixed-by MAINT_ID",
    },
    {
        "id": 11,
        "title": "Fix slot T3 label",
        "status": "open",
        "blocks": "slot",
        "head": "T3",
        "slot": "T3",
        "when": "any",
        "duration_min": None,
        "waiting_on": [],
        "why": None,
        "done_hint": None,
    },
    {
        "id": 12,
        "title": "Camera height",
        "status": "open",
        "blocks": "none",
        "head": None,
        "slot": None,
        "when": "idle",
        "duration_min": 20,
        "waiting_on": [9],
        "why": None,
        "done_hint": None,
    },
    {
        "id": 4,
        "title": "PA bordeaux",
        "status": "snoozed",
        "blocks": "none",
        "head": "T1",
        "slot": None,
        "when": "idle",
        "duration_min": None,
        "waiting_on": [],
        "why": None,
        "done_hint": None,
    },
]


def _sync_vars(package: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    states = {
        "sensor.printops_u1_tasks": {"state": "3", "attributes": {"tasks": TASKS}},
        "todo.printops_u1": {"state": "2"},
    }
    env = _env(states)
    seq = package["script"]["printops_todo_sync"]["sequence"]
    scope: dict[str, Any] = {"printer": "u1"}
    for k, tmpl in seq[0]["variables"].items():
        scope[k] = render(env, tmpl, scope)
    assert render(env, seq[1]["value_template"], scope) is True
    scope["got"] = {scope["todo_list"]: {"items": items}}
    for k, tmpl in seq[3]["variables"].items():
        scope[k] = render(env, tmpl, scope)
    scope["_env"] = env
    return scope


def test_the_sync_plan_adds_open_tasks_closes_ticked_ones_and_turns_typed_items_into_tasks(
    package: dict[str, Any],
) -> None:
    items = [
        {
            "uid": "a",
            "summary": "Repair u1/T2",
            "status": "completed",
            "description": "#1 · BLOCKS head T2",
        },
        {
            "uid": "b",
            "summary": "old",
            "status": "needs_action",
            "description": "#7 · any time",
        },
        {"uid": "c", "summary": "Buy a spare hotend", "status": "needs_action"},
        {
            "uid": "d",
            "summary": "Clean bed",
            "status": "needs_action",
            "description": "with IPA",
        },
        {"uid": "e", "summary": "Ticked before the sync", "status": "completed"},
        # typed by a person: starts with "#12" but not in the generated "#N · " form
        {
            "uid": "f",
            "summary": "Spare nozzles",
            "status": "needs_action",
            "description": "#12 buy spare nozzles",
            "due": datetime.date(2026, 10, 20),
        },
    ]
    v = _sync_vars(package, items)
    assert (
        v["todo_list"] == "todo.printops_u1"
        and v["sensor"] == "sensor.printops_u1_tasks"
    )
    assert [i["uid"] for i in v["marked"]] == ["a", "b"]
    assert [i["uid"] for i in v["unmarked"]] == ["c", "d", "f"]
    assert all(
        set(i) == {"uid", "summary", "status", "description"} for i in v["items"]
    )  # `due` dropped
    assert v["open_ids"] == [1, 11, 12]  # the snoozed task is not on the list
    assert v["shown"] == [1, 7]
    assert [t["id"] for t in v["to_add"]] == [11, 12]

    env = v["_env"]
    synced = package["script"]["printops_todo_sync"]["sequence"][5]["repeat"][
        "sequence"
    ]
    tid = synced[0]["variables"]["tid"]
    done_branch, gone_branch = synced[1]["choose"]
    ids = {
        i["uid"]: render(env, tid, {**v, "repeat": {"item": i}}) for i in v["marked"]
    }
    assert ids == {"a": 1, "b": 7}
    # ticked + still open → done in 3d-api; on the list but closed there → removed
    assert (
        render(
            env,
            done_branch["conditions"],
            {**v, "repeat": {"item": items[0]}, "tid": 1},
        )
        is True
    )
    assert (
        render(
            env,
            gone_branch["conditions"],
            {**v, "repeat": {"item": items[1]}, "tid": 7},
        )
        is True
    )
    assert (
        render(
            env,
            gone_branch["conditions"],
            {**v, "repeat": {"item": items[0]}, "tid": 1},
        )
        is False
    )


def test_the_refusal_of_a_derived_task_becomes_the_notification_text(
    package: dict[str, Any],
) -> None:
    v = _sync_vars(package, [])
    synced = package["script"]["printops_todo_sync"]["sequence"][5]["repeat"][
        "sequence"
    ]
    notify = synced[1]["choose"][0]["sequence"][1]["else"][0]
    assert notify["action"] == "persistent_notification.create"
    res = {
        "status": 409,
        "content": {
            "detail": "task #1 is derived from a fault … 3d printops maint fault close 1"
        },
    }
    msg = render(v["_env"], notify["data"]["message"], {**v, "tid": "1", "res": res})
    assert msg == res["content"]["detail"]
    raw = render(
        v["_env"],
        notify["data"]["message"],
        {**v, "tid": "1", "res": {"status": 502, "content": "Bad Gateway"}},
    )
    assert raw == "HTTP 502: Bad Gateway"
    ok = synced[1]["choose"][0]["sequence"][1]["if"]
    assert render(v["_env"], ok, {"res": {"status": 200, "content": {}}}) is True
    closed = {
        "status": 409,
        "content": {"detail": "task #12 is already done (2026-10-17)"},
    }
    assert (
        render(v["_env"], ok, {"res": closed}) is True
    )  # closed elsewhere since the poll: no notice
    assert (
        render(v["_env"], ok, {"res": res}) is False
    )  # a derived task: notice + untick


def test_item_descriptions_start_with_the_task_id_and_say_what_blocks(
    package: dict[str, Any],
) -> None:
    v = _sync_vars(package, [])
    add = package["script"]["printops_todo_sync"]["sequence"][6]["repeat"]["sequence"][
        0
    ]
    assert add["action"] == "todo.add_item"
    desc = {
        t["id"]: render(
            v["_env"], add["data"]["description"], {**v, "repeat": {"item": t}}
        )
        for t in TASKS[:3]
    }
    assert desc[1] == (
        "#1 · BLOCKS head T2 · any time\nopen fault #1\n"
        "Closes with: 3d printops maint fault close 1 --fixed-by MAINT_ID"
    )
    assert desc[11] == "#11 · BLOCKS slot T3 · any time"
    assert desc[12] == "#12 · when idle · 20 min · waits for #9"
    for d in desc.values():  # the sync finds its own items again by this prefix
        assert re.match(r"#[0-9]+", d)


def test_the_automation_syncs_the_printer_whose_entity_changed_or_both(
    package: dict[str, Any],
) -> None:
    [auto] = package["automation"]
    for_each = auto["actions"][0]["repeat"]["for_each"]
    env = _env({})
    assert render(
        env, for_each, {"trigger": {"entity_id": "sensor.printops_k1se_tasks"}}
    ) == ["k1se"]
    assert render(env, for_each, {"trigger": {"entity_id": "todo.printops_u1"}}) == [
        "u1"
    ]
    assert render(env, for_each, {"trigger": {"platform": "time_pattern"}}) == [
        "k1se",
        "u1",
    ]


def test_rest_commands_send_json_bodies(package: dict[str, Any]) -> None:
    env = _env({})
    rc = package["rest_command"]
    assert json.loads(render_text(env, rc["printops_task_done"]["payload"], {})) == {
        "note": "done in Home Assistant"
    }
    assert json.loads(
        render_text(
            env, rc["printops_task_add"]["payload"], {"title": 'Fix "T3"', "why": None}
        )
    ) == {
        "title": 'Fix "T3"',
        "why": None,
        "source": "manual",
        "refs": ["home-assistant"],
    }
    assert json.loads(
        render_text(env, rc["printops_task_snooze"]["payload"], {"until": "2026-10-25"})
    ) == {"until": "2026-10-25", "reason": "snoozed in Home Assistant"}
    for name in ("printops_k1se_tasks", "printops_u1_tasks"):
        [sensor] = next(
            r for r in package["rest"] if name.split("_")[1] in r["resource"]
        )["sensor"]
        assert sensor["unique_id"] == name and sensor["json_attributes"] == [
            "printer",
            "summary",
            "tasks",
        ]


def render_text(env: Any, template: str, variables: dict[str, Any]) -> str:
    return str(env.from_string(template).render(**variables))


DASHBOARD = PACKAGE.parents[1] / "dashboards" / "ui-3d-printers.yaml"


def _tasks_section() -> list[dict[str, Any]]:
    data = yaml.safe_load(DASHBOARD.read_text(encoding="utf-8"))
    for section in data["views"][0]["sections"]:
        cards = section["cards"]
        if cards and cards[0].get("heading") == "Калибровки и ТО":
            return list(cards)
    raise AssertionError("no «Калибровки и ТО» section on the 3D Printers overview")


def test_the_dashboard_lists_each_printers_tasks_with_done_and_snooze(
    package: dict[str, Any],
) -> None:
    cards = _tasks_section()
    assert [c["entity"] for c in cards if c["type"] == "todo-list"] == [
        "todo.printops_k1se",
        "todo.printops_u1",
    ]
    md = {c["title"]: c["content"] for c in cards if c["type"] == "markdown"}
    states = {
        "sensor.printops_u1_tasks": {
            "state": "2",
            "attributes": {
                "summary": {"open": 2, "blocking": 2, "waiting": 0, "snoozed": 1},
                "tasks": TASKS,
            },
        }
    }
    text = render_text(_env(states), md["U1"], {})
    assert "**2** open · **2** blocking · 0 waiting · 1 snoozed" in text
    assert "- **#1** Repair u1/T2: head T2 does not work — ⛔ blocks head T2" in text
    assert "- **#12** Camera height · ⏳ waits for #9 · 20 min" in text
    assert "- **#4** PA bordeaux · 💤 snoozed" in text
    assert render_text(_env({}), md["K1 SE"], {}).strip() == "3d-api: no data"
    buttons = [b for c in cards if c["type"] == "horizontal-stack" for b in c["cards"]]
    actions = [
        (b["tap_action"]["perform_action"], b["tap_action"]["data"]) for b in buttons
    ]
    assert actions == [
        ("script.printops_task_action", {"action": "done"}),
        ("script.printops_task_action", {"action": "snooze", "days": 1}),
        ("script.printops_task_action", {"action": "snooze"}),
    ]
    assert (
        "printops_task" in package["input_text"]
        and "printops_task_action" in package["script"]
    )


def test_the_action_script_snoozes_until_a_date_or_until_reopened(
    package: dict[str, Any],
) -> None:
    env = _env({"input_text.printops_task": {"state": " 12 "}})
    late = datetime.timezone(
        datetime.timedelta(hours=2)
    )  # 23:30 CEST: a bare date would end at 02:00
    env.globals["now"] = lambda: datetime.datetime(2026, 10, 16, 23, 30, tzinfo=late)
    env.globals["timedelta"] = datetime.timedelta
    variables = package["script"]["printops_task_action"]["sequence"][0]["variables"]
    assert render(env, variables["task"], {}) == 12
    assert (
        render_text(env, variables["until"], {"days": 1}) == "2026-10-17T23:30:00+02:00"
    )
    assert (
        render_text(env, variables["until"], {}) == "None"
    )  # HA renders it native: null → until reopened
    assert (
        render_text(env, variables["until"], {"days": "tomorrow"}) == "None"
    )  # not a number: no past-date snooze
    assert render_text(env, variables["problem"], {"task": 12, "action": "done"}) == ""
    assert render_text(
        env, variables["problem"], {"task": "", "action": "done"}
    ).startswith("type a task number")
    assert (
        render_text(env, variables["problem"], {"task": 12, "action": "doen"})
        == "unknown action doen: use done or snooze"
    )
    seq = package["script"]["printops_task_action"]["sequence"]
    message = seq[2]["variables"]["message"]
    assert (
        render_text(
            env,
            message,
            {"problem": "", "res": {"status": 409, "content": {"detail": "derived"}}},
        )
        == "derived"
    )
    assert (
        render_text(env, message, {"problem": ""})
        == "3d-api did not answer (http://192.168.0.39:8765)"
    )
    assert seq[1]["then"][0]["continue_on_error"] is True
