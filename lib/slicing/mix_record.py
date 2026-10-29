"""mix_record.py — the calibration records a tester readout yields (`3d mix record`, #417).

WHAT: `plan()` turns a `mix-readout/1` record (and, when given, a `mix-fit/1` record) into the
  records plan §20.4 asks for, in the order the plan says: first one `filament_color` per pure
  reference swatch whose Spoolman filament id is known (hex = the fitted effective colour when a
  fit is given, else the measured swatch; `spoolman_hex` = the nominal colour it corrects), then
  one `mix_color` per mix swatch whose mix key is known (hex = the measured swatch,
  `predicted_hex` = what the model predicted, ΔE between them). Every record carries, in `params`,
  the sha256 of the readout file (`readout_sha256`), the photo's, the swatch number and the
  manifest id; its evidence lists the photo and the readout. Each record comes in two forms: the
  `POST /v1/calibrations` body, and a settings-history buffer line (`printops.settings_change/1`,
  plan §7.4) that `3d settings import` turns into the same record when 3d-api is not reachable.
WHY: epic #410 / issue #417: measured colours must reach 3d-api with history, and repeating the
  same readout must change nothing (idempotent): the API form is skipped when a record of the same
  readout and swatch exists, the buffer form has a uid made of the readout sha256 and the scope.
INVARIANTS: stdlib only; deterministic (no clock: the caller passes `at`); a swatch without a
  Spoolman filament id (filament_color) or mix key (mix_color) is reported as skipped with the
  reason, never recorded under a guess.
"""

from __future__ import annotations

import datetime
import getpass
from dataclasses import dataclass, field
from typing import Any

from printops.color import hex_delta_e, srgb_to_lab

LINE_SCHEMA = "printops.settings_change/1"
METHOD = "mix_tester_photo"
SOURCE = "3d mix record"


class RecordError(ValueError):
    """The readout cannot be turned into records."""


@dataclass(frozen=True)
class Item:
    kind: str  # filament_color | mix_color
    scope: str  # "f<filament id>" or the mix key
    number: int  # swatch number in the tester
    body: dict[str, Any]  # POST /v1/calibrations
    line: dict[str, Any]  # settings-history buffer line

    @property
    def uid(self) -> str:
        return str(self.line["uid"])

    def label(self) -> str:
        v = self.body["values"]
        what = (
            f"filament {self.body['filament_id']}"
            if self.kind == "filament_color"
            else f"mix {self.body['mix']}"
        )
        return f"{self.kind} {what}: hex={v['hex']}"


@dataclass
class Plan:
    items: list[Item] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _fit_hex(fit: dict[str, Any] | None, filament_id: int) -> str | None:
    for f in (fit or {}).get("filaments", []):
        if f.get("filament_id") == filament_id:
            return str(f["fitted_hex"])
    return None


@dataclass(frozen=True)
class _Ctx:
    readout_sha256: str
    photo_sha256: Any
    manifest_id: Any
    printer: str
    readout_name: str
    at: str
    who: str
    evidence: list[str]


def _item(
    ctx: _Ctx,
    kind: str,
    scope: str,
    n: int,
    values: dict[str, Any],
    extra: dict[str, Any],
) -> Item:
    params = {
        "readout_sha256": ctx.readout_sha256,
        "photo_sha256": ctx.photo_sha256,
        "swatch": n,
        "manifest_id": ctx.manifest_id,
        "source": SOURCE,
    }
    body = {
        "kind": kind,
        "method": METHOD,
        "printer": ctx.printer,
        "values": values,
        "params": params,
        "evidence": ctx.evidence,
        "note": f"{SOURCE}: swatch {n} of {ctx.readout_name}",
        **extra,
    }
    line = {
        "schema": LINE_SCHEMA,
        "uid": f"mix-readout-{ctx.readout_sha256[:16]}-{ctx.printer}-{kind}-s{n}",
        "printer": ctx.printer,
        "at": ctx.at,
        "recorded_at": ctx.at,
        "actor": ctx.who,
        "head": None,
        "kind": kind,
        "key": f"{kind}/{scope}",
        "old": None,
        "new": values,
        "status": "measured",
        "source": "photo",
        "method": METHOD,
        "params": params,
        "evidence": ctx.evidence,
        "command": SOURCE,
        "note": body["note"],
        **extra,
    }
    return Item(kind, scope, n, body, line)


def _filament_item(
    ctx: _Ctx, r: dict[str, Any], fit: dict[str, Any] | None, out: Plan
) -> None:
    fid = (r.get("calibration") or {}).get("filament_id")
    if not isinstance(fid, int):
        out.skipped.append(
            f"swatch {r['number']} (T{r.get('slot')} pure): no Spoolman filament id in the tester "
            "(load it with `--printer`), so no filament_color record"
        )
        return
    hex_ = _fit_hex(fit, fid) or r["measured_hex"]
    nominal = r["predicted_hex"]
    lab = srgb_to_lab(hex_)
    values = {
        "hex": hex_,
        "spoolman_hex": nominal,
        "delta_e": round(hex_delta_e(nominal, hex_), 2),
        "lab_l": round(lab[0], 2),
        "lab_a": round(lab[1], 2),
        "lab_b": round(lab[2], 2),
    }
    out.items.append(
        _item(
            ctx, "filament_color", f"f{fid}", int(r["number"]), values, {"filament_id": fid}
        )
    )


def _mix_item(ctx: _Ctx, r: dict[str, Any], out: Plan) -> None:
    key = (r.get("calibration") or {}).get("mix")
    if not isinstance(key, str):
        out.skipped.append(
            f"swatch {r['number']} (mix {r.get('pattern')}): the filament ids of its heads are not all "
            "known, so no mix key and no mix_color record"
        )
        return
    values = {
        "hex": r["measured_hex"],
        "predicted_hex": r["predicted_hex"],
        "delta_e_predicted": round(float(r["delta_e_predicted"]), 2),
    }
    out.items.append(_item(ctx, "mix_color", key, int(r["number"]), values, {"mix": key}))


def plan(
    readout: dict[str, Any],
    readout_sha256: str,
    printer: str,
    *,
    readout_name: str,
    at: str,
    fit: dict[str, Any] | None = None,
    actor: str | None = None,
) -> Plan:
    """The records of a readout (module doc). `at` is an ISO time ("2026-10-24T12:00:00Z")."""
    if not isinstance(readout, dict) or readout.get("schema") != "mix-readout/1":
        raise RecordError("schema must be mix-readout/1")
    if not printer:
        raise RecordError("a printer is required: the records are per printer")
    photo = readout.get("photo") or {}
    manifest = readout.get("manifest") or {}
    ctx = _Ctx(
        readout_sha256,
        photo.get("sha256"),
        manifest.get("id"),
        printer,
        readout_name,
        at,
        actor or f"cli:{getpass.getuser()} ({SOURCE})",
        [
            f"photo {photo.get('file', '?')} sha256:{photo.get('sha256', '?')}",
            f"readout {readout_name} sha256:{readout_sha256}",
        ],
    )
    out = Plan()
    rows = readout.get("swatches", [])
    for r in rows:
        if r.get("kind") == "reference":
            _filament_item(ctx, r, fit, out)
    for r in rows:
        if r.get("kind") == "candidate":
            _mix_item(ctx, r, out)
    if not out.items:
        raise RecordError(
            "the readout yields no record: no swatch has a Spoolman filament id or a mix key. "
            + "; ".join(out.skipped)
        )
    return out


def existing(
    item: Item, records: list[dict[str, Any]], readout_sha256: str
) -> dict[str, Any] | None:
    """The stored record that is `item` from the same readout, if any (the API form's idempotency)."""
    for rec in records:
        p = rec.get("params") or {}
        if (
            rec.get("kind") == item.kind
            and p.get("readout_sha256") == readout_sha256
            and p.get("swatch") == item.number
        ):
            return rec
    return None


def now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

