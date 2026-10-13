#!/usr/bin/env python3
"""halves_check.py — do the two halves of a part split for printing still fit together?

A model that is too big (or too overhanging) is often cut in two along a plane, each half
printed lying on its cut face, and the halves glued back with dowels. If the generator
built the two halves independently, their cut faces may no longer be the same shape: one
half sticks out past the other, dowel holes miss each other, or a crumb that only touched
the other half floats on one face. None of this shows up in a per-part mesh check.

This tool brings both halves of every marked pair into their common ASSEMBLY frame, takes
the cut face of each (a section 0.05 mm inside the material), rasterises both faces on the
same grid and compares them:

  outline   max deviation between the faces (how far one half sticks out past the other,
            or into the other's dowel hole) — must be <= --tol; IoU >= --min-iou
  dowels    holes through the face: same count (= halves_dowels if given), centres within
            --tol, equal diameters within --tol
  size      face width/height equal within --tol (catches a shortened tip or base)
  islands   no face island without a partner on the other face
  side      each half lies entirely on its own side of the cut plane

PAIR METADATA (per object, Orca/Bambu `Metadata/model_settings.config` <object> metadata
keys, or `<metadata name=…>` inside the <object> of 3D/3dmodel.model):
  halves_pair         pair id shared by both halves ("TA"); "none" = not a half (silences
                      the name heuristic for objects named like X1/X2)
  halves_side         "1" or "2"
  halves_to_assembly  16 numbers, row-major 4x4: the object's LOCAL coordinates (its
                      components assembled, before the build-item transform — so moving or
                      rotating the object on the plate does not invalidate it) -> assembly
                      frame, mm
  halves_local_min    "x y z": min corner of the object's local bbox when the matrix was
                      written; a slicer that re-centres the object shifts the local frame,
                      the difference is undone before the matrix is applied
  halves_cut          "nx ny nz d": cut plane n·p = d in the assembly frame; side 1 lies on
                      n·p >= d, side 2 on n·p <= d (both halves carry the same value)
  halves_dowels       optional: number of dowel holes each face must have

STL pairs (`--pair A.stl B.stl`, comma-join colour parts) must already share the assembly
frame; the cut plane is the largest flat face they share unless --plane is given.

Exit: 0 PASS, 1 FAIL (or unmarked numbered halves with --strict-names, or no pairs with
--require-pairs), 2 usage / load / metadata error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import numpy as np

Mesh = tuple[np.ndarray, np.ndarray]          # (V float (n,3), F int (m,3))

CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PROD = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
UNITS = {"micron": 0.001, "millimeter": 1.0, "centimeter": 10.0, "inch": 25.4, "foot": 304.8, "meter": 1000.0}
KEYS = ("halves_pair", "halves_side", "halves_to_assembly", "halves_local_min", "halves_cut", "halves_dowels")
# One cut face's raster: at the cap the masks, difference grid and distance transforms
# already take hundreds of MB; a 300x300 mm face still fits at the default 0.05 mm.
MAX_RASTER_PIXELS = 40_000_000


class MetadataError(ValueError):
    """the pair metadata of a 3MF is missing a partner or cannot be parsed"""


class RasterTooLarge(ValueError):
    """--res is too fine for the cut face: the raster would exceed MAX_RASTER_PIXELS"""


@dataclass
class Half:
    name: str
    meshes: list[Mesh]                       # assembly frame


@dataclass
class Pair:
    pair_id: str
    a: Half                                  # side 1: n·p >= d
    b: Half                                  # side 2: n·p <= d
    normal: np.ndarray
    offset: float
    dowels: int | None = None
    source: str = ""


@dataclass
class Options:
    tol: float = 0.2                         # mm
    res: float = 0.05                        # raster pixel, mm
    probe: float = 0.05                      # section depth inside each half, mm
    min_iou: float = 0.9
    min_area: float = 0.3                    # mm²: smaller holes / islands are noise


# ---------------------------------------------------------------- geometry
def plane_basis(n: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """in-plane axes (e1, e2); e2 is world z projected into the plane when it can be (images upright)"""
    n = np.asarray(n, float) / np.linalg.norm(n)
    up = np.array([0.0, 0.0, 1.0])
    if abs(n @ up) < 0.9:
        e2 = up - (up @ n) * n
        e2 /= np.linalg.norm(e2)
        e1 = np.cross(n, e2)
    else:
        x = np.array([1.0, 0.0, 0.0]) - n[0] * n
        e1 = x / np.linalg.norm(x)
        e2 = np.cross(n, e1)
    return e1, e2


def section_segments(V: np.ndarray, F: np.ndarray, n: np.ndarray, d: float) -> np.ndarray:
    """(k, 2, 3) line segments where the plane n·p = d cuts the triangles"""
    s = np.asarray(V, float) @ np.asarray(n, float) - d
    s = np.where(s == 0.0, 1e-12, s)                   # a vertex on the plane counts as above it
    T = np.asarray(V, float)[F]
    S = s[F]
    pos = S > 0
    cross = pos.any(1) & ~pos.all(1)
    T, S = T[cross], S[cross]
    pts, hits = [], []
    for i, j in ((0, 1), (1, 2), (2, 0)):
        si, sj = S[:, i], S[:, j]
        hit = (si > 0) != (sj > 0)
        t = np.where(hit, si / np.where(hit, si - sj, 1.0), 0.0)
        pts.append(T[:, i] + (T[:, j] - T[:, i]) * t[:, None])
        hits.append(hit)
    P, H = np.stack(pts, 1), np.stack(hits, 1)               # every crossing triangle has exactly two crossing edges
    two = np.argsort(~H, axis=1, kind="stable")[:, :2]
    return P[np.arange(len(P))[:, None], two]


@dataclass
class Grid:
    u0: float
    w0: float
    res: float
    width: int
    height: int

    @classmethod
    def around(cls, segs: list[np.ndarray], e1: np.ndarray, e2: np.ndarray, res: float, margin: float = 2.0) -> Grid:
        P = np.vstack([s.reshape(-1, 3) for s in segs if len(s)])
        u, w = P @ e1, P @ e2
        u0, w0 = float(u.min()) - margin, float(w.min()) - margin
        width = int(np.ceil((u.max() + margin - u0) / res))
        height = int(np.ceil((w.max() + margin - w0) / res))
        # Checked before any mask exists: a tiny --res on a large face would otherwise
        # allocate masks, a difference grid and distance transforms of 10^9+ pixels.
        if width * height > MAX_RASTER_PIXELS:
            raise RasterTooLarge(f"cut face raster {width}x{height} px at --res {res} mm exceeds "
                                 f"{MAX_RASTER_PIXELS:,} px; use a coarser --res")
        return cls(u0, w0, res, width, height)

    def uw(self, row: float, col: float) -> tuple[float, float]:
        return self.u0 + (col + 0.5) * self.res, self.w0 + (row + 0.5) * self.res


def fill_mask(seg: np.ndarray, grid: Grid, e1: np.ndarray, e2: np.ndarray) -> np.ndarray:
    """scanline fill (even-odd per closed loop set) of the section segments: (height, width) bool"""
    mask = np.zeros((grid.height, grid.width), bool)
    if not len(seg):
        return mask
    u, w = seg @ e1, seg @ e2                                     # (k, 2) each
    keep = w[:, 0] != w[:, 1]
    u, w = u[keep], w[keep]
    wlo, whi = w.min(1), w.max(1)
    # pixel-centre rows yc with wlo <= yc < whi (half-open: every closed loop crosses a row an even number of times)
    r_lo = np.ceil((wlo - grid.w0) / grid.res - 0.5).astype(np.int64)
    r_hi = np.ceil((whi - grid.w0) / grid.res - 0.5).astype(np.int64)
    cnt = np.clip(r_hi - r_lo, 0, None)
    if not cnt.sum():
        return mask
    idx = np.repeat(np.arange(len(u)), cnt)
    rows = np.repeat(r_lo, cnt) + (np.arange(cnt.sum()) - np.repeat(np.cumsum(cnt) - cnt, cnt))
    yc = grid.w0 + (rows + 0.5) * grid.res
    u0, u1, w0, w1 = u[idx, 0], u[idx, 1], w[idx, 0], w[idx, 1]
    xs = u0 + (yc - w0) * (u1 - u0) / (w1 - w0)
    order = np.lexsort((xs, rows))
    rows, xs = rows[order], xs[order]
    start = np.r_[True, rows[1:] != rows[:-1]]
    first_of_row = np.maximum.accumulate(np.where(start, np.arange(len(rows)), 0))
    k = np.arange(len(rows)) - first_of_row
    pair = (k % 2 == 0) & (np.r_[rows[1:] == rows[:-1], False])
    i = np.nonzero(pair)[0]
    c_lo = np.clip(np.ceil((xs[i] - grid.u0) / grid.res - 0.5).astype(np.int64), 0, grid.width)
    c_hi = np.clip(np.ceil((xs[i + 1] - grid.u0) / grid.res - 0.5).astype(np.int64), 0, grid.width)
    r = rows[i]
    ok = (r >= 0) & (r < grid.height) & (c_hi > c_lo)
    diff = np.zeros((grid.height, grid.width + 1), np.int32)
    np.add.at(diff, (r[ok], c_lo[ok]), 1)
    np.add.at(diff, (r[ok], c_hi[ok]), -1)
    return np.cumsum(diff, axis=1)[:, :-1] > 0


def face_mask(segs: list[np.ndarray], grid: Grid, e1: np.ndarray, e2: np.ndarray) -> np.ndarray:
    """union of the per-mesh fills: colour parts are filled one by one, so a shared seam is not a hole"""
    mask = np.zeros((grid.height, grid.width), bool)
    for seg in segs:
        mask |= fill_mask(seg, grid, e1, e2)
    return mask


def _components(mask: np.ndarray, grid: Grid, min_px: float, connectivity: int) -> tuple[np.ndarray, list[dict]]:
    """labelled connected pieces of the mask (area >= min_px pixels) with their centre in plane coordinates"""
    from scipy import ndimage

    st = ndimage.generate_binary_structure(2, connectivity)
    lab, num = ndimage.label(mask, structure=st)
    out: list[dict] = []
    if not num:
        return lab, out
    idx = np.arange(1, num + 1)
    area = ndimage.sum(np.ones_like(lab), lab, idx)
    cen = ndimage.center_of_mass(np.ones_like(lab), lab, idx)
    border = set(np.unique(np.r_[lab[0], lab[-1], lab[:, 0], lab[:, -1]]).tolist())
    for k, a, c in zip(idx, area, cen):
        if a < min_px:
            continue
        u, w = grid.uw(c[0], c[1])
        out.append(dict(label=int(k), area_mm2=float(a) * grid.res ** 2, u=u, w=w, border=int(k) in border))
    return lab, out


def holes_of(mask: np.ndarray, grid: Grid, min_area: float) -> list[dict]:
    """holes through the face: background regions that do not reach the grid border"""
    holes = [h for h in _components(~mask, grid, min_area / grid.res ** 2, 1)[1] if not h["border"]]
    for h in holes:
        h["d"] = 2 * float(np.sqrt(h["area_mm2"] / np.pi))
    return holes


def _extent(mask: np.ndarray, res: float) -> tuple[float, float]:
    rows, cols = np.nonzero(mask.any(1))[0], np.nonzero(mask.any(0))[0]
    if not len(rows):
        return 0.0, 0.0
    return float((cols[-1] - cols[0] + 1) * res), float((rows[-1] - rows[0] + 1) * res)


def check_pair(pair: Pair, opts: Options) -> dict:
    from scipy import ndimage

    n = np.asarray(pair.normal, float)
    n = n / np.linalg.norm(n)
    d = float(pair.offset)
    e1, e2 = plane_basis(n)
    rep: dict = dict(pair=pair.pair_id, a=pair.a.name, b=pair.b.name, file=pair.source,
                     plane=[*map(float, n), d], failures=[], warnings=[])
    fails: list[str] = rep["failures"]
    # each half on its own side of the cut
    for half, sign in ((pair.a, 1.0), (pair.b, -1.0)):
        s = np.concatenate([V @ n - d for V, _ in half.meshes]) * sign
        if s.min() < -opts.tol:
            fails.append(f"side: {half.name} reaches {-s.min():.2f} mm across the cut plane onto the other half's side")
    segs = [[section_segments(V, F, n, d + sign * opts.probe) for V, F in hh.meshes]
            for hh, sign in ((pair.a, 1.0), (pair.b, -1.0))]
    for hf, sg in zip((pair.a, pair.b), segs):
        if not sum(len(x) for x in sg):
            fails.append(f"face: {hf.name} has no cut face at the plane (n={n.round(4).tolist()}, d={d:.3f})")
    if any(not sum(len(x) for x in sg) for sg in segs):
        rep.update(status="FAIL")
        return rep
    grid = Grid.around(segs[0] + segs[1], e1, e2, opts.res)
    A = face_mask(segs[0], grid, e1, e2)
    B = face_mask(segs[1], grid, e1, e2)
    rep["_grid"], rep["_masks"] = grid, (A, B)
    inter, union = float((A & B).sum()), float((A | B).sum())
    rep["iou"] = inter / union if union else 0.0
    rep["area_a_mm2"], rep["area_b_mm2"] = float(A.sum()) * opts.res ** 2, float(B.sum()) * opts.res ** 2
    # outline deviation: how far a pixel of one face lies from the other face
    da = ndimage.distance_transform_edt(~B) * opts.res        # distance to B, for pixels of A
    db = ndimage.distance_transform_edt(~A) * opts.res
    dev_a = float(da[A].max()) if A.any() else 0.0
    dev_b = float(db[B].max()) if B.any() else 0.0
    rep["overhang_a_mm"], rep["overhang_b_mm"] = dev_a, dev_b
    rep["max_dev_mm"] = max(dev_a, dev_b)
    if rep["max_dev_mm"] > opts.tol:
        worst = np.unravel_index(np.argmax(np.where(A, da, 0)), A.shape) if dev_a >= dev_b else \
            np.unravel_index(np.argmax(np.where(B, db, 0)), B.shape)
        u, w = grid.uw(float(worst[0]), float(worst[1]))
        p = u * e1 + w * e2 + d * n
        who = pair.a.name if dev_a >= dev_b else pair.b.name
        fails.append(f"outline: cut faces differ by up to {rep['max_dev_mm']:.2f} mm (> tol {opts.tol}) — {who} sticks out "
                     f"there; {pair.a.name}-only {float((A & ~B).sum()) * opts.res ** 2:.1f} mm², "
                     f"{pair.b.name}-only {float((B & ~A).sum()) * opts.res ** 2:.1f} mm²; worst at assembly "
                     f"({p[0]:.2f}, {p[1]:.2f}, {p[2]:.2f})")
    if rep["iou"] < opts.min_iou:
        fails.append(f"outline: face IoU {rep['iou']:.3f} < {opts.min_iou}")
    # face size (tip / base / width)
    sa, sb = _extent(A, opts.res), _extent(B, opts.res)
    rep["face_size_a"], rep["face_size_b"] = [round(v, 3) for v in sa], [round(v, 3) for v in sb]
    for k, label in ((0, "width"), (1, "height")):
        if abs(sa[k] - sb[k]) > opts.tol + opts.res:
            fails.append(f"size: face {label} {sa[k]:.2f} mm ({pair.a.name}) vs {sb[k]:.2f} mm ({pair.b.name})")
    # silhouettes of the whole halves along the plane axes (information: an off-centre cut differs legitimately)
    for key, half in (("silhouette_a", pair.a), ("silhouette_b", pair.b)):
        P = np.vstack([V for V, _ in half.meshes])
        rep[key] = [round(float(np.ptp(P @ e1)), 3), round(float(np.ptp(P @ e2)), 3)]
    # dowel holes
    ha, hb = holes_of(A, grid, opts.min_area), holes_of(B, grid, opts.min_area)
    rep["holes_a"] = [dict(u=round(h["u"], 3), w=round(h["w"], 3), d=round(h["d"], 3)) for h in ha]
    rep["holes_b"] = [dict(u=round(h["u"], 3), w=round(h["w"], 3), d=round(h["d"], 3)) for h in hb]
    from scipy.optimize import linear_sum_assignment

    pairs = []
    cost = np.array([[np.hypot(b_["u"] - a_["u"], b_["w"] - a_["w"]) for b_ in hb] for a_ in ha]).reshape(len(ha), len(hb))
    for i, j in zip(*linear_sum_assignment(cost)):        # best overall matching, not greedy
        h, off = ha[i], float(cost[i, j])
        pairs.append(dict(u=round(h["u"], 3), w=round(h["w"], 3), offset_mm=round(off, 3), d_a=round(h["d"], 3), d_b=round(hb[j]["d"], 3)))
    rep["hole_pairs"] = pairs
    if len(ha) != len(hb):
        fails.append(f"dowels: {len(ha)} holes in {pair.a.name} vs {len(hb)} in {pair.b.name}")
    if pair.dowels is not None and (len(ha) != pair.dowels or len(hb) != pair.dowels):
        fails.append(f"dowels: expected {pair.dowels} holes per face, found {len(ha)} / {len(hb)}")
    for hp in pairs:
        if hp["offset_mm"] > opts.tol:
            fails.append(f"dowels: hole at ({hp['u']:.2f}, {hp['w']:.2f}) misses its partner by {hp['offset_mm']:.2f} mm")
        if abs(hp["d_a"] - hp["d_b"]) > opts.tol:
            fails.append(f"dowels: hole at ({hp['u']:.2f}, {hp['w']:.2f}) is Ø{hp['d_a']:.2f} vs Ø{hp['d_b']:.2f} mm")
    if not ha and not hb and pair.dowels is None:
        rep["warnings"].append("no dowel holes on the cut face")
    # islands: every piece of one face needs a partner on the other
    la, ia = _components(A, grid, opts.min_area / opts.res ** 2, 2)
    lb, ib = _components(B, grid, opts.min_area / opts.res ** 2, 2)
    rep["islands"] = [len(ia), len(ib)]
    for lab, other, isl, who in ((la, B, ia, pair.a.name), (lb, A, ib, pair.b.name)):
        for c in isl:
            piece = lab == c["label"]
            if (piece & other).sum() < 0.5 * piece.sum():
                fails.append(f"islands: {who} has a {c['area_mm2']:.1f} mm² face piece at ({c['u']:.2f}, {c['w']:.2f}) "
                             f"with no partner on the other half")
    rep["status"] = "FAIL" if fails else "PASS"
    return rep


# ---------------------------------------------------------------- PNG overlay (stdlib writer)
def write_png(path: str, rep: dict, max_px: int = 1800) -> None:
    """common face grey, first-half-only red, second-half-only blue, holes outlined black; e2 (up) at the top"""
    A, B = rep["_masks"]
    step = max(1, int(np.ceil(max(A.shape) / max_px)))
    A, B = A[::-1][::step, ::step], B[::-1][::step, ::step]
    img = np.full(A.shape + (3,), 255, np.uint8)
    img[A & B] = (185, 185, 185)
    img[A & ~B] = (214, 39, 40)
    img[B & ~A] = (31, 119, 180)
    edge = (A | B) & ~(np.roll(A | B, 1, 0) & np.roll(A | B, -1, 0) & np.roll(A | B, 1, 1) & np.roll(A | B, -1, 1))
    img[edge & (A & B)] = (90, 90, 90)
    h, w = A.shape
    raw = b"".join(b"\x00" + img[r].tobytes() for r in range(h))
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


# ---------------------------------------------------------------- 3MF
def _floats(text: str, n: int, key: str, obj: str) -> np.ndarray:
    try:
        v = np.array([float(x) for x in text.split()], float)
    except ValueError as e:
        raise MetadataError(f"{obj}: {key} is not numeric: {text!r}") from e
    if len(v) != n:
        raise MetadataError(f"{obj}: {key} needs {n} numbers, got {len(v)}: {text!r}")
    if not np.all(np.isfinite(v)):
        raise MetadataError(f"{obj}: {key} needs {n} finite numbers, got {text!r}")
    return v


def _dowel_count(pid: str, values: Sequence[str | None]) -> int | None:
    """halves_dowels from both halves: each, when present, a non-negative integer; both equal"""
    counts = []
    for text in values:
        if text is None or text == "":
            continue
        try:
            count = int(text)
        except ValueError as e:
            raise MetadataError(f"pair {pid}: halves_dowels must be a whole number, got {text!r}") from e
        if count < 0:
            raise MetadataError(f"pair {pid}: halves_dowels must not be negative, got {text!r}")
        counts.append(count)
    if len(set(counts)) > 1:
        raise MetadataError(f"pair {pid}: the halves disagree on halves_dowels ({counts[0]} vs {counts[1]})")
    return counts[0] if counts else None


def _transform(attr: str | None) -> np.ndarray:
    M = np.eye(4)
    if attr:
        try:
            v = [float(x) for x in attr.split()]
        except ValueError as e:
            raise MetadataError(f"component transform is not numeric: {attr!r}") from e
        if len(v) != 12 or not np.all(np.isfinite(v)):
            raise MetadataError(f"component transform needs 12 finite numbers, got {attr!r}")
        M[:3, :3] = np.array(v[:9]).reshape(3, 3).T      # 3MF: m00 m01 m02 m10 … row vectors (p' = p · M)
        M[:3, 3] = v[9:12]
    return M


class _Package:
    def __init__(self, path: str):
        self.zf = zipfile.ZipFile(path)
        self.models: dict[str, dict[str, ET.Element]] = {}
        unit = ET.fromstring(self.zf.read("3D/3dmodel.model")).get("unit", "millimeter")
        if unit not in UNITS:
            raise MetadataError(f"unknown 3MF model unit {unit!r}")
        self.scale = UNITS[unit]                       # model units -> mm (the metadata is in mm)

    def objects(self, part: str) -> dict[str, ET.Element]:
        part = part.lstrip("/")
        if part not in self.models:
            root = ET.fromstring(self.zf.read(part))
            self.models[part] = {o.get("id", ""): o for o in root.iter(f"{{{CORE}}}object")}
        return self.models[part]

    def meshes(self, part: str, oid: str, M: np.ndarray, depth: int = 0) -> list[Mesh]:
        if depth > 16:
            raise MetadataError(f"component nesting too deep at object {oid}")
        obj = self.objects(part).get(oid)
        if obj is None:
            raise MetadataError(f"object {oid} not found in {part}")
        out: list[Mesh] = []
        mesh = obj.find(f"{{{CORE}}}mesh")
        if mesh is not None:
            try:
                V = np.array([[float(v.get(k, "0")) for k in "xyz"] for v in mesh.iter(f"{{{CORE}}}vertex")], float).reshape(-1, 3)
                F = np.array([[int(t.get(k, "0")) for k in ("v1", "v2", "v3")] for t in mesh.iter(f"{{{CORE}}}triangle")], np.int64).reshape(-1, 3)
            except ValueError as e:
                raise MetadataError(f"object {oid}: mesh vertex/triangle attribute is not numeric ({e})") from e
            out.append((V @ M[:3, :3].T + M[:3, 3], F))
        for c in obj.iter(f"{{{CORE}}}component"):
            sub = c.get(f"{{{PROD}}}path") or part
            out += self.meshes(sub, c.get("objectid", ""), M @ _transform(c.get("transform")), depth + 1)
        return out


def _object_meta(pkg: _Package) -> dict[str, dict[str, str]]:
    """object id -> metadata (name + halves_* keys) from model_settings.config and 3dmodel.model"""
    meta: dict[str, dict[str, str]] = {}
    for oid, obj in pkg.objects("3D/3dmodel.model").items():
        m = meta.setdefault(oid, {})
        if obj.get("name"):
            m["name"] = obj.get("name", "")
        for md in obj.findall(f"{{{CORE}}}metadatagroup/{{{CORE}}}metadata") + obj.findall(f"{{{CORE}}}metadata"):
            if md.get("name") in KEYS:
                m[md.get("name", "")] = (md.text or md.get("value") or "").strip()
    if "Metadata/model_settings.config" in pkg.zf.namelist():
        cfg = ET.fromstring(pkg.zf.read("Metadata/model_settings.config"))
        for o in cfg.findall("object"):
            m = meta.setdefault(o.get("id", ""), {})
            for md in o.findall("metadata"):
                k = md.get("key", "")
                if k == "name" or k in KEYS:
                    m[k] = md.get("value", "")
    for oid, m in meta.items():
        m["name"] = (m.get("name") or f"object {oid}").split()[0]
    return meta


def load_3mf(path: str | Path) -> tuple[list[Pair], list[str]]:
    """pairs marked in the 3MF (assembly frame) + warnings for numbered objects that look like unmarked halves"""
    path = str(path)
    pkg = _Package(path)
    meta = _object_meta(pkg)
    built = {it.get("objectid", "") for it in ET.fromstring(pkg.zf.read("3D/3dmodel.model")).iter(f"{{{CORE}}}item")}
    groups: dict[str, dict[int, str]] = {}
    for oid, m in meta.items():
        pid = m.get("halves_pair", "").strip()
        if not pid or pid.lower() == "none":
            continue
        side = m.get("halves_side", "").strip()
        if side not in ("1", "2"):
            raise MetadataError(f"{m['name']}: halves_side must be 1 or 2, got {side!r}")
        g = groups.setdefault(pid, {})
        if int(side) in g:
            raise MetadataError(f"pair {pid}: two objects claim side {side} ({meta[g[int(side)]]['name']}, {m['name']})")
        g[int(side)] = oid
    pairs = []
    for pid, g in sorted(groups.items()):
        if set(g) != {1, 2}:
            have = ", ".join(f"{meta[o]['name']} (side {s})" for s, o in g.items())
            raise MetadataError(f"pair {pid}: only {have} in this file — both halves must be in the same 3MF")
        halves, cuts = [], []
        for sd in (1, 2):
            oid = g[sd]
            m = meta[oid]
            if "halves_to_assembly" not in m:
                raise MetadataError(f"{m['name']}: halves_to_assembly missing")
            M = _floats(m["halves_to_assembly"], 16, "halves_to_assembly", m["name"]).reshape(4, 4)
            if abs(np.linalg.det(M[:3, :3])) < 1e-9:
                raise MetadataError(f"{m['name']}: halves_to_assembly is singular")
            local = [(V * pkg.scale, F) for V, F in pkg.meshes("3D/3dmodel.model", oid, np.eye(4))]
            if not local:
                raise MetadataError(f"{m['name']}: object has no geometry")
            if "halves_local_min" in m:
                now = np.vstack([V for V, _ in local]).min(0)
                shift = _floats(m["halves_local_min"], 3, "halves_local_min", m["name"]) - now
                local = [(V + shift, F) for V, F in local]
            halves.append(Half(m["name"], [(V @ M[:3, :3].T + M[:3, 3], F) for V, F in local]))
            if "halves_cut" in m:
                cuts.append(_floats(m["halves_cut"], 4, "halves_cut", m["name"]))
        if len(cuts) != 2:
            raise MetadataError(f"pair {pid}: halves_cut must be given on both halves")
        if not np.all(np.isfinite(cuts[0])) or np.linalg.norm(cuts[0][:3]) < 1e-9:
            raise MetadataError(f"pair {pid}: halves_cut needs a finite, non-zero plane normal, got {cuts[0].tolist()}")
        if not np.allclose(cuts[0], cuts[1], atol=1e-6):
            raise MetadataError(f"pair {pid}: the halves disagree on halves_cut ({cuts[0].tolist()} vs {cuts[1].tolist()})")
        nn = float(np.linalg.norm(cuts[0][:3]))
        dowels = _dowel_count(pid, (meta[g[1]].get("halves_dowels"), meta[g[2]].get("halves_dowels")))
        pairs.append(Pair(pid, halves[0], halves[1], cuts[0][:3] / nn, float(cuts[0][3]) / nn,
                          dowels, os.path.basename(path)))
    # numbered objects without any halves metadata: X1 + X2 on the same plate
    unmarked: dict[str, list[str]] = {}
    for oid, m in meta.items():
        if oid not in built or "halves_pair" in m:
            continue
        mt = re.match(r"^(.*?)[ _-]?([12])$", m["name"])
        if mt and mt.group(1):
            unmarked.setdefault(mt.group(1), []).append(m["name"])
    warnings = [f"{os.path.basename(path)}: objects {' and '.join(sorted(v))} look like the two halves of one part but carry "
                f"no halves_pair metadata (mark them, or set halves_pair=none if they are not halves)"
                for _, v in sorted(unmarked.items()) if len(v) >= 2]
    return pairs, warnings


# ---------------------------------------------------------------- STL pairs
def parse_plane(spec: str) -> tuple[np.ndarray, float]:
    m = re.fullmatch(r"\s*([xyz])\s*=\s*(-?[\d.]+(?:e-?\d+)?)\s*", spec)
    if m:
        n = np.zeros(3)
        n["xyz".index(m.group(1))] = 1.0
        return n, float(m.group(2))
    v = [float(x) for x in spec.replace(",", " ").split()]
    with np.errstate(over="ignore"):        # 1e308-sized components overflow the norm to inf
        nn = float(np.linalg.norm(v[:3])) if len(v) == 4 else 0.0
    if len(v) != 4 or not np.all(np.isfinite(v)) or not np.isfinite(nn) or nn == 0:
        raise ValueError(f"plane must be 'x=V' / 'y=V' / 'z=V' or 'nx ny nz d' with finite numbers, got {spec!r}")
    return np.array(v[:3]) / nn, v[3] / nn


def _load_stl(path: str) -> Mesh:
    import trimesh

    m = cast("trimesh.Trimesh", trimesh.load(path, process=False, force="mesh"))
    m.merge_vertices()
    return np.asarray(m.vertices, float), np.asarray(m.faces, np.int64)


def _flat_faces(meshes: list[Mesh]) -> list[tuple[float, np.ndarray, float]]:
    """coplanar triangle groups (area, outward normal, offset), largest first"""
    keys, areas, normals, offs = [], [], [], []
    for V, F in meshes:
        T = V[F]
        c = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
        a = np.linalg.norm(c, axis=1)
        ok = a > 1e-12
        nrm = c[ok] / a[ok, None]
        off = np.einsum("ij,ij->i", nrm, T[ok, 0])
        keys.append(np.c_[np.round(nrm, 3), np.round(off, 2)])
        areas.append(a[ok] / 2)
        normals.append(nrm)
        offs.append(off)
    K = np.vstack(keys)
    _, inv = np.unique(K, axis=0, return_inverse=True)
    tot = np.bincount(inv.ravel(), np.concatenate(areas))
    N, offsets = np.vstack(normals), np.concatenate(offs)
    out = []
    for g in np.argsort(-tot)[:8]:
        sel = inv.ravel() == g
        n = N[sel].mean(0)
        n /= np.linalg.norm(n)
        out.append((float(tot[g]), n, float(offsets[sel].mean())))
    return out


def load_stl_pair(a_paths: list[str], b_paths: list[str], plane: tuple[np.ndarray, float] | None, name: str) -> Pair:
    A = [_load_stl(p) for p in a_paths]
    B = [_load_stl(p) for p in b_paths]
    if plane is None:
        fb = _flat_faces(B)
        for _, n_face, off in _flat_faces(A):
            # B's cut face looks back at A: opposite outward normal, same plane
            if any(nb @ n_face < -0.999 and abs(ob + off) < 0.05 for _, nb, ob in fb):
                n, d = -n_face, -off
                break
        else:
            raise ValueError(f"{name}: no flat face shared by both halves — pass --plane")
    else:
        n, d = plane
    if np.mean(np.concatenate([V @ n for V, _ in A])) < d:     # side 1 = the first file set
        n, d = -n, -d
    an, bn = _half_name(a_paths), _half_name(b_paths)
    return Pair(name, Half(an, A), Half(bn, B), n, d, None, f"{an}+{bn}")


def _half_name(paths: list[str]) -> str:
    """a half split into colour parts (TA1__TREE_D.stl, TA1__TRUNK.stl) is named by their common stem (TA1)"""
    stems = [os.path.splitext(os.path.basename(p))[0] for p in paths]
    return (os.path.commonprefix(stems).rstrip("_-. ") if len(stems) > 1 else stems[0]) or stems[0]


# ---------------------------------------------------------------- CLI
def _row(rep: dict) -> str:
    if "iou" not in rep:
        return f"{rep['file']:<24} {rep['pair']:<6} {rep['a'] + '/' + rep['b']:<14} {'-':>6} {'-':>8} {'-':>12} {'-':>25} {'-':>7}  {rep['status']}"
    hp = rep["hole_pairs"]
    dw = f"{len(rep['holes_a'])}/{len(rep['holes_b'])} ({max([h['offset_mm'] for h in hp], default=0):.2f})"
    size = "{:.1f}x{:.1f}/{:.1f}x{:.1f}".format(*rep["face_size_a"], *rep["face_size_b"])
    isl = "{}/{}".format(*rep["islands"])
    return (f"{rep['file']:<24} {rep['pair']:<6} {rep['a'] + '/' + rep['b']:<14} {rep['iou']:>6.3f} {rep['max_dev_mm']:>8.2f} "
            f"{dw:>12} {size:>25} {isl:>7}  {rep['status']}")


def check_inputs(inputs: Sequence[str], stl_pairs: Sequence[Sequence[str]] = (),
                 *, plane: tuple[np.ndarray, float] | None = None, opts: Options | None = None,
                 png_dir: str | None = None, strict_names: bool = False, require_pairs: bool = False) -> dict:
    """Check every marked pair in `inputs` (.3mf files / directories) and every STL pair
    ("A.stl[,A2.stl]", "B.stl[,B2.stl]"). Returns a JSON-ready report:
    {status: PASS|FAIL|ERROR, exit_code: 0|1|2, tol_mm, pairs: [...], warnings: [...], errors: [...]}.
    The callable form of `3d halves-check` (pre-print / pre-commit gates use it)."""
    opts = opts or Options()
    files: list[str] = []
    for inp in inputs:
        files += sorted(str(p) for p in Path(inp).rglob("*.3mf")) if os.path.isdir(inp) else [inp]
    reports: list[dict] = []
    warnings: list[str] = []
    errors: list[str] = []
    jobs: list[tuple[str, Pair]] = []
    for f in files:
        try:
            pairs, warn = load_3mf(f)
        except (MetadataError, zipfile.BadZipFile, KeyError, ET.ParseError, OSError) as e:
            errors.append(f"{f}: {e}")
            continue
        warnings += warn
        jobs += [(os.path.splitext(os.path.basename(f))[0], p) for p in pairs]
    for i, (pa, pb) in enumerate(stl_pairs):
        an, bn = pa.split(","), pb.split(",")
        try:
            name = os.path.commonprefix([os.path.basename(an[0]), os.path.basename(bn[0])]).rstrip("_-.12") or f"pair{i + 1}"
            jobs.append((name, load_stl_pair(an, bn, plane, name)))
        except (ValueError, OSError) as e:
            errors.append(str(e))
    for stem, pair in jobs:
        try:
            rep = check_pair(pair, opts)
        except RasterTooLarge as e:
            errors.append(f"pair {pair.pair_id} ({pair.source}): {e}")
            continue
        if png_dir and "_masks" in rep:
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{stem}__{pair.pair_id}").lstrip(".")
            rep["png"] = os.path.join(png_dir, f"{safe}.png")
            write_png(rep["png"], rep)
        reports.append({k: v for k, v in rep.items() if not k.startswith("_")})
    fail = any(r["status"] == "FAIL" for r in reports) or (strict_names and bool(warnings))
    if require_pairs and not reports:
        fail = True
        warnings.append("no paired halves found (--require-pairs)")
    status = "ERROR" if errors else ("FAIL" if fail else "PASS")
    return dict(status=status, exit_code=2 if errors else (1 if fail else 0), tol_mm=opts.tol,
                pairs=reports, warnings=warnings, errors=errors)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="3d halves-check", description="check that paired halves of a split part match")
    ap.add_argument("inputs", nargs="*", help=".3mf files or directories (searched recursively for .3mf)")
    ap.add_argument("--pair", nargs=2, action="append", default=[], metavar=("A.stl[,A2.stl]", "B.stl[,B2.stl]"))
    ap.add_argument("--plane", default="auto")
    ap.add_argument("--tol", type=float, default=0.2)
    ap.add_argument("--res", type=float, default=0.05)
    ap.add_argument("--min-iou", type=float, default=0.9)
    ap.add_argument("--png", metavar="DIR")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict-names", action="store_true")
    ap.add_argument("--require-pairs", action="store_true")
    a = ap.parse_args(argv)
    bad = [f"--{k} {v}" for k, v in (("tol", a.tol), ("res", a.res)) if not (np.isfinite(v) and v > 0)]
    if not (np.isfinite(a.min_iou) and 0 <= a.min_iou <= 1):
        bad.append(f"--min-iou {a.min_iou} (must be within 0..1)")
    if a.plane != "auto" and not a.pair:
        bad.append("--plane needs --pair (3MF pairs carry their own halves_cut)")
    if bad:
        print("error: invalid option: " + "; ".join(bad) + " (tol/res must be finite and > 0)", file=sys.stderr)
        return 2
    plane = None
    if a.pair and a.plane != "auto":
        try:
            plane = parse_plane(a.plane)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    opts = Options(tol=a.tol, res=a.res, min_iou=a.min_iou)
    result = check_inputs(a.inputs, a.pair, plane=plane, opts=opts, png_dir=a.png,
                          strict_names=a.strict_names, require_pairs=a.require_pairs)
    status, reports, warnings, errors = result["status"], result["pairs"], result["warnings"], result["errors"]
    if a.json:
        print(json.dumps(dict(status=status, tol_mm=opts.tol, pairs=reports, warnings=warnings, errors=errors), indent=1))
    else:
        print(f"halves-check: tol {opts.tol} mm, raster {opts.res} mm")
        if reports:
            print(f"{'file':<24} {'pair':<6} {'halves':<14} {'IoU':>6} {'max dev':>8} {'dowels(Δ)':>12} {'face WxH mm (1/2)':>25} {'islands':>7}  status")
            for r in reports:
                print(_row(r))
                for msg in r["failures"]:
                    print(f"    FAIL {msg}")
                for msg in r["warnings"]:
                    print(f"    warn {msg}")
                if r.get("png"):
                    print(f"    overlay {r['png']}  (grey both, red {r['a']} only, blue {r['b']} only)")
        for w in warnings:
            print(f"WARN {w}")
        for err in errors:
            print(f"ERROR {err}", file=sys.stderr)
        print(f">>> HALVES-CHECK: {status} ({len(reports)} pair{'s' if len(reports) != 1 else ''})")
    return int(result["exit_code"])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
