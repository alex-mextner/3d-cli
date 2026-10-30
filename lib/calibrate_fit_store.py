"""calibrate_fit_store.py — where `fit_clearance` calibration records live: 3d-api, with the offline JSONL buffer.

WHAT: one record per printer x head x nozzle x filament holds the readings of the coupons printed for it
  (`values.readings` = JSON list of `{fit_type, shape, gap_mm, result}`, the S0 contract of
  `fits.validate_fit_clearance`; kind `fit_clearance` in fleet/calibrations.yaml). A new record for the same
  scope supersedes the older one in 3d-api, so `merge_readings` carries the ladders it does not re-read.
    read_all   the current records: 3d-api (`GET /v1/calibrations?kind=fit_clearance&current=true`) plus the lines
               of the settings-history buffer (printops.history_buffer) that 3d-api has not imported yet
    write      POST /v1/calibrations; when 3d-api is not configured or does not answer, append a
               `printops.settings_change/1` line to `<data dir>/printer-history/<printer>.jsonl` — the buffer
               `3d settings import` replays (idempotent by uid; the import needs the spool: `--spool UID=ID`)
    rows_of    a record -> `fits.Correction` rows (`source: calibration`, `measured_at`, the record id in `note`)
ACCESSED VIA: `3d calibrate fit` (calibrate_fit_cli.py) and the correction source `fits_sources/calibration.py`
  (so `3d fits resolve`, `3d fit-check --fit`, `3d fit-niche --fit` all see a recorded coupon).
INVARIANTS:
  - A source that cannot read its store never returns a silently partial list: a torn buffer line raises a
    `ThreeDError` naming file and line; an UNREACHABLE 3d-api falls back to the buffer and says so on stderr.
  - `THREED_FIT_CALIBRATION_API=off` disables every 3d-api call (the test suite sets it: no run may reach a real
    API through the owner's `~/.config/3d-cli/printops.yaml`).
  - Stdlib + pyyaml only at import; fits / printops imports are lazy.
"""

from __future__ import annotations

import getpass
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from errors import ThreeDError

if TYPE_CHECKING:
    from fits import Correction
    from printops.client import ApiClient

KIND = "fit_clearance"
ENV_API = "THREED_FIT_CALIBRATION_API"
READ_TIMEOUT_S = 3.0
WRITE_TIMEOUT_S = 10.0
SOURCE_LABEL = "3d calibrate fit record"
METHODS = ("fit_coupon", "caliper", "manual")


@dataclass(frozen=True)
class StoredRecord:
    id: str  # "#12" (3d-api) or "buffer:<uid prefix>"
    origin: str  # "3d-api" | "buffer"
    printer: str
    head: str | None
    material: str | None
    nozzle_mm: float | None
    measured_at: str
    readings: list[dict[str, Any]]
    coupon: str | None = None
    uid: str | None = (
        None  # the settings-history line uid (an imported record carries it in params)
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "origin": self.origin,
            "printer": self.printer,
            "head": self.head,
            "material": self.material,
            "nozzle_mm": self.nozzle_mm,
            "measured_at": self.measured_at,
            "coupon": self.coupon,
            "uid": self.uid,
            "readings": self.readings,
        }


@dataclass
class ReadResult:
    records: list[StoredRecord] = field(default_factory=list)
    notes: list[str] = field(
        default_factory=list
    )  # what could not be read, for the caller to show


# --------------------------------------------------------------------------- 3d-api
def api_client(
    api: str | None = None, token: str | None = None, *, timeout: float = READ_TIMEOUT_S
) -> ApiClient | None:
    """The 3d-api client, or None when no URL is configured or the env switch is off."""
    if os.environ.get(ENV_API, "").strip().lower() in ("off", "0", "no", "false"):
        return None
    from printops.client import ApiClient, ApiConfigError

    try:
        client = ApiClient.from_config(api=api, token=token)
    except ApiConfigError:
        return None
    client.timeout = timeout
    return client


def _parse_readings(raw: Any, where: str) -> list[dict[str, Any]]:
    import fits

    try:
        rows = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError as e:
        raise ThreeDError(
            f"{where}: `readings` is not JSON ({e})", command="calibrate fit"
        ) from e
    if not isinstance(rows, list):
        raise ThreeDError(
            f"{where}: `readings` must be a list of fit_clearance readings",
            command="calibrate fit",
        )
    try:
        return [fits.validate_fit_clearance(row) for row in rows]
    except (fits.FitTableError, AttributeError, TypeError) as e:
        raise ThreeDError(f"{where}: {e}", command="calibrate fit") from e


def _from_api(rec: Mapping[str, Any]) -> StoredRecord:
    where = f"3d-api calibration #{rec['id']}"
    values = rec.get("values") or {}
    fil = rec.get("filament") or {}
    nozzle = rec.get("nozzle") or {}
    params = rec.get("params") or {}
    return StoredRecord(
        id=f"#{rec['id']}",
        origin="3d-api",
        printer=str(rec.get("printer") or ""),
        head=rec.get("head"),
        material=fil.get("material"),
        nozzle_mm=nozzle.get("diameter_mm"),
        measured_at=str(rec.get("recorded_at") or rec["created_at"]),
        readings=_parse_readings(values.get("readings"), where),
        coupon=values.get("coupon"),
        uid=params.get("settings_change_uid"),
    )


def read_api(client: ApiClient) -> list[StoredRecord]:
    data = client.get("/v1/calibrations", kind=KIND, current="true", limit=1000)
    return [
        _from_api(r)
        for r in data["calibrations"]
        if r.get("status") == "recorded" and r.get("values")
    ]


# --------------------------------------------------------------------------- offline buffer
def _buffer_dir() -> Path:
    from printops import history_buffer

    return history_buffer.buffer_dir()


def _from_line(path: Path, line: Any) -> StoredRecord | None:
    d = line.data
    if d.get("kind") != KIND or d.get("status") not in ("measured", "applied"):
        return None
    where = f"{path}:{line.n}"
    new = d.get("new")
    if not isinstance(new, dict) or "readings" not in new:
        raise ThreeDError(
            f"{where}: a {KIND} line needs `new.readings`", command="calibrate fit"
        )
    nozzle = d.get("nozzle") if isinstance(d.get("nozzle"), dict) else {}
    uid = line.uid
    return StoredRecord(
        id=f"buffer:{uid[:8]}",
        origin="buffer",
        printer=str(d["printer"]),
        head=d.get("head") if isinstance(d.get("head"), str) else None,
        material=d.get("material") if isinstance(d.get("material"), str) else None,
        nozzle_mm=nozzle.get("diameter_mm"),
        measured_at=str(d.get("at") or d.get("recorded_at") or ""),
        readings=_parse_readings(new["readings"], where),
        coupon=new.get("coupon") if isinstance(new.get("coupon"), str) else None,
        uid=uid,
    )


def read_buffer() -> list[StoredRecord]:
    """Every fit_clearance line of the settings-history buffer (imported or not; `read_all` drops the imported).

    Within a file the newest line (the last one appended) comes first; files are in name order, which says
    nothing about their age."""
    from printops import history_buffer as hb

    d = _buffer_dir()
    out: list[StoredRecord] = []
    for path in sorted(d.glob("*.jsonl")) if d.is_dir() else []:
        try:
            lines = hb.parse_lines(path.read_text(encoding="utf-8").splitlines())
        except hb.HistoryBufferError as e:
            raise ThreeDError(
                f"{path}: {e.message}" + (f" (line {e.line})" if e.line else ""),
                command="calibrate fit",
                remediation=[
                    "Fix or remove that buffer line: a torn write is never skipped silently."
                ],
            ) from e
        for line in reversed(lines):
            rec = _from_line(path, line)
            if rec is not None:
                out.append(rec)
    return out


def _read_api_records(client: ApiClient, result: ReadResult, *, warn: bool) -> set[str]:
    """Append 3d-api's records (newest first) to `result`; -> their uids (those buffer lines are imported)."""
    from printops.client import ApiError

    imported: set[str] = set()
    try:
        for rec in read_api(client):
            result.records.append(rec)
            if rec.uid:
                imported.add(rec.uid)
    except ApiError as e:
        if e.status != 0:
            raise ThreeDError(
                f"3d-api refused the fit_clearance read: {e.message}",
                command="calibrate fit",
                remediation=["Check PRINTOPS_TOKEN / `3d printops token --help`."],
            ) from e
        note = f"3d-api is unreachable ({e.message}): fit calibrations come from the offline buffer only"
        result.notes.append(note)
        if warn:
            print(f"calibrate fit: {note}", file=sys.stderr)
    return imported


def read_all(api: ApiClient | None = None, *, warn: bool = True) -> ReadResult:
    """The current records, newest first: 3d-api (when `api` or the config names one) + not-yet-imported buffer lines."""
    result = ReadResult()
    client = api if api is not None else api_client()
    imported = (
        _read_api_records(client, result, warn=warn) if client is not None else set()
    )
    # 3d-api and every buffer file are newest first, so the stable sort below leaves records of one source that
    # tie on `measured_at` (a resolution of one second) newest first, and `fits.select_correction` keeps the
    # first of equal instants; across buffer files there is no order to keep (their names are printer ids)
    result.records.extend(r for r in read_buffer() if r.uid not in imported)
    result.records.sort(key=lambda r: r.measured_at, reverse=True)
    return result


def rows_of(record: StoredRecord) -> list[Correction]:
    """`fits.Correction` rows of one record (smallest free gap / largest press gap per fit x shape)."""
    import fits

    where = (
        " (offline buffer, not imported into 3d-api yet)"
        if record.origin == "buffer"
        else ""
    )
    return fits.corrections_from_fit_clearance(
        record.readings,
        printer=record.printer,
        material=record.material or fits.WILDCARD,
        nozzle_mm=record.nozzle_mm,
        measured_at=record.measured_at,
        note=f"calibration record {record.id}{where}",
    )


# --------------------------------------------------------------------------- write
@dataclass(frozen=True)
class NewRecord:
    printer: str
    head: str | None
    material: str
    nozzle_mm: float
    readings: list[dict[str, Any]]
    method: str = "fit_coupon"
    coupon: str | None = None
    note: str | None = None
    spool_id: int | None = None
    filament_id: int | None = None


@dataclass(frozen=True)
class Written:
    origin: str  # "3d-api" | "buffer"
    id: str
    where: str  # URL or file
    uid: str | None
    record: dict[str, Any]


def _values(new: NewRecord) -> dict[str, Any]:
    from calibrate_fit import readings_json

    values: dict[str, Any] = {"readings": readings_json(new.readings)}
    if new.coupon:
        values["coupon"] = new.coupon
    return values


def _preflight_material(client: ApiClient, new: NewRecord) -> None:
    """The record takes the material of the spool it is bound to: refuse a different `--material` BEFORE storing."""
    import fits

    from errors import UsageError

    if new.head is None or new.filament_id is not None:
        return
    data = client.get(
        "/v1/calibrations/required",
        printer=new.printer,
        head=new.head,
        spool=new.spool_id,
        all="true",
    )
    materials = {
        str((i.get("filament") or {}).get("material"))
        for i in data.get("items", [])
        if (i.get("filament") or {}).get("material")
    }
    if materials and not any(
        fits.norm_name(m) == fits.norm_name(new.material) for m in materials
    ):
        raise UsageError(
            f"--material {new.material} but {new.printer}/{new.head} has {', '.join(sorted(materials))} loaded",
            command="calibrate fit record",
            remediation=[
                "Record against the spool you printed the coupon with: --spool ID (or --filament ID).",
                "Bind the spool first: `3d printops spool load --help`.",
            ],
        )


def write(new: NewRecord, client: ApiClient | None) -> Written:
    """Store the record in 3d-api; offline (no URL / unreachable) in the settings-history buffer."""
    from printops.client import ApiError

    if client is not None:
        client.timeout = WRITE_TIMEOUT_S
        try:
            _preflight_material(client, new)
            body: dict[str, Any] = {
                "kind": KIND,
                "method": new.method,
                "printer": new.printer,
                "values": _values(new),
                "note": new.note or f"fit coupon readout for {new.material}",
            }
            if new.head:
                body["head"] = new.head
            if new.spool_id is not None:
                body["spool_id"] = new.spool_id
            if new.filament_id is not None:
                body["filament_id"] = new.filament_id
            rec = client.post("/v1/calibrations", body)
            return Written("3d-api", f"#{rec['id']}", client.base_url, None, rec)
        except ApiError as e:
            if e.status != 0:
                raise ThreeDError(
                    f"3d-api refused the record: {e.message}",
                    command="calibrate fit record",
                    remediation=[
                        "A filament-scoped record needs the spool: pass --spool ID (or --filament ID) "
                        "or bind one with `3d printops spool load`.",
                        "Without 3d-api the record goes to the offline buffer: set THREED_FIT_CALIBRATION_API=off.",
                    ],
                ) from e
    return _write_buffer(new)


def _uuid7() -> str:
    from printops.printer_task_buffer import uuid7

    return uuid7()


def _write_buffer(new: NewRecord) -> Written:
    from calibrate_fit import utc_now
    from printops import history_buffer as hb

    uid = _uuid7()
    line: dict[str, Any] = {
        "schema": hb.SCHEMA,
        "uid": uid,
        "printer": new.printer,
        "at": utc_now(),
        "kind": KIND,
        "key": f"fit_clearance/{new.material}",
        "new": _values(new),
        "status": "measured",
        "source": SOURCE_LABEL,
        "actor": getpass.getuser(),
        "method": new.method,
        "material": new.material,
        "nozzle": {"diameter_mm": new.nozzle_mm},
        "note": new.note or f"fit coupon readout for {new.material}",
    }
    if new.head:
        line["head"] = new.head
    if new.spool_id is not None:
        line["spool_id"] = new.spool_id
    if new.filament_id is not None:
        line["filament_id"] = new.filament_id
    try:
        path = hb.append(new.printer, line)
    except hb.HistoryBufferError as e:
        raise ThreeDError(
            e.message, command="calibrate fit record", remediation=e.remediation
        ) from e
    return Written("buffer", f"buffer:{uid[:8]}", str(path), uid, line)
