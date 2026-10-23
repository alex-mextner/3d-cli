"""Photo-to-photo registration: SIFT + RANSAC homography, decomposed relative pose, mask warping, repeatability IoU.

Numpy + OpenCV only (no torch, no model, no GPU). The homography is the PLANE-induced one: it is exact for points on a
single plane (the wall, the carpet) and only approximate for tall objects, so the relative pose it yields is the pose of
photo B w.r.t. photo A for that plane: `X_B = R @ X_A + t_over_depth * d`, with `d` the distance of the plane from camera A
(unknown without a scale: pass `plane_depth_mm` to get `t_mm`).

`baseline_ok` answers "does the second photo add parallax?". It is False when the relative rotation is below 3 degrees AND
the translation perpendicular to the plane normal (`lateral_translation_ratio`) is below 5 % of the plane depth: the second
photo is then (almost) the same viewpoint, or a zoom/dolly along the optical axis (measured: carpet HEIC
vs IMG_4934 -> 1853 matches, rotation ~0 deg, translation 0), so nothing about the 3-D shape can be learnt from it. A pure
camera rotation >= 3 degrees about its own centre still reports `baseline_ok=True`; use `lateral_translation_ratio` to tell it apart.
"""

from __future__ import annotations

import math
import time
from typing import Any

import numpy as np

from .calibrate import default_intrinsics
from .errors import BadInput
from .io import to_gray

Array = np.ndarray
MIN_INLIERS = 15
BASELINE_MIN_ROT_DEG = 3.0
BASELINE_MIN_TRANSLATION_RATIO = 0.05


def _rot_angle_deg(R: Array) -> float:
    return math.degrees(math.acos(float(np.clip((np.trace(R) - 1.0) / 2.0, -1.0, 1.0))))


def _empty(
    reason: str,
    size_a: tuple[int, int],
    size_b: tuple[int, int],
    timing: dict[str, float],
    matches: int = 0,
    inliers: int = 0,
) -> dict[str, Any]:
    return {
        "ok": False,
        "reason": reason,
        "matches": matches,
        "inliers": inliers,
        "median_reproj_px": None,
        "H_a_to_b": None,
        "rel_R": None,
        "rel_rot_deg": None,
        "t_over_depth": None,
        "translation_ratio": None,
        "lateral_translation_ratio": None,
        "t_mm": None,
        "plane_normal": None,
        "n_solutions": 0,
        "baseline_ok": False,
        "quality": [reason],
        "warped_masks": [],
        "repeatability": [],
        "image_size_a": list(size_a),
        "image_size_b": list(size_b),
        "timing_s": timing,
    }


def decompose_plane_homography(
    H: Array,
    K_a: Array,
    K_b: Array,
    pts_a: Array,
    pts_b: Array,
) -> tuple[Array, Array, Array, int] | None:
    """Relative pose (R, t/d, n) of the plane-induced homography `H` (A px -> B px) or None when it cannot be split.

    Of OpenCV's up to 4 candidate decompositions the one that puts the most matched points in front of both cameras wins
    (ties: the plane normal most aligned with camera A's optical axis). Returns (R, t_over_depth, normal, n_solutions)."""
    import cv2

    Hn = np.linalg.inv(K_b) @ H @ K_a
    det = float(np.linalg.det(Hn))
    if not math.isfinite(det) or abs(det) < 1e-12:
        return None
    Hn = Hn / np.cbrt(det)
    try:
        n_sol, Rs, Ts, Ns = cv2.decomposeHomographyMat(Hn, np.eye(3))
    except cv2.error:
        return None
    if n_sol == 0:
        return None
    xa = np.column_stack([pts_a, np.ones(len(pts_a))]) @ np.linalg.inv(K_a).T
    best_i, best_key = -1, (-1, -2.0)
    for i in range(n_sol):
        R, t, n = Rs[i], Ts[i].reshape(3), Ns[i].reshape(3)
        s = 1.0 / np.where(
            np.abs(xa @ n) < 1e-9, 1e-9, xa @ n
        )  # depth along each ray of A with plane n.X = 1
        ok_a = s > 0
        zb = ((xa * s[:, None]) @ R.T + t)[:, 2]
        front = int(np.sum(ok_a & (zb > 0)))
        key = (front, float(n[2]))
        if key > best_key:
            best_i, best_key = i, key
    return Rs[best_i], Ts[best_i].reshape(3), Ns[best_i].reshape(3), int(n_sol)


def _as_mask_list(masks: Any, shape: tuple[int, int], name: str) -> list[Array]:
    if masks is None:
        return []
    arr = [
        np.asarray(m).astype(bool)
        for m in (
            masks if not isinstance(masks, np.ndarray) or masks.ndim == 3 else [masks]
        )
    ]
    for i, m in enumerate(arr):
        if m.shape != shape:
            raise BadInput(
                f"{name}[{i}] is {m.shape[1]}x{m.shape[0]} but its image is {shape[1]}x{shape[0]}"
            )
    return arr


def register(
    a_rgb: Array,
    b_rgb: Array,
    masks_a: Any = None,
    K_a: Any = None,
    K_b: Any = None,
    *,
    masks_b: Any = None,
    plane_depth_mm: float | None = None,
    max_side: int = 2000,
    flag_below: float = 0.75,
    ratio: float = 0.75,
    ransac_px: float = 4.0,
) -> dict[str, Any]:
    """Register photo A to photo B.

    `masks_a` / `masks_b`: optional lists (or 3-D arrays) of bool HxW instance masks of A / B. `K_a` / `K_b`: 3x3
    intrinsics; default `default_intrinsics` (f = 0.375 * diagonal). Returns a dict:
      ok, reason, matches, inliers, median_reproj_px, H_a_to_b (3x3, full-resolution px), rel_R (B w.r.t. A),
      rel_rot_deg, t_over_depth (unit: plane distance in A), translation_ratio (norm of it), t_mm (needs plane_depth_mm),
      plane_normal, n_solutions, baseline_ok, quality (flags), K_a, K_b,
      warped_masks (A masks warped into B, bool arrays), repeatability
      ([{index, iou, best_b_index, area_a_px, warped_area_px, inside_b_frac, unstable}], iou None without masks_b),
      timing_s.
    Never raises for 'cannot register' (ok=False + reason: too_few_features | too_few_matches | no_homography |
    too_few_inliers); raises BadInput for malformed arguments."""
    import cv2

    t_all = time.perf_counter()
    ga, gb = to_gray(a_rgb), to_gray(b_rgb)
    (ha, wa), (hb, wb) = ga.shape, gb.shape
    if min(ha, wa, hb, wb) < 16:
        raise BadInput("images are too small to register")
    ma = _as_mask_list(masks_a, (ha, wa), "masks_a")
    mb = _as_mask_list(masks_b, (hb, wb), "masks_b")
    Ka = (
        np.asarray(K_a, float).reshape(3, 3)
        if K_a is not None
        else default_intrinsics(wa, ha)
    )
    Kb = (
        np.asarray(K_b, float).reshape(3, 3)
        if K_b is not None
        else default_intrinsics(wb, hb)
    )
    timing: dict[str, float] = {}

    def shrink(g: Array) -> tuple[Array, float]:
        s = min(1.0, max_side / max(g.shape))
        return (
            cv2.resize(g, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
            if s < 1.0
            else g
        ), s

    def _unshrink(pts: Array, small: tuple[int, ...], full: tuple[int, ...]) -> Array:
        sx, sy = small[1] / full[1], small[0] / full[0]
        return np.column_stack(
            [(pts[:, 0] + 0.5) / sx - 0.5, (pts[:, 1] + 0.5) / sy - 0.5]
        ).astype(np.float32)

    t0 = time.perf_counter()
    sa, fa = shrink(ga)
    sb, fb = shrink(gb)
    sift = cv2.SIFT.create(4000)
    kpa, da = sift.detectAndCompute(sa, None)
    kpb, db = sift.detectAndCompute(sb, None)
    timing["features_s"] = time.perf_counter() - t0
    if da is None or db is None or len(kpa) < 8 or len(kpb) < 8:
        return _empty("too_few_features", (wa, ha), (wb, hb), timing)

    t0 = time.perf_counter()
    knn = cv2.BFMatcher().knnMatch(da, db, k=2)
    good = [m[0] for m in knn if len(m) == 2 and m[0].distance < ratio * m[1].distance]
    timing["match_s"] = time.perf_counter() - t0
    if len(good) < MIN_INLIERS:
        return _empty("too_few_matches", (wa, ha), (wb, hb), timing, matches=len(good))
    # SIFT works on the shrunk copies: map back with the REAL per-axis ratio and the pixel-centre convention
    pa = _unshrink(
        np.array([kpa[m.queryIdx].pt for m in good], dtype=np.float32),
        sa.shape,
        ga.shape,
    )
    pb = _unshrink(
        np.array([kpb[m.trainIdx].pt for m in good], dtype=np.float32),
        sb.shape,
        gb.shape,
    )
    H, inl = cv2.findHomography(
        pa, pb, cv2.RANSAC, ransac_px / min(fa, fb), maxIters=5000, confidence=0.999
    )
    if H is None or inl is None or not np.isfinite(H).all():
        return _empty("no_homography", (wa, ha), (wb, hb), timing, matches=len(good))
    keep = np.asarray(inl).ravel() > 0
    n_inl = int(keep.sum())
    if n_inl < MIN_INLIERS:
        return _empty(
            "too_few_inliers",
            (wa, ha),
            (wb, hb),
            timing,
            matches=len(good),
            inliers=n_inl,
        )
    H = H / H[2, 2]
    proj = cv2.perspectiveTransform(pa[keep][None].astype(np.float64), H)[0]
    med = float(np.median(np.linalg.norm(proj - pb[keep], axis=1)))

    quality: list[str] = []
    dec = decompose_plane_homography(
        H, Ka, Kb, pa[keep].astype(float), pb[keep].astype(float)
    )
    if dec is None:
        # no decomposable translation: report the closest pure rotation of K_b^-1 H K_a
        M = np.linalg.inv(Kb) @ H @ Ka
        U, _, Vt = np.linalg.svd(
            M / np.cbrt(np.linalg.det(M)) if abs(np.linalg.det(M)) > 1e-12 else M
        )
        R = U @ Vt
        if np.linalg.det(R) < 0:
            R = U @ np.diag([1, 1, -1]) @ Vt
        t_over_d, normal, n_sol = np.zeros(3), None, 0
        quality.append("no_decomposable_translation")
    else:
        R, t_over_d, normal, n_sol = dec
        U, _, Vt = np.linalg.svd(
            R
        )  # OpenCV leaves R slightly non-orthonormal for near-degenerate homographies
        R = U @ Vt
    rot = _rot_angle_deg(R)
    ratio_t = float(np.linalg.norm(t_over_d))
    # A zoom (or a dolly along the optical axis) looks like t || plane normal: it adds no parallax for the plane, so the
    # translation that counts is the part perpendicular to the normal.
    lateral = ratio_t
    if normal is not None:
        lateral = float(np.linalg.norm(t_over_d - float(t_over_d @ normal) * normal))
        if (
            ratio_t >= BASELINE_MIN_TRANSLATION_RATIO
            and lateral < BASELINE_MIN_TRANSLATION_RATIO
        ):
            quality.append("zoom_or_dolly")
    baseline_ok = bool(
        rot >= BASELINE_MIN_ROT_DEG or lateral >= BASELINE_MIN_TRANSLATION_RATIO
    )
    if not baseline_ok:
        quality.append("same_viewpoint")
    if n_inl < 50:
        quality.append("few_inliers")

    warped: list[Array] = []
    rep: list[dict[str, Any]] = []
    mb_boxes = [_bbox(b) for b in mb]
    t0 = time.perf_counter()
    for i, m in enumerate(ma):
        w = (
            cv2.warpPerspective(
                m.astype(np.uint8), H, (wb, hb), flags=cv2.INTER_NEAREST
            )
            > 0
        )
        warped.append(w)
        entry: dict[str, Any] = {
            "index": i,
            "area_a_px": int(m.sum()),
            "warped_area_px": int(w.sum()),
            "inside_b_frac": _inside_fraction(m, H, (wb, hb)),
            "best_b_index": None,
            "iou": None,
            "unstable": None,
        }
        if mb:
            ious = _ious(w, _bbox(w), mb, mb_boxes)
            j = int(np.argmax(ious))
            entry.update(
                best_b_index=j, iou=ious[j], unstable=bool(ious[j] < flag_below)
            )
        rep.append(entry)
    timing["masks_s"] = time.perf_counter() - t0
    timing["total_s"] = time.perf_counter() - t_all
    return {
        "ok": True,
        "reason": None,
        "matches": len(good),
        "inliers": n_inl,
        "median_reproj_px": med,
        "H_a_to_b": H,
        "rel_R": R,
        "rel_rot_deg": rot,
        "t_over_depth": t_over_d,
        "translation_ratio": ratio_t,
        "lateral_translation_ratio": lateral,
        "t_mm": None if plane_depth_mm is None else t_over_d * float(plane_depth_mm),
        "plane_normal": normal,
        "n_solutions": n_sol,
        "baseline_ok": baseline_ok,
        "quality": quality,
        "K_a": Ka,
        "K_b": Kb,
        "warped_masks": warped,
        "repeatability": rep,
        "image_size_a": [wa, ha],
        "image_size_b": [wb, hb],
        "timing_s": timing,
    }


def _bbox(mask: Array) -> tuple[int, int, int, int, int]:
    """(y0, y1, x0, x1, area) of a mask; an empty mask has area 0."""
    ys, xs = np.nonzero(mask)
    if xs.size == 0:
        return 0, 0, 0, 0, 0
    return (
        int(ys.min()),
        int(ys.max()) + 1,
        int(xs.min()),
        int(xs.max()) + 1,
        int(xs.size),
    )


def _ious(
    w: Array,
    wbox: tuple[int, int, int, int, int],
    masks: list[Array],
    boxes: list[tuple[int, int, int, int, int]],
) -> list[float]:
    """IoU of `w` with every mask, intersecting only inside the bounding-box overlap (no full-frame work per pair)."""
    out: list[float] = []
    for b, bb in zip(masks, boxes, strict=True):
        y0, y1, x0, x1 = (
            max(wbox[0], bb[0]),
            min(wbox[1], bb[1]),
            max(wbox[2], bb[2]),
            min(wbox[3], bb[3]),
        )
        inter = (
            int((w[y0:y1, x0:x1] & b[y0:y1, x0:x1]).sum()) if y1 > y0 and x1 > x0 else 0
        )
        out.append(inter / max(wbox[4] + bb[4] - inter, 1))
    return out


def _inside_fraction(mask: Array, H: Array, size_b: tuple[int, int]) -> float:
    """Fraction of the mask's pixels that land inside photo B's frame (a mask warped half out of frame cannot score a
    fair IoU)."""
    ys, xs = np.nonzero(mask)
    if xs.size == 0:
        return 0.0
    step = max(1, xs.size // 5000)
    p = (
        np.column_stack([xs[::step], ys[::step], np.ones(len(xs[::step]))]).astype(
            float
        )
        @ H.T
    )
    with np.errstate(divide="ignore", invalid="ignore"):
        q = p[:, :2] / p[:, 2:3]
    ok = (
        np.isfinite(q).all(axis=1)
        & (q[:, 0] >= 0)
        & (q[:, 0] <= size_b[0] - 1)
        & (q[:, 1] >= 0)
        & (q[:, 1] <= size_b[1] - 1)
    )
    return float(ok.mean())
