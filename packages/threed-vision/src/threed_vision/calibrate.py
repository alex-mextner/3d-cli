"""Photo -> scale/camera: honeycomb grid calibration, two-point scale, reference-object scale, default intrinsics.

Numpy + OpenCV (+ scipy's KD-tree) only: no torch, no model, no GPU.

Grid frame (identical to hsw-editor `packages/core/src/grid.ts`): millimetres, x to the right, y UP, z out of the wall
towards the viewer. A cell `(col, row)` has its centre at
`((col + 0.5) * col_pitch, (row + 0.5 + 0.5 * (col & 1)) * row_pitch)` (odd columns are shifted up half a row). A photo
cannot tell where the board corner is, so the labelling origin is arbitrary: the lowest-left detected inlier cell is
labelled `(0, 0)`. Anchor it to the board in the UI.

Camera model: pinhole, SQUARE pixels, principal point at the image centre, no lens distortion, a single unknown focal
length. `R` / `t_mm` map grid-frame mm to the OpenCV camera frame (x right, y down, z forward): `X_cam = R @ X_grid + t_mm`.

Pipeline of `detect_grid`: `find_cell_centres` (band-pass blob detector + periodic-texture filter, which is also the
panel ROI) -> `solve_grid_pose` (lattice index assignment by seeded region growing with a homography, then focal +
rotation + translation by robust Levenberg-Marquardt reprojection minimisation). `solve_grid_pose` works from a plain
list of centres, so it is testable without any image.

Scale fallbacks (`scale_from_points`, `scale_from_reference`) assume the measured segment is parallel to the image plane
(fronto-parallel); a segment foreshortened by a tilt of `a` degrees is under-measured by `1 - cos(a)` (about 3.4 % at 15
degrees, 13 % at 30 degrees). Their `quality` list says so.
"""

from __future__ import annotations

import math
import time
from typing import Any

import numpy as np

from .errors import BadInput
from .io import to_gray

DEFAULT_GRID_PITCH_MM: tuple[float, float] = (20.4382, 23.6)
DEFAULT_F_RATIO = (
    0.375  # focal length / image diagonal of the calibrated HSW phone camera
)
MIN_INLIERS = 12
STRONG_INLIERS = 60
MIN_BLOB_CONTRAST = 1.0  # grey levels (0..255) of band-pass response below which an extremum is sensor noise
MAX_SEED_POINTS = 3000
EPS_G = 3e-5  # px^-1, see solve_grid_pose

Array = np.ndarray


# --------------------------------------------------------------------------------------------------------------------
# intrinsics and manual scale
# --------------------------------------------------------------------------------------------------------------------
def _size(image_size: Any) -> tuple[int, int]:
    try:
        w, h = (int(round(float(v))) for v in image_size)
    except (TypeError, ValueError) as exc:
        raise BadInput(
            f"image_size must be (width, height), got {image_size!r}"
        ) from exc
    if w <= 0 or h <= 0:
        raise BadInput(f"image_size must be positive, got {image_size!r}")
    return w, h


def default_intrinsics(
    w: int, h: int, f_px: float | None = None, f_ratio: float = DEFAULT_F_RATIO
) -> Array:
    """Pinhole K (3x3) with a centred principal point; f = `f_px` or `f_ratio * diagonal` (0.375 = the HSW phone)."""
    w, h = _size((w, h))
    f = float(f_px) if f_px is not None else float(f_ratio) * math.hypot(w, h)
    if not math.isfinite(f) or f <= 0:
        raise BadInput(f"focal length must be a positive number of pixels, got {f!r}")
    return np.array([[f, 0.0, w / 2.0], [0.0, f, h / 2.0], [0.0, 0.0, 1.0]])


def _f_labels(f_px: float | None, f_ratio: float) -> tuple[str, str]:
    """(f_source, quality flag) for how a manual-scale focal length was chosen."""
    if f_px is not None:
        return "given", "f_given"
    if f_ratio != DEFAULT_F_RATIO:
        return "given", "f_ratio_given"
    return "default", "f_default_ratio"


def _scale_result(
    length_px: float,
    length_mm: float,
    image_size: Any,
    f_px: float | None,
    f_ratio: float,
    source: str,
    quality: list[str],
) -> dict[str, Any]:
    w, h = _size(image_size)
    if not (math.isfinite(length_mm) and length_mm > 0):
        raise BadInput(
            f"known length must be a positive number of mm, got {length_mm!r}"
        )
    if not (math.isfinite(length_px) and length_px > 0):
        raise BadInput("the measured segment has zero length in pixels")
    K = default_intrinsics(w, h, f_px, f_ratio)
    mm_per_px = length_mm / length_px
    flags = ["fronto_parallel_assumed", *quality]
    flags.append(_f_labels(f_px, f_ratio)[1])
    if length_px < 0.02 * math.hypot(w, h):
        flags.append(
            "short_reference"
        )  # < 2 % of the diagonal: a 1 px error is > 1 % of the scale at 50 px
    return {
        "mm_per_px": mm_per_px,
        "plane_depth_mm": float(K[0, 0]) * mm_per_px,
        "K": K,
        "f_px": float(K[0, 0]),
        "f_source": _f_labels(f_px, f_ratio)[0],
        "source": source,
        "length_px": float(length_px),
        "image_size": [w, h],
        "quality": flags,
    }


def scale_from_points(
    p1_px: Any,
    p2_px: Any,
    distance_mm: float,
    *,
    image_size: Any,
    f_px: float | None = None,
    f_ratio: float = DEFAULT_F_RATIO,
) -> dict[str, Any]:
    """Scale from two clicked points a known distance apart (e.g. the two ends of a ruler lying on the plane).

    Returns `{mm_per_px, plane_depth_mm, K, f_px, f_source, source: "points", length_px, image_size, quality}`.
    Assumes the segment is parallel to the image plane; `plane_depth_mm = f_px * mm_per_px` (pinhole)."""
    a, b = np.asarray(p1_px, float).reshape(-1), np.asarray(p2_px, float).reshape(-1)
    if (
        a.size != 2
        or b.size != 2
        or not (np.isfinite(a).all() and np.isfinite(b).all())
    ):
        raise BadInput("p1_px and p2_px must be finite (x, y) pixel points")
    return _scale_result(
        float(np.hypot(*(b - a))),
        float(distance_mm),
        image_size,
        f_px,
        f_ratio,
        "points",
        [],
    )


def scale_from_reference(
    mask: Array,
    length_mm: float,
    *,
    image_size: Any,
    f_px: float | None = None,
    f_ratio: float = DEFAULT_F_RATIO,
) -> dict[str, Any]:
    """Scale from a reference object of known length: `mask` (HxW bool) is the object's segmentation, `length_mm` its
    longest dimension. The pixel length is the long side of the minimum-area rotated rectangle of the mask (+1 px: pixel
    centres span n-1 for n pixels). Same output as `scale_from_points` with `source: "ref"`; `quality` adds
    `mask_touches_border` when the object is cut by the frame (length under-estimated)."""
    import cv2

    m = np.asarray(mask).astype(bool)
    if m.ndim != 2:
        raise BadInput("mask must be a 2-D array")
    w, h = _size(image_size)
    if m.shape != (h, w):
        raise BadInput(f"mask is {m.shape[1]}x{m.shape[0]} but image_size is {w}x{h}")
    ys, xs = np.nonzero(m)
    if xs.size < 2:
        raise BadInput("reference mask is empty")
    pts = np.column_stack([xs, ys]).astype(np.float32)
    (_, (rw, rh), _) = cv2.minAreaRect(pts)
    length_px = max(float(rw), float(rh)) + 1.0
    quality = []
    if xs.min() == 0 or ys.min() == 0 or xs.max() == w - 1 or ys.max() == h - 1:
        quality.append("mask_touches_border")
    return _scale_result(
        length_px, float(length_mm), (w, h), f_px, f_ratio, "ref", quality
    )


# --------------------------------------------------------------------------------------------------------------------
# homography helpers (what the UI calls instead of re-implementing the perspective divide)
# --------------------------------------------------------------------------------------------------------------------
def apply_homography(H: Any, pts: Any) -> Array:
    """Map Nx2 (or a single 2-vector) points through a 3x3 homography with the perspective divide."""
    P = np.asarray(pts, float)
    single = P.ndim == 1
    P = P.reshape(-1, 2)
    Hm = np.asarray(H, float).reshape(3, 3)
    q = np.column_stack([P, np.ones(len(P))]) @ Hm.T
    with np.errstate(divide="ignore", invalid="ignore"):
        out = q[:, :2] / q[:, 2:3]
    return out[0] if single else out


def px_to_grid_mm(H_px_to_grid_mm: Any, pts_px: Any) -> Array:
    """Photo pixels -> wall-plane mm (grid frame) with `homography_px_to_grid_mm`."""
    return apply_homography(H_px_to_grid_mm, pts_px)


def grid_mm_to_px(H_grid_mm_to_px: Any, pts_mm: Any) -> Array:
    """Wall-plane mm (grid frame) -> photo pixels with `homography_grid_mm_to_px`."""
    return apply_homography(H_grid_mm_to_px, pts_mm)


def _scale_gradient(H_grid_to_px: Array, cells_px: Array) -> float:
    """|d ln(mm/px) / d px| at the centroid of the cells: how strongly the grid's scale changes across the photo."""
    Hi = np.linalg.inv(H_grid_to_px)
    c = cells_px.mean(axis=0)
    d = 0.1 * float(np.ptp(cells_px, axis=0).max()) + 1.0

    def lg(p: Array) -> float:
        return math.log(max(local_mm_per_px(Hi, p), 1e-12))

    gx = (lg(c + [d, 0.0]) - lg(c - [d, 0.0])) / (2 * d)
    gy = (lg(c + [0.0, d]) - lg(c - [0.0, d])) / (2 * d)
    return float(math.hypot(gx, gy))


def local_mm_per_px(H_px_to_grid_mm: Any, pt_px: Any) -> float:
    """Wall mm per pixel at `pt_px`: sqrt|det| of the homography's Jacobian there (exact for the perspective map)."""
    p = np.asarray(pt_px, float).reshape(2)
    Hm = np.asarray(H_px_to_grid_mm, float).reshape(3, 3)
    v = Hm @ np.array([p[0], p[1], 1.0])
    w = v[2]
    if abs(w) < 1e-12:
        return float("nan")
    J = (
        np.array(
            [
                [Hm[0, 0] - v[0] / w * Hm[2, 0], Hm[0, 1] - v[0] / w * Hm[2, 1]],
                [Hm[1, 0] - v[1] / w * Hm[2, 0], Hm[1, 1] - v[1] / w * Hm[2, 1]],
            ]
        )
        / w
    )
    return float(math.sqrt(abs(np.linalg.det(J))))


def _scale_from_grid(calibration: dict[str, Any], at_px: Any = None) -> dict[str, Any]:
    """Scale of the wall plane from a `detect_grid` result (also after a JSON round trip) or from an HSW
    `photo_calibration.json` (`focal_px`, `camera_R_object_to_cv`, `camera_t_object_to_cv_mm`, `image_size_px`).

    `at_px` = where on the photo the scale is wanted (default: centroid of the grid's cells, else the image centre);
    the plane is perspective-foreshortened, so mm/px varies across the photo. Returns `{mm_per_px, plane_depth_mm, K,
    f_px, f_source, source: "grid", at_px, image_size, quality}`; the scale only holds for objects lying IN the grid
    plane (flag `plane_of_grid_only`)."""
    c = calibration
    quality = [
        "plane_of_grid_only",
        *[str(q) for q in c.get("quality", []) if q != "f_estimated"],
    ]
    if c.get("homography_grid_mm_to_px") is not None:
        if not c.get("ok", True):
            raise BadInput(
                f"grid calibration is not usable (reason: {c.get('reason')})"
            )
        Hm = np.asarray(c["homography_grid_mm_to_px"], float).reshape(3, 3)
        K = np.asarray(c["K"], float).reshape(3, 3)
        R = np.asarray(c["R"], float).reshape(3, 3)
        t = np.asarray(c["t_mm"], float).reshape(3)
        w, h = _size(c["image_size"])
        f_source = str(c.get("f_source", "estimated"))
        cells = np.asarray(c.get("inlier_cells_px", []), float).reshape(-1, 2)
    elif c.get("focal_px") is not None and c.get("camera_R_object_to_cv") is not None:
        w, h = _size(c["image_size_px"])
        K = default_intrinsics(w, h, float(c["focal_px"]))
        R = np.asarray(c["camera_R_object_to_cv"], float).reshape(3, 3)
        t = np.asarray(c["camera_t_object_to_cv_mm"], float).reshape(3)
        Hm = K @ np.column_stack([R[:, 0], R[:, 1], t])
        f_source = "calibration"
        cells = np.zeros((0, 2))
    else:
        raise BadInput(
            "calibration JSON has neither detect_grid keys (homography_grid_mm_to_px, K, R, t_mm, image_size) "
            "nor HSW photo_calibration keys (focal_px, camera_R_object_to_cv, camera_t_object_to_cv_mm, image_size_px)"
        )
    if at_px is not None:
        pt = np.asarray(at_px, float).reshape(2)
    else:
        pt = cells.mean(axis=0) if len(cells) else np.array([w / 2.0, h / 2.0])
    Hi = np.linalg.inv(Hm)
    X = apply_homography(Hi, pt)
    mm_per_px = local_mm_per_px(Hi, pt)
    depth = float((R[:, :2] @ X + t)[2])
    if not (math.isfinite(mm_per_px) and math.isfinite(depth) and depth > 0):
        raise BadInput(
            "the grid plane does not project to a finite scale at that pixel"
        )
    return {
        "mm_per_px": mm_per_px,
        "plane_depth_mm": depth,
        "K": K,
        "f_px": float(K[0, 0]),
        "f_source": f_source,
        "source": "grid",
        "at_px": pt,
        "image_size": [w, h],
        "quality": quality,
    }


def scale_from_grid(
    calibration: dict[str, Any], *, at_px: Any = None, image_size: Any = None
) -> dict[str, Any]:
    """Scale of the wall plane from a `detect_grid` result (also after a JSON round trip) or from an HSW
    `photo_calibration.json` (`focal_px`, `camera_R_object_to_cv`, `camera_t_object_to_cv_mm`, `image_size_px`).

    `image_size` (w, h) of the photo the scale is wanted for: a calibration made on a photo of another size raises
    `BadInput` (its homography is in that photo's pixels). Malformed calibrations raise `BadInput` too, never a bare
    KeyError/ValueError. Everything else as `_scale_from_grid`."""
    if not isinstance(calibration, dict):
        raise BadInput("the calibration must be a JSON object")
    if calibration.get("ok") is False:
        raise BadInput(
            f"the calibration is not usable (reason: {calibration.get('reason')})"
        )
    try:
        res = _scale_from_grid(calibration, at_px)
    except (KeyError, ValueError, TypeError, np.linalg.LinAlgError) as exc:
        raise BadInput(f"malformed grid calibration: {exc!r}") from exc
    if (
        image_size is not None
        and [int(v) for v in _size(image_size)] != res["image_size"]
    ):
        raise BadInput(
            f"the calibration is for a {res['image_size'][0]}x{res['image_size'][1]} photo but the photo is "
            f"{_size(image_size)[0]}x{_size(image_size)[1]}"
        )
    return res


def scale_from_homography(
    H_px_to_mm: Any,
    *,
    image_size: Any,
    at_px: Any = None,
    f_px: float | None = None,
    f_ratio: float = DEFAULT_F_RATIO,
) -> dict[str, Any]:
    """Scale at a pixel of a photo whose pixel -> plane-mm homography is `H_px_to_mm` (e.g. a photo registered onto a
    calibrated one: `H_other_px_to_mm @ H_a_to_b`). `at_px` defaults to the image centre. `plane_depth_mm = f * mm_per_px`
    assumes the plane is fronto-parallel there. Raises `BadInput` when the pixel is not on the plane's finite side."""
    w, h = _size(image_size)
    K = default_intrinsics(w, h, f_px, f_ratio)
    pt = np.asarray(at_px if at_px is not None else (w / 2.0, h / 2.0), float).reshape(
        2
    )
    Hm = np.asarray(H_px_to_mm, float).reshape(3, 3)
    mm_per_px = local_mm_per_px(Hm, pt)
    # the sign of w = h3 . (x, y, 1) is arbitrary (a homography is defined up to scale), so compare with the image
    # centre, which is taken to be on the finite side of the plane
    w_here = float(Hm[2] @ [pt[0], pt[1], 1.0])
    w_centre = float(Hm[2] @ [w / 2.0, h / 2.0, 1.0])
    if not (math.isfinite(mm_per_px) and mm_per_px > 0) or w_here * w_centre <= 0:
        raise BadInput(
            "that pixel is not on the finite side of the shared plane (above its horizon?)"
        )
    f_source, f_flag = _f_labels(f_px, f_ratio)
    return {
        "mm_per_px": mm_per_px,
        "plane_depth_mm": float(K[0, 0]) * mm_per_px,
        "K": K,
        "f_px": float(K[0, 0]),
        "f_source": f_source,
        "source": "homography",
        "at_px": pt,
        "image_size": [w, h],
        "quality": ["plane_of_overlap_only", "fronto_parallel_assumed", f_flag],
    }


# --------------------------------------------------------------------------------------------------------------------
# (a)+(b) cell-centre detection
# --------------------------------------------------------------------------------------------------------------------
def _periodic_filter(pts: Array, strength: Array) -> tuple[Array, Array]:
    """Keep only points that sit in a regular hexagonal neighbourhood (6 near-equal nearest-neighbour distances) at the
    dominant spacing: this is the 'high-frequency periodic texture' ROI in point form."""
    from scipy.spatial import cKDTree

    if len(pts) < 8:
        return pts, strength
    d, _ = cKDTree(pts).query(pts, k=7)
    d1, d6, dmed = d[:, 1], d[:, 6], np.median(d[:, 1:], axis=1)
    ok = (d6 < 1.45 * np.maximum(d1, 1e-9)) & np.isfinite(d6)
    if ok.sum() < 8:
        return pts[:0], strength[:0]
    spacing = float(np.median(dmed[ok]))
    keep = ok & (np.abs(dmed - spacing) < 0.35 * spacing)
    return pts[keep], strength[keep]


def find_cell_centres(
    rgb: Array,
    *,
    sigma: float = 3.0,
    polarity: str = "dark",
    max_points: int = 6000,
) -> dict[str, Any]:
    """Detect honeycomb cell centres as extrema of a difference-of-Gaussians band-pass tuned to the cell size.

    `sigma` is the blob scale in pixels (cell pitch / ~5; 3 for ~15 px cells); for sigma > 4 the image is shrunk so the
    filter stays small and the centres are mapped back. `polarity` "dark" = cell floor darker than its walls (recessed
    hexagons, the printed HSW panel), "bright" the opposite. Sub-pixel position: parabola through the 3x3 extremum.
    Every extremum above the noise floor is returned (strongest `max_points`); keeping only the ones that form the
    lattice is `solve_grid_pose`'s job (periodic-texture filter, then lattice growth).
    Returns `{points (Nx2 float, pixel-centre coords, x/y), strength, sigma, polarity, timing_s}`."""
    import cv2

    if polarity not in ("dark", "bright"):
        raise BadInput("polarity must be 'dark' or 'bright'")
    t0 = time.perf_counter()
    g = to_gray(rgb, np.float32)
    H0, W0 = g.shape
    base = 3.0
    shrink = max(1.0, sigma / base) if sigma > 4.0 else 1.0
    s = sigma / shrink
    sx = sy = shrink
    if shrink > 1.0:
        ws, hs = max(8, round(W0 / shrink)), max(8, round(H0 / shrink))
        g = cv2.resize(g, (ws, hs), interpolation=cv2.INTER_AREA)
        sx, sy = (
            W0 / ws,
            H0 / hs,
        )  # the real per-axis ratios (rounding makes them differ from `shrink`)
    band = cv2.GaussianBlur(g, (0, 0), s) - cv2.GaussianBlur(g, (0, 0), 1.6 * s)
    S = -band if polarity == "dark" else band
    r = max(2, int(round(2.0 * s)))
    peak = (cv2.dilate(S, np.ones((2 * r + 1, 2 * r + 1), np.uint8)) == S) & (S > 0)
    b = max(2, int(round(2 * s)))
    peak[:b, :] = peak[-b:, :] = False
    peak[:, :b] = peak[:, -b:] = False
    ys, xs = np.nonzero(peak)
    v = S[ys, xs]
    out_pts: Array = np.zeros((0, 2))
    out_v: Array = np.zeros(0)
    if v.size:
        noise = 1.4826 * float(np.median(np.abs(S - np.median(S))))
        # 1 grey level absolute floor: a flat image has a zero MAD and would otherwise 'detect' rounding noise
        thr = max(2.5 * noise, 0.15 * float(np.percentile(v, 99)), MIN_BLOB_CONTRAST)
        sel = v > thr
        ys, xs, v = ys[sel], xs[sel], v[sel]
        if v.size > max_points:
            top = np.argsort(-v)[:max_points]
            ys, xs, v = ys[top], xs[top], v[top]
        c = S[ys, xs]
        dx = S[ys, xs - 1] - 2 * c + S[ys, xs + 1]
        dy = S[ys - 1, xs] - 2 * c + S[ys + 1, xs]
        ox = np.where(
            dx < -1e-9,
            np.clip(
                0.5 * (S[ys, xs - 1] - S[ys, xs + 1]) / np.where(dx < -1e-9, dx, -1),
                -1,
                1,
            ),
            0,
        )
        oy = np.where(
            dy < -1e-9,
            np.clip(
                0.5 * (S[ys - 1, xs] - S[ys + 1, xs]) / np.where(dy < -1e-9, dy, -1),
                -1,
                1,
            ),
            0,
        )
        px = (xs + ox + 0.5) * sx - 0.5
        py = (ys + oy + 0.5) * sy - 0.5
        out_pts = np.column_stack([px, py])
        out_v = v
    return {
        "points": out_pts,
        "strength": out_v,
        "sigma": float(sigma),
        "polarity": polarity,
        "timing_s": time.perf_counter() - t0,
    }


# --------------------------------------------------------------------------------------------------------------------
# (c) lattice index assignment
# --------------------------------------------------------------------------------------------------------------------
def _lattice_mm(ij: Array, pitch: tuple[float, float]) -> Array:
    """Axial lattice indices (i along the 30 degree neighbour, j along the vertical one) -> mm in the grid frame."""
    pc, pr = pitch
    return np.column_stack([ij[:, 0] * pc, (ij[:, 1] + 0.5 * ij[:, 0]) * pr])


def _mm_to_lattice(X: Array, pitch: tuple[float, float]) -> Array:
    pc, pr = pitch
    i = X[:, 0] / pc
    return np.column_stack([i, X[:, 1] / pr - 0.5 * i])


def _hex_dist(d: Array) -> Array:
    return np.maximum(
        np.maximum(np.abs(d[..., 0]), np.abs(d[..., 1])), np.abs(d[..., 0] + d[..., 1])
    )


def _fit_h(X: Array, P: Array, thresh: float | None) -> Array | None:
    import cv2

    if len(X) < 4:
        return None
    Xn = X.astype(np.float64)
    method = cv2.RANSAC if (thresh is not None and len(X) >= 12) else 0
    H, _ = cv2.findHomography(
        Xn,
        P.astype(np.float64),
        method,
        thresh if thresh is not None else 3.0,
        maxIters=500,
    )
    if H is None or not np.isfinite(H).all() or abs(H[2, 2]) < 1e-12:
        return None
    return H / H[2, 2]


def _assign(
    points: Array,
    H: Array,
    pitch: tuple[float, float],
    labelled: set[tuple[int, int]] | None,
    grow: int,
    tol_px: float,
) -> tuple[dict[int, tuple[int, int]], Array]:
    """Label every point whose homography-predicted lattice node is within `tol_px`; with `labelled` given, only nodes
    within `grow` hex steps of an already labelled node are accepted (bounded extrapolation). Returns {point: (i, j)} and
    the per-point residual in px (inf when unassigned)."""
    try:
        Hi = np.linalg.inv(H)
    except np.linalg.LinAlgError:
        return {}, np.full(len(points), np.inf)
    X = apply_homography(Hi, points)
    ok = np.isfinite(X).all(axis=1)
    ij = np.zeros((len(points), 2))
    ij[ok] = np.rint(_mm_to_lattice(X[ok], pitch))
    pred = np.full_like(points, np.inf)
    pred[ok] = apply_homography(H, _lattice_mm(ij[ok], pitch))
    res = np.linalg.norm(points - pred, axis=1)
    res[~np.isfinite(res)] = np.inf
    cand = np.nonzero(res < tol_px)[0]
    if labelled is not None:
        lab = np.array(sorted(labelled), dtype=np.int64).reshape(-1, 2)
        if len(lab) == 0:
            return {}, res
        offs = np.array(
            [
                (a, b)
                for a in range(-grow, grow + 1)
                for b in range(-grow, grow + 1)
                if abs(a + b) <= grow
            ],
            dtype=np.int64,
        )
        keys_near = np.unique(
            (
                (lab[:, None, 0] + offs[None, :, 0]) * 100003
                + lab[:, None, 1]
                + offs[None, :, 1]
            ).ravel()
        )
        keys = ij[cand, 0].astype(np.int64) * 100003 + ij[cand, 1].astype(np.int64)
        cand = cand[np.isin(keys, keys_near)]
    best: dict[tuple[int, int], int] = {}
    for k in cand:
        key = (int(ij[k, 0]), int(ij[k, 1]))
        if key not in best or res[k] < res[best[key]]:
            best[key] = int(k)
    return {k: key for key, k in best.items()}, res


class _BudgetExceeded(Exception):
    """The wall-clock budget ran out before any lattice hypothesis was found (internal)."""


def _grow(
    points: Array,
    p0: int,
    u: Array,
    v: Array,
    pitch: tuple[float, float],
    spacing: float,
    deadline: float | None = None,
) -> tuple[Array, dict[int, tuple[int, int]], Array] | None:
    """Seeded region growing: affine patch around point `p0` with neighbour vectors u (-> axial i) and v (-> axial j),
    then iterate homography fit + bounded extrapolation until the labelling is stable."""
    A = np.column_stack([u, v])
    if abs(np.linalg.det(A)) < 1e-6:
        return None
    c = (points - points[p0]) @ np.linalg.inv(A).T
    ij = np.rint(c)
    res = np.linalg.norm(points - (points[p0] + ij @ A.T), axis=1)
    sel = np.nonzero((res < 0.25 * spacing) & (_hex_dist(ij) <= 2))[0]
    lab = {int(k): (int(ij[k, 0]), int(ij[k, 1])) for k in sel}
    H: Array | None = None
    for _ in range(40):
        if deadline is not None and time.perf_counter() >= deadline:
            return None
        if len(lab) < 4:
            return None
        ks = np.array(sorted(lab))
        X = _lattice_mm(np.array([lab[int(k)] for k in ks], float), pitch)
        H = _fit_h(X, points[ks], 0.2 * spacing)
        if H is None:
            return None
        new, res = _assign(points, H, pitch, set(lab.values()), 3, 0.22 * spacing)
        if set(new) == set(lab) and new == lab:
            break
        if len(new) < 4:
            return None
        lab = new
    assert H is not None
    return H, lab, res


def _assign_lattice(
    points: Array,
    pitch: tuple[float, float],
    seed: int = 0,
    max_seeds: int = 40,
    deadline: float | None = None,
) -> tuple[dict[int, tuple[int, int]], Array] | None:
    """Find the dominant hexagonal lattice in `points`; returns {point index: axial (i, j)} (orientation-labelled so the
    wall frame is right-handed in the photo; the roll is fixed later) and the homography fitted to it."""
    from scipy.spatial import cKDTree

    n = len(points)
    if n < MIN_INLIERS:
        return None
    k = min(7, n)
    d, nn = cKDTree(points).query(points, k=k)
    hexness = d[:, -1] / np.maximum(d[:, 1], 1e-9)
    order = np.argsort(hexness)
    rng = np.random.default_rng(seed)
    pool = order[: max(1, n // 2)]
    seeds = list(rng.permutation(pool)[:max_seeds])
    best: tuple[int, float, Array, dict[int, tuple[int, int]]] | None = None
    for p0 in seeds:
        if deadline is not None and time.perf_counter() >= deadline:
            if best is None:
                raise _BudgetExceeded
            break
        vec = points[nn[p0, 1:]] - points[p0]
        if len(vec) < 3:
            continue
        spacing = float(np.median(d[p0, 1:]))
        u = vec[0]
        crs = u[0] * vec[:, 1] - u[1] * vec[:, 0]
        cosang = (vec @ u) / np.maximum(
            np.linalg.norm(vec, axis=1) * np.linalg.norm(u), 1e-9
        )
        cand = np.nonzero((crs < 0) & (cosang > 0.3) & (cosang < 0.75))[
            0
        ]  # 41..72 degrees, correct handedness
        if cand.size == 0:
            continue
        v = vec[int(cand[np.argmin(np.abs(cosang[cand] - 0.5))])]
        grown = _grow(points, int(p0), u, v, pitch, spacing, deadline)
        if grown is None:
            continue
        H, lab, res = grown
        good = int(np.sum([res[kk] < 0.2 * spacing for kk in lab]))
        score = (good, -float(np.median([res[kk] for kk in lab])))
        if best is None or score > (best[0], best[1]):
            best = (score[0], score[1], H, lab)
        if good >= 0.7 * n and good >= STRONG_INLIERS:
            break
    if best is None and deadline is not None and time.perf_counter() >= deadline:
        raise _BudgetExceeded
    if best is None or best[0] < MIN_INLIERS:
        return None
    return best[3], best[2]


# --------------------------------------------------------------------------------------------------------------------
# (d) pose + focal
# --------------------------------------------------------------------------------------------------------------------
def _project(f: float, cx: float, cy: float, rvec: Array, t: Array, X3: Array) -> Array:
    import cv2

    K = np.array([[f, 0, cx], [0, f, cy], [0, 0, 1.0]])
    return cv2.projectPoints(X3, rvec, t, K, None)[0].reshape(-1, 2)


def _pose_from_h(H: Array, K: Array) -> tuple[Array, Array]:
    M = np.linalg.inv(K) @ H
    lam = 2.0 / (np.linalg.norm(M[:, 0]) + np.linalg.norm(M[:, 1]))
    if M[2, 2] * lam < 0:
        lam = -lam
    r1, r2, t = lam * M[:, 0], lam * M[:, 1], lam * M[:, 2]
    U, _, Vt = np.linalg.svd(np.column_stack([r1, r2, np.cross(r1, r2)]))
    R = U @ Vt
    if np.linalg.det(R) < 0:
        R = U @ np.diag([1, 1, -1]) @ Vt
    return R, t


def _refine_pose(
    P: Array,
    X: Array,
    f0: float,
    cx: float,
    cy: float,
    R0: Array,
    t0: Array,
    fit_f: bool,
) -> tuple[float, Array, Array, float]:
    """Huber-weighted Levenberg-Marquardt over (f?, rvec, t). Returns f, R, t and the relative std of f (inf if f fixed
    or unobservable)."""
    import cv2

    X3 = np.column_stack([X, np.zeros(len(X))])
    rvec0, _ = cv2.Rodrigues(R0)
    p = np.concatenate([[f0] if fit_f else [], rvec0.ravel(), t0])
    off = 1 if fit_f else 0

    def unpack(q: Array) -> tuple[float, Array, Array]:
        return (float(q[0]) if fit_f else f0), q[off : off + 3], q[off + 3 : off + 6]

    def resid(q: Array) -> Array:
        f, rv, tv = unpack(q)
        return (_project(f, cx, cy, rv, tv, X3) - P).ravel()

    def jac(q: Array) -> Array:
        J = np.empty((2 * len(P), len(q)))
        for k in range(len(q)):
            step = 1e-6 * max(1.0, abs(q[k])) if k >= off else 1e-5 * abs(q[k])
            if k >= off + 3:
                step = 1e-4 * max(1.0, abs(q[k]))
            qp, qm = q.copy(), q.copy()
            qp[k] += step
            qm[k] -= step
            J[:, k] = (resid(qp) - resid(qm)) / (2 * step)
        return J

    huber = 1.0
    lam = 1e-3
    r = resid(p)
    for _ in range(80):
        rn = np.hypot(r[0::2], r[1::2])
        w = np.sqrt(np.where(rn > huber, huber / np.maximum(rn, 1e-12), 1.0)).repeat(2)
        J = jac(p) * w[:, None]
        g = J.T @ (w * r)
        A = J.T @ J
        improved = False
        for _try in range(10):
            try:
                dq = -np.linalg.solve(A + lam * np.diag(np.diag(A) + 1e-12), g)
            except np.linalg.LinAlgError:
                lam *= 10
                continue
            pn = p + dq
            rr = resid(pn)
            rnn = np.hypot(rr[0::2], rr[1::2])
            if float(np.sum(np.minimum(rnn**2, 2 * huber * rnn - huber**2))) <= float(
                np.sum(np.minimum(rn**2, 2 * huber * rn - huber**2))
            ):
                p, r, lam, improved = pn, rr, max(lam / 5, 1e-9), True
                break
            lam *= 10
        if not improved or float(np.abs(dq).max()) < 1e-9 * max(
            1.0, float(np.abs(p).max())
        ):
            break
    f, rv, tv = unpack(p)
    R, _ = cv2.Rodrigues(rv)
    f_rel = float("inf")
    if fit_f:
        rn = np.hypot(r[0::2], r[1::2])
        w = np.sqrt(np.where(rn > huber, huber / np.maximum(rn, 1e-12), 1.0)).repeat(2)
        J = jac(p) * w[:, None]
        sig2 = float(np.sum((w * r) ** 2)) / max(2 * len(P) - len(p), 1)
        try:
            cov = sig2 * np.linalg.inv(J.T @ J)
            f_rel = math.sqrt(max(cov[0, 0], 0.0)) / max(abs(f), 1e-9)
        except np.linalg.LinAlgError:
            pass
    return f, R, tv.copy(), f_rel


def _initial_focal(
    P: Array, X: Array, H: Array, cx: float, cy: float, diag: float
) -> float:
    """Coarse focal from the homography: scan f, build the pose with r1 ~ r2 (orthonormal), keep the lowest reprojection."""
    best_f, best_e = 0.75 * diag, math.inf
    X3 = np.column_stack([X, np.zeros(len(X))])
    import cv2

    for f in diag * np.geomspace(0.15, 5.0, 70):
        K = np.array([[f, 0, cx], [0, f, cy], [0, 0, 1.0]])
        R, t = _pose_from_h(H, K)
        if t[2] <= 0:
            continue
        rv, _ = cv2.Rodrigues(R)
        e = float(np.median(np.linalg.norm(_project(f, cx, cy, rv, t, X3) - P, axis=1)))
        if e < best_e:
            best_f, best_e = float(f), e
    return best_f


def _roll_deg(R: Array) -> float:
    """Angle of the grid +y axis from image-up (0 = upright)."""
    return math.degrees(math.atan2(R[0, 1], -R[1, 1]))


def _rot_z(deg: float) -> Array:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1.0]])


def _fail(
    reason: str,
    n_cells: int,
    size: tuple[int, int],
    pitch: tuple[float, float],
    timing: dict[str, float],
) -> dict[str, Any]:
    return {
        "ok": False,
        "reason": reason,
        "K": None,
        "f_px": None,
        "R": None,
        "t_mm": None,
        "homography_grid_mm_to_px": None,
        "homography_px_to_grid_mm": None,
        "reproj_median_px": None,
        "reproj_p95_px": None,
        "n_cells": int(n_cells),
        "n_inliers": 0,
        "inlier_cells_px": np.zeros((0, 2)),
        "inlier_cells": np.zeros((0, 2), int),
        "f_source": None,
        "quality": [reason],
        "image_size": list(size),
        "grid_pitch_mm": list(pitch),
        "mm_per_px_centre": None,
        "plane_depth_mm": None,
        "tilt_deg": None,
        "roll_deg": None,
        "coverage": None,
        "f_rel_uncertainty": None,
        "roi_xywh": [0, 0, 0, 0],
        "timing_s": timing,
    }


def solve_grid_pose(
    points_px: Any,
    *,
    image_size: Any,
    grid_pitch_mm: tuple[float, float] = DEFAULT_GRID_PITCH_MM,
    f_px: float | None = None,
    f_ratio: float | None = None,
    seed: int = 0,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Cell centres (Nx2 px, any outliers allowed) -> lattice labelling + camera pose. Never raises on 'no grid': returns
    `ok=False` with a `reason` ("too_few_cells", "no_lattice", "degenerate_lattice", "implausible_pose").

    Focal length: `f_px` / `f_ratio * diagonal` are taken as KNOWN (only R, t are solved); with neither, f is estimated
    from the perspective of the grid alone (square pixels, centred principal point) and `f_source` is "estimated". When the
    grid is too fronto-parallel for f to be observable (relative std > 15 %, or outside 0.15..5 diagonals) the default
    0.375*diagonal is used instead, `f_source` is "default" and `quality` contains `f_unobservable_prior_used`."""
    t_all = time.perf_counter()
    w, h = _size(image_size)
    pitch = (float(grid_pitch_mm[0]), float(grid_pitch_mm[1]))
    if not (pitch[0] > 0 and pitch[1] > 0):
        raise BadInput("grid_pitch_mm must be two positive numbers")
    if abs(pitch[0] / pitch[1] - math.sqrt(3) / 2) > 0.02:
        raise BadInput(
            f"grid_pitch_mm {pitch[0]} x {pitch[1]} is not a regular honeycomb (column pitch / row pitch must be sqrt(3)/2 = 0.866)",
            "the labelling relies on the 6-fold symmetry of a regular hexagonal lattice",
        )
    pts = (
        np.asarray(points_px, float).reshape(-1, 2)
        if np.size(points_px)
        else np.zeros((0, 2))
    )
    pts = pts[np.isfinite(pts).all(axis=1)]
    timing: dict[str, float] = {}
    if len(pts) < MIN_INLIERS:
        return _fail("too_few_cells", len(pts), (w, h), pitch, timing)

    t0 = time.perf_counter()
    seed_pts, _ = _periodic_filter(pts, np.zeros(len(pts)))
    if (
        len(seed_pts) > MAX_SEED_POINTS
    ):  # bounded work on textured clutter; deterministic subsample
        keep = np.random.default_rng(seed).choice(
            len(seed_pts), MAX_SEED_POINTS, replace=False
        )
        seed_pts = seed_pts[np.sort(keep)]
    try:
        got = _assign_lattice(seed_pts, pitch, seed=seed, deadline=deadline)
    except _BudgetExceeded:
        timing["lattice_s"] = time.perf_counter() - t0
        return _fail("time_budget", len(pts), (w, h), pitch, timing)
    timing["lattice_s"] = time.perf_counter() - t0
    if got is None:
        return _fail("no_lattice", len(pts), (w, h), pitch, timing)
    lab, H0 = got
    idx = np.array(sorted(lab))
    ij = np.array([lab[int(k)] for k in idx], float)
    if len(np.unique(ij[:, 0])) < 3 or len(np.unique(ij[:, 1] + 0.5 * ij[:, 0])) < 3:
        return _fail("degenerate_lattice", len(pts), (w, h), pitch, timing)

    t0 = time.perf_counter()
    cx, cy, diag = w / 2.0, h / 2.0, math.hypot(w, h)
    known_f = (
        f_px if f_px is not None else (f_ratio * diag if f_ratio is not None else None)
    )
    if known_f is not None and not (math.isfinite(known_f) and known_f > 0):
        raise BadInput(
            f"focal length must be a positive number of pixels, got {known_f!r}"
        )
    quality: list[str] = []
    P = seed_pts[idx]
    X = _lattice_mm(ij, pitch)
    f_source = "given" if known_f is not None else "estimated"
    f0 = (
        float(known_f)
        if known_f is not None
        else _initial_focal(P, X, H0, cx, cy, diag)
    )
    K0 = np.array([[f0, 0, cx], [0, f0, cy], [0, 0, 1.0]])
    R0, t0v = _pose_from_h(H0, K0)
    f, R, t, f_rel = _refine_pose(P, X, f0, cx, cy, R0, t0v, known_f is None)

    # final inlier set: every centre that lands on a lattice node of the refined model, grown outwards from the seed
    # patch so scattered non-lattice extrema elsewhere in the photo can never be adopted; two re-fit rounds
    nodes = {lab[int(k)] for k in idx}
    fit_f = f_source == "estimated"
    f_rel2 = f_rel
    for _round in range(2):
        K = np.array([[f, 0, cx], [0, f, cy], [0, 0, 1.0]])
        Hm = K @ np.column_stack([R[:, 0], R[:, 1], t])
        labs = _relabel(pts, Hm, pitch, nodes)
        sel = np.array(sorted(labs))
        if len(sel) < MIN_INLIERS:
            return _fail("no_lattice", len(pts), (w, h), pitch, timing)
        ij = np.array([labs[int(k)] for k in sel], float)
        nodes = set(labs.values())
        P, X = pts[sel], _lattice_mm(ij, pitch)
        f, R, t, f_rel2 = _refine_pose(P, X, f, cx, cy, R, t, fit_f)
    # Wrong-polarity trap: the WALL JUNCTIONS of a honeycomb form a triangular lattice with one node in three missing (the
    # cell centres), 1/sqrt(3) of the true pitch. Its occupied nodes fall into two classes of (i - j) mod 3 and the third
    # class is empty; a real cell lattice fills all three evenly. Rejecting it makes the caller try the other polarity/scale.
    classes = np.sort(np.bincount((ij[:, 0] - ij[:, 1]).astype(int) % 3, minlength=3))
    if (
        len(sel) >= 24
        and classes[0] < 0.15 * classes[2]
        and classes[1] > 0.6 * classes[2]
    ):
        return _fail("vertex_lattice", len(pts), (w, h), pitch, timing)
    timing["pose_s"] = time.perf_counter() - t0
    if t[2] <= 0:
        return _fail("implausible_pose", len(pts), (w, h), pitch, timing)
    footprint = float(np.hypot(*np.ptp(P, axis=0))) / diag
    f_unc = float("nan")
    if f_source == "estimated":
        # The Jacobian (noise-only) std understates the truth: focal length only enters through the perspective gradient
        # of the lattice scale, g = |d ln(mm/px) / d px|, and a centre bias of 0.1 % per 100 px (what changing the detector
        # scale by +-30 % does) moves g by ~1e-5 per px. So f is uncertain by ~EPS_G / g: measured on IMG_4937 (g = 3.1e-4)
        # sweeping the detector scale 2.2..3.4 px moved f by +-10 % while the formal std was 1.5 %; on synthetic panels
        # 5 degrees of tilt (g = 1.5e-4) gave +16 %, 25 degrees (g = 7.5e-4) +2 %. g comes from the free homography H0, so
        # it does not depend on the (possibly wrong) focal length. Heuristic constant, calibrated on those measurements.
        f_unc = max(f_rel2, EPS_G / max(_scale_gradient(H0, P), 1e-9))
        if f_unc > 0.15 or footprint < 0.2 or not (0.15 * diag < f < 5.0 * diag):
            quality.append("f_unobservable_prior_used")
            f_source = "default"
            f, R, t, _ = _refine_pose(P, X, DEFAULT_F_RATIO * diag, cx, cy, R, t, False)
        else:
            quality.append("f_estimated" if f_unc <= 0.05 else "f_weak")
    K = np.array([[f, 0, cx], [0, f, cy], [0, 0, 1.0]])
    tilt = math.degrees(
        math.acos(float(np.clip(-R[2, 2], -1.0, 1.0)))
    )  # of the pose actually returned

    # fix the grid-frame labelling: the lattice has 6-fold symmetry, so pick the 60-degree rotation whose +y is most upright
    k_best = min(range(6), key=lambda k: abs(_roll_deg(R @ _rot_z(-60.0 * k))))
    theta = 60.0 * k_best
    Rz = _rot_z(theta)
    R = R @ _rot_z(-theta)
    rot = Rz[:2, :2]
    Xr = X @ rot.T
    ijr = np.rint(_mm_to_lattice(Xr, pitch)).astype(int)
    col = ijr[:, 0] - ijr[:, 0].min()
    # (col, row): row = j + floor(col / 2) up to a constant; shift so min(col) = 0 and min(row) = 0
    row = ijr[:, 1] + np.floor_divide(col, 2)
    row = row - int(row.min())
    # moving the grid origin to the cell centre ((col + .5) pc, (row + .5 + .5 (col & 1)) pr) translates the grid frame
    Xnew = np.column_stack(
        [(col + 0.5) * pitch[0], (row + 0.5 + 0.5 * (col & 1)) * pitch[1]]
    )
    shift_mm = np.median(Xnew - Xr, axis=0)
    t = t - R[:, :2] @ shift_mm
    Hm = K @ np.column_stack([R[:, 0], R[:, 1], t])
    Hm = Hm / Hm[2, 2]
    proj = apply_homography(Hm, Xnew)
    err = np.linalg.norm(proj - P, axis=1)
    Hinv = np.linalg.inv(Hm)
    Hinv = Hinv / Hinv[2, 2]
    cen = P.mean(axis=0)
    centre_mm = px_to_grid_mm(Hinv, cen)
    Xc = np.array([centre_mm[0], centre_mm[1], 0.0])
    depth = float((R @ Xc + t)[2])
    q = list(quality)
    if len(sel) < 30:
        q.append("few_cells")
    if tilt < 5.0:
        q.append("near_fronto_parallel")
    # plausibility: a real lattice fits to a few percent of the cell spacing, fills the region it spans and has cells big
    # enough to localise; chance lattices in carpet, clutter or sensor noise fail at least one of these
    import cv2

    mm_px = max(local_mm_per_px(Hinv, cen), 1e-12)
    spacing_px = pitch[1] / mm_px
    med_err = float(np.median(err))
    hull_area = float(cv2.contourArea(cv2.convexHull(P.astype(np.float32))))
    coverage = len(sel) / max(hull_area * mm_px**2 / (pitch[0] * pitch[1]), 1.0)
    if (
        spacing_px < 6.0
        or coverage < 0.4
        or med_err > max(1.5, 0.05 * spacing_px)
        or (len(sel) < 30 and med_err > 0.03 * spacing_px)
    ):
        reason = (
            "cells_too_small"
            if spacing_px < 6.0
            else "sparse_lattice"
            if coverage < 0.4
            else "poor_fit"
        )
        return _fail(reason, len(pts), (w, h), pitch, timing)
    return {
        "ok": True,
        "reason": None,
        "K": K,
        "coverage": float(coverage),
        "f_px": float(f),
        "R": R,
        "t_mm": t,
        "homography_grid_mm_to_px": Hm,
        "homography_px_to_grid_mm": Hinv,
        "reproj_median_px": float(np.median(err)),
        "reproj_p95_px": float(np.percentile(err, 95)),
        "n_cells": int(len(pts)),
        "n_inliers": int(len(sel)),
        "inlier_cells_px": P,
        "inlier_cells": np.column_stack([col, row]).astype(int),
        "f_source": f_source,
        "quality": q,
        "image_size": [w, h],
        "grid_pitch_mm": list(pitch),
        "mm_per_px_centre": local_mm_per_px(Hinv, cen),
        "plane_depth_mm": depth,
        "tilt_deg": tilt,
        "roll_deg": _roll_deg(R),
        "f_rel_uncertainty": float(f_unc) if math.isfinite(f_unc) else None,
        "roi_xywh": [
            int(P[:, 0].min()),
            int(P[:, 1].min()),
            int(np.ptp(P[:, 0])),
            int(np.ptp(P[:, 1])),
        ],
        "timing_s": {**timing, "total_s": time.perf_counter() - t_all},
    }


def _relabel(
    pts: Array,
    Hm: Array,
    pitch: tuple[float, float],
    start: set[tuple[int, int]],
) -> dict[int, tuple[int, int]]:
    """Label every point on a lattice node of the model (tolerance 12 % of the local spacing), growing outwards from the
    `start` nodes by at most 2 hex steps per pass until stable."""
    mm = _lattice_mm(np.array(sorted(start), float), pitch).mean(axis=0)
    step = apply_homography(Hm, mm + np.array([0.0, pitch[1]])) - apply_homography(
        Hm, mm
    )
    tol = 0.12 * float(np.linalg.norm(step))
    nodes = set(start)
    labelled: dict[int, tuple[int, int]] = {}
    for _ in range(25):
        labelled, _res = _assign(pts, Hm, pitch, nodes, 2, tol)
        grown = nodes | set(labelled.values())
        if grown == nodes:
            break
        nodes = grown
    return labelled


# --------------------------------------------------------------------------------------------------------------------
# public entry point
# --------------------------------------------------------------------------------------------------------------------
_SIGMAS = (3.0, 2.0, 4.2, 6.0, 8.5, 12.0)


def detect_grid(
    rgb: Array,
    *,
    grid_pitch_mm: tuple[float, float] = DEFAULT_GRID_PITCH_MM,
    f_px: float | None = None,
    f_ratio: float | None = None,
    sigmas: tuple[float, ...] = _SIGMAS,
    time_budget_s: float = 8.0,
) -> dict[str, Any]:
    """Honeycomb grid camera calibration from one photo of the wall panel. See the module docstring for the frame.

    Keys: `ok`, `reason`, `K`, `f_px`, `R`, `t_mm`, `homography_grid_mm_to_px`, `homography_px_to_grid_mm` (row-major 3x3,
    photo px -> wall mm: what hsw-editor stores as Calibration.homography), `reproj_median_px`, `reproj_p95_px`,
    `n_cells` (detected centres after the periodic filter), `n_inliers`, `inlier_cells_px` (Nx2), `inlier_cells` (Nx2
    `[col, row]` labels), `f_source` ("estimated" | "given" | "default"), `quality` (flag list), `image_size`,
    `grid_pitch_mm`, `mm_per_px_centre`, `plane_depth_mm`, `tilt_deg`, `roll_deg`, `roi_xywh`, `polarity`, `sigma`,
    `timing_s`. Never raises on 'no grid' and never returns NaN: `ok=False` + `reason`, numeric fields None.
    `sigmas` are the blob scales tried in order (cell pitch in px / 5); the first strong lattice wins.

    `time_budget_s` (default 8) is a wall-clock budget enforced between blob scales and inside the lattice-growth loops, so
    a photo without a grid (carpet, mesh, grille: quasi-periodic texture) returns in seconds, not minutes: `ok=False`,
    `reason="time_budget"` and `quality=["time_budget"]` when the budget ran out before any lattice was found; the best
    partial diagnostics (`n_cells`, `timing_s`) stay in the result. A lattice found before the budget is still returned."""
    t0 = time.perf_counter()
    g = np.asarray(rgb)
    if g.ndim not in (2, 3) or g.size == 0:
        raise BadInput("rgb must be a non-empty HxWx3 (or HxW) image")
    h, w = g.shape[:2]
    for name, val in (("f_px", f_px), ("f_ratio", f_ratio)):
        if val is not None and not (math.isfinite(val) and val > 0):
            raise BadInput(f"{name} must be a positive number, got {val!r}")
    best: dict[str, Any] | None = None
    t_detect = 0.0
    expired = False
    deadline = t0 + float(time_budget_s)

    def attempt(sigma: float, pol: str) -> dict[str, Any]:
        nonlocal t_detect, expired
        cc = find_cell_centres(g, sigma=sigma, polarity=pol)
        t_detect += cc["timing_s"]
        res = solve_grid_pose(
            cc["points"],
            image_size=(w, h),
            grid_pitch_mm=grid_pitch_mm,
            f_px=f_px,
            f_ratio=f_ratio,
            deadline=deadline,
        )
        expired = expired or res["reason"] == "time_budget"
        res["polarity"], res["sigma"] = pol, float(sigma)
        return res

    # Recessed cells are dark: try dark at every scale before bright. At too small a blob scale the bright extrema of a
    # honeycomb (wall junctions plus shaded hole centres) form a complete triangular lattice at 1/sqrt(3) of the cell
    # pitch that fits just as well but has the wrong scale, so bright is only a fallback for printed light cells.
    for pol in ("dark", "bright"):
        if pol == "bright" and best is not None and best["ok"]:
            break  # never let a bright result displace a dark one (wrong-scale junction lattice)
        for sigma in sigmas:
            if time.perf_counter() >= deadline and best is not None:
                expired = True
                break
            res = attempt(sigma, pol)
            if best is None or (res["ok"], res["n_inliers"]) > (
                best["ok"],
                best["n_inliers"],
            ):
                best = res
            if res["ok"] and res["n_inliers"] >= STRONG_INLIERS:
                break
        else:
            continue
        break
    assert best is not None
    if best["ok"] and time.perf_counter() < deadline:
        # the lattice now tells the true cell size: re-detect at the matching blob scale (row pitch in px / 4.8) when the
        # scale that found it was off by more than 20 %, which sharpens the centres of large cells
        spacing_px = grid_pitch_mm[1] / best["mm_per_px_centre"]
        tuned = spacing_px / 4.8
        if abs(tuned / best["sigma"] - 1.0) > 0.2:
            again = attempt(tuned, best["polarity"])
            if again["ok"] and again["n_inliers"] >= 0.8 * best["n_inliers"]:
                best = again
    if not best["ok"] and expired:
        best = {**best, "reason": "time_budget", "quality": ["time_budget"]}
    best["timing_s"] = {
        **best["timing_s"],
        "detect_s": t_detect,
        "total_s": time.perf_counter() - t0,
    }
    return best
