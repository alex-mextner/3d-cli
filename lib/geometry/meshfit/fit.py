"""fit.py — `fit_silhouette`: nudge region handles so the model's silhouette matches reference masks.

WHAT: HEURISTIC, deliberately small. Each handle is a region plus a direction and a travel limit; the
  surface follows through a biharmonic deformation that is LINEAR in the handle travels, so one sparse
  factorisation gives a response field per handle and every candidate pose costs only a weighted sum
  plus one rasterisation per view. A greedy coordinate search (first improvement, fixed handle order,
  step halving) maximises the mean silhouette IoU against the reference masks. Keep-out zones are honoured
  inside the loop (the same (1 - keep-out weight) multiplier the engine applies), so the IoU the loop
  reports is the IoU of the mesh it returns.

WHY: matching a reference photo by hand means pushing the chest out, pulling an ear up, … until the
  outline fits. This does the same with a fixed, reproducible search. It is NOT a general non-rigid
  registration: it is a local optimiser over a few user-named handles, it can stall in local optima, it
  needs a calibrated or `align: mask_bbox` camera, and silhouettes cannot see depth — read the IoU
  table, do not trust it blindly.

Op parameters (`op: fit_silhouette`):
  views:   [{mask: ref.png, polarity: light|dark, view: front|...|top  | ortho: {direction, up, center,
            mm_per_px} | perspective: {eye, center, up, fov_deg} | camera_json: camera.json (3d fit-camera),
            align: mask_bbox | none, size: [w, h]}]
  handles: [{region: ref, direction: [x,y,z], max_move: mm}]       (the same region may appear twice)
  iterations (30), step_mm (1.0), min_step_mm (0.05), min_gain (1e-4), anchors, influence (mm).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

import numpy as np
from numpy.typing import NDArray

from errors import InputNotFound, MissingDependency, UsageError

from . import deform, raster
from .regions import LOCK_WEIGHT, RegionContext

Array = NDArray[np.float64]
IntArray = NDArray[np.int64]
BoolArray = NDArray[np.bool_]


def load_mask(path: str, polarity: str) -> BoolArray:
    if not os.path.isfile(path):
        raise InputNotFound(path, command="mesh fit-silhouette")
    try:
        from PIL import Image
    except ImportError as err:
        raise MissingDependency(
            "Pillow",
            install="pip install pillow   (or: uv sync --extra preprocess)",
            degrades="reading reference masks for fit-silhouette",
            command="mesh fit-silhouette",
        ) from err
    with Image.open(path) as im:
        grey = np.asarray(im.convert("L"))
    on = grey > 127
    if polarity == "dark":
        on = ~on
    elif polarity != "light":
        raise UsageError(
            f"fit_silhouette: polarity must be light or dark, got {polarity!r}",
            command="mesh fit-silhouette",
        )
    return np.asarray(on, dtype=bool)


def build_camera(
    view: Mapping[str, Any], V: Array, mask: BoolArray, base_dir: str
) -> raster.Camera:
    h, w = mask.shape
    if "size" in view and tuple(view["size"]) != (w, h):
        raise UsageError(
            f"fit_silhouette: view size {view['size']} does not match the mask ({w}x{h})",
            command="mesh fit-silhouette",
        )
    if "camera_json" in view:
        path = (
            view["camera_json"]
            if os.path.isabs(view["camera_json"])
            else os.path.join(base_dir, view["camera_json"])
        )
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        cam = data["camera"]
        return raster.perspective_camera(
            np.array(cam[:3]),
            np.array(cam[3:6]),
            np.array([0.0, 0.0, 1.0]),
            float(view.get("fov_deg", 22.5)),
            w,
            h,
        )
    if "perspective" in view:
        p = view["perspective"]
        return raster.perspective_camera(
            np.array(p["eye"]),
            np.array(p["center"]),
            np.array(p.get("up", [0, 0, 1])),
            float(p.get("fov_deg", 22.5)),
            w,
            h,
        )
    if "ortho" in view:
        o = view["ortho"]
        center = (
            np.array(o["center"], dtype=np.float64)
            if "center" in o
            else (V.min(axis=0) + V.max(axis=0)) / 2.0
        )
        if "mm_per_px" in o:
            return raster.ortho_camera(
                np.array(o["direction"]),
                np.array(o.get("up", [0, 0, 1])),
                center,
                float(o["mm_per_px"]),
                w,
                h,
            )
        cam = raster.ortho_camera(
            np.array(o["direction"]),
            np.array(o.get("up", [0, 0, 1])),
            center,
            1.0,
            w,
            h,
        )
        return _frame_extent(cam, V)
    if "view" in view:
        return raster.auto_ortho(str(view["view"]), V, w, h)
    raise UsageError(
        "fit_silhouette: each view needs one of view | ortho | perspective | camera_json",
        command="mesh fit-silhouette",
    )


def _frame_extent(cam: raster.Camera, V: Array) -> raster.Camera:
    scale = max(
        np.ptp(V @ cam.right) / (0.8 * cam.width),
        np.ptp(V @ cam.up) / (0.8 * cam.height),
        1e-9,
    )
    return raster.Camera(
        cam.origin,
        cam.forward,
        cam.right,
        cam.up,
        cam.width,
        cam.height,
        float(scale),
        False,
    )


def align_to_mask_bbox(
    cam: raster.Camera, V: Array, F: IntArray, mask: BoolArray
) -> raster.Camera:
    """Orthographic only: scale and shift the camera so the baseline silhouette bbox equals the mask bbox."""
    if cam.perspective:
        raise UsageError(
            "fit_silhouette: align: mask_bbox supports orthographic views only",
            command="mesh fit-silhouette",
        )
    own = raster.silhouette(cam, V, F)
    ys, xs = np.nonzero(own)
    ry, rx = np.nonzero(mask)
    if len(xs) == 0 or len(rx) == 0:
        raise UsageError(
            "fit_silhouette: empty silhouette or empty mask, cannot align",
            command="mesh fit-silhouette",
        )
    own_w, own_h = xs.max() - xs.min() + 1, ys.max() - ys.min() + 1
    ref_w, ref_h = rx.max() - rx.min() + 1, ry.max() - ry.min() + 1
    ratio = 0.5 * (own_w / ref_w + own_h / ref_h)
    scale = cam.scale * ratio
    shift_px = np.array(
        [
            (rx.min() + rx.max()) / 2.0 - (xs.min() + xs.max()) / 2.0,
            (ry.min() + ry.max()) / 2.0 - (ys.min() + ys.max()) / 2.0,
        ]
    )
    # moving the image content by shift_px (right, down) means moving the camera origin the other way
    origin = cam.origin - cam.right * shift_px[0] * scale + cam.up * shift_px[1] * scale
    return raster.Camera(
        origin,
        cam.forward,
        cam.right,
        cam.up,
        cam.width,
        cam.height,
        float(scale),
        False,
    )


class _Basis:
    """Biharmonic responses of the free surface to a unit move of each handle group (one LU, many right-hand sides)."""

    def __init__(
        self,
        V: Array,
        F: IntArray,
        groups: list[IntArray],
        directions: Array,
        fixed: BoolArray,
        zone: BoolArray | None,
    ) -> None:
        n = len(V)
        all_handle = np.zeros(n, dtype=bool)
        for g in groups:
            all_handle[g] = True
        free_mask = ~(all_handle | fixed)
        if zone is not None:
            free_mask &= zone
        self.free = np.nonzero(free_mask)[0]
        system = (
            deform.PatchSystem(V, F, self.free, "biharmonic")
            if len(self.free)
            else None
        )
        self.responses: list[
            Array
        ] = []  # per group: (n, 3) displacement for a 1 mm move along its direction
        for g, u in zip(groups, directions, strict=True):
            field = np.zeros((n, 3))
            field[g] = u
            if system is not None:
                field[self.free] = system.solve(field)
            self.responses.append(field)


def fit_silhouette(
    ctx: RegionContext, op: Mapping[str, Any], lock: BoolArray, keepout_w: Array
) -> tuple[Array, dict[str, Any]]:
    V, F = ctx.V, ctx.F
    base_dir = ctx.base_dir
    views: list[tuple[raster.Camera, BoolArray, str]] = []
    for i, view in enumerate(op["views"]):
        mask = load_mask(
            view["mask"]
            if os.path.isabs(view["mask"])
            else os.path.join(base_dir, view["mask"]),
            str(view.get("polarity", "light")),
        )
        cam = build_camera(view, V, mask, base_dir)
        if str(view.get("align", "none")) == "mask_bbox":
            cam = align_to_mask_bbox(cam, V, F, mask)
        views.append((cam, mask, str(view.get("name", f"view{i}"))))

    groups: list[IntArray] = []
    dirs: list[Array] = []
    limits: list[float] = []
    for h in op["handles"]:
        idx = np.nonzero((ctx.weight(h["region"]) >= LOCK_WEIGHT) & ~lock)[0]
        if len(idx) == 0:
            raise UsageError(
                "fit_silhouette: a handle region selects no vertex at weight 1 outside the keep-out zone",
                command="mesh fit-silhouette",
            )
        u = np.asarray(h["direction"], dtype=np.float64)
        groups.append(idx)
        dirs.append(u / np.linalg.norm(u))
        limits.append(float(h.get("max_move", 5.0)))
    fixed = lock.copy()
    for ref in op.get("anchors", []) or []:
        fixed |= ctx.weight(ref) >= LOCK_WEIGHT
    for g in groups:
        fixed[g] = False
    zone: BoolArray | None = None
    if "influence" in op:
        from scipy.spatial import cKDTree

        zone = cKDTree(V[np.unique(np.concatenate(groups))]).query(V, workers=1)[
            0
        ] <= float(op["influence"])
    if not fixed.any() and zone is None:
        raise UsageError(
            "fit_silhouette: no anchors: add `anchors:`, a keep-out zone or an `influence` radius",
            command="mesh fit-silhouette",
        )
    basis = _Basis(V, F, groups, np.stack(dirs), fixed, zone)
    fade = (1.0 - keepout_w)[:, None]
    responses = [r * fade for r in basis.responses]
    for r in responses:
        r[lock] = 0.0
    active = np.nonzero(np.any([np.abs(r).sum(axis=1) > 0 for r in responses], axis=0))[
        0
    ]

    def pose(a: Array) -> Array:
        out = V.copy()
        for k, r in enumerate(responses):
            if a[k] != 0.0:
                out[active] += a[k] * r[active]
        return out

    def score(a: Array) -> tuple[float, list[float]]:
        Vp = pose(a)
        ious = [
            raster.mask_iou(raster.silhouette(cam, Vp, F), mask)
            for cam, mask, _ in views
        ]
        return float(np.mean(ious)), ious

    a = np.zeros(len(groups))
    best, per_view = score(a)
    initial = best
    step = float(op.get("step_mm", 1.0))
    min_step = float(op.get("min_step_mm", 0.05))
    min_gain = float(op.get("min_gain", 1e-4))
    table: list[dict[str, Any]] = [
        {
            "iteration": 0,
            "step_mm": step,
            "iou": round(best, 6),
            "travel_mm": a.round(4).tolist(),
        }
    ]
    for it in range(1, int(op.get("iterations", 30)) + 1):
        improved = False
        for k in range(len(groups)):
            for sign in (1.0, -1.0):
                cand = a.copy()
                cand[k] = float(np.clip(a[k] + sign * step, -limits[k], limits[k]))
                if cand[k] == a[k]:
                    continue
                iou, ious = score(cand)
                if iou > best + min_gain:
                    best, per_view, a, improved = iou, ious, cand, True
                    break
        table.append(
            {
                "iteration": it,
                "step_mm": round(step, 6),
                "iou": round(best, 6),
                "travel_mm": a.round(4).tolist(),
            }
        )
        if not improved:
            step *= 0.5
            if step < min_step:
                break
    details: dict[str, Any] = {
        "heuristic": "greedy coordinate search over biharmonic handle responses; local optimum only",
        "iou_before": round(initial, 6),
        "iou_after": round(best, 6),
        "iou_per_view": {
            name: round(v, 6) for (_, _, name), v in zip(views, per_view, strict=True)
        },
        "handle_travel_mm": [round(float(x), 4) for x in a],
        "handle_limit_mm": limits,
        "free_vertices": int(len(basis.free)),
        "table": table,
    }
    return pose(a), details


def format_table(details: Mapping[str, Any]) -> str:
    lines = ["iter  step_mm   IoU       handle travel (mm)"]
    lines.extend(
        f"{row['iteration']:>4}  {row['step_mm']:<8.3f}  {row['iou']:<8.5f}  {row['travel_mm']}"
        for row in details["table"]
    )
    lines.append(
        f"IoU {details['iou_before']:.5f} -> {details['iou_after']:.5f}  (heuristic: greedy search, local optimum)"
    )
    return "\n".join(lines)
