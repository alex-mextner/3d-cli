"""mix_fit.py — per-filament effective wall colours fitted to measured mixes (`mix-fit/1`, #417).

WHAT: `fit()` takes one or more `mix-readout/1` records (slicing/mix_readout.py) of testers and
  returns, per filament, the colour it shows on a wall in a mix:
  - the pure reference swatch of each filament gives its printed colour (measured, not Spoolman's);
  - the mixes then fit per-filament effective colours (linear sRGB, 3 numbers per filament) by
    least squares over every measured mix: the model colour of a mix is the mean of its layers in
    linear light (slicing/mix_model.py, plan §20.3), the residual is the CIELAB difference to the
    measured swatch, and a regularisation term pulls each filament back to its pure swatch
    (weight `lam`, a pure swatch counts as `lam` of a mix: the pure swatch is seen on a flat
    layer, a mix on thin layers, where a light filament over a dark one looks darker);
  - the report gives, per mix, the error of the prediction from the nominal colours (Spoolman's),
    from the pure measured colours and from the fitted ones, and the means: before and after.
WHY: epic #410 / issue #417: measured colours are thrown away unless they correct the model; the
  per-spool corrections are what make the first real print land (Snapdragon yellow, bordeaux).
INVARIANTS: numpy/scipy are imported inside the functions; the fit is deterministic (a fixed
  start, bounds 0..1, no random restarts); filaments are keyed by Spoolman filament id when the
  tester knew it, else by head number; a filament without a mix keeps its pure colour (and is
  reported as such); the fit never reads the nominal colour of a filament as data.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from printops.color import hex_delta_e, hex_from_linear, linear_rgb, parse_hex

SCHEMA = "mix-fit/1"
DEFAULT_LAMBDA = 0.1  # weight of the pure swatch against one mix swatch


class FitError(ValueError):
    """The readouts cannot be fitted (wrong schema, a mix of a filament with no reference swatch)."""


@dataclass
class _Filament:
    key: str
    slot: int
    filament_id: int | None
    nominal_hex: str
    pure: list[tuple[float, float, float]] = field(
        default_factory=list
    )  # linear, per readout


@dataclass(frozen=True)
class _Mix:
    readout: int
    number: int
    pattern: str
    layers: tuple[str, ...]  # filament keys, one per layer
    measured_hex: str
    mix_key: str | None


def _key(filament_id: int | None, slot: int) -> str:
    return f"f{filament_id}" if filament_id is not None else f"slot{slot}"


def _add_references(
    r: dict[str, Any], filaments: dict[str, _Filament]
) -> dict[int, str]:
    """Register the reference swatches of readout `r`; returns head number -> filament key."""
    by_slot: dict[int, str] = {}
    for row in r.get("swatches", []):
        if row.get("kind") != "reference":
            continue
        cal = row.get("calibration") or {}
        fid = cal.get("filament_id") if isinstance(cal.get("filament_id"), int) else None
        slot = int(row["slot"])
        key = _key(fid, slot)
        f = filaments.setdefault(key, _Filament(key, slot, fid, row["predicted_hex"]))
        f.pure.append(linear_rgb(row["measured_hex"]))
        by_slot[slot] = key
    return by_slot


def _candidate_mix(i: int, row: dict[str, Any], by_slot: dict[int, str]) -> _Mix:
    missing = sorted({d for d in row["pattern"] if int(d) not in by_slot})
    if missing:
        raise FitError(
            f"readout {i + 1}, swatch {row['number']} ({row['pattern']}): no reference swatch "
            f"of head(s) {', '.join(missing)} in this tester"
        )
    cal = row.get("calibration") or {}
    return _Mix(
        i,
        int(row["number"]),
        str(row["pattern"]),
        tuple(by_slot[int(d)] for d in row["pattern"]),
        str(row["measured_hex"]),
        cal.get("mix") if isinstance(cal.get("mix"), str) else None,
    )


def collect(
    readouts: Sequence[dict[str, Any]],
) -> tuple[dict[str, _Filament], list[_Mix]]:
    """The filaments and mixes of the readouts (validated: every mix layer has its reference)."""
    filaments: dict[str, _Filament] = {}
    mixes: list[_Mix] = []
    for i, r in enumerate(readouts):
        if not isinstance(r, dict) or r.get("schema") != "mix-readout/1":
            raise FitError(f"readout {i + 1}: schema must be mix-readout/1")
        by_slot = _add_references(r, filaments)
        mixes.extend(
            _candidate_mix(i, row, by_slot)
            for row in r.get("swatches", [])
            if row.get("kind") == "candidate"
        )
    if not filaments:
        raise FitError("the readouts hold no reference (pure) swatch")
    return filaments, mixes


def _mean_hex(
    layers: Sequence[str], colours: dict[str, tuple[float, float, float]]
) -> str:
    n = len(layers)
    mean = tuple(sum(colours[k][c] for k in layers) / n for c in range(3))
    return hex_from_linear((mean[0], mean[1], mean[2]))


def _solve(
    keys: list[str],
    mixes: list[_Mix],
    pure_arr: Any,
    lam: float,
) -> Any:
    """The fitted linear colours (one row per key); the pure colours when there is no mix."""
    import numpy as np
    from scipy.optimize import least_squares

    from slicing.mix_target import lab_of_linear

    if not mixes:
        return pure_arr
    index = {k: i for i, k in enumerate(keys)}
    meas_lab = lab_of_linear(np.array([linear_rgb(m.measured_hex) for m in mixes]))
    pure_lab = lab_of_linear(pure_arr)
    layer_idx = [[index[k] for k in m.layers] for m in mixes]

    def resid(x: Any) -> Any:
        c = x.reshape(len(keys), 3)
        model = np.array([c[idx].mean(axis=0) for idx in layer_idx])
        r_mix = (lab_of_linear(np.clip(model, 0.0, 1.0)) - meas_lab).ravel()
        r_pure = np.sqrt(lam) * (lab_of_linear(np.clip(c, 0.0, 1.0)) - pure_lab).ravel()
        return np.concatenate([r_mix, r_pure])

    sol = least_squares(
        resid, pure_arr.ravel(), bounds=(0.0, 1.0), xtol=1e-10, ftol=1e-10, gtol=1e-10
    )
    return sol.x.reshape(len(keys), 3)


def _filament_rows(
    keys: list[str],
    filaments: dict[str, _Filament],
    mixes: list[_Mix],
    pure: dict[str, tuple[float, float, float]],
    fitted: dict[str, tuple[float, float, float]],
) -> list[dict[str, Any]]:
    used = {k for m in mixes for k in m.layers}
    rows = []
    for k in keys:
        f = filaments[k]
        fh, ph = hex_from_linear(fitted[k]), hex_from_linear(pure[k])
        rows.append(
            {
                "key": k,
                "slot": f.slot,
                "filament_id": f.filament_id,
                "nominal_hex": f.nominal_hex,
                "pure_hex": ph,
                "fitted_hex": fh,
                "delta_e_nominal_pure": round(hex_delta_e(f.nominal_hex, ph), 2),
                "delta_e_pure_fitted": round(hex_delta_e(ph, fh), 2),
                "delta_e_nominal_fitted": round(hex_delta_e(f.nominal_hex, fh), 2),
                "readouts": len(f.pure),
                "mixes": sum(1 for m in mixes if k in m.layers),
                "constrained_by_mixes": k in used,
            }
        )
    return rows


def _mix_report_row(
    m: _Mix,
    nominal: dict[str, tuple[float, float, float]],
    pure: dict[str, tuple[float, float, float]],
    fitted: dict[str, tuple[float, float, float]],
) -> dict[str, Any]:
    n_hex = _mean_hex(m.layers, nominal)
    p_hex = _mean_hex(m.layers, pure)
    f_hex = _mean_hex(m.layers, fitted)
    return {
        "readout": m.readout,
        "number": m.number,
        "pattern": m.pattern,
        "mix": m.mix_key,
        "measured_hex": m.measured_hex,
        "nominal_hex": n_hex,
        "pure_hex": p_hex,
        "fitted_hex": f_hex,
        "delta_e_nominal": round(hex_delta_e(n_hex, m.measured_hex), 2),
        "delta_e_pure": round(hex_delta_e(p_hex, m.measured_hex), 2),
        "delta_e_fitted": round(hex_delta_e(f_hex, m.measured_hex), 2),
    }


def _summary(n_filaments: int, out_mix: list[dict[str, Any]]) -> dict[str, Any]:
    def mean(key: str) -> float | None:
        return (
            round(sum(r[key] for r in out_mix) / len(out_mix), 2) if out_mix else None
        )

    summary: dict[str, Any] = {
        "filaments": n_filaments,
        "mixes": len(out_mix),
        "mean_delta_e_nominal": mean("delta_e_nominal"),
        "mean_delta_e_pure": mean("delta_e_pure"),
        "mean_delta_e_fitted": mean("delta_e_fitted"),
    }
    summary["improved"] = bool(
        out_mix and summary["mean_delta_e_fitted"] < summary["mean_delta_e_nominal"]
    )
    return summary


def fit(
    readouts: Sequence[dict[str, Any]], lam: float = DEFAULT_LAMBDA
) -> dict[str, Any]:
    """Fit the effective colours (module doc); returns the `mix-fit/1` record."""
    import numpy as np

    if not lam >= 0:
        raise FitError(f"lambda {lam!r} must be a number >= 0")
    filaments, mixes = collect(readouts)
    keys = sorted(filaments)
    pure = {}
    for k in keys:
        r, g, b = (float(v) for v in np.mean(filaments[k].pure, axis=0))
        pure[k] = (r, g, b)
    pure_arr = np.array([pure[k] for k in keys])
    fitted_arr = _solve(keys, mixes, pure_arr, lam)
    fitted = {
        k: (float(fitted_arr[i][0]), float(fitted_arr[i][1]), float(fitted_arr[i][2]))
        for i, k in enumerate(keys)
    }
    nominal = {k: linear_rgb(filaments[k].nominal_hex) for k in keys}
    out_fil = _filament_rows(keys, filaments, mixes, pure, fitted)
    out_mix = [_mix_report_row(m, nominal, pure, fitted) for m in mixes]
    return {
        "schema": SCHEMA,
        "lambda": lam,
        "readouts": [
            {
                "manifest": r.get("manifest", {}).get("id"),
                "photo_sha256": (r.get("photo") or {}).get("sha256"),
            }
            for r in readouts
        ],
        "filaments": out_fil,
        "mixes": out_mix,
        "summary": _summary(len(out_fil), out_mix),
        "notes": [
            f"filament {f['key']} has no mix swatch: its fitted colour is its pure swatch"
            for f in out_fil
            if not f["constrained_by_mixes"]
        ],
    }


def _conflicting_ids(fils: list[Any]) -> list[str]:
    """One message per Spoolman filament id that the fit lists with two different fitted colours: the
    consumers (recipes, records) would otherwise each pick a different one."""
    seen: dict[int, str | None] = {}
    clash: set[int] = set()
    for f in fils:
        fid = f.get("filament_id") if isinstance(f, dict) else None
        if not isinstance(fid, int):
            continue
        hex_ = parse_hex(f.get("fitted_hex"))
        if fid in seen and seen[fid] != hex_:
            clash.add(fid)
        seen.setdefault(fid, hex_)
    return [f"filament {fid} is listed with different fitted_hex values" for fid in sorted(clash)]


def validate_fit(rec: Any) -> list[str]:
    """Problems with a `mix-fit/1` record ([] = valid)."""
    if not isinstance(rec, dict) or rec.get("schema") != SCHEMA:
        return [f"schema must be {SCHEMA}"]
    errs = []
    fils = rec.get("filaments")
    if not (isinstance(fils, list) and fils):
        return ["filaments must be a non-empty list"]
    for f in fils:
        ok = (
            isinstance(f, dict)
            and isinstance(f.get("slot"), int)
            and parse_hex(f.get("fitted_hex")) is not None
            and parse_hex(f.get("nominal_hex")) is not None
        )
        if not ok:
            errs.append(f"filament entry {f!r} needs slot, nominal_hex and fitted_hex")
    errs.extend(_conflicting_ids(fils))
    if not isinstance(rec.get("mixes"), list) or not isinstance(
        rec.get("summary"), dict
    ):
        errs.append("mixes (a list) and summary (an object) are required")
    return errs
