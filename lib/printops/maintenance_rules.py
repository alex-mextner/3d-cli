"""maintenance_rules.py — fleet/maintenance-rules.yaml: typed loader, schema, and the due evaluation.

WHAT (plan §7.3, https://github.com/alex-mextner/3d-cli/issues/166):
  - `rules:` — every N print-hours / filament metres / tool changes / jobs / days → a maintenance
    task (lube the rails, check the belts, …). A rule is due when ANY of its thresholds is reached
    since its baseline: the latest maintenance logged for it (`rule_id`), or — when logged
    without a rule — the latest one with the same component and task (for a `head` rule: on the
    same head). Never logged = no baseline: counters count from the printer's first job, days
    are unknown, so the rule is due until its first log ("never logged").
  - `invalidates:` — which maintenance (component × kind) invalidates calibrations, as the
    trigger name of the calibration catalog (fleet/calibrations.yaml `invalidates_on`, #167).

COUNTERS come from a printops.counters.CounterProvider (Moonraker history now, the job ledger
#170 later). A threshold whose counter is unknown is reported, never guessed: a rule whose
every threshold is unknown has status `no_counter`.

EVENTS: `check()` emits `maintenance.due` (subject "<printer>[/<head>]") once per rule ×
printer × head × baseline; the `maintenance_due` table remembers what was announced.

INVARIANTS: stdlib + lazy pyyaml; validate() reports EVERY problem (lint), load_rules() raises
RulesError with all of them. THREED_MAINTENANCE_RULES=<path> overrides the file, "" disables it.
"""

from __future__ import annotations

import datetime
import math
import os
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from errors import ThreeDError
from printops import db, events
from printops.counters import COUNTERS, CounterProvider, Usage

ENV = "THREED_MAINTENANCE_RULES"
COMPONENTS = ("nozzle", "hotend", "ptfe", "extruder", "plate", "belts", "kinematics", "fan", "head",
              "other", "printer")
TASKS = ("clean", "lube", "replace", "tighten", "repair", "inspect", "firmware_update", "move")
# fleet/calibrations.yaml `invalidates_on` vocabulary (agreed with #167).
TRIGGERS = ("nozzle_swap", "nozzle_diameter", "extruder", "ptfe", "hotend", "kinematics", "belts",
            "plate", "head_swap", "move", "repair")
THRESHOLDS = (*COUNTERS, "days")
SCOPES = ("printer", "head")
DEFAULT_CHECK_EVERY_MIN = 60
DEFAULT_SOON_FRACTION = 0.9
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_TOP_KEYS = frozenset({"check_every_min", "soon_fraction", "invalidates", "rules"})
_RULE_KEYS = frozenset({"id", "title", "printers", "component", "task", "scope", "every", "source", "note"})
_INV_KEYS = frozenset({"component", "when", "trigger"})


def _unknown_keys(where: str, mapping: dict[Any, Any], accepted: frozenset[str]) -> list[str]:
    prefix = f"{where}." if where else ""
    return [f"{prefix}{k}: unknown key (accepted: {', '.join(sorted(accepted))})"
            for k in sorted(set(map(str, mapping)) - accepted)]


OK, SOON, DUE, NO_COUNTER = "ok", "soon", "due", "no_counter"


class RulesError(ThreeDError):
    """fleet/maintenance-rules.yaml is unreadable or breaks the schema. Exit 2."""

    exit_code = 2


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    printers: tuple[str, ...]
    component: str
    task: str
    scope: str  # printer | head
    every: dict[str, float]  # threshold name -> limit
    source: str
    note: str | None = None


@dataclass(frozen=True)
class Invalidation:
    component: str
    when: tuple[str, ...]  # maintenance kinds
    trigger: str


@dataclass(frozen=True)
class Rules:
    path: Path | None
    rules: tuple[Rule, ...]
    invalidations: tuple[Invalidation, ...]
    check_every_min: int = DEFAULT_CHECK_EVERY_MIN
    soon_fraction: float = DEFAULT_SOON_FRACTION

    def trigger_for(self, component: str, kind: str) -> str | None:
        for inv in self.invalidations:
            if inv.component == component and kind in inv.when:
                return inv.trigger
        return None

    def get(self, rule_id: str) -> Rule | None:
        return next((r for r in self.rules if r.id == rule_id), None)


EMPTY = Rules(None, (), ())


def rules_path() -> Path | None:
    """The rules file to read, or None when THREED_MAINTENANCE_RULES="" disables it."""
    env = os.environ.get(ENV)
    if env is not None:
        return Path(env).expanduser() if env.strip() else None
    from cli.env import repo_root

    return Path(repo_root()) / "fleet" / "maintenance-rules.yaml"


def load_rules(path: Path | None = None, *, printers: Iterable[str] | None = None) -> Rules:
    """Parsed rules; EMPTY when the layer is disabled or the default file is absent. An explicit
    path (or THREED_MAINTENANCE_RULES) that does not exist, or any schema problem, raises.
    With `printers` (the fleet names) a rule naming an unknown printer is a schema problem too."""
    from printops.fleet import FleetError, read_yaml

    explicit = path is not None or bool(os.environ.get(ENV, "").strip())
    path = path or rules_path()
    if path is None:
        return EMPTY
    if not path.is_file():
        if explicit:
            raise RulesError(f"maintenance rules file not found: {path}", command="printops maint",
                             remediation=[f"Fix the path, or unset {ENV} to use <repo>/fleet/maintenance-rules.yaml."])
        return EMPTY
    try:
        data = read_yaml(path)
    except FleetError as e:
        raise RulesError(e.message, command="printops maint") from e
    rules, problems = validate(data, path, printers)
    if rules is None or problems:
        raise RulesError(f"{path}: {len(problems)} schema problem(s)", command="printops maint",
                         remediation=[*problems, "Run `3d lint --printops` after fixing; see docs/commands/printops-maint.md."])
    return rules


def validate(data: Any, path: Path | None, printers: Iterable[str] | None = None) -> tuple[Rules | None, list[str]]:
    """(rules, problems). With `printers` (the fleet names), unknown printer names are problems too."""
    if data is None:
        return Rules(path, (), ()), []
    if not isinstance(data, dict):
        return None, ["top level: must be a mapping with `rules:` and `invalidates:`"]
    bad: list[str] = _unknown_keys("", data, _TOP_KEYS)
    every_min = data.get("check_every_min", DEFAULT_CHECK_EVERY_MIN)
    if isinstance(every_min, bool) or not isinstance(every_min, int) or not 1 <= every_min <= 24 * 60:
        bad.append(f"check_every_min: {every_min!r} is not a whole number of minutes 1..1440")
        every_min = DEFAULT_CHECK_EVERY_MIN
    soon = data.get("soon_fraction", DEFAULT_SOON_FRACTION)
    if isinstance(soon, bool) or not isinstance(soon, (int, float)) or not 0 < soon <= 1:
        bad.append(f"soon_fraction: {soon!r} is not a fraction 0 < f <= 1")
        soon = DEFAULT_SOON_FRACTION
    known = set(printers) if printers is not None else None
    rules = _rules(data.get("rules", []), bad, known)
    invs = _invalidations(data.get("invalidates", []), bad)
    return Rules(path, rules, invs, int(every_min), float(soon)), bad


def _rules(value: Any, bad: list[str], known: set[str] | None) -> tuple[Rule, ...]:
    if not isinstance(value, list):
        bad.append("rules: must be a list")
        return ()
    out: list[Rule] = []
    seen: set[str] = set()
    for i, r in enumerate(value):
        where = f"rules[{i}]"
        if not isinstance(r, dict):
            bad.append(f"{where}: must be a mapping")
            continue
        n = len(bad)
        bad.extend(_unknown_keys(where, r, _RULE_KEYS))
        rid = r.get("id")
        if not isinstance(rid, str) or not _ID.fullmatch(rid):
            bad.append(f"{where}.id: {rid!r} must be lower-case letters, digits and '-'")
        elif rid in seen:
            bad.append(f"{where}.id: {rid!r} is used twice")
        else:
            seen.add(rid)
            where = f"rules[{rid}]"
        bad.extend(f"{where}.{key}: required text" + (" (where the interval comes from)" if key == "source" else "")
                   for key in ("title", "source") if not isinstance(r.get(key), str) or not r[key].strip())
        note = r.get("note")
        if note is not None and not isinstance(note, str):
            bad.append(f"{where}.note: must be text")
        printers = r.get("printers")
        if not isinstance(printers, list) or not printers or not all(isinstance(p, str) and p for p in printers):
            bad.append(f"{where}.printers: required, a non-empty list of fleet printer names")
            printers = []
        elif len(set(printers)) != len(printers):
            bad.append(f"{where}.printers: a printer is listed twice")
        elif known is not None:
            bad.extend(f"{where}.printers: {p!r} is not in fleet/printers.yaml" for p in printers if p not in known)
        for key, accepted in (("component", COMPONENTS), ("task", TASKS)):
            if r.get(key) not in accepted:
                bad.append(f"{where}.{key}: {r.get(key)!r} is not one of {', '.join(accepted)}")
        scope = r.get("scope", "printer")
        if scope not in SCOPES:
            bad.append(f"{where}.scope: {scope!r} is not one of {', '.join(SCOPES)}")
        # combinations `maint log --rule` would refuse: a rule nobody could ever mark done
        if scope == "head" and r.get("component") in ("printer", "plate"):
            bad.append(f"{where}.scope: a {r.get('component')} rule is printer-wide, not per head")
        if r.get("component") == "nozzle" and r.get("task") == "replace":
            bad.append(f"{where}: a nozzle replacement is logged by `maint nozzle swap`, not as a rule")
        every = r.get("every")
        limits: dict[str, float] = {}
        if not isinstance(every, dict) or not every:
            bad.append(f"{where}.every: required, a mapping of one or more of {', '.join(THRESHOLDS)}")
        else:
            for k, v in every.items():
                if k not in THRESHOLDS:
                    bad.append(f"{where}.every.{k}: unknown threshold (accepted: {', '.join(THRESHOLDS)})")
                elif isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not v > 0:
                    bad.append(f"{where}.every.{k}: {v!r} is not a number > 0")  # .inf would never come due
                else:
                    limits[str(k)] = float(v)
        if len(bad) == n:
            out.append(Rule(str(rid), r["title"].strip(), tuple(printers), r["component"], r["task"], scope,
                            limits, r["source"].strip(), note.strip() if isinstance(note, str) else None))
    return tuple(out)


def _invalidations(value: Any, bad: list[str]) -> tuple[Invalidation, ...]:
    if not isinstance(value, list):
        bad.append("invalidates: must be a list")
        return ()
    out: list[Invalidation] = []
    pairs: set[tuple[Any, Any]] = set()
    for i, v in enumerate(value):
        where = f"invalidates[{i}]"
        if not isinstance(v, dict):
            bad.append(f"{where}: must be a mapping {{component, when, trigger}}")
            continue
        n = len(bad)
        bad.extend(_unknown_keys(where, v, _INV_KEYS))
        if v.get("component") not in COMPONENTS:
            bad.append(f"{where}.component: {v.get('component')!r} is not one of {', '.join(COMPONENTS)}")
        when = v.get("when")
        if not isinstance(when, list) or not when or not all(w in TASKS for w in when):
            bad.append(f"{where}.when: must be a non-empty list of maintenance kinds ({', '.join(TASKS)})")
            when = []
        if v.get("trigger") not in TRIGGERS:
            bad.append(f"{where}.trigger: {v.get('trigger')!r} is not one of {', '.join(TRIGGERS)}")
        for w in when:
            if (v.get("component"), w) in pairs:
                bad.append(f"{where}: {v.get('component')} × {w} is mapped twice")
            pairs.add((v.get("component"), w))
        if len(bad) == n:
            out.append(Invalidation(v["component"], tuple(when), v["trigger"]))
    return tuple(out)


# -- due evaluation ---------------------------------------------------------------------------
@dataclass
class DueItem:
    rule: str
    title: str
    printer: str
    head: str | None
    component: str
    task: str
    status: str  # ok | soon | due | no_counter
    every: dict[str, float]
    used: dict[str, float | None]  # threshold -> usage since the baseline (None = unknown)
    progress: dict[str, float]  # threshold -> used / limit, known ones only
    reasons: list[str]
    baseline: dict[str, Any] | None  # {"maintenance_id", "at"} or None = never logged
    counter_source: str
    source: str
    log_command: str
    errors: list[str] = field(default_factory=list)

    @property
    def subject(self) -> str:
        return f"{self.printer}/{self.head}" if self.head else self.printer

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "subject": self.subject}


def _parse(at: str) -> datetime.datetime:
    dt = datetime.datetime.fromisoformat(at.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


def baseline(conn: sqlite3.Connection, rule: Rule, printer: str, head: str | None) -> sqlite3.Row | None:
    """The latest maintenance that resets `rule` on (printer, head): logged for the rule, or
    logged without a rule with the same component and task — on the same head for a head rule,
    printer-wide for a printer rule (work on one head does not service the whole printer)."""
    head_sql, args = (" AND head IS NULL", ()) if rule.scope == "printer" else (" AND head = ?", (head,))
    return conn.execute(
        "SELECT id, at FROM maintenance WHERE printer = ?" + head_sql +  # noqa: S608 — fixed fragments
        " AND (rule_id = ? OR (rule_id IS NULL AND component = ? AND kind = ?)) ORDER BY at DESC, id DESC LIMIT 1",
        (printer, *args, rule.id, rule.component, rule.task),
    ).fetchone()


def _targets(rule: Rule, fleet: Any, only: str | None) -> list[tuple[str, str | None]]:
    out: list[tuple[str, str | None]] = []
    for name in rule.printers:
        if only is not None and name != only:
            continue
        printer = fleet.printers.get(name) if fleet is not None else None
        if printer is None:
            continue  # not in the fleet (lint reports it); nothing to maintain
        if rule.scope == "printer":
            out.append((name, None))
        else:
            out.extend((name, h.label) for h in printer.heads)
    return out


def evaluate(conn: sqlite3.Connection, rules: Rules, fleet: Any, provider: CounterProvider, *,
             printer: str | None = None, now: datetime.datetime | None = None) -> list[DueItem]:
    """Every (rule, printer[, head]) with its status. Read-only (counters are read, never written)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    pending: list[tuple[Rule, str, str | None, sqlite3.Row | None]] = []
    for rule in rules.rules:
        for name, head in _targets(rule, fleet, printer):
            pending.append((rule, name, head, baseline(conn, rule, name, head)))
    # Earliest baseline first (None = from the start): a provider that caches one history read
    # per printer then serves every later baseline from it.
    pending.sort(key=lambda t: (t[1], "" if t[3] is None else str(t[3]["at"])))
    items: list[DueItem] = []
    for rule, name, head, base in pending:
        # Calendar-only rules need no counters: the printer is not asked.
        counted = any(k != "days" for k in rule.every)
        usage = provider.usage(name, head, None if base is None else str(base["at"])) if counted else Usage()
        items.append(_item(rule, name, head, base, usage, now, rules.soon_fraction))
    items.sort(key=lambda i: (i.printer, i.rule, i.head or ""))
    return items


def _item(rule: Rule, printer: str, head: str | None, base: sqlite3.Row | None, usage: Usage,
          now: datetime.datetime, soon_fraction: float) -> DueItem:
    raw: dict[str, float | None] = {}
    for key in rule.every:
        if key == "days":
            raw[key] = None if base is None else (now - _parse(str(base["at"]))).total_seconds() / 86400
        else:
            value = getattr(usage, key)
            raw[key] = None if value is None else float(value)
    # decide on the raw numbers; rounding is for display only (99.96 of 100 h is not due)
    ratio = {k: v / rule.every[k] for k, v in raw.items() if v is not None}
    used = {k: None if v is None else round(v, 2) for k, v in raw.items()}
    progress = {k: round(r, 3) for k, r in ratio.items()}
    reasons = [f"{k} {used[k]:g} ≥ {rule.every[k]:g}" for k, r in ratio.items() if r >= 1]
    if base is None and "days" in rule.every:
        reasons.append("never logged")
    if reasons:
        status = DUE
    elif not ratio:
        status = NO_COUNTER
    elif max(ratio.values()) >= soon_fraction:
        status = SOON
    else:
        status = OK
    unknown = [k for k, v in used.items() if v is None and not (k == "days" and base is None)]
    if unknown:
        why = usage.error or (f"{usage.source} does not count {', '.join(unknown)}" if usage.source else
                              f"no job history of {printer} is readable; the job ledger (#170) will count it")
        reasons.append(f"unknown: {', '.join(unknown)} ({why})")
    log = f"3d printops maint log {printer}{'/' + head if head else ''} --rule {rule.id}"
    return DueItem(
        rule=rule.id, title=rule.title, printer=printer, head=head, component=rule.component, task=rule.task,
        status=status, every=dict(rule.every), used=used, progress=progress, reasons=reasons,
        baseline=None if base is None else {"maintenance_id": int(base["id"]), "at": str(base["at"])},
        counter_source=usage.source, source=rule.source, log_command=log,
        errors=[usage.error] if usage.error else [])


@dataclass
class CheckReport:
    items: list[DueItem]
    announced: list[dict[str, Any]]  # maintenance.due events emitted by this check

    def to_dict(self) -> dict[str, Any]:
        return {"items": [i.to_dict() for i in self.items], "announced": self.announced,
                "due": sum(1 for i in self.items if i.status == DUE)}


def check(conn: sqlite3.Connection, rules: Rules, fleet: Any, provider: CounterProvider, *,
          printer: str | None = None, actor: str = "maintenance", now: datetime.datetime | None = None) -> CheckReport:
    """evaluate() and emit `maintenance.due` for every due item not yet announced for its baseline."""
    items = evaluate(conn, rules, fleet, provider, printer=printer, now=now)
    announced: list[dict[str, Any]] = []
    for item in items:
        if item.status != DUE:
            continue
        base_id = item.baseline["maintenance_id"] if item.baseline else 0
        key = (item.rule, item.printer, item.head or "", base_id)
        # BEGIN IMMEDIATE: this SELECT already holds the write lock, so a concurrent check (the
        # background loop vs POST /v1/maintenance/check) waits and then sees the row — no PK clash.
        with db.transaction(conn):
            if conn.execute("SELECT 1 FROM maintenance_due WHERE rule_id = ? AND printer = ? AND head = ? "
                            "AND baseline_id = ?", key).fetchone():
                continue
            rule = rules.get(item.rule)
            now_base = baseline(conn, rule, item.printer, item.head) if rule is not None else None
            if (int(now_base["id"]) if now_base is not None else 0) != base_id:
                continue  # logged since evaluate() read it: this item is stale, the next check re-decides
            payload = {"rule": item.rule, "title": item.title, "printer": item.printer, "head": item.head,
                       "component": item.component, "task": item.task, "reasons": item.reasons,
                       "used": item.used, "every": item.every, "baseline": item.baseline,
                       "log_command": item.log_command}
            ev = events.emit(conn, "maintenance.due", item.subject, payload, actor)
            conn.execute("INSERT INTO maintenance_due (rule_id, printer, head, baseline_id, due_at, reason, event_id) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)", (*key, ev.at, "; ".join(item.reasons), ev.id))
            announced.append({"event_id": ev.id, **payload})
    return CheckReport(items, announced)


def lint_rules_file(path: Path | None = None, *, fleet: Path | None = None) -> tuple[Path | None, list[Any]]:
    """(file, findings) for `3d lint --printops`: rule `maintenance/rules-schema` (errors), with
    printer names checked against the fleet file (`fleet`, else the default) when it loads.
    No file at the default place is fine (no rules); an explicitly named missing file is not."""
    from printops.fleet import FleetError, load_fleet, read_yaml
    from printops.fleet_lint import ERROR, Finding

    explicit = path is not None or bool(os.environ.get(ENV, "").strip())
    path = path or rules_path()
    if path is None or not path.is_file():
        missing = [Finding("maintenance/rules-schema", ERROR, str(path), "maintenance rules file not found")]
        return path, missing if explicit else []
    try:
        data = read_yaml(path)
    except FleetError as e:
        return path, [Finding("maintenance/rules-schema", ERROR, str(path), e.message)]
    try:
        loaded = load_fleet(fleet)
    except FleetError:
        loaded = None  # fleet/schema reports it; check the rules on their own
    _, problems = validate(data, path, loaded.printers if loaded is not None else None)
    return path, [Finding("maintenance/rules-schema", ERROR, str(path), p) for p in problems]
