"""native_fields.py — the one-off migration from PrintOps' duplicate Spoolman extra fields to
Spoolman's native fields (docs/printops/native-fields.md has the field-by-field audit).

ACCESSED VIA: `3d printops fields migrate-native` (printops.cli.fields) and 3d-api
`GET|POST /v1/fields/migrate-native` (routes_fields.py). Spoolman work plus one lookup the caller
supplies, `location_path` (PrintOps location id -> path): the id is meaningless without the
PrintOps DB, so this module does not open it.

WHAT MOVES (legacy extra → native):
  vendor.spool_tare_g          → vendor.empty_spool_weight
  spool.printops_slot          → spool.location = `slot:<printer>/<head>`
  spool.printops_location_id   → spool.location = that location's path when the text is empty (the
                                 id was the only record of the placement then); dropped when the
                                 text is there (Spoolman's text is the truth) or the id names no
                                 location. A spool that is loaded keeps its slot (it is where the
                                 spool is now; the id only says where it was stored).
  spool.opened_at              → spool.first_used (the earlier of the two)
  spool.tag_uids               → spool tags (POST /spool/{id}/tag)
  filament.nozzle/bed_temp_min/max (kept, they are ranges) → native settings_extruder_temp /
                                 settings_bed_temp = the midpoint, only where the native one is empty

INVARIANTS:
  - Dry run (`apply=False`) only reads; the report is the plan.
  - A value is moved with ONE PATCH that writes the native field and clears the legacy extra
    (`{"extra": {key: null}}`), and the reply is checked: the native value landed and the extra is
    gone, else the item is `failed`. Nothing native is ever overwritten: a different native value is
    a `conflict` (both values reported, nothing written).
  - A legacy field DEFINITION is deleted last, per field, and only when a fresh read shows no item
    still holds a value: Spoolman destroys every stored value together with the definition.
  - Idempotent: after a full apply a second run finds nothing. A SpoolmanError on one item marks that
    item `failed`; the others still run.
"""

from __future__ import annotations

import datetime
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from printops.spool_mirror import SLOT_PREFIX
from printops.spoolman import (
    JSON,
    LEGACY_EXTRA_FIELDS,
    Entity,
    Spoolman,
    SpoolmanError,
    close_legacy_field,
    decode_extra,
    normalize_uid,
)

NATIVE_AGREE_G = 0.05  # native and legacy tare closer than this are the same value
BLOCKING = frozenset({"conflict", "invalid", "failed"})
WRITING = frozenset({"copy", "drop", "adjust", "link", "fill"})
ACTIONS = ("copy", "drop", "adjust", "link", "fill", "conflict", "invalid", "failed")
MAX_LOCATION = 64  # Spoolman spool.location is VARCHAR(64)

LocationPath = Callable[[int], str | None]  # PrintOps location id -> the path Spoolman stores, None if unknown


@dataclass(frozen=True)
class Change:
    """What happens (dry run) or happened (apply) to one item."""

    field: str  # "vendor.spool_tare_g", or "filament.settings_extruder_temp" for a fill
    item_id: int
    item: str  # human label
    action: str  # one of ACTIONS
    legacy: str | None  # the raw stored legacy value
    native: Any  # the native value before the run
    detail: str

    def to_dict(self) -> JSON:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass(frozen=True)
class FieldOutcome:
    """What became of one legacy field definition."""

    field: str
    state: str  # absent | would_delete | kept | deleted | failed
    detail: str

    def to_dict(self) -> JSON:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass(frozen=True)
class MigrationReport:
    apply: bool
    changes: list[Change]
    fields: list[FieldOutcome]

    @property
    def blocked(self) -> bool:
        """Something needs a human: a conflicting / invalid / failed item or a failed field delete."""
        return any(c.action in BLOCKING for c in self.changes) or any(
            f.state == "failed" for f in self.fields
        )

    @property
    def pending(self) -> bool:
        """A dry run that would still write (move a value, delete a definition)."""
        return not self.apply and (
            any(c.action in WRITING for c in self.changes)
            or any(f.state == "would_delete" for f in self.fields)
        )

    def to_dict(self) -> JSON:
        return {
            "apply": self.apply,
            "changes": [c.to_dict() for c in self.changes],
            "summary": {
                a: sum(1 for c in self.changes if c.action == a) for a in ACTIONS
            },
            "fields": [f.to_dict() for f in self.fields],
            "blocked": self.blocked,
            "pending": self.pending,
        }


@dataclass(frozen=True)
class _Plan:
    action: str
    detail: str
    native: Any = None
    body: JSON | None = (
        None  # the PATCH body (without the legacy-clearing extra) when it writes
    )
    tags: tuple[str, ...] = ()  # tag UIDs to link before the PATCH
    verify: Callable[[JSON], str] | None = (
        None  # reply -> failure text ('' = confirmed)
    )


def _stored(item: Mapping[str, Any], key: str) -> tuple[Any, bool]:
    """(decoded legacy value, ok): ok is False when the stored text is not JSON."""
    try:
        return decode_extra((item.get("extra") or {}).get(key)), True
    except ValueError:
        return None, False


def _timestamp(value: Any) -> datetime.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)


def _utc(moment: datetime.datetime) -> str:
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _label(entity: Entity, item: Mapping[str, Any]) -> str:
    if entity == "spool":
        fil = item.get("filament") or {}
        name = " ".join(
            str(p)
            for p in ((fil.get("vendor") or {}).get("name"), fil.get("name"))
            if p
        )
        return f"#{item['id']} {name}".strip()
    return str(item.get("name") or f"#{item['id']}")


# -- planners: one per legacy field -------------------------------------------------------------


def _plan_tare(vendor: Mapping[str, Any], raw: str) -> _Plan:
    value, ok = _stored(vendor, "spool_tare_g")
    native = (
        vendor.get("empty_spool_weight") or None
    )  # 0 is "not set", as in the tare chain
    if (
        not ok
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        return _Plan(
            "invalid",
            f"stored value {raw!r} is not a positive number; fix or clear it in Spoolman",
            native,
        )
    grams = float(value)
    if native is None:

        def verify(reply: JSON) -> str:
            got = reply.get("empty_spool_weight")
            ok_ = got and abs(float(got) - grams) < NATIVE_AGREE_G
            return (
                ""
                if ok_
                else f"Spoolman answered empty_spool_weight {got!r} instead of {grams:g}"
            )

        return _Plan(
            "copy",
            f"empty_spool_weight := {grams:g} g",
            None,
            {"empty_spool_weight": grams},
            verify=verify,
        )
    if abs(float(native) - grams) < NATIVE_AGREE_G:
        return _Plan(
            "drop",
            f"already empty_spool_weight {float(native):g} g; dropping the duplicate extra",
            native,
        )
    return _Plan(
        "conflict",
        f"extra says {grams:g} g, empty_spool_weight says {float(native):g} g; pick one in Spoolman, clear the other, re-run",
        native,
    )


def _copy_location(wanted: str, detail: str) -> _Plan:
    """The plan that writes `wanted` into the native location and checks the reply."""

    def verify(reply: JSON) -> str:
        return (
            ""
            if reply.get("location") == wanted
            else f"Spoolman answered location {reply.get('location')!r}"
        )

    return _Plan("copy", detail, None, {"location": wanted}, verify=verify)


def _plan_slot(spool: Mapping[str, Any], raw: str) -> _Plan:
    value, ok = _stored(spool, "printops_slot")
    location = str(spool.get("location") or "").strip()
    if not ok or not isinstance(value, str):
        return _Plan(
            "invalid",
            f"stored value {raw!r} is not a slot text; clear it in Spoolman",
            location or None,
        )
    slot = value.strip()
    if not slot:
        return _Plan(
            "drop",
            "spool is not loaded; dropping the empty slot value",
            location or None,
        )
    wanted = f"{SLOT_PREFIX}{slot}"
    if location.casefold() == wanted.casefold():
        return _Plan(
            "drop",
            f"location is already {location!r}; dropping the duplicate",
            location,
        )
    if len(wanted) > MAX_LOCATION:
        return _Plan(
            "invalid",
            f"{wanted!r} does not fit Spoolman's {MAX_LOCATION}-character location",
            location or None,
        )
    if location:
        return _Plan(
            "conflict",
            f"slot says {wanted!r}, location says {location!r}; move the spool in Spoolman, re-run",
            location,
        )

    return _copy_location(wanted, f"location := {wanted!r}")


def _plan_location_id(
    spool: Mapping[str, Any], raw: str, location_path: LocationPath
) -> _Plan:
    location = str(spool.get("location") or "").strip()
    if location:
        return _Plan("drop", f"id {raw} duplicates the location text; dropped", location)
    value, ok = _stored(spool, "printops_location_id")
    path = (
        location_path(value)
        if ok and isinstance(value, int) and not isinstance(value, bool)
        else None
    )
    if path is None:
        return _Plan(
            "drop",
            f"id {raw} names no PrintOps location; dropped (the location text is empty, so the spool has no recorded place)",
        )
    if len(path) > MAX_LOCATION:
        return _Plan(
            "invalid",
            f"{path!r} does not fit Spoolman's {MAX_LOCATION}-character location",
        )
    return _copy_location(
        path, f"location := {path!r} (recorded as PrintOps location #{value})"
    )


def _plan_opened(spool: Mapping[str, Any], raw: str) -> _Plan:
    value, ok = _stored(spool, "opened_at")
    opened = _timestamp(value) if ok else None
    first = spool.get("first_used")
    if opened is None:
        return _Plan(
            "invalid",
            f"stored value {raw!r} is not an ISO date; fix or clear it in Spoolman",
            first,
        )
    current = _timestamp(first)
    if current is not None and current <= opened:
        return _Plan(
            "drop",
            f"first_used {first} is not later than opened_at; dropping the duplicate",
            first,
        )
    target = _utc(opened)

    def verify(reply: JSON) -> str:
        got = _timestamp(reply.get("first_used"))
        return (
            ""
            if got is not None and abs((got - opened).total_seconds()) < 1
            else (f"Spoolman answered first_used {reply.get('first_used')!r}")
        )

    if current is None:
        return _Plan(
            "copy",
            f"first_used := {target}",
            None,
            {"first_used": target},
            verify=verify,
        )
    return _Plan(
        "adjust",
        f"opened_at {target} is earlier than first_used {first}; first_used := {target}",
        first,
        {"first_used": target},
        verify=verify,
    )


def _stored_uids(spool: Mapping[str, Any]) -> tuple[str, ...] | None:
    """The normalized UIDs of the legacy `tag_uids` (a text field holding a JSON list); None when
    the stored text is not such a list."""
    value, ok = _stored(spool, "tag_uids")
    if ok and isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if not ok or not isinstance(value, list) or not all(isinstance(u, str) for u in value):
        return None
    return tuple(dict.fromkeys(n for n in map(normalize_uid, value) if n))


def _native_uids(item: Mapping[str, Any]) -> set[str]:
    return {normalize_uid(str(t.get("uid"))) for t in item.get("tags") or []}


def _plan_tags(spool: Mapping[str, Any], raw: str, holders: dict[str, int]) -> _Plan:
    uids = _stored_uids(spool)
    if uids is None:
        return _Plan("invalid", f"stored value {raw!r} is not a JSON list of tag UIDs; fix or clear it in Spoolman")
    me = int(spool["id"])
    held = _native_uids(spool)
    count = len(held) or None
    taken = {u: holders[u] for u in uids if holders.get(u, me) != me}
    if taken:
        who = ", ".join(f"{u} (spool #{sid})" for u, sid in taken.items())
        return _Plan("conflict", f"tag(s) already linked to another spool: {who}; unlink it there, re-run", count)
    if not uids:
        return _Plan("drop", "empty list; dropped", count)
    if set(uids) <= held:
        return _Plan("drop", "tags already linked natively; dropping the duplicate", count)

    def verify(reply: JSON) -> str:
        missing = [u for u in uids if u not in _native_uids(reply)]
        return f"Spoolman does not list tag(s) {', '.join(missing)} after linking" if missing else ""

    holders.update(dict.fromkeys(uids, me))  # a later spool listing the same UID conflicts
    return _Plan("link", f"link tag(s) {', '.join(uids)}", count, {}, uids, verify)


_PLANNERS: dict[tuple[Entity, str], Callable[[Mapping[str, Any], str], _Plan]] = {
    ("vendor", "spool_tare_g"): _plan_tare,
    ("spool", "printops_slot"): _plan_slot,
    ("spool", "opened_at"): _plan_opened,
}


def _link(spoolman: Spoolman, item_id: int, uid: str) -> tuple[str, str] | None:
    """Link one tag; (action, detail) when it cannot be done, None when it is linked."""
    try:
        spoolman.link_tag(item_id, uid)
    except SpoolmanError as e:
        if e.status == 409:
            return "conflict", f"tag {uid} is already linked to another spool ({e.message}); unlink it there, re-run"
        return ("invalid" if e.status == 400 else "failed"), f"tag {uid}: {e.message}"
    return None


def _execute(
    spoolman: Spoolman, entity: Entity, item_id: int, key: str, plan: _Plan
) -> tuple[str, str]:
    """Run a writing plan: link tags, then ONE PATCH (native value + clearing the legacy extra),
    then check the reply. Returns (action, detail)."""
    for uid in plan.tags:
        refusal = _link(spoolman, item_id, uid)
        if refusal:
            return refusal
    body: JSON = {**(plan.body or {}), "extra": {key: None}}
    try:
        reply = spoolman.update(entity, item_id, body)
    except SpoolmanError as e:
        return "failed", f"Spoolman refused the write: {e.message}"
    if key in (reply.get("extra") or {}):
        return "failed", "Spoolman kept the legacy extra value after the PATCH"
    failure = plan.verify(reply) if plan.verify else ""
    return ("failed", failure) if failure else (plan.action, plan.detail)


def _planner(
    key: str,
    entity: Entity,
    listing: list[JSON],
    location_path: LocationPath,
) -> Callable[[Mapping[str, Any], str], _Plan]:
    if key == "printops_location_id":

        def plan_location(item: Mapping[str, Any], raw: str) -> _Plan:
            return _plan_location_id(item, raw, location_path)

        return plan_location
    planner = _PLANNERS.get((entity, key))
    if planner is not None:
        return planner
    # spool.tag_uids: needs who holds which tag now
    holders = {normalize_uid(str(t["uid"])): int(i["id"]) for i in listing for t in i.get("tags") or []}

    def plan_tags(item: Mapping[str, Any], raw: str) -> _Plan:
        return _plan_tags(item, raw, holders)

    return plan_tags


def _migrate_field(
    spoolman: Spoolman,
    entity: Entity,
    key: str,
    *,
    apply: bool,
    location_path: LocationPath,
    planned_location: dict[int, str],
) -> list[Change]:
    """The changes of one legacy field. A dry run writes nothing, so a location an earlier field
    would write (the slot's) is kept in `planned_location` and seen by the later fields as if the
    write had happened: the plan is what an apply does."""
    listing = spoolman.find(entity, {"allow_archived": "true"} if entity == "spool" else None)
    planner = _planner(key, entity, listing, location_path)
    out: list[Change] = []
    for item in listing:
        raw = (item.get("extra") or {}).get(key)
        if raw is None:
            continue
        seen = {**item, "location": planned_location[int(item["id"])]} if int(item["id"]) in planned_location else item
        plan = planner(seen, str(raw))
        if not apply and plan.action == "copy" and "location" in (plan.body or {}):
            planned_location[int(item["id"])] = str((plan.body or {})["location"])
        action, detail = plan.action, plan.detail
        if apply and action in WRITING:
            action, detail = _execute(spoolman, entity, int(item["id"]), key, plan)
        out.append(
            Change(
                f"{entity}.{key}",
                int(item["id"]),
                _label(entity, item),
                action,
                str(raw),
                plan.native,
                detail,
            )
        )
    return out


# -- native nominal temperatures ----------------------------------------------------------------

TEMP_PAIRS = (
    ("nozzle_temp_min", "nozzle_temp_max", "settings_extruder_temp"),
    ("bed_temp_min", "bed_temp_max", "settings_bed_temp"),
)


def _int_extra(filament: Mapping[str, Any], key: str) -> int | None:
    value, ok = _stored(filament, key)
    return (
        value if ok and isinstance(value, int) and not isinstance(value, bool) else None
    )


def _write_fill(spoolman: Spoolman, fil_id: int, native_key: str, mid: int, detail: str) -> tuple[str, str]:
    try:
        reply = spoolman.update("filament", fil_id, {native_key: mid})
    except SpoolmanError as e:
        return "failed", f"Spoolman refused the write: {e.message}"
    if reply.get(native_key) != mid:
        return "failed", f"Spoolman answered {native_key} {reply.get(native_key)!r}"
    return "fill", detail


def _fill_temps(spoolman: Spoolman, *, apply: bool) -> list[Change]:
    """Where a filament has both ends of a temperature range (kept: the gates need ranges) and no
    native single value, set the native one to the midpoint so Spoolman's UI shows it."""
    out: list[Change] = []
    for fil in spoolman.find("filament"):
        for low_key, high_key, native_key in TEMP_PAIRS:
            low, high = _int_extra(fil, low_key), _int_extra(fil, high_key)
            if low is None or high is None or low > high or fil.get(native_key) is not None:
                continue
            mid = (low + high + 1) // 2
            action, detail = "fill", f"{native_key} := {mid} (midpoint of {low}..{high})"
            if apply:
                action, detail = _write_fill(spoolman, int(fil["id"]), native_key, mid, detail)
            out.append(
                Change(f"filament.{native_key}", int(fil["id"]), _label("filament", fil), action, f"{low}..{high}", None, detail)
            )
    return out


def migrate_native(
    spoolman: Spoolman, *, apply: bool, location_path: LocationPath
) -> MigrationReport:
    """Move every legacy extra field's values to Spoolman's native fields (see the module doc).
    Dry run unless `apply`; the report lists every item and the fate of every legacy definition.
    `location_path` turns a recorded PrintOps location id into the path Spoolman stores."""
    legacy_entities: tuple[Entity, ...] = ("vendor", "spool")
    defined = {
        (e, str(f["key"])) for e in legacy_entities for f in spoolman.list_fields(e)
    }
    changes: list[Change] = []
    outcomes: list[FieldOutcome] = []
    planned_location: dict[int, str] = {}
    for entity, key in LEGACY_EXTRA_FIELDS:
        moved = _migrate_field(
            spoolman,
            entity,
            key,
            apply=apply,
            location_path=location_path,
            planned_location=planned_location,
        )
        changes.extend(moved)
        label = f"{entity}.{key}"
        if (entity, key) not in defined:
            outcomes.append(
                FieldOutcome(label, "absent", "the field definition does not exist")
            )
            continue
        blocking = sum(1 for c in moved if c.action in BLOCKING)
        state, detail = close_legacy_field(
            spoolman, entity, key, blocking=blocking, apply=apply
        )
        outcomes.append(FieldOutcome(label, state, detail))
    changes.extend(_fill_temps(spoolman, apply=apply))
    return MigrationReport(apply, changes, outcomes)
