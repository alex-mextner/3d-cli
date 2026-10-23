"""layer_heights.py — per-object variable layer-height profiles for Orca 3MFs, capped inside colour-mix zones.

WHAT: builds, simulates, checks and writes `Metadata/layer_heights_profile.txt` (`object_id=N|z0;h0;z1;h1;...`,
  N = 1-based object index, values `%f`). `cap_profile` makes the profile for ONE object: a requirement
  curve h_req(z) on a 0.05 mm grid (`base` interpolated, else `hmax`), min'ed with `cap` inside each mix zone,
  smoothed by a Lipschitz envelope (neighbouring layers differ by at most `slope` * mean thickness),
  Douglas-Peucker compressed, with the last 3 mm rescaled so the simulated stack lands on the object top.
  `simulate_layers` is a line-by-line port of Orca's `generate_object_layers` (Slicing.cpp), so tests and
  callers see the layers Orca will really cut. Pure-Python ports of the chekhov-u1 `layer_profile.py` helpers.
WHY: a layer-alternating colour mix only reads as a blend when its stripes are thin (<= mix_pattern.LAYER_CAP_MM,
  0.12 mm), but printing a whole object that thin is slow; Orca takes a per-object profile from the 3MF, so
  only the mixed band is capped. Orca silently drops a profile whose last z differs from the object height
  by more than 1e-3, hence `check_profile` and the exact last-z rule.
INVARIANTS: stdlib only; pure functions except `write_profiles_3mf`, which never edits src and writes
  atomically (tmp file in the destination directory + rename); no layer whose mid-height lies in a zone is
  ever thicker than `cap` (every post-envelope step can only lower values; compression is biased downward in
  zones); profile values are mm; the writer never rewrites z values.
"""

from __future__ import annotations

import bisect
import os
import zipfile
from collections.abc import Mapping, Sequence

FIRST_LAYER_MM = 0.2
HMIN_MM = 0.08
HMAX_MM = 0.32
DEFAULT_SLOPE = 0.20

PROFILE_ENTRY = "Metadata/layer_heights_profile.txt"
_GRID_MM = 0.05
_COMPRESS_TOL = 0.002
_ROUND_GUARD = 1e-4  # profile values are rounded to 4 decimals; keep that from nudging a zone value over cap
_TAIL_MM = 3.0
_CAP_TOL = 1e-6
_CAP_ROUNDS = 7
_RETRY_LOWER_MM = 0.002
_LAST_Z_TOL = 1e-3
# Orca samples the profile near a layer BOTTOM (print_z + hmin/2), so a layer pair steps by ~slope * the thicker
# layer, not the mean; tightening the envelope keeps the step within slope * mean(h) plus compression slack
_ORCA_SAMPLE_MARGIN = 0.8
MIN_LAYER_MM = 0.01  # no real process prints thinner; a smaller (or non-finite) value would make the simulation crawl or never end


def _require_layer(value: float, what: str) -> None:
    if not value >= MIN_LAYER_MM:  # also rejects NaN
        raise ValueError(f"{what} {value!r} mm is below the {MIN_LAYER_MM:g} mm minimum")


def simulate_layers(
    profile: Sequence[float],
    height: float,
    first: float = FIRST_LAYER_MM,
    hmin: float = HMIN_MM,
) -> list[float]:
    """Layer TOP z values Orca cuts for a flat profile [z0,h0,z1,h1,...] (port of generate_object_layers).

    Raises ValueError when `first` or a profile height is below MIN_LAYER_MM or not a number."""
    _require_layer(first, "first layer")
    for h in profile[1::2]:
        _require_layer(h, "profile layer height")
    zs = list(profile[0::2])
    hs = list(profile[1::2])
    tops = [first]
    print_z = first
    idx = 0
    n = len(zs)
    slice_z = print_z + 0.5 * hmin
    while slice_z < height:
        nxt = idx + 1
        while nxt < n and slice_z >= zs[nxt]:
            idx, nxt = nxt, nxt + 1
        h = hs[idx]
        if nxt < n:
            h = hs[idx] + (hs[nxt] - hs[idx]) * (slice_z - zs[idx]) / (
                zs[nxt] - zs[idx]
            )
        slice_z = print_z + 0.5 * h
        if slice_z >= height:
            break
        print_z += h
        tops.append(print_z)
        slice_z = print_z + 0.5 * hmin
    return tops


def uniform_layers(
    height: float, layer_h: float, first: float = FIRST_LAYER_MM
) -> list[float]:
    """Layer tops without a profile; the last top may land within half a layer of `height`.

    Raises ValueError when `layer_h` or `first` is below MIN_LAYER_MM or not a number."""
    _require_layer(layer_h, "layer height")
    _require_layer(first, "first layer")
    tops = [first]
    while tops[-1] + 0.5 * layer_h < height:
        tops.append(tops[-1] + layer_h)
    return tops


def thicknesses(tops: Sequence[float]) -> list[float]:
    """Per-layer thickness; the first layer starts at z=0."""
    prev = 0.0
    out: list[float] = []
    for t in tops:
        out.append(t - prev)
        prev = t
    return out


def layers_in_zone(tops: Sequence[float], z_lo: float, z_hi: float) -> list[int]:
    """Indices of layers whose MID height lies in [z_lo, z_hi]."""
    th = thicknesses(tops)
    return [i for i, (t, h) in enumerate(zip(tops, th, strict=True)) if z_lo <= t - 0.5 * h <= z_hi]


def parse_profiles(text: str) -> dict[int, list[float]]:
    """Parse layer_heights_profile.txt; blank lines are skipped, a malformed line raises ValueError(line N)."""
    out: dict[int, list[float]] = {}
    for no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            head, _, body = line.partition("|")
            key, eq, num = head.partition("=")
            if key != "object_id" or not eq or not body:
                raise ValueError("bad shape")
            out[int(num)] = [float(v) for v in body.split(";")]
        except ValueError as exc:
            raise ValueError(
                f"layer_heights_profile line {no}: malformed {raw!r}"
            ) from exc
    return out


def profile_line(object_id: int, profile: Sequence[float]) -> str:
    """One newline-terminated profile line, values written `%f`. Never rewrites z (see `check_profile`)."""
    if len(profile) % 2 or not profile:
        raise ValueError(
            f"profile for object {object_id} needs an even, non-zero number of values"
        )
    return f"object_id={object_id}|" + ";".join(f"{v:f}" for v in profile) + "\n"


def check_profile(profile: Sequence[float], height: float) -> str | None:
    """Error text when Orca would drop or misread the profile, else None."""
    if not profile or len(profile) % 2:
        return "profile length must be even and non-zero"
    zs = profile[0::2]
    hs = profile[1::2]
    if any(b <= a for a, b in zip(zs, zs[1:], strict=False)):
        return "z values must be strictly increasing"
    if any(not h >= MIN_LAYER_MM for h in hs):
        return f"layer heights must be at least {MIN_LAYER_MM:g} mm"
    if abs(zs[-1] - height) > _LAST_Z_TOL:
        return f"last z {zs[-1]:.4f} differs from object height {height:.4f} by more than {_LAST_Z_TOL}"
    return None


def _lipschitz(h: list[float], zs: list[float], slope: float) -> list[float]:
    """Min-plus envelope: the largest curve <= h that changes by at most `slope` mm per mm of z."""
    out = list(h)
    for i in range(1, len(out)):
        out[i] = min(out[i], out[i - 1] + slope * (zs[i] - zs[i - 1]))
    for i in range(len(out) - 2, -1, -1):
        out[i] = min(out[i], out[i + 1] + slope * (zs[i + 1] - zs[i]))
    return out


def _compress(
    zs: list[float], hs: list[float], tol: float
) -> tuple[list[float], list[float]]:
    """Douglas-Peucker on (z, h): fewest points whose piecewise-linear curve stays within tol of h(z)."""
    keep = [False] * len(zs)
    keep[0] = keep[-1] = True
    stack = [(0, len(zs) - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        worst, k = -1.0, a
        for i in range(a + 1, b):
            lin = hs[a] + (hs[b] - hs[a]) * (zs[i] - zs[a]) / (zs[b] - zs[a])
            err = abs(lin - hs[i])
            if err > worst:
                worst, k = err, i
        if worst > tol:
            keep[k] = True
            stack += [(a, k), (k, b)]
    idx = [i for i, kept in enumerate(keep) if kept]
    return [zs[i] for i in idx], [hs[i] for i in idx]


def _interp(base: Sequence[float], z: float) -> float:
    bz = base[0::2]
    bh = base[1::2]
    if z <= bz[0]:
        return bh[0]
    if z >= bz[-1]:
        return bh[-1]
    j = bisect.bisect_right(bz, z)
    return bh[j - 1] + (bh[j] - bh[j - 1]) * (z - bz[j - 1]) / (bz[j] - bz[j - 1])


def cap_profile(
    height: float,
    zones: Sequence[tuple[float, float]],
    *,
    cap: float = 0.12,
    base: Sequence[float] | None = None,
    hmin: float = HMIN_MM,
    hmax: float = HMAX_MM,
    slope: float = DEFAULT_SLOPE,
    first: float = FIRST_LAYER_MM,
) -> list[float]:
    """Flat profile [z0,h0,...] for ONE object of `height` mm with layers <= `cap` inside `zones`.

    Zones are (z_lo, z_hi) in object coordinates from the bottom, clipped to [0, height]; an empty or
    inverted zone raises ValueError, as does cap outside [hmin, hmax].
    """
    if not hmin <= cap <= hmax:
        raise ValueError(f"cap {cap} must lie within [hmin {hmin}, hmax {hmax}]")
    if height <= 0:
        raise ValueError("height must be > 0")
    clipped: list[tuple[float, float]] = []
    for z_lo, z_hi in zones:
        if z_hi <= z_lo:
            raise ValueError(f"empty or inverted zone ({z_lo}, {z_hi})")
        lo, hi = max(z_lo, 0.0), min(z_hi, height)
        if hi > lo:
            clipped.append((lo, hi))

    for rnd in range(_CAP_ROUNDS):
        # each retry widens the zone-entry margin and lowers zone values further, both geometrically
        margin = _GRID_MM * 2**rnd
        extra = _COMPRESS_TOL + _ROUND_GUARD + _RETRY_LOWER_MM * (2**rnd - 1)
        flat = _build_flat(height, clipped, cap, base, hmin, hmax, slope, first, margin, extra)
        if flat is not None:
            return flat
    raise ValueError(
        f"cannot keep layers <= cap {cap} inside zones {clipped} with hmin {hmin}, slope {slope}"
    )


def _in_zones(zones: Sequence[tuple[float, float]], z: float, margin: float = 0.0) -> bool:
    return any(lo - margin <= z <= hi + margin for lo, hi in zones)


def _zone_violation(tops: Sequence[float], zones: Sequence[tuple[float, float]], cap: float) -> bool:
    """True when a layer (other than the fixed first one) with its MID in a zone is thicker than cap."""
    th = thicknesses(tops)
    return any(
        h > cap + _CAP_TOL and _in_zones(zones, t - 0.5 * h)
        for t, h in list(zip(tops, th, strict=True))[1:]
    )


def _build_flat(
    height: float,
    zones: Sequence[tuple[float, float]],
    cap: float,
    base: Sequence[float] | None,
    hmin: float,
    hmax: float,
    slope: float,
    first: float,
    margin: float,
    extra: float,
) -> list[float] | None:
    """One build attempt; None when no tail-landing candidate keeps every zone layer <= cap."""
    n = int(height / _GRID_MM)
    zs = [i * _GRID_MM for i in range(n + 1)]
    if height - zs[-1] > 1e-9:
        zs.append(height)
    else:
        zs[-1] = height

    zone_flags = [_in_zones(zones, z, margin) for z in zs]
    req = [hmax if base is None else min(max(_interp(base, z), hmin), hmax) for z in zs]
    req = [min(r, cap) if f else r for r, f in zip(req, zone_flags, strict=True)]
    env = [min(max(h, hmin), hmax) for h in _lipschitz(req, zs, slope * _ORCA_SAMPLE_MARGIN)]
    lowered = [max(h - extra, hmin) if f else h for h, f in zip(env, zone_flags, strict=True)]

    best: tuple[float, list[float]] | None = None
    for step in range(26):
        sc = 1.0 - 0.01 * step
        tail = [
            max(h * (1 - (1 - sc) * min(max((z - (height - _TAIL_MM)) / _TAIL_MM, 0.0), 1.0)), hmin)
            if z > height - _TAIL_MM
            else h
            for z, h in zip(zs, lowered, strict=True)
        ]
        zz, hh = _compress(zs, [round(h, 4) for h in tail], _COMPRESS_TOL)
        flat: list[float] = []
        for z, h in zip(zz, hh, strict=True):
            flat += [round(z, 4), h]
        flat[-2] = round(height, 4)
        tops = simulate_layers(flat, height, first, hmin)
        if _zone_violation(tops, zones, cap):
            continue
        err = abs(tops[-1] - height)
        if best is None or err < best[0] - 1e-9:
            best = (err, flat)
    return None if best is None else best[1]


def write_profiles_3mf(
    src: str | os.PathLike[str],
    dst: str | os.PathLike[str],
    profiles: Mapping[int, Sequence[float]],
) -> None:
    """Copy 3MF `src` to `dst`, replacing/adding the given objects' lines in the layer-heights profile entry.

    Other entries keep their bytes, order and compression; other objects' existing profile lines are kept.
    """
    src_p, dst_p = os.fspath(src), os.fspath(dst)
    if os.path.abspath(src_p) == os.path.abspath(dst_p) or (
        os.path.exists(dst_p) and os.path.samefile(src_p, dst_p)
    ):
        raise ValueError("dst must differ from src")
    tmp = f"{dst_p}.tmp{os.getpid()}"
    try:
        with zipfile.ZipFile(src_p) as zin, zipfile.ZipFile(tmp, "w") as zout:
            existing: dict[int, list[float]] = {}
            if PROFILE_ENTRY in zin.namelist():
                existing = parse_profiles(zin.read(PROFILE_ENTRY).decode("utf-8"))
            existing.update({k: list(v) for k, v in profiles.items()})
            body = "".join(profile_line(k, v) for k, v in existing.items()).encode(
                "utf-8"
            )
            written = False
            for info in zin.infolist():
                data = (
                    body if info.filename == PROFILE_ENTRY else zin.read(info.filename)
                )
                written = written or info.filename == PROFILE_ENTRY
                zout.writestr(info, data, compress_type=info.compress_type)
            if not written:
                zout.writestr(PROFILE_ENTRY, body, compress_type=zipfile.ZIP_DEFLATED)
        os.replace(tmp, dst_p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
