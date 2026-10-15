"""tares.py — empty-spool weights (tares): the sourced tare DB and the per-spool tare chain.

ACCESSED VIA: printops.spools (receive writes the tare into Spoolman spool_weight, measure
subtracts it), 3d-api routes_spools (`/v1/tares`, `/v1/spools/{id}/tare`), lint_spools
(spools/no-tare), `3d printops tare …` (plan §5.3, F6, issue #163).

THE CHAIN for one spool, first hit wins (each step has a source and ± grams):
  1. `spool_tare_choice` — the tare 3d-api itself wrote into Spoolman spool.spool_weight, with
     the provenance it had then (only while Spoolman still holds that value);
  2. Spoolman spool.spool_weight set by someone else (Spoolman UI) — unsourced, ±UNSOURCED_SIGMA;
  3. Spoolman filament.spool_weight, then vendor extra `spool_tare_g`, then vendor
     empty_spool_weight — the user's Spoolman data, ±UNSOURCED_SIGMA;
  4. the tare DB (`spool_tare`, migration 0050: SpoolmanDB seed + `tare add`): rows of the
     spool's vendor, narrowed by spool kind (vendor extra `spool_kind`) and nominal net
     (filament.weight) when that keeps any row; used only if the remaining rows agree on one
     value — otherwise the tare is ambiguous and the candidates are reported.
  No hit → `None`: the spool cannot be weighed until a tare is given (lint spools/no-tare).
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from printops import db
from printops.spoolman import decode_extra

SPOOL_KINDS = ("cardboard", "plastic", "refill")
UNSOURCED_SIGMA = (
    15.0  # a spool_weight typed into Spoolman by hand, origin unknown [assumption]
)
MEASURED_SIGMA = (
    2.0  # an empty spool weighed on a kitchen scale (±1 g scale + residue) [assumption]
)
AGREE_WITHIN_G = 0.5  # tare DB candidates closer than this count as one value


class TareError(ValueError):
    """Invalid tare input (HTTP 422)."""


@dataclass(frozen=True)
class Tare:
    grams: float
    sigma: float
    source: str
    origin: str  # choice | spool | filament | vendor | db
    tare_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tare_g": self.grams,
            "sigma_g": self.sigma,
            "source": self.source,
            "origin": self.origin,
            "tare_id": self.tare_id,
        }


@dataclass(frozen=True)
class TareRow:
    id: int
    vendor: str
    spool_kind: str | None
    net_g: float | None
    tare_g: float
    sigma_g: float
    source: str
    note: str | None
    created_at: str
    actor: str

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass(frozen=True)
class Resolution:
    tare: Tare | None
    candidates: list[TareRow] = field(
        default_factory=list
    )  # DB rows considered (ambiguity)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tare": self.tare.to_dict() if self.tare else None,
            "reason": self.reason,
            "candidates": [c.to_dict() for c in self.candidates],
        }


def vendor_key(name: str) -> str:
    """ "Bambu Lab", "BAMBU-LAB", "bambulab" → "bambulab"."""
    return re.sub(r"[^0-9a-z]+", "", name.lower())


def _row(r: sqlite3.Row) -> TareRow:
    return TareRow(
        id=int(r["id"]),
        vendor=str(r["vendor"]),
        spool_kind=r["spool_kind"],
        net_g=r["net_g"],
        tare_g=float(r["tare_g"]),
        sigma_g=float(r["sigma_g"]),
        source=str(r["source"]),
        note=r["note"],
        created_at=str(r["created_at"]),
        actor=str(r["actor"]),
    )


def list_tares(conn: sqlite3.Connection, vendor: str | None = None) -> list[TareRow]:
    if vendor:
        rows = conn.execute(
            "SELECT * FROM spool_tare WHERE archived = 0 AND vendor_key = ? ORDER BY id",
            (vendor_key(vendor),),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM spool_tare WHERE archived = 0 ORDER BY vendor_key, id"
        ).fetchall()
    return [_row(r) for r in rows]


def add_tare(
    conn: sqlite3.Connection,
    *,
    vendor: str,
    tare_g: float,
    source: str,
    actor: str,
    spool_kind: str | None = None,
    net_g: float | None = None,
    sigma_g: float | None = None,
    note: str | None = None,
) -> TareRow:
    """A sourced empty-spool weight. `source` is mandatory: a URL, or how it was weighed."""
    if not vendor_key(vendor):
        raise TareError("vendor must contain letters or digits")
    if not source.strip():
        raise TareError(
            "a tare needs a source (URL, or e.g. 'weighed empty spool, kitchen scale ±1 g')"
        )
    if not 0 < tare_g < 5000:
        raise TareError(f"tare_g must be in (0, 5000) g, got {tare_g}")
    if spool_kind is not None and spool_kind not in SPOOL_KINDS:
        raise TareError(
            f"spool_kind must be one of {', '.join(SPOOL_KINDS)}; got {spool_kind!r}"
        )
    if net_g is not None and net_g <= 0:
        raise TareError(f"net_g must be > 0, got {net_g}")
    sigma = MEASURED_SIGMA if sigma_g is None else sigma_g
    if sigma < 0:
        raise TareError(f"sigma_g must be ≥ 0, got {sigma}")
    cur = conn.execute(
        "INSERT INTO spool_tare (vendor, vendor_key, spool_kind, net_g, tare_g, sigma_g, source, note,"
        " created_at, actor) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            vendor.strip(),
            vendor_key(vendor),
            spool_kind,
            net_g,
            tare_g,
            sigma,
            source.strip(),
            note,
            db.utc_now(),
            actor,
        ),
    )
    return _row(
        conn.execute(
            "SELECT * FROM spool_tare WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    )


def lookup(
    conn: sqlite3.Connection,
    vendor: str,
    *,
    spool_kind: str | None = None,
    net_g: float | None = None,
) -> Resolution:
    """Step 4 of the chain: the tare DB for one vendor (see module doc)."""
    rows = list_tares(conn, vendor)
    if not rows:
        return Resolution(None, [], f"no tare known for vendor {vendor!r}")
    for narrow in (
        lambda r: r.spool_kind == spool_kind if spool_kind else True,
        lambda r: r.net_g == net_g if net_g else True,
    ):
        kept = [r for r in rows if narrow(r)]
        rows = kept or rows
    # Our own weighings beat a catalogue: if any row was measured here (sigma ≤ MEASURED_SIGMA), use those.
    measured = [r for r in rows if r.sigma_g <= MEASURED_SIGMA]
    rows = measured or rows
    values = sorted({r.tare_g for r in rows})
    if values[-1] - values[0] > AGREE_WITHIN_G:
        listing = ", ".join(
            f"{r.tare_g:g} g ({r.spool_kind or 'kind ?'}, net {r.net_g or '?'})"
            for r in rows
        )
        return Resolution(
            None,
            rows,
            f"ambiguous tare for {vendor!r}: {listing}; give the spool kind/net or --tare",
        )
    best = min(rows, key=lambda r: (r.sigma_g, -r.id))
    return Resolution(
        Tare(best.tare_g, best.sigma_g, best.source, "db", best.id), rows, ""
    )


def record_choice(
    conn: sqlite3.Connection, spool_id: int, tare: Tare, *, actor: str
) -> None:
    conn.execute(
        "INSERT INTO spool_tare_choice (spool_id, tare_g, sigma_g, source, tare_id, at, actor)"
        " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (spool_id) DO UPDATE SET tare_g = excluded.tare_g,"
        " sigma_g = excluded.sigma_g, source = excluded.source, tare_id = excluded.tare_id, at = excluded.at,"
        " actor = excluded.actor",
        (
            spool_id,
            tare.grams,
            tare.sigma,
            tare.source,
            tare.tare_id,
            db.utc_now(),
            actor,
        ),
    )


def resolve(conn: sqlite3.Connection, spool: Mapping[str, Any]) -> Resolution:
    """The tare chain for a Spoolman spool JSON (see module doc)."""
    spool_id = int(spool["id"])
    fil = spool.get("filament") or {}
    sw = spool.get("spool_weight")
    if sw:
        choice = conn.execute(
            "SELECT * FROM spool_tare_choice WHERE spool_id = ?", (spool_id,)
        ).fetchone()
        if choice is not None and abs(float(choice["tare_g"]) - float(sw)) < 0.05:
            return Resolution(
                Tare(
                    float(sw),
                    float(choice["sigma_g"]),
                    str(choice["source"]),
                    "choice",
                    choice["tare_id"],
                )
            )
        return Resolution(
            Tare(
                float(sw),
                UNSOURCED_SIGMA,
                "Spoolman spool.spool_weight (set by hand)",
                "spool",
            )
        )
    return resolve_new(conn, fil)


def resolve_new(
    conn: sqlite3.Connection,
    filament: Mapping[str, Any],
    *,
    spool_kind: str | None = None,
) -> Resolution:
    """Steps 3–4 of the chain for a filament (a spool not created yet, or one without spool_weight)."""
    vendor = filament.get("vendor") or {}
    if filament.get("spool_weight"):
        return Resolution(
            Tare(
                float(filament["spool_weight"]),
                UNSOURCED_SIGMA,
                "Spoolman filament.spool_weight",
                "filament",
            )
        )
    extra = vendor.get("extra") or {}
    vendor_tare = decode_extra(extra.get("spool_tare_g"))
    if vendor_tare:
        return Resolution(
            Tare(
                float(vendor_tare),
                UNSOURCED_SIGMA,
                "Spoolman vendor spool_tare_g",
                "vendor",
            )
        )
    if vendor.get("empty_spool_weight"):
        return Resolution(
            Tare(
                float(vendor["empty_spool_weight"]),
                UNSOURCED_SIGMA,
                "Spoolman vendor.empty_spool_weight",
                "vendor",
            )
        )
    if not vendor.get("name"):
        return Resolution(
            None, [], "the filament has no vendor, so no tare can be looked up"
        )
    kind = spool_kind or decode_extra(extra.get("spool_kind"))
    return lookup(
        conn, str(vendor["name"]), spool_kind=kind, net_g=filament.get("weight")
    )
