"""Closed-form initialisation + candidate pose search for the loft fitters (numpy + OpenCV only, ms-scale).

Per view the closed-form profile gives length, centre, axis and width profile in pixels; with calibrated cameras these become a
3-D start (triangulated centre, axis from the plane intersection, widths scaled by depth/focal). Pose ambiguities that a
silhouette cannot resolve (axis direction, lateral sign, roll about the axis, thickness) are enumerated as candidates and
ranked by the independent hard rasteriser, so the expensive optimiser starts from the best few.
"""
from __future__ import annotations

import math
import time
from typing import Any

import numpy as np

from .errors import BadInput
from .fit_profile import MIN_PIXELS, as_bool_mask, fit_profile
from .loft import (
    KIND_TEMPLATE,
    TEMPLATE_ASPECT,
    TEMPLATE_P,
    W_MIN_MM,
    apply_fixed_dims,
    basis_from_axis,
    build_faces,
    loft_vertices,
)
from .render import hard_mask

CROP_S = 96  # candidate ranking / CMA resolution (px, square crop around each mask)
FLIP = np.diag([-1.0, 1.0, -1.0])
FLAG_BELOW_IOU = 0.75  # a view / held-out view with IoU below this is "not explained": ask for another view (spike: good fits predict unseen views at >= 0.8)


def validate_views(views: Any) -> list[dict[str, Any]]:
    """Normalise `[{"mask","K","R","t"}]` -> float64 arrays, bool masks; BadInput for anything unusable (never NaN downstream)."""
    if not isinstance(views, (list, tuple)) or not views:
        raise BadInput("need at least one view: [{'mask': HxW bool, 'K': 3x3, 'R': 3x3, 't': 3}]")
    out = []
    for i, v in enumerate(views):
        if not isinstance(v, dict):
            raise BadInput(f"view {i} must be a dict with mask, K, R, t (got {type(v).__name__})")
        try:
            mask = as_bool_mask(v["mask"])
            K = np.asarray(v["K"], np.float64)
            R = np.asarray(v["R"], np.float64)
            t = np.asarray(v["t"], np.float64).reshape(-1)
        except KeyError as exc:
            raise BadInput(f"view {i} is missing {exc.args[0]!r} (need mask, K, R, t)") from exc
        except (TypeError, ValueError) as exc:  # ragged / non-numeric K, R, t or a malformed mask
            raise BadInput(f"view {i}: mask, K, R, t must be numeric arrays ({exc})") from exc
        if K.shape != (3, 3) or R.shape != (3, 3) or t.shape != (3,):
            raise BadInput(f"view {i}: K and R must be 3x3 and t length 3 (got {K.shape}, {R.shape}, {t.shape})")
        if not (np.isfinite(K).all() and np.isfinite(R).all() and np.isfinite(t).all()):
            raise BadInput(f"view {i}: K/R/t contain NaN/inf")
        if not np.allclose(K[2], [0.0, 0.0, 1.0]):
            raise BadInput(f"view {i}: K must be a pinhole matrix with last row [0, 0, 1] (got {K[2].tolist()})")
        if K[0, 0] <= 0 or K[1, 1] <= 0:
            raise BadInput(f"view {i}: focal lengths must be positive (K[0,0]={K[0, 0]}, K[1,1]={K[1, 1]})")
        if not np.allclose(R @ R.T, np.eye(3), atol=1e-3) or np.linalg.det(R) < 0:
            raise BadInput(f"view {i}: R is not a proper rotation (world->camera)")
        n = int(mask.sum())
        if n < MIN_PIXELS:
            raise BadInput(f"view {i}: mask has {n} foreground pixel(s); need >= {MIN_PIXELS}")
        out.append({"mask": mask, "K": K, "R": R, "t": t, "name": v.get("name")})
    return out


FIXED_DIM_KEYS = ("length_mm", "max_width_mm", "thickness_mm")


def check_fixed_dims(fixed_dims: dict[str, float] | None) -> dict[str, float]:
    """User overrides (hard constraints): only length_mm / max_width_mm / thickness_mm, each a positive finite number."""
    out: dict[str, float] = {}
    for k, v in (fixed_dims or {}).items():
        if k not in FIXED_DIM_KEYS:
            raise BadInput(f"unknown fixed dimension {k!r}", remediation=f"use any of {list(FIXED_DIM_KEYS)}")
        if not (isinstance(v, (int, float)) and math.isfinite(v) and v > 0):
            raise BadInput(f"fixed dimension {k} must be a positive finite number, got {v!r}")
        out[k] = float(v)
    return out


def camera_center(v: dict[str, Any]) -> np.ndarray:
    return -v["R"].T @ v["t"]


def _ray(v: dict[str, Any], xy: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    d = v["R"].T @ (np.linalg.inv(v["K"]) @ np.array([xy[0], xy[1], 1.0]))
    return camera_center(v), d / np.linalg.norm(d)


def _point_at_depth(v: dict[str, Any], xy: np.ndarray, depth: float) -> np.ndarray:
    """World point on the pixel's ray whose camera z-depth is `depth` mm."""
    xc = np.linalg.inv(v["K"]) @ np.array([xy[0], xy[1], 1.0])
    return v["R"].T @ (xc * depth - v["t"])


def _triangulate(rays: list[tuple[np.ndarray, np.ndarray]]) -> np.ndarray:
    A = np.zeros((3, 3))
    b = np.zeros(3)
    for o, d in rays:
        P = np.eye(3) - np.outer(d, d)
        A += P
        b += P @ o
    if np.linalg.cond(A) > 1e8:
        raise BadInput(
            "the views have (almost) no parallax: camera centres coincide or all rays are parallel, so the object cannot be located",
            remediation="add a view from a different position/direction, or fit one view with a known depth",
        )
    return np.linalg.solve(A, b)


def _depth(v: dict[str, Any], x: np.ndarray) -> float:
    return float((v["R"] @ x + v["t"])[2])


def crop_view(mask: np.ndarray, K: np.ndarray, R: np.ndarray, t: np.ndarray, S: int = CROP_S, margin: float = 0.25) -> dict[str, Any]:
    """Square, margin-padded crop of the mask's bbox resampled to S x S (float 0..1) + the matching intrinsics."""
    import cv2

    ys, xs = np.nonzero(mask)
    cx, cy = (xs.min() + xs.max()) / 2, (ys.min() + ys.max()) / 2
    half = max(xs.max() - xs.min(), ys.max() - ys.min()) / 2 * (1 + margin) + 2
    x0, y0 = cx - half, cy - half
    s = S / (2 * half)
    # warpAffine ignores INTER_AREA (it point-samples bilinearly): a 5 px shaft would break up at 8 source px per crop pixel. Shrink the
    # bbox with a true area average first, then place it on the S x S canvas with a near-unit affine.
    bx, by = int(xs.min()), int(ys.min())
    sub = mask[by : int(ys.max()) + 1, bx : int(xs.max()) + 1].astype(np.float32)
    bw, bh = sub.shape[1], sub.shape[0]
    if s < 1.0:
        # whole-pixel output sizes would inflate a thin part (5 px * 0.157 = 0.8 px -> 1 px): zero-pad the bbox so w2 / wp == s
        w2, h2 = max(1, math.ceil(bw * s)), max(1, math.ceil(bh * s))
        wp, hp = max(bw, round(w2 / s)), max(bh, round(h2 / s))
        sub = np.pad(sub, ((0, hp - bh), (0, wp - bw)))
        sub = np.asarray(cv2.resize(sub, (w2, h2), interpolation=cv2.INTER_AREA), np.float32)
        sx, sy = w2 / wp, h2 / hp
    else:
        sx = sy = 1.0
    # cv2.resize puts small pixel j at source index (j + 0.5)/sx - 0.5 (+ bx); canvas index convention is (idx - x0) * s, as for K2
    A = np.array([[s / sx, 0, (0.5 / sx - 0.5 + bx - x0) * s], [0, s / sy, (0.5 / sy - 0.5 + by - y0) * s]], np.float32)
    m = cv2.warpAffine(sub, A, (S, S), flags=cv2.INTER_LINEAR)
    K2 = np.array(K, np.float64).copy()
    K2[0, 0] *= s
    K2[1, 1] *= s
    K2[0, 2] = (K2[0, 2] - x0) * s
    K2[1, 2] = (K2[1, 2] - y0) * s
    return {"mask": m, "K": K2, "R": np.asarray(R, np.float64), "t": np.asarray(t, np.float64), "scale": s, "x0": x0, "y0": y0}


def _station_arrays(fp: dict[str, Any], s: float) -> tuple[np.ndarray, np.ndarray]:
    st = fp["stations"]
    w = np.array([(q["hi_mm"] - q["lo_mm"]) / 2 * s for q in st])
    # station centres relative to the axis line through the extent centre (the loft origin), not through the centroid
    c = np.array([((q["hi_mm"] + q["lo_mm"]) / 2 - fp["lateral_offset_mm"]) * s for q in st])
    return np.maximum(w, W_MIN_MM), c


def aligned_profile(mask: np.ndarray, K: int, angle_deg: float | None = None, iters: int = 2) -> dict[str, Any]:
    """`fit_profile` with the axis rotated onto the CENTRE LINE of the profile (px units).

    A tilted axis makes the stations' lateral centres drift linearly along it; with free per-station centres that shear fits the mask
    about as well as the true axis (a vertical screwdriver "explained" by a 45-degree axis with a +-80 mm centre ramp). Removing the
    linear trend of the centres, twice, puts the axis back where the object is straight. `angle_deg=None` starts from the PCA axis.
    """
    full = as_bool_mask(mask)
    rows, cols = np.flatnonzero(full.any(1)), np.flatnonzero(full.any(0))
    if rows.size == 0:
        return fit_profile(full, 1.0, K=K)  # raises BadInput: empty mask
    off = np.array([cols[0], rows[0]], np.float64)
    mask = full[rows[0] : rows[-1] + 1, cols[0] : cols[-1] + 1]  # crop once: every pass below would otherwise rescan the whole frame
    fp = fit_profile(mask, 1.0, K=K, axis_angle_deg=angle_deg)
    for _ in range(iters):
        st = fp["stations"]
        u = (np.array([q["t"] for q in st]) - 0.5) * fp["length_mm"]
        c = np.array([(q["hi_mm"] + q["lo_mm"]) / 2 for q in st])
        slope = float(np.polyfit(u, c, 1)[0]) if fp["length_mm"] > 0 else 0.0
        turn = math.degrees(math.atan(slope))
        if abs(turn) < 0.25:
            break
        fp = fit_profile(mask, 1.0, K=K, axis_angle_deg=fp["axis_deg"] + max(-60.0, min(60.0, turn)))
    fp["center_px"] = (np.array(fp["center_px"]) + off).tolist()  # back to the frame of the input mask
    fp["extent_center_px"] = (np.array(fp["extent_center_px"]) + off).tolist()
    return fp


def classical_init(views: list[dict[str, Any]], K: int, known_depth: float | None) -> dict[str, Any]:
    """3-D start from per-view closed-form profiles aligned to their centre lines. `known_depth` (mm, camera z) is required for a single view."""
    fps = [aligned_profile(v["mask"], K) for v in views]
    rays, normals = [], []
    for v, fp in zip(views, fps, strict=True):
        rays.append(_ray(v, np.array(fp["extent_center_px"])))
        a2, c2 = np.array(fp["axis_unit"]), np.array(fp["center_px"])
        r = [_ray(v, c2 + a2 * 40)[1], _ray(v, c2 - a2 * 40)[1]]
        n = np.cross(r[0], r[1])
        normals.append(n / max(np.linalg.norm(n), 1e-12))
    if len(views) > 1:
        t0 = _triangulate(rays)
        axis = np.linalg.svd(np.array(normals))[2][-1]
    else:
        if known_depth is None or not (math.isfinite(known_depth) and known_depth > 0):
            raise BadInput(
                "a single view needs known_depth (mm from the camera to the object) so the size is defined",
                remediation="pass known_depth, e.g. from calibrate.scale_from_reference(...)['plane_depth_mm'], or add a second view",
            )
        v = views[0]
        t0 = _point_at_depth(v, np.array(fps[0]["extent_center_px"]), float(known_depth))
        a2 = np.array(fps[0]["axis_unit"])
        axis = v["R"].T @ np.array([a2[0], a2[1], 0.0])  # fronto-parallel assumption
    Ws, Cs, Ls = [], [], []
    for v, fp in zip(views, fps, strict=True):
        Z = _depth(v, t0)
        if not Z > 1.0:
            raise BadInput(f"the estimated object centre is behind (or at) the camera (depth {Z:.1f} mm); check R/t (world->camera)")
        s = Z / v["K"][0, 0]
        w, c = _station_arrays(fp, s)
        Ws.append(w)
        Cs.append(c)
        Ls.append(fp["length_mm"] * s)
    jj = int(np.argmax([w.sum() for w in Ws]))
    # per-station max over views: a foreshortened view under-estimates width, never over-estimates a convex section
    return {"t0": t0, "axis": axis, "L": max(Ls), "w": np.max(Ws, 0), "c": Cs[jj], "profiles": fps}


def planar_sets(view: dict[str, Any], K: int, known_depth: float, step_deg: int = 15) -> list[dict[str, Any]]:
    """Single top-down view: search the in-plane axis angle (PCA is ill-defined for near-square silhouettes)."""
    sets = []
    seen: set[int] = set()
    s = known_depth / view["K"][0, 0]
    for ang in range(0, 180, step_deg):
        fp = aligned_profile(view["mask"], K, float(ang))
        key = int(round(fp["axis_deg"] % 180.0 / 2.0))  # different starts converge onto the same centre line: keep one
        if key in seen:
            continue
        seen.add(key)
        t0 = _point_at_depth(view, np.array(fp["extent_center_px"]), float(known_depth))
        a2 = np.array(fp["axis_unit"])
        w, c = _station_arrays(fp, s)
        sets.append({"t0": t0, "axis": view["R"].T @ np.array([a2[0], a2[1], 0.0]), "L": fp["length_mm"] * s, "w": w, "c": c})
    return sets


def classify(views: list[dict[str, Any]], K: int) -> tuple[str, list[dict[str, Any]]]:
    """(kind of the largest view, closed-form profile of every view) at 1 px = 1 unit."""
    fps = [fit_profile(v["mask"], 1.0, K=K) for v in views]
    j = int(np.argmax([fp["pixels"] for fp in fps]))
    return str(fps[j]["kind"]), fps


def template_for(kind: str) -> str:
    return KIND_TEMPLATE.get(kind, "flat")


def resolve_template(template: str | float | None, kind: str) -> tuple[str, float]:
    """(template name, superellipse exponent). `template` None -> by closed-form kind; a name or one of {2, 4, 8}."""
    if template is None or template == "auto":
        name = template_for(kind)
    elif isinstance(template, str):
        if template not in TEMPLATE_P:
            raise BadInput(f"unknown template {template!r}", remediation=f"use one of {sorted(TEMPLATE_P)} or 'auto'")
        name = template
    else:
        by_p = {v: k for k, v in TEMPLATE_P.items()}
        if float(template) not in by_p:
            raise BadInput(f"unsupported template exponent {template!r}", remediation=f"use one of {sorted(TEMPLATE_P.values())}")
        name = by_p[float(template)]
    return name, TEMPLATE_P[name]


def _aspect_options(template: str, fixed_dims: dict[str, float] | None) -> tuple[float, ...]:
    if template == "lathe":
        return (1.0,)
    prior = TEMPLATE_ASPECT[template]
    opts = [prior]
    opts += [a for a in (0.12, 0.3, 1.0) if abs(a - prior) > 1e-9]
    if fixed_dims and "thickness_mm" in fixed_dims:
        return (1.0,)  # overwritten by apply_fixed_dims; one candidate is enough
    return tuple(opts)


def pose_candidates(
    sets: list[dict[str, Any]], template: str, planar: bool, fixed_dims: dict[str, float] | None, rolls: int = 12
) -> list[dict[str, Any]]:
    """Enumerate the pose ambiguities (axis flip, lateral sign, roll about the axis, thickness prior) of each closed-form start."""
    if template == "lathe":
        roll_list = [0.0]  # circular section: roll is invisible
    elif planar:
        roll_list = [0.0, math.pi / 2]  # lying flat or on its side
    else:
        roll_list = [2 * math.pi * r / rolls for r in range(rolls)]
    cands = []
    all_aspects = _aspect_options(template, fixed_dims)
    for st in sets:
        for flip in (0, 1):
            for csg in (1.0, -1.0):
                for ri, roll in enumerate(roll_list):
                    # roll_list[0] is the exact 0.0: lying flat in a top-down view, thickness is invisible, so keep only the prior
                    opts = all_aspects[:1] if planar and ri == 0 else all_aspects
                    for asp in opts:
                        B = basis_from_axis(st["axis"], roll)
                        if flip:
                            B = B @ FLIP
                        L, w, a = apply_fixed_dims(st["L"], st["w"], asp, fixed_dims)
                        cands.append({"L": L, "c": st["c"] * csg, "w": w, "aspect": a, "R0": B, "t0": st["t0"]})
    return cands


def mesh_world(cand: dict[str, Any], p: float, K: int, M: int) -> np.ndarray:
    V = loft_vertices(cand["L"], cand["c"], cand["w"], cand["aspect"], p, M)
    return V @ cand["R0"].T + cand["t0"]


def crop_all(views: list[dict[str, Any]], S: int = CROP_S) -> list[dict[str, Any]]:
    return [crop_view(v["mask"], v["K"], v["R"], v["t"], S) for v in views]


def crop_iou(cand: dict[str, Any], p: float, K: int, M: int, crops: list[dict[str, Any]], F: np.ndarray) -> float:
    """Mean hard-raster IoU of a candidate over the S x S crops."""
    V = mesh_world(cand, p, K, M)
    S = crops[0]["mask"].shape[0]
    r = []
    for cv in crops:
        h = hard_mask(V, F, cv["K"], cv["R"], cv["t"], (S, S))
        m = cv["mask"] > 0.5
        r.append((h & m).sum() / max(int((h | m).sum()), 1))
    return float(np.mean(r))


def ranked_candidates(
    views: list[dict[str, Any]],
    template: str | float | None,
    *,
    K: int = 8,
    known_depth: float | None = None,
    planar: bool = False,
    fixed_dims: dict[str, float] | None = None,
    ring: int = 10,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Closed-form start -> pose candidates ranked by hard IoU (best first, deterministic).

    Returns (candidates, info); each candidate is `{"init": {...}, "score": mean crop IoU}`; info has `kind`, `template`, `p`,
    per-view closed-form `profiles` and `timing_s`.
    """
    t = time.perf_counter()
    fixed_dims = check_fixed_dims(fixed_dims)
    if K < 3:
        raise BadInput(f"need K >= 3 stations, got {K}")
    kind, profiles = classify(views, K)
    name, p = resolve_template(template, kind)
    if planar and len(views) != 1:
        raise BadInput("planar applies to ONE top-down view (the object lies on a plane parallel to the image)", remediation="drop planar for several views")
    if planar:
        if known_depth is None or not (math.isfinite(known_depth) and known_depth > 0):
            raise BadInput("planar fitting needs known_depth (mm from the camera to the object plane)")
        sets = planar_sets(views[0], K, float(known_depth))
    else:
        sets = [classical_init(views, K, known_depth)]
    t_init = time.perf_counter() - t
    t = time.perf_counter()
    cands = pose_candidates(sets, name, planar and len(views) == 1, fixed_dims)
    crops = crop_all(views)
    F = build_faces(K, ring)
    scored = [(crop_iou(c, p, K, ring, crops, F), i, c) for i, c in enumerate(cands)]
    # IoUs within 1e-3 are ties (the thickness prior is generated first and must win when thickness is not observable)
    scored.sort(key=lambda x: (-round(x[0], 3), x[1]))
    info = {"kind": kind, "template": name, "p": p, "profiles": profiles, "timing_s": {"init": t_init, "candidates": time.perf_counter() - t}}
    return [{"init": c, "score": s} for s, _, c in scored], info
