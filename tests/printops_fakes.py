"""printops_fakes.py — FakeSpoolman: an in-memory Spoolman v0.27 for PrintOps tests.

It implements `printops.spoolman.Spoolman` directly (fast unit tests) and can also serve the
same state over real HTTP (`with fake.serve() as url:`) so `HttpSpoolman`, the 3d-api
container wiring and `bin/3d` e2e runs are exercised against the wire format.

Faithful to Spoolman v0.27.0 (checked against its source, tag v0.27.0):
  - vendor / filament / spool CRUD; responses nest vendor in filament and filament in spool,
    omit None fields (response_model_exclude_none) and carry `extra` + `tags`;
  - spool create: spool_weight falls back to filament.spool_weight, initial_weight to
    filament.weight, `remaining_weight` → used_weight = initial − remaining; filament create
    copies vendor.empty_spool_weight into spool_weight;
  - computed remaining_weight / remaining_length / used_length (density + diameter);
  - `PUT /spool/{id}/use` {use_weight | use_length} (exactly one; used_weight never < 0;
    first_used/last_used set), `PUT /spool/{id}/measure` {weight: gross g} with Spoolman's
    tare chain (spool → filament → vendor) and reset-on-overweight;
  - extra fields: registry per entity via `POST /field/{entity}/{key}` (type and multi_choice
    immutable, choices only grow, choice needs choices + multi_choice); values are
    JSON-encoded strings validated against the registry; PATCH merges `extra`, null deletes;
  - `location` ≤ 64 chars; archived spools hidden from lists unless allow_archived=true;
  - errors: 404/400 `{"message": ...}`, 422 `{"detail": ...}`.
"""

from __future__ import annotations

import contextlib
import copy
import datetime
import json
import math
import threading
from collections.abc import Iterator, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, NoReturn
from urllib.parse import parse_qs, urlsplit

from printops.spoolman import ENTITIES, Entity, SpoolmanError, normalize_uid

JSON = dict[str, Any]
FIELD_TYPES = {
    "text",
    "integer",
    "integer_range",
    "float",
    "float_range",
    "datetime",
    "boolean",
    "choice",
}
VERSION = "0.27.0"

_VENDOR_KEYS = ("name", "comment", "empty_spool_weight", "external_id")
_FILAMENT_KEYS = (
    "name",
    "vendor_id",
    "material",
    "price",
    "density",
    "diameter",
    "weight",
    "spool_weight",
    "article_number",
    "comment",
    "settings_extruder_temp",
    "settings_bed_temp",
    "color_hex",
    "multi_color_hexes",
    "multi_color_direction",
    "external_id",
)
_SPOOL_KEYS = (
    "first_used",
    "last_used",
    "filament_id",
    "price",
    "initial_weight",
    "spool_weight",
    "used_weight",
    "location",
    "lot_nr",
    "comment",
    "archived",
)
_KEYS: dict[str, tuple[str, ...]] = {
    "vendor": _VENDOR_KEYS,
    "filament": _FILAMENT_KEYS,
    "spool": _SPOOL_KEYS,
}


def _now() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0, tzinfo=None)
        .isoformat()
        + "Z"
    )


def _err(status: int, message: str) -> SpoolmanError:
    return SpoolmanError(message, status=status)


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _validate_choice(field: Mapping[str, Any], data: Any) -> None:
    choices = field.get("choices") or []
    if field.get("multi_choice"):
        if not isinstance(data, list) or not all(
            isinstance(x, str) and x in choices for x in data
        ):
            raise ValueError("Value list contains invalid choices.")
    elif not isinstance(data, str) or data not in choices:
        raise ValueError("Value is not a valid choice.")


def _validate_value(field: Mapping[str, Any], raw: str) -> None:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        raise ValueError("Value is not valid JSON.") from None
    t = field["field_type"]
    if t == "choice":
        _validate_choice(field, data)
        return
    ok = {
        "text": isinstance(data, str),
        "datetime": isinstance(data, str),
        "integer": _is_int(data),
        "float": _is_num(data),
        "boolean": isinstance(data, bool),
        "integer_range": isinstance(data, list)
        and len(data) == 2
        and all(x is None or _is_int(x) for x in data),
        "float_range": isinstance(data, list)
        and len(data) == 2
        and all(x is None or _is_num(x) for x in data),
    }
    if not ok[t]:
        raise ValueError(f"Value is not a valid {t}.")


def _filter_spools(rows: list[JSON], p: Mapping[str, str]) -> list[JSON]:
    if p.get("allow_archived", "false").lower() not in ("true", "1"):
        rows = [r for r in rows if not r["archived"]]
    if "location" in p:
        rows = [r for r in rows if r.get("location") == p["location"]]
    rows = _filter_spools_by_filament(rows, p)
    return rows


def _filter_spools_by_filament(rows: list[JSON], p: Mapping[str, str]) -> list[JSON]:
    fid = p.get("filament.id") or p.get("filament_id")
    if fid:
        ids = {int(x) for x in fid.split(",")}
        rows = [r for r in rows if r["filament"]["id"] in ids]
    if "filament.material" in p:
        material = p["filament.material"].lower()
        rows = [
            r for r in rows if r["filament"].get("material", "").lower() == material
        ]
    return rows


def _filter_filaments(rows: list[JSON], p: Mapping[str, str]) -> list[JSON]:
    if "material" in p:
        rows = [
            r for r in rows if r.get("material", "").lower() == p["material"].lower()
        ]
    vid = p.get("vendor.id") or p.get("vendor_id")
    if vid:
        rows = [r for r in rows if r.get("vendor", {}).get("id") == int(vid)]
    return rows


class FakeSpoolman:
    """In-memory Spoolman. `down = True` makes every call fail as if unreachable."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.items: dict[str, dict[int, JSON]] = {e: {} for e in ENTITIES}
        self.fields: dict[str, list[JSON]] = {e: [] for e in ENTITIES}
        self._next: dict[str, int] = dict.fromkeys(ENTITIES, 1)
        self._tags: dict[str, int] = {}  # normalized tag UID -> spool id (a UID identifies ONE spool)
        self.calls: list[tuple[str, str]] = []
        self.down = False
        self.url: str | None = None

    # -- helpers --------------------------------------------------------------------------
    def _enter(self, method: str, path: str) -> None:
        if self.down:
            raise SpoolmanError("Spoolman at fake is unreachable: connection refused")
        self.calls.append((method, path))

    def writes(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if c[0] != "GET"]

    def _row(self, entity: str, item_id: int) -> JSON:
        row = self.items[entity].get(int(item_id))
        if row is None:
            raise _err(404, f"No {entity} with ID {item_id} found.")
        return row

    def _check_extra(self, entity: str, extra: Mapping[str, Any] | None) -> None:
        if not extra:
            return
        registry = {f["key"]: f for f in self.fields[entity]}
        for key, value in extra.items():
            if key not in registry:
                raise _err(400, f"Unknown extra field {key}.")
            if value is None:
                continue
            if not isinstance(value, str):
                raise _err(422, f"extra.{key}: Input should be a valid string")
            try:
                _validate_value(registry[key], value)
            except ValueError as e:
                raise _err(400, f"Invalid extra field for key {key}: {e}") from None

    def _render(self, entity: str, row: JSON) -> JSON:
        out = {
            k: v
            for k, v in row.items()
            if v is not None and k not in ("vendor_id", "filament_id")
        }
        out["extra"] = dict(row.get("extra") or {})
        if entity == "filament":
            out["tags"] = []
            if row.get("vendor_id") is not None:
                out["vendor"] = self._render(
                    "vendor", self.items["vendor"][row["vendor_id"]]
                )
        if entity == "spool":
            self._render_spool(out, row)
        return out

    def _render_spool(self, out: JSON, row: JSON) -> None:
        out["tags"] = [{"uid": u, "added": _now()} for u, sid in sorted(self._tags.items()) if sid == row["id"]]
        fil = self.items["filament"][row["filament_id"]]
        out["filament"] = self._render("filament", fil)
        used = float(row["used_weight"])
        remaining: float | None = None
        if row.get("initial_weight") is not None:
            remaining = max(row["initial_weight"] - used, 0)
        elif fil.get("weight") is not None:
            remaining = max(fil["weight"] - used, 0)
        out["used_length"] = _length(used, fil)
        if remaining is not None:
            out["remaining_weight"] = remaining
            out["remaining_length"] = _length(remaining, fil)
        out["archived"] = bool(row.get("archived"))

    def _validate(
        self, entity: str, data: Mapping[str, Any], *, creating: bool
    ) -> None:
        unknown = [
            k for k in data if k not in (*_KEYS[entity], "extra", "remaining_weight")
        ]
        if unknown:
            raise _err(422, f"unknown field(s): {', '.join(unknown)}")
        if entity == "vendor" and (creating or "name" in data):
            name = data.get("name")
            if not isinstance(name, str) or not 1 <= len(name) <= 64:
                raise _err(422, "name: String should have at most 64 characters")
        if entity == "filament":
            self._validate_filament(data, creating=creating)
        if entity == "spool":
            self._validate_spool(data, creating=creating)
        self._check_extra(entity, data.get("extra"))

    def _validate_filament(self, data: Mapping[str, Any], *, creating: bool) -> None:
        for k in ("density", "diameter"):
            if (creating or k in data) and not (
                isinstance(data.get(k), (int, float)) and data[k] > 0
            ):
                raise _err(422, f"{k}: Input should be greater than 0")
        if data.get("vendor_id") is not None:
            self._row("vendor", data["vendor_id"])

    def _validate_spool(self, data: Mapping[str, Any], *, creating: bool) -> None:
        if creating or "filament_id" in data:
            if data.get("filament_id") is None:
                raise _err(422, "filament_id: Field required")
            self._row("filament", data["filament_id"])
        loc = data.get("location")
        if loc is not None and len(loc) > 64:
            raise _err(422, "location: String should have at most 64 characters")
        for k in (
            "initial_weight",
            "spool_weight",
            "remaining_weight",
            "used_weight",
            "price",
        ):
            if data.get(k) is not None and data[k] < 0:
                raise _err(422, f"{k}: Input should be greater than or equal to 0")

    # -- Spoolman Protocol ----------------------------------------------------------------
    def info(self) -> JSON:
        self._enter("GET", "/info")
        return {
            "version": VERSION,
            "debug_mode": False,
            "automatic_backups": True,
            "db_type": "sqlite",
            "data_dir": "/home/app/.local/share/spoolman",
            "logs_dir": "/home/app/.local/share/spoolman",
            "backups_dir": "/home/app/.local/share/spoolman/backups",
        }

    def list_fields(self, entity: Entity) -> list[JSON]:
        self._enter("GET", f"/field/{entity}")
        with self._lock:
            return copy.deepcopy(self.fields[entity])

    def set_field(
        self, entity: Entity, key: str, params: Mapping[str, Any]
    ) -> list[JSON]:
        self._enter("POST", f"/field/{entity}/{key}")
        with self._lock:
            self._check_field_request(entity, key, params)
            new = {"key": key, "entity_type": entity, "order": 0, **dict(params)}
            self._check_field_choices(new)
            if new.get("default_value") is not None:
                try:
                    _validate_value(new, new["default_value"])
                except ValueError as e:
                    return self._bad(f"Default value is not valid: {e}")
            return self._upsert_field(entity, key, new)

    def _check_field_request(
        self, entity: str, key: str, params: Mapping[str, Any]
    ) -> None:
        if entity not in ENTITIES:
            raise _err(422, f"entity_type: Input should be {', '.join(ENTITIES)}")
        if (
            not key
            or len(key) > 64
            or not all(c.islower() or c.isdigit() or c == "_" for c in key)
        ):
            raise _err(422, "key: String should match pattern '^[a-z0-9_]+$'")
        if params.get("field_type") not in FIELD_TYPES or not params.get("name"):
            raise _err(422, "field_type/name: invalid ExtraFieldParameters")

    def _upsert_field(
        self, entity: str, key: str, new: Mapping[str, Any]
    ) -> list[JSON]:
        existing = self.fields[entity]
        old = next((f for f in existing if f["key"] == key), None)
        stored = {k: v for k, v in new.items() if v is not None}
        if old is not None:
            self._check_field_redefinition(old, new)
            existing[existing.index(old)] = stored
        else:
            existing.append(stored)
        return copy.deepcopy(existing)

    def _check_field_choices(self, new: Mapping[str, Any]) -> None:
        if new["field_type"] == "choice":
            if new.get("choices") is None:
                self._bad("Choices must be set for field type choice.")
            if new.get("multi_choice") is None:
                self._bad("Multi choice must be set for field type choice.")
        elif new.get("choices") is not None or new.get("multi_choice") is not None:
            self._bad("Choices must not be set for field type other than choice.")

    def _check_field_redefinition(
        self, old: Mapping[str, Any], new: Mapping[str, Any]
    ) -> None:
        if old["field_type"] != new["field_type"]:
            self._bad("Field type cannot be changed.")
        if new["field_type"] == "choice":
            if old.get("multi_choice") != new.get("multi_choice"):
                self._bad("Multi choice cannot be changed.")
            if any(c not in new["choices"] for c in old.get("choices") or []):
                self._bad("Cannot remove existing choices.")

    def _bad(self, message: str) -> NoReturn:
        raise _err(400, message)

    def find(
        self, entity: Entity, params: Mapping[str, str] | None = None
    ) -> list[JSON]:
        self._enter("GET", f"/{entity}")
        p = dict(params or {})
        with self._lock:
            rows = [
                self._render(entity, r) for _, r in sorted(self.items[entity].items())
            ]
        if entity == "spool":
            rows = _filter_spools(rows, p)
        if entity == "filament":
            rows = _filter_filaments(rows, p)
        if entity == "vendor" and "name" in p:
            rows = [r for r in rows if r["name"] == p["name"]]
        return rows

    def get(self, entity: Entity, item_id: int) -> JSON:
        self._enter("GET", f"/{entity}/{item_id}")
        with self._lock:
            return self._render(entity, self._row(entity, item_id))

    def create(self, entity: Entity, data: Mapping[str, Any]) -> JSON:
        self._enter("POST", f"/{entity}")
        with self._lock:
            self._validate(entity, data, creating=True)
            row: JSON = {k: data.get(k) for k in _KEYS[entity]}
            row["id"] = self._next[entity]
            row["registered"] = _now()
            row["extra"] = {
                k: v for k, v in (data.get("extra") or {}).items() if v is not None
            }
            if (
                entity == "filament"
                and row["spool_weight"] is None
                and row["vendor_id"] is not None
            ):
                row["spool_weight"] = self.items["vendor"][row["vendor_id"]].get(
                    "empty_spool_weight"
                )
            if entity == "spool":
                self._default_spool_row(row, data)
            self.items[entity][row["id"]] = row
            self._next[entity] += 1
            return self._render(entity, row)

    def _default_spool_row(self, row: JSON, data: Mapping[str, Any]) -> None:
        fil = self.items["filament"][row["filament_id"]]
        if row["spool_weight"] is None:
            row["spool_weight"] = fil.get("spool_weight")
        if row["initial_weight"] is None:
            row["initial_weight"] = fil.get("weight")
        if row["used_weight"] is None:
            remaining = data.get("remaining_weight")
            if remaining is None:
                row["used_weight"] = 0.0
            else:
                if not row["initial_weight"]:
                    raise _err(
                        400,
                        "remaining_weight can only be used if the initial_weight is "
                        "defined or the filament has a weight set.",
                    )
                row["used_weight"] = max(row["initial_weight"] - remaining, 0)
        row["archived"] = bool(row["archived"])

    def update(self, entity: Entity, item_id: int, data: Mapping[str, Any]) -> JSON:
        self._enter("PATCH", f"/{entity}/{item_id}")
        with self._lock:
            row = self._row(entity, item_id)
            self._validate(entity, data, creating=False)
            for k, v in data.items():
                if k == "extra":
                    merged = {**row["extra"], **v}
                    row["extra"] = {
                        ek: ev for ek, ev in merged.items() if ev is not None
                    }
                elif k == "remaining_weight":
                    if row.get("initial_weight") is None:
                        raise _err(
                            400,
                            "remaining_weight can only be used if initial_weight is set.",
                        )
                    row["used_weight"] = max(row["initial_weight"] - v, 0)
                else:
                    row[k] = v
            return self._render(entity, row)

    def delete(self, entity: Entity, item_id: int) -> None:
        self._enter("DELETE", f"/{entity}/{item_id}")
        with self._lock:
            self._row(entity, item_id)
            self._check_deletable(entity, item_id)
            del self.items[entity][int(item_id)]
            if entity == "spool":
                self._tags = {u: sid for u, sid in self._tags.items() if sid != int(item_id)}

    def _check_deletable(self, entity: Entity, item_id: int) -> None:
        if entity == "vendor":
            # Spoolman nulls the vendor of its filaments rather than refusing.
            for f in self.items["filament"].values():
                if f.get("vendor_id") == item_id:
                    f["vendor_id"] = None
        if entity == "filament" and any(
            s["filament_id"] == item_id for s in self.items["spool"].values()
        ):
            raise _err(
                403,
                "Failed to delete filament, see server logs for more information.",
            )

    def _consume(self, row: JSON, weight: float) -> None:
        row["used_weight"] = (
            row["used_weight"] + weight if row["used_weight"] + weight >= 0 else 0.0
        )
        stamp = _now()
        if row.get("first_used") is None:
            row["first_used"] = stamp
        row["last_used"] = stamp

    def use(
        self,
        spool_id: int,
        *,
        use_weight: float | None = None,
        use_length: float | None = None,
    ) -> JSON:
        self._enter("PUT", f"/spool/{spool_id}/use")
        with self._lock:
            if use_weight is not None and use_length is not None:
                raise _err(400, "Only specify either use_weight or use_length.")
            if use_weight is None and use_length is None:
                raise _err(400, "Either use_weight or use_length must be specified.")
            row = self._row("spool", spool_id)
            if use_weight is None:
                fil = self.items["filament"][row["filament_id"]]
                use_weight = (
                    fil["density"]
                    * (float(use_length or 0) * math.pi * (fil["diameter"] / 2) ** 2)
                    / 1000
                )
            self._consume(row, use_weight)
            return self._render("spool", row)

    def measure(self, spool_id: int, weight: float) -> JSON:
        self._enter("PUT", f"/spool/{spool_id}/measure")
        with self._lock:
            row = self.items["spool"].get(int(spool_id))
            if row is None:
                raise _err(400, "Spool not found.")
            initial, tare = self._measure_baseline(row)
            if not initial:
                raise _err(400, "Initial weight is not set.")
            tare = tare or 0
            initial_gross = initial + tare
            if weight > initial_gross:
                row["initial_weight"] = weight - tare
                row["used_weight"] = 0.0
                return self._render("spool", row)
            current_use = initial_gross - row["used_weight"]
            to_use = current_use - weight
            if initial_gross - to_use < tare:
                to_use = current_use - tare
            self._consume(row, to_use)
            return self._render("spool", row)

    def _measure_baseline(self, row: JSON) -> tuple[Any, Any]:
        """Spool's initial/tare weights, falling back to its filament (and vendor) when unset."""
        initial, tare = row.get("initial_weight"), row.get("spool_weight")
        if initial and tare:
            return initial, tare
        fil = self.items["filament"][row["filament_id"]]
        vendor = (
            self.items["vendor"].get(fil["vendor_id"]) if fil.get("vendor_id") else None
        )
        if not tare:
            tare = (
                fil.get("spool_weight")
                if fil.get("spool_weight") is not None
                else (vendor.get("empty_spool_weight") if vendor else None)
            )
        if not initial:
            initial = fil.get("weight") or 0
        return initial, tare

    def delete_field(self, entity: Entity, key: str) -> list[JSON]:
        """DELETE /field/{entity}/{key}: like the real one, also destroys every stored value."""
        self._enter("DELETE", f"/field/{entity}/{key}")
        with self._lock:
            existing = self.fields[entity]
            old = next((f for f in existing if f["key"] == key), None)
            if old is None:
                raise _err(404, f"Extra field with key {key} does not exist for entity type {entity}")
            existing.remove(old)
            for row in self.items[entity].values():
                row["extra"].pop(key, None)
            return copy.deepcopy(existing)

    def link_tag(self, spool_id: int, uid: str) -> JSON:
        """POST /spool/{id}/tag: UID normalized (separators stripped, upper case, hex only); a UID
        another spool holds -> 409; re-linking to the holder is a success."""
        self._enter("POST", f"/spool/{spool_id}/tag")
        with self._lock:
            self._row("spool", spool_id)
            norm = normalize_uid(uid)
            if not norm or any(c not in "0123456789ABCDEF" for c in norm):
                raise _err(400, f'"{uid}" is not a valid tag UID. A UID must be hexadecimal.')
            holder = self._tags.get(norm)
            if holder is not None and holder != int(spool_id):
                raise _err(409, f"Tag {norm} is already linked to spool {holder}.")
            self._tags[norm] = int(spool_id)
            return {"uid": norm, "added": _now()}

    # -- HTTP -----------------------------------------------------------------------------
    @contextlib.contextmanager
    def serve(self) -> Iterator[str]:
        """Serve this fake at http://127.0.0.1:<port> (Spoolman's /api/v1 layout); yields the root URL."""
        handler = _make_handler(self)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            yield self.url
        finally:
            server.shutdown()
            server.server_close()
            self.url = None


def _length(weight: float, fil: Mapping[str, Any]) -> float:
    return (weight / fil["density"]) * 1000 / (math.pi * (fil["diameter"] / 2) ** 2)


def _make_handler(fake: FakeSpoolman) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def _send(self, status: int, body: Any) -> None:
            raw = b"" if body is None else json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self) -> JSON:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n)) if n else {}

        def _dispatch(self, method: str) -> None:
            url = urlsplit(self.path)
            params = {k: v[-1] for k, v in parse_qs(url.query).items()}
            parts = [p for p in url.path.split("/") if p]
            if parts[:2] != ["api", "v1"]:
                return self._send(404, {"detail": "Not Found"})
            parts = parts[2:]
            try:
                result = self._route(method, parts, params)
            except SpoolmanError as e:
                if e.status == 0:
                    return self._send(503, {"message": e.message})
                key = "detail" if e.status == 422 else "message"
                return self._send(e.status, {key: e.message})
            except (ValueError, KeyError, TypeError) as e:
                return self._send(422, {"detail": str(e)})
            return self._send(200, result)

        def _route(self, method: str, parts: list[str], params: dict[str, str]) -> Any:
            if parts == ["health"] and method == "GET":
                return {"status": "healthy"}
            if parts == ["info"] and method == "GET":
                return fake.info()
            if parts and parts[0] == "field":
                return self._route_field(method, parts)
            if parts and parts[0] in ENTITIES:
                return self._route_entity(method, parts, params)
            raise SpoolmanError("Not Found", status=404)

        def _route_field(self, method: str, parts: list[str]) -> Any:
            if len(parts) == 2 and method == "GET":
                return fake.list_fields(_entity(parts[1]))
            if len(parts) == 3 and method == "POST":
                return fake.set_field(_entity(parts[1]), parts[2], self._body())
            if len(parts) == 3 and method == "DELETE":
                return fake.delete_field(_entity(parts[1]), parts[2])
            raise SpoolmanError("Not Found", status=404)

        def _route_entity(
            self, method: str, parts: list[str], params: dict[str, str]
        ) -> Any:
            entity = _entity(parts[0])
            if len(parts) == 1:
                if method == "GET":
                    return fake.find(entity, params)
                if method == "POST":
                    return fake.create(entity, self._body())
            if len(parts) == 2:
                item_id = int(parts[1])
                if method == "GET":
                    return fake.get(entity, item_id)
                if method == "PATCH":
                    return fake.update(entity, item_id, self._body())
                if method == "DELETE":
                    fake.delete(entity, item_id)
                    return {"message": "Success!"}
            if len(parts) == 3 and entity == "spool":
                return self._route_spool_action(method, parts)
            raise SpoolmanError("Not Found", status=404)

        def _route_spool_action(self, method: str, parts: list[str]) -> Any:
            if method == "POST" and parts[2] == "tag":
                return fake.link_tag(int(parts[1]), str(self._body()["uid"]))
            if method != "PUT":
                raise SpoolmanError("Not Found", status=404)
            body = self._body()
            if parts[2] == "use":
                return fake.use(
                    int(parts[1]),
                    use_weight=body.get("use_weight"),
                    use_length=body.get("use_length"),
                )
            if parts[2] == "measure":
                return fake.measure(int(parts[1]), float(body["weight"]))
            raise SpoolmanError("Not Found", status=404)

        def do_GET(self) -> None:  # noqa: N802 — http.server API
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def do_PATCH(self) -> None:  # noqa: N802
            self._dispatch("PATCH")

        def do_PUT(self) -> None:  # noqa: N802
            self._dispatch("PUT")

        def do_DELETE(self) -> None:  # noqa: N802
            self._dispatch("DELETE")

    return Handler


def _entity(name: str) -> Entity:
    for e in ENTITIES:
        if e == name:
            return e
    raise SpoolmanError(
        f"entity_type: Input should be {', '.join(ENTITIES)}", status=422
    )
