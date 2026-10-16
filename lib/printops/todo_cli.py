"""`3d printer todo …` — the to-do list of each printer through 3d-api (#281; plan §7.5).

Parses, calls 3d-api (`/v1/printers/{p}/tasks`, `/v1/tasks/…`; printops.api.routes_tasks) and renders.
WITHOUT 3d-API (no URL configured, or it does not answer): `add`, `done`, `dismiss` and `snooze`
append an operation to the JSONL buffer (printops.printer_task_buffer) and say so; `list` shows
the buffer; `import` moves it into 3d-api once it is back (idempotent by uid).
Lives under `3d printer` (next to status/start/watch, where the list is needed); the maintenance
journals stay under `3d printops maint`.
"""

from __future__ import annotations

import getpass
import json
import sys
import urllib.parse
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from errors import InputNotFound, ThreeDError, UsageError
from printops import printer_task_buffer as buf
from printops.cli._common import Parsed, api_errors, client, parse
from printops.client import ApiClient, ApiConfigError, ApiError

CMD = "printer"
USAGE = """3d printer todo <action> [args] [--api URL] [--token TOKEN]
  The to-do list of each printer: calibrations it needs, due maintenance, repairs of open
  faults (these three are derived and close themselves when the work is recorded), and
  manual tasks. Served by 3d-api; without it the CLI keeps a JSONL buffer.

  list [PRINTER] [--all] [--json]       open tasks (snoozed ones last); --all adds done/dismissed
  add PRINTER --title TEXT [options]    a manual task (options below)
  done ID [--note TEXT]                 a manual task is done (a derived one refuses and names
                                        the command that records the work)
  dismiss ID --reason TEXT              drop a task without doing it
  snooze ID [--until DATE] [--reason T] hide it until DATE (ISO date/time); without --until
                                        until `reopen`
  reopen ID                             a snoozed/done/dismissed task back to open
  import FILE... [--dry-run] [--json]   replay buffer files into 3d-api (by uid: twice adds 0)

add options:
  --why TEXT           why it is needed
  --type T             calibration | inspect | maintenance | repair | setup | other (default other)
  --head T2            the head it is about        --slot S    the slot it is about
  --component C        nozzle | hotend | ptfe | extruder | plate | belts | kinematics | fan | head |
                       other | printer
  --blocks B           none (default) | printer | head[:T2] | slot[:T3] — what it keeps from printing
  --when W             idle | before_print | any (default any)   --when-note TEXT
  --duration MIN       minutes it takes         --no-human   a machine can do it alone
  --depends-on ID,..   tasks that must be done first (the task shows as WAITING until then)
  --ref R,..           issues / chat refs       --kinds K,..  calibration kinds it is about
  --spool ID           the Spoolman spool it is about
  --followup           a mechanical follow-up of a calibration (source calibration_followup)

ID is the task number (or its uid; offline, the uid or a unique uid prefix from `list`).
--json prints the API (or buffer) objects. 3d-api: --api URL / PRINTOPS_API, --token / PRINTOPS_TOKEN.
Buffer: ~/.local/share/3d/printer-todo/<printer>.jsonl ($XDG_DATA_HOME/3d/printer-todo).

Examples:
  3d printer todo list
  3d printer todo list u1 --json | jq '.tasks[] | select(.blocks != "none") | {id, title, blocks}'
  3d printer todo add k1se --title "Check X belt tension" --type inspect --when idle --duration 15
  3d printer todo add u1 --title "Fix T3 label" --type setup --blocks slot:T3 --head T3
  3d printer todo add k1se --title "Cold SHAPER_CALIBRATE X" --type calibration --depends-on 9 --no-human
  3d printer todo done 12 --note "belts re-tensioned"
  3d printer todo dismiss 7 --reason "camera mount postponed to #263"
  3d printer todo snooze 4 --until 2026-10-25 --reason "after the house queue"
  3d printer todo import ~/.local/share/3d/printer-todo/k1se.jsonl --dry-run
  3d printer todo import ~/.local/share/3d/printer-todo/*.jsonl"""

COMMON_VALUES = frozenset({"--api", "--token"})
COMMON_FLAGS = frozenset({"--json"})


def _q(segment: str) -> str:
    return urllib.parse.quote(segment, safe="")


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _list_opt(p: Parsed, opt: str) -> list[str]:
    return [s.strip() for s in p.opts.get(opt, "").split(",") if s.strip()]


def _sub(name: str) -> str:
    return f"todo {name}"


def _api(p: Parsed) -> ApiClient | None:
    """The 3d-api client, or None when no URL is configured (→ the buffer)."""
    try:
        return ApiClient.from_config(
            api=p.opts.get("--api"), token=p.opts.get("--token")
        )
    except ApiConfigError:
        return None


def _offline(e: ApiError | None) -> bool:
    return e is None or e.status == 0


# -- rendering -----------------------------------------------------------------------------------
def _state(t: Mapping[str, Any]) -> str:
    if t["status"] == "open" and t.get("waiting_on"):
        return "WAITING"
    return str(t["status"]).upper()


def _where(t: Mapping[str, Any]) -> str:
    where = t["printer"] + (f"/{t['head']}" if t.get("head") else "")
    return where + (
        f" slot {t['slot']}" if t.get("slot") and t.get("slot") != t.get("head") else ""
    )


def _details(t: Mapping[str, Any]) -> str:
    bits: list[str] = []
    if t.get("blocks") and t["blocks"] != "none":
        what = {"head": f"head {t.get('head')}", "slot": f"slot {t.get('slot')}"}.get(
            t["blocks"], "the printer"
        )
        bits.append(f"BLOCKS {what}")
    bits.append(
        {"idle": "when idle", "before_print": "before a print", "any": "any time"}.get(
            t.get("when") or "any", "any time"
        )
    )
    if t.get("when_note"):
        bits[-1] += f" ({t['when_note']})"
    if t.get("duration_min"):
        bits.append(f"{t['duration_min']} min")
    bits.append("human" if t.get("needs_human", True) else "no human needed")
    if t.get("waiting_on"):
        bits.append("waits for " + ", ".join(f"#{i}" for i in t["waiting_on"]))
    if t["status"] == "snoozed":
        bits.append(
            f"snoozed until {t['snoozed_until'][:16].replace('T', ' ')}"
            if t.get("snoozed_until")
            else "snoozed until reopened"
        )
    if t.get("closed_at"):
        bits.append(
            f"{t['status']} {str(t['closed_at'])[:10]}"
            + (f" by {t['closed_by']}" if t.get("closed_by") else "")
        )
    if t.get("note"):
        bits.append(f"note: {t['note']}")
    src = t.get("source") or "manual"
    if t.get("done_hint") and t.get("closed_at") is None:
        bits.append(f"from {src.replace('_', ' ')} — closes with: {t['done_hint']}")
    elif src != "manual":
        bits.append(f"from {src.replace('_', ' ')}")
    return " · ".join(bits)


def _line(t: Mapping[str, Any], ident: str) -> str:
    return f"{ident:<6} {_state(t):<9} {_where(t):<10} {t['type']:<11} {t['title']}"


def _render(
    tasks: list[Mapping[str, Any]], *, idents: Callable[[Mapping[str, Any]], str]
) -> None:
    by_printer: dict[str, list[Mapping[str, Any]]] = {}
    for t in tasks:
        by_printer.setdefault(t["printer"], []).append(t)
    for printer, items in sorted(by_printer.items()):
        active = [t for t in items if t["status"] in ("open", "in_progress")]
        head = [f"{len(active)} open"]
        waiting = sum(1 for t in active if t.get("waiting_on"))
        if waiting:
            head[-1] += f" ({waiting} waiting)"
        blocking = sum(1 for t in active if t.get("blocks") not in (None, "none"))
        if blocking:
            head.append(f"{blocking} blocking")
        snoozed = sum(1 for t in items if t["status"] == "snoozed")
        if snoozed:
            head.append(f"{snoozed} snoozed")
        print(f"{printer}: {', '.join(head)}")
        for t in items:
            print("  " + _line(t, idents(t)))
            if t.get("why"):
                print(f"         why: {t['why']}")
            print(f"         {_details(t)}")


def _show(t: Mapping[str, Any], verb: str, p: Parsed) -> None:
    if "--json" in p.flags:
        _print_json(t)
        return
    print(f"{verb} {_line(t, '#' + str(t['id']))}")
    print(f"         {_details(t)}")


# -- buffer (offline) ----------------------------------------------------------------------------
def _buffer_tasks(printer: str | None) -> list[dict[str, Any]]:
    files = (
        [buf.buffer_path(printer)]
        if printer
        else sorted(buf.buffer_dir().glob("*.jsonl"))
    )
    out: list[dict[str, Any]] = []
    for path in files:
        try:
            tasks = buf.replay(buf.read(path))
        except buf.BufferError as e:
            raise ThreeDError(
                f"{path}: {e}",
                command=f"{CMD} todo",
                remediation=["Fix or remove that line (a torn write?), then retry."],
            ) from e
        now = buf.utc_now()
        for t in tasks.values():
            if (
                t["status"] == "snoozed"
                and t.get("snoozed_until")
                and str(t["snoozed_until"]) <= now
            ):
                t["status"], t["snoozed_until"] = "open", None
            t["waiting_on"] = [
                d
                for d in t.get("depends_on") or []
                if d in tasks and tasks[d]["status"] not in buf.CLOSED
            ]
            out.append(t)
    return out


def _find_offline(ref: str, sub: str) -> dict[str, Any]:
    if ref.isdigit():
        raise UsageError(
            f"3d-api is not reachable and task numbers live there; name the task by its uid "
            f"(`3d {CMD} todo list` shows it offline)",
            command=f"{CMD} {sub}",
        )
    hits = [t for t in _buffer_tasks(None) if str(t["uid"]).startswith(ref)]
    if len(hits) != 1:
        raise UsageError(
            f"{'no' if not hits else 'more than one'} buffered task with uid {ref!r}",
            command=f"{CMD} {sub}",
            remediation=[f"`3d {CMD} todo list` (offline) shows the buffered uids."],
        )
    return hits[0]


def _actor() -> str:
    try:
        return f"cli:{getpass.getuser()}"
    except (KeyError, OSError):
        return "cli"


def _append(
    printer: str, op: str, task_uid: str, fields: Mapping[str, Any], why: str
) -> int:
    from printops.fleet import FleetError, load_fleet

    model = None
    try:
        fleet = load_fleet()
    except FleetError:
        fleet = None
    if fleet is not None:
        p = fleet.printers.get(printer)
        if p is None:  # a typo here would stop the whole buffer's import later
            raise UsageError(
                f"unknown printer {printer!r}; fleet has: {', '.join(sorted(fleet.printers))}",
                command=f"{CMD} todo",
            )
        model = p.model
    now = buf.utc_now()
    uid = task_uid if op == "open" else buf.uuid7()
    record = {
        "uid": uid,
        "schema": buf.TASK_SCHEMA,
        "op": op,
        "task_uid": task_uid,
        "printer": printer,
        "printer_model": model,
        **fields,
        "at": now,
        "recorded_at": now,
        "actor": _actor(),
    }
    buf.parse_lines([json.dumps(record)])  # never write a line the import would refuse
    path = buf.append(printer, record)
    print(
        f"3d-api {why}: {op} recorded in the buffer {path} (task {task_uid}); "
        f"move it with `3d {CMD} todo import {path}` once 3d-api is back",
        file=sys.stderr,
    )
    return 0


# -- actions -------------------------------------------------------------------------------------
def _list(argv: list[str], sub: str) -> int:
    p = parse(
        argv,
        sub,
        values=COMMON_VALUES,
        flags=COMMON_FLAGS | {"--all"},
        n_pos=(0, 1),
        prefix=CMD,
    )
    printer = p.pos[0] if p.pos else None
    status = "all" if "--all" in p.flags else None
    api = _api(p)
    err: ApiError | None = None
    if api is not None:
        try:
            if printer:
                data = api.get(f"/v1/printers/{_q(printer)}/tasks", status=status)
                tasks = data["tasks"]
            else:
                data = api.get("/v1/tasks", status=status)
                tasks = data["tasks"]
        except ApiError as e:
            if not _offline(e):
                with api_errors(sub, prefix=CMD):
                    raise
            err = e
        else:
            if "--json" in p.flags:
                _print_json(data)
            elif not tasks:
                print(
                    f"no {'' if status else 'open '}tasks"
                    + (f" on {printer}" if printer else "")
                )
            else:
                _render(tasks, idents=lambda t: f"#{t['id']}")
            return 0
    tasks = [
        t for t in _buffer_tasks(printer) if status or t["status"] not in buf.CLOSED
    ]
    why = (
        "is not configured"
        if api is None
        else f"is not reachable ({err.message if err else ''})"
    )
    if "--json" in p.flags:
        _print_json({"offline": True, "buffer": str(buf.buffer_dir()), "tasks": tasks})
        return 0
    print(
        f"3d-api {why}: showing the buffer {buf.buffer_dir()} (not derived tasks)",
        file=sys.stderr,
    )
    if tasks:
        _render(tasks, idents=lambda t: str(t["uid"])[:8])
    else:
        print("no buffered tasks")
    return 0


def _blocks(p: Parsed, sub: str) -> tuple[str, str | None, str | None]:
    raw = p.opts.get("--blocks", "none")
    scope, _, where = raw.partition(":")
    if scope not in buf.BLOCKS or (where and scope not in ("head", "slot")):
        raise UsageError(
            f"--blocks {raw!r}: use none, printer, head[:T2] or slot[:T3]",
            command=f"{CMD} {sub}",
        )
    head = where if scope == "head" and where else p.opts.get("--head")
    slot = where if scope == "slot" and where else p.opts.get("--slot")
    return scope, head, slot


def _add(argv: list[str], sub: str) -> int:
    p = parse(
        argv,
        sub,
        values=COMMON_VALUES
        | {
            "--title",
            "--why",
            "--type",
            "--head",
            "--slot",
            "--component",
            "--blocks",
            "--when",
            "--when-note",
            "--duration",
            "--depends-on",
            "--ref",
            "--kinds",
            "--spool",
        },
        flags=COMMON_FLAGS | {"--no-human", "--followup"},
        n_pos=1,
        prefix=CMD,
    )
    printer = p.pos[0]
    if not p.opts.get("--title"):
        raise UsageError("--title is required (what to do)", command=f"{CMD} {sub}")
    scope, head, slot = _blocks(p, sub)
    try:
        deps = [int(x) for x in _list_opt(p, "--depends-on")]
    except ValueError as e:
        raise UsageError(
            "--depends-on needs task numbers like 3 or 3,4", command=f"{CMD} {sub}"
        ) from e
    body: dict[str, Any] = {
        "title": p.opts["--title"],
        "why": p.opts.get("--why"),
        "type": p.opts.get("--type", "other"),
        "head": head,
        "slot": slot,
        "component": p.opts.get("--component"),
        "blocks": scope,
        "when": p.opts.get("--when", "any"),
        "when_note": p.opts.get("--when-note"),
        "duration_min": p.int_opt("--duration", sub),
        "needs_human": "--no-human" not in p.flags,
        "depends_on": deps,
        "refs": _list_opt(p, "--ref"),
        "calibration_kinds": _list_opt(p, "--kinds"),
        "spool_id": p.int_opt("--spool", sub),
        "source": "calibration_followup" if "--followup" in p.flags else "manual",
    }
    body = {k: v for k, v in body.items() if v is not None}
    api = _api(p)
    if api is not None:
        try:
            task = api.post(f"/v1/printers/{_q(printer)}/tasks", body)
        except ApiError as e:
            if not _offline(e):
                with api_errors(sub, prefix=CMD):
                    raise
            why = f"is not reachable ({e.message})"
        else:
            _show(task, "added", p)
            return 0
    else:
        why = "is not configured"
    if deps:
        raise UsageError(
            "--depends-on takes task numbers from 3d-api, which is not reachable: add the task "
            "without it and set the dependency after the import",
            command=f"{CMD} {sub}",
        )
    task_uid = buf.uuid7()
    fields = {
        k: body.get(k)
        for k in (
            "head",
            "slot",
            "component",
            "type",
            "title",
            "why",
            "when",
            "when_note",
            "duration_min",
            "needs_human",
            "calibration_kinds",
            "refs",
        )
    }
    fields.update(
        {
            "blocks": {"scope": scope, "head": head, "slot": slot},
            "source": body["source"],
            "source_key": None,
            "depends_on": [],
            "status": "open",
            "spools": [{"name": None, "spool_id": body["spool_id"]}]
            if "spool_id" in body
            else [],
        }
    )
    return _append(printer, "open", task_uid, fields, why)


def _close(argv: list[str], sub: str, action: str) -> int:
    values = COMMON_VALUES | (
        {"--note"}
        if action == "done"
        else {"--reason"}
        if action == "dismiss"
        else {"--until", "--reason"}
        if action == "snooze"
        else set()
    )
    p = parse(argv, sub, values=values, flags=COMMON_FLAGS, n_pos=1, prefix=CMD)
    ref = p.pos[0]
    if action == "dismiss" and not (p.opts.get("--reason") or "").strip():
        raise UsageError(
            "--reason is required: why the task is dropped without doing it",
            command=f"{CMD} {sub}",
        )
    body: dict[str, Any] = {
        "done": {"note": p.opts.get("--note")},
        "dismiss": {"reason": p.opts.get("--reason")},
        "snooze": {"until": p.opts.get("--until"), "reason": p.opts.get("--reason")},
        "reopen": {},
    }[action]
    body = {k: v for k, v in body.items() if v is not None}
    api = _api(p)
    if api is not None:
        try:
            task = api.post(f"/v1/tasks/{_q(ref)}/{action}", body)
        except ApiError as e:
            if not _offline(e):
                with api_errors(sub, prefix=CMD):
                    raise
            why = f"is not reachable ({e.message})"
        else:
            verb = {
                "done": "done",
                "dismiss": "dismissed",
                "snooze": "snoozed",
                "reopen": "reopened",
            }[action]
            _show(task, verb, p)
            return 0
    else:
        why = "is not configured"
    if action == "reopen":
        raise ThreeDError(
            f"3d-api {why}: reopen needs it (the buffer only records new operations)",
            command=f"{CMD} {sub}",
        )
    t = _find_offline(ref, sub)
    if t["status"] in buf.CLOSED:
        raise ThreeDError(
            f"buffered task {t['uid']} is already {t['status']}", command=f"{CMD} {sub}"
        )
    from printops.printer_tasks import done_hint

    if action == "done" and t.get("source") in (
        "calibration",
        "maintenance_rule",
        "fault",
    ):
        raise ThreeDError(
            f"task {t['uid']} is derived from a {t['source'].replace('_', ' ')}; it closes when the "
            f"work is recorded: {done_hint(t)}",
            command=f"{CMD} {sub}",
            remediation=[
                "To drop it without the work: `3d printer todo dismiss ID --reason …`."
            ],
        )
    fields: dict[str, Any] = {
        "status": {"done": "done", "dismiss": "dismissed", "snooze": "snoozed"}[action]
    }
    if action == "done":
        fields["note"] = p.opts.get("--note")
    elif action == "dismiss":
        fields["reason"] = p.opts["--reason"]
    else:
        fields.update(
            {"snoozed_until": p.opts.get("--until"), "reason": p.opts.get("--reason")}
        )
    return _append(str(t["printer"]), action, str(t["uid"]), fields, why)


def _import(argv: list[str], sub: str) -> int:
    p = parse(
        argv,
        sub,
        values=COMMON_VALUES,
        flags=COMMON_FLAGS | {"--dry-run"},
        n_pos=(1, 100),
        prefix=CMD,
    )
    api = client(p, sub, prefix=CMD)
    reports = []
    for name in p.pos:
        path = Path(name).expanduser()
        if not path.is_file():
            raise InputNotFound(f"no buffer file {path}", command=f"{CMD} {sub}")
        lines = path.read_text(encoding="utf-8").splitlines()
        with api_errors(sub, prefix=CMD):
            params = "?dry_run=true" if "--dry-run" in p.flags else ""
            rep = api.post(f"/v1/tasks/import{params}", {"lines": lines})
        reports.append({"file": str(path), **rep})
        if "--json" in p.flags:
            continue
        mode = "DRY RUN (rolled back) " if rep["dry_run"] else ""
        print(
            f"{mode}{path}: {rep['lines']} lines — {rep['created']} created, {rep['merged']} merged, "
            f"{rep['applied']} applied, {rep['refused']} refused, {rep['notices']} notices, "
            f"{rep['skipped']} already imported"
        )
        for it in rep["items"]:
            title = f"  {it['title']}" if it.get("title") else ""
            task = f"#{it['task_id']}" if it.get("task_id") else "-"
            print(
                f"  line {it['line']:<3} {it['op']:<11} {task:<6} {it['outcome']}{title}"
            )
        for w in rep["warnings"]:
            print(f"  warning: {w}")
    if "--json" in p.flags:
        _print_json(reports)
    return 0


ACTIONS: dict[str, Callable[[list[str], str], int]] = {
    "list": _list,
    "ls": _list,
    "add": _add,
    "done": lambda a, s: _close(a, s, "done"),
    "dismiss": lambda a, s: _close(a, s, "dismiss"),
    "snooze": lambda a, s: _close(a, s, "snooze"),
    "reopen": lambda a, s: _close(a, s, "reopen"),
    "import": _import,
}


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0 if argv else 1
    action, rest = argv[0], argv[1:]
    if "-h" in rest or "--help" in rest:
        print(USAGE)
        return 0
    handler = ACTIONS.get(action)
    if handler is None:
        raise UsageError(
            f"unknown todo action {action!r}",
            command=f"{CMD} todo",
            remediation=[
                "Use list, add, done, dismiss, snooze, reopen or import — "
                "see `3d printer todo --help`."
            ],
        )
    return handler(rest, _sub(action))
