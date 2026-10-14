"""spoolman.py — the Spoolman v0.27 REST boundary: Protocol, urllib client, extra-field registry.

ACCESSED VIA: 3d-api (the single Spoolman writer, plan §5.1) and its slices. Tests use
`tests/printops_fakes.py::FakeSpoolman`, which implements the same Protocol in memory and can
also serve it over HTTP so `HttpSpoolman` itself is exercised.

WHAT LIVES IN SPOOLMAN (decision D2): vendor / filament / spool identity, remaining weight, lot,
price, native `location` (the PrintOps location path, ≤64 chars) and the extra fields listed in
`EXTRA_FIELDS`. Everything else is PrintOps SQLite.

INVARIANTS:
  - Stdlib only (urllib); no new runtime deps.
  - Extra-field VALUES are JSON-encoded strings on the wire (`{"dry_state": "\"ok\""}`); use
    `encode_extra` / `decode_extra`, never hand-quote.
  - `ensure_extra_fields` is idempotent: it creates missing fields, re-posts ones whose
    name/unit/order/choices drifted, and refuses (SpoolmanError) a field whose TYPE differs —
    Spoolman cannot change a field type, and silently using a differently-typed field would
    corrupt data. Adding a field = append to EXTRA_FIELDS (contract: append-only, via PR).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

Entity = Literal["vendor", "filament", "spool"]
ENTITIES: tuple[Entity, ...] = ("vendor", "filament", "spool")
FieldType = Literal["text", "integer", "integer_range", "float", "float_range", "datetime", "boolean", "choice"]

JSON = dict[str, Any]


class SpoolmanError(RuntimeError):
    """Spoolman answered with an error status (`status` > 0) or could not be reached (`status` 0)."""

    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class ExtraFieldConflict(SpoolmanError):
    """A field PrintOps needs exists in Spoolman with another type; retrying cannot fix it."""


class Spoolman(Protocol):
    """The subset of the Spoolman v0.27 API PrintOps uses (paths relative to /api/v1)."""

    def info(self) -> JSON: ...                                   # GET /info
    def list_fields(self, entity: Entity) -> list[JSON]: ...      # GET /field/{entity}
    def set_field(self, entity: Entity, key: str, params: Mapping[str, Any]) -> list[JSON]: ...  # POST /field/{entity}/{key}
    def find(self, entity: Entity, params: Mapping[str, str] | None = None) -> list[JSON]: ...    # GET /{entity}
    def get(self, entity: Entity, item_id: int) -> JSON: ...      # GET /{entity}/{id}
    def create(self, entity: Entity, data: Mapping[str, Any]) -> JSON: ...   # POST /{entity}
    def update(self, entity: Entity, item_id: int, data: Mapping[str, Any]) -> JSON: ...  # PATCH /{entity}/{id}
    def delete(self, entity: Entity, item_id: int) -> None: ...   # DELETE /{entity}/{id}
    def use(self, spool_id: int, *, use_weight: float | None = None, use_length: float | None = None) -> JSON: ...  # PUT /spool/{id}/use
    def measure(self, spool_id: int, weight: float) -> JSON: ...  # PUT /spool/{id}/measure (gross g)


def encode_extra(value: Any) -> str:
    """Python value -> the JSON string Spoolman stores in `extra`."""
    return json.dumps(value)


def decode_extra(raw: str | None) -> Any:
    return None if raw is None else json.loads(raw)


class HttpSpoolman:
    """Spoolman over HTTP. `base_url` is the server root, e.g. http://spoolman:8000."""

    def __init__(self, base_url: str, *, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _call(self, method: str, path: str, body: Mapping[str, Any] | None = None,
              params: Mapping[str, str] | None = None) -> Any:
        url = f"{self.base_url}/api/v1{path}"
        if params:
            url += "?" + urllib.parse.urlencode(dict(params))
        data = None if body is None else json.dumps(dict(body)).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 — operator-configured URL
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise SpoolmanError(f"Spoolman {method} {path}: HTTP {e.code}: {_error_text(e.read())}", status=e.code) from e
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise SpoolmanError(f"Spoolman at {self.base_url} is unreachable: {reason}") from e
        try:
            return json.loads(raw) if raw else None
        except ValueError as e:
            # e.g. a proxy's HTML error page: report it as a Spoolman failure, not a crash
            raise SpoolmanError(f"Spoolman {method} {path}: reply is not JSON: {raw[:120]!r}", status=502) from e

    def info(self) -> JSON:
        return dict(self._call("GET", "/info"))

    def list_fields(self, entity: Entity) -> list[JSON]:
        return list(self._call("GET", f"/field/{entity}"))

    def set_field(self, entity: Entity, key: str, params: Mapping[str, Any]) -> list[JSON]:
        return list(self._call("POST", f"/field/{entity}/{urllib.parse.quote(key)}", params))

    def find(self, entity: Entity, params: Mapping[str, str] | None = None) -> list[JSON]:
        return list(self._call("GET", f"/{entity}", params=params))

    def get(self, entity: Entity, item_id: int) -> JSON:
        return dict(self._call("GET", f"/{entity}/{int(item_id)}"))

    def create(self, entity: Entity, data: Mapping[str, Any]) -> JSON:
        return dict(self._call("POST", f"/{entity}", data))

    def update(self, entity: Entity, item_id: int, data: Mapping[str, Any]) -> JSON:
        return dict(self._call("PATCH", f"/{entity}/{int(item_id)}", data))

    def delete(self, entity: Entity, item_id: int) -> None:
        self._call("DELETE", f"/{entity}/{int(item_id)}")

    def use(self, spool_id: int, *, use_weight: float | None = None, use_length: float | None = None) -> JSON:
        body = {k: v for k, v in (("use_weight", use_weight), ("use_length", use_length)) if v is not None}
        return dict(self._call("PUT", f"/spool/{int(spool_id)}/use", body))

    def measure(self, spool_id: int, weight: float) -> JSON:
        return dict(self._call("PUT", f"/spool/{int(spool_id)}/measure", {"weight": weight}))


def _error_text(raw: bytes) -> str:
    try:
        body = json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", "replace")[:300]
    if isinstance(body, dict):
        for key in ("message", "detail"):
            if key in body:
                return str(body[key])
    return str(body)[:300]


@dataclass(frozen=True)
class ExtraField:
    entity: Entity
    key: str
    name: str
    field_type: FieldType
    unit: str | None = None
    choices: tuple[str, ...] | None = None

    def params(self, order: int) -> JSON:
        """Body for POST /field/{entity}/{key} (ExtraFieldParameters)."""
        body: JSON = {"name": self.name, "order": order, "field_type": self.field_type}
        if self.unit is not None:
            body["unit"] = self.unit
        if self.choices is not None:
            body["choices"] = list(self.choices)
            body["multi_choice"] = False
        return body


# The PrintOps extra fields (contract §"Spoolman extra fields"). APPEND-ONLY: never rename,
# retype or reorder an entry — Spoolman cannot change a field's type, and `order` is derived
# from the position here.
EXTRA_FIELDS: tuple[ExtraField, ...] = (
    ExtraField("spool", "printops_slot", "PrintOps slot", "text"),
    ExtraField("spool", "opened_at", "Opened at", "datetime"),
    ExtraField("spool", "last_dried_at", "Last dried at", "datetime"),
    ExtraField("spool", "dry_state", "Dry state", "choice", choices=("fresh", "ok", "due", "wet")),
    ExtraField("spool", "remaining_confidence_g", "Remaining ± (confidence)", "float", unit="g"),
    ExtraField("spool", "tag_uids", "Tag UIDs (JSON list)", "text"),
    ExtraField("spool", "printops_location_id", "PrintOps location id", "integer"),
    ExtraField("filament", "nozzle_temp_min", "Nozzle temp min", "integer", unit="°C"),
    ExtraField("filament", "nozzle_temp_max", "Nozzle temp max", "integer", unit="°C"),
    ExtraField("filament", "bed_temp_min", "Bed temp min", "integer", unit="°C"),
    ExtraField("filament", "bed_temp_max", "Bed temp max", "integer", unit="°C"),
    ExtraField("filament", "abrasive", "Abrasive", "boolean"),
    ExtraField("filament", "hygroscopic_class", "Hygroscopic class", "choice",
               choices=("low", "medium", "high", "extreme")),
    ExtraField("filament", "drying_temp_c", "Drying temp", "integer", unit="°C"),
    ExtraField("filament", "drying_hours", "Drying time", "float", unit="h"),
    ExtraField("filament", "measured_hex", "Measured colour (hex)", "text"),
    ExtraField("filament", "max_volumetric_mm3_s", "Max volumetric speed", "float", unit="mm³/s"),
    ExtraField("vendor", "spool_tare_g", "Empty spool weight (tare)", "float", unit="g"),
    ExtraField("vendor", "spool_kind", "Spool kind", "choice", choices=("cardboard", "plastic", "refill")),
)

# PrintOps fields sort after any fields the user added by hand in the Spoolman UI.
ORDER_BASE = 100


@dataclass
class FieldsReport:
    created: list[str]
    updated: list[str]
    unchanged: list[str]

    def to_dict(self) -> JSON:
        return {"created": self.created, "updated": self.updated, "unchanged": self.unchanged}


def _differs(existing: Mapping[str, Any], wanted: Mapping[str, Any]) -> bool:
    for key in ("name", "order", "unit"):
        if existing.get(key) != wanted.get(key):
            return True
    if "choices" in wanted:
        have = list(existing.get("choices") or [])
        if any(c not in have for c in wanted["choices"]):
            return True
    return False


def _shape(field: Mapping[str, Any]) -> str:
    shape = repr(field.get("field_type"))
    return shape + (" multi_choice" if field.get("multi_choice") else "")


def _compatible(have: Mapping[str, Any], wanted: ExtraField) -> bool:
    """Same type and, for choices, same multi_choice — the two things Spoolman cannot change."""
    if have.get("field_type") != wanted.field_type:
        return False
    return wanted.field_type != "choice" or not have.get("multi_choice")


def _registered(have: Mapping[str, Any] | None, wanted: ExtraField) -> bool:
    """Usable as PrintOps needs it: present, compatible, and offering every PrintOps choice."""
    if have is None or not _compatible(have, wanted):
        return False
    return all(c in (have.get("choices") or []) for c in wanted.choices or ())


def ensure_extra_fields(spoolman: Spoolman, fields: tuple[ExtraField, ...] = EXTRA_FIELDS) -> FieldsReport:
    """Register every PrintOps extra field in Spoolman (idempotent; see module invariants)."""
    report = FieldsReport(created=[], updated=[], unchanged=[])
    for entity in ENTITIES:
        wanted = [f for f in fields if f.entity == entity]
        if not wanted:
            continue
        existing = {str(f["key"]): f for f in spoolman.list_fields(entity)}
        for f in wanted:
            label = f"{entity}.{f.key}"
            order = ORDER_BASE + fields.index(f)
            params = f.params(order)
            have = existing.get(f.key)
            if have is not None and not _compatible(have, f):
                raise ExtraFieldConflict(
                    f"Spoolman extra field {label} exists as {_shape(have)}, PrintOps needs "
                    f"{_shape(f.params(0))}; Spoolman cannot change a field type or multi_choice — "
                    "rename or delete the conflicting field in Spoolman (Settings → Extra fields) and re-run"
                )
            if have is not None and f.choices is not None:
                # Spoolman refuses to drop choices: keep the user's extra ones, add ours.
                params["choices"] = list(dict.fromkeys([*(have.get("choices") or []), *f.choices]))
            if have is None:
                spoolman.set_field(entity, f.key, params)
                report.created.append(label)
            elif _differs(have, params):
                spoolman.set_field(entity, f.key, params)
                report.updated.append(label)
            else:
                report.unchanged.append(label)
    return report


def missing_extra_fields(spoolman: Spoolman, fields: tuple[ExtraField, ...] = EXTRA_FIELDS) -> list[str]:
    """`entity.key` of every PrintOps field missing in Spoolman or registered incompatibly."""
    missing: list[str] = []
    for entity in ENTITIES:
        have = {str(f["key"]): f for f in spoolman.list_fields(entity)}
        missing.extend(
            f"{entity}.{f.key}" for f in fields if f.entity == entity and not _registered(have.get(f.key), f)
        )
    return missing
