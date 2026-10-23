"""Independent check of a fit: render it with the hard cv2 rasteriser into other views and compare to their masks.

`check(fit, views)` - views = extra photos (`[{"mask","K","R","t"}]`, world -> camera in the fit's world frame):
  per view: IoU of the rendered fit vs the mask, `flag` when below `flag_below` (default 0.75); `min_iou`;
  `status`: ok | flagged | unverified; `needs_views`: suggested camera directions to shoot next.

Single-photo mode (`views=[]`): there is NO second view, so the fit cannot be validated against reality. What runs instead is a
SANITY check on the fitted model: it is rendered from canonical synthetic cameras (top, side, end, three-quarter) and tested for
degenerate silhouettes, a thickness that is plausible for its template, and a top-view fill ratio (area vs length x width) that
real tools respect. This catches broken or absurd fits; it cannot prove the hidden 3-D shape (thickness, roll, concavities). The
status is then `unverified`, never `ok`, and `needs_views` says which photo would settle it.

Angles in `needs_views` / `views[*]` are in the OBJECT frame: azimuth in the length(X)-width(Y) plane from +X toward +Y, elevation
from that plane toward +Z (thickness axis); elevation 90 = straight down the thickness axis (a top-down photo of a flat object).
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .errors import BadInput
from .fit_init import FLAG_BELOW_IOU, camera_center, validate_views
from .loft import EXPORT_RING, RING_M, TEMPLATE_ASPECT, TEMPLATE_P, dims_mm, object_view_angles
from .render import hard_mask, mesh_from_fit, overlay, project, view_iou

CANONICAL = (
    ("top", 0.0, 90.0),
    ("side", 90.0, 0.0),
    ("end", 0.0, 0.0),
    ("three_quarter", 45.0, 35.0),
)
SIZE = 256
DEGENERATE_AREA_RATIO = 3e-3  # smallest canonical silhouette vs the largest
ASPECT_BAND = 3.5  # plausible thickness/width: prior/3.5 .. prior*3.5
FILL_RANGE = (
    0.2,
    1.1,
)  # top-view area / (L x W); a box is ~1, a tapered lathe ~0.5-0.8 (1.1: raster boundary pixels)
FAR_FROM_SEEN_DEG = 25.0


def validate_fit(fit: Any) -> None:
    """BadInput unless `fit` is a well-formed threed_vision fit (the dict of `fit_auto` / `fit.json`): known template, positive finite
    L / widths / aspect, 3x3 pose, 3-vector centre. Never lets a hand-edited or truncated fit reach the renderer."""
    try:
        tmpl, p, pose = fit["template"], fit["params"], fit["pose"]
        name, pexp = tmpl["name"], float(tmpl["p"])
        w = np.asarray(p["w_mm"], np.float64)
        c = np.asarray(p["c_mm"], np.float64)
        R0 = np.asarray(pose["R0"], np.float64)
        t_mm = np.asarray(pose["t_mm"], np.float64)
        L, aspect = float(p["L_mm"]), float(p["aspect"])
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise BadInput(
            f"not a threed_vision fit (missing or malformed {exc!r}); expected the dict returned by fit_auto / fit.json"
        ) from exc
    if name not in TEMPLATE_P:
        raise BadInput(f"fit has an unknown template {name!r}", remediation=f"templates are {sorted(TEMPLATE_P)}")
    if w.ndim != 1 or w.size < 3 or c.shape != w.shape or R0.shape != (3, 3) or t_mm.shape != (3,):
        raise BadInput("fit is malformed: need >= 3 stations (w_mm, c_mm of equal length), a 3x3 pose.R0 and a 3-vector pose.t_mm")
    if not all(np.isfinite(a).all() for a in (w, c, R0, t_mm, [L, aspect, pexp])):
        raise BadInput("fit has non-finite parameters (null / NaN / inf)")
    ring = tmpl.get("ring", RING_M)
    if not (isinstance(ring, int) and not isinstance(ring, bool) and 4 <= ring <= 256):
        raise BadInput(f"fit has an unusable template.ring {ring!r} (an integer 4..256)")
    if not (L > 0 and aspect > 0 and pexp > 0 and (w > 0).all()):
        raise BadInput("fit is degenerate: L_mm, aspect and every w_mm must be positive")


def _dims(fit: dict[str, Any]) -> dict[str, float]:
    p = fit["params"]
    return dims_mm(p["L_mm"], np.asarray(p["w_mm"], np.float64), p["aspect"])


def _lookat(
    eye: np.ndarray, target: np.ndarray, up: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    z = target - eye
    z = z / np.linalg.norm(z)
    x = np.cross(z, up)
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    R = np.stack([x, y, z])
    return R, -R @ eye


def canonical_camera(
    fit: dict[str, Any], azimuth_deg: float, elevation_deg: float, size: int = SIZE
) -> dict[str, Any]:
    """A synthetic pinhole camera looking at the fitted object from (azimuth, elevation) in the object frame (weak perspective, object fills ~70 %)."""
    d = _dims(fit)
    r = 0.5 * math.sqrt(
        d["length_mm"] ** 2 + d["max_width_mm"] ** 2 + d["thickness_mm"] ** 2
    )
    dist = 8.0 * r
    f = 0.35 * size * dist / max(r, 1e-6)
    az, el = math.radians(azimuth_deg), math.radians(elevation_deg)
    do = np.array(
        [math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el)]
    )
    R0 = np.asarray(fit["pose"]["R0"], np.float64)
    c = np.asarray(fit["pose"]["t_mm"], np.float64)
    up = R0 @ (
        np.array([1.0, 0.0, 0.0]) if abs(do[2]) > 0.95 else np.array([0.0, 0.0, 1.0])
    )
    R, t = _lookat(c + dist * (R0 @ do), c, up)
    K = np.array([[f, 0, size / 2], [0, f, size / 2], [0, 0, 1.0]])
    return {"K": K, "R": R, "t": t, "mm_per_px": dist / f, "size": size}


def _canonical_report(fit: dict[str, Any]) -> list[dict[str, Any]]:
    V, F = mesh_from_fit(fit, ring=EXPORT_RING)
    out = []
    for name, az, el in CANONICAL:
        cam = canonical_camera(fit, az, el)
        m = hard_mask(V, F, cam["K"], cam["R"], cam["t"], (cam["size"], cam["size"]))
        ys, xs = np.nonzero(m)
        s = cam["mm_per_px"]
        out.append(
            {
                "name": name,
                "azimuth_deg": az,
                "elevation_deg": el,
                "area_mm2": float(m.sum()) * s * s,
                "extent_mm": [
                    float(xs.max() - xs.min() + 1) * s,
                    float(ys.max() - ys.min() + 1) * s,
                ]
                if xs.size
                else [0.0, 0.0],
            }
        )
    return out


def sanity(fit: dict[str, Any]) -> dict[str, Any]:
    """Plausibility of the fitted model alone (no photo needed). See the module docstring for what this can and cannot show."""
    validate_fit(fit)
    return _sanity(fit)


def _sanity(fit: dict[str, Any]) -> dict[str, Any]:
    canon = _canonical_report(fit)
    areas = np.array([c["area_mm2"] for c in canon])
    checks = []
    ratio = float(areas.min() / max(areas.max(), 1e-12))
    checks.append(
        {
            "name": "canonical_views_nondegenerate",
            "ok": bool(np.isfinite(areas).all() and ratio >= DEGENERATE_AREA_RATIO),
            "value": ratio,
            "detail": f"smallest canonical silhouette is {ratio:.4f} of the largest (flag below {DEGENERATE_AREA_RATIO})",
        }
    )
    name, aspect = fit["template"]["name"], float(fit["params"]["aspect"])
    prior = TEMPLATE_ASPECT[name]
    if fit.get("fixed_dims") and "thickness_mm" in fit["fixed_dims"]:
        checks.append(
            {
                "name": "thickness_prior",
                "ok": True,
                "value": aspect,
                "detail": "thickness was set by the user; not judged",
            }
        )
    else:
        lo, hi = (
            (prior / ASPECT_BAND, prior * ASPECT_BAND)
            if name != "lathe"
            else (0.99, 1.01)
        )
        checks.append(
            {
                "name": "thickness_prior",
                "ok": bool(lo <= aspect <= hi),
                "value": aspect,
                "detail": f"thickness/width {aspect:.2f}; plausible for a {name} template: {lo:.2f}..{hi:.2f}",
            }
        )
    d = _dims(fit)
    fill = canon[0]["area_mm2"] / max(d["length_mm"] * d["max_width_mm"], 1e-12)
    checks.append(
        {
            "name": "fill_ratio",
            "ok": bool(FILL_RANGE[0] <= fill <= FILL_RANGE[1]),
            "value": float(fill),
            "detail": f"top-view silhouette area / (length x max width) = {fill:.2f}; real tools fall in {FILL_RANGE[0]}..{FILL_RANGE[1]} (a sliver means length/width/profile disagree)",
        }
    )
    return {"ok": all(c["ok"] for c in checks), "checks": checks, "canonical": canon}


def _dir(az_deg: float, el_deg: float) -> np.ndarray:
    az, el = math.radians(az_deg), math.radians(el_deg)
    return np.array(
        [math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el)]
    )


def _angle_deg(a: np.ndarray, b: np.ndarray, mirror: bool) -> float:
    c = float(np.clip(a @ b, -1.0, 1.0))
    if (
        mirror
    ):  # orthographic silhouettes of d and -d are mirror images: same information
        c = abs(c)
    return math.degrees(math.acos(c))


def suggest_views(
    fit: dict[str, Any], seen: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Ordered, de-duplicated suggestions {azimuth_deg, elevation_deg, direction_object, reason, priority}.

    1. flat/box templates never seen from above (|elevation| < 60 in every view): a top-down view (shows the true outline + width).
    2. the canonical direction (26-neighbourhood of the object frame) farthest from every seen direction (silhouette information
       grows with the angle to what was already seen; d and -d count as the same).
    3. the opposite side of the best-fitting view: lowest priority - for an orthographic silhouette it is a mirror image, so it only
       helps with perspective, self-occlusion and as the easiest thing to shoot (flip the object over).
    """
    dirs = [_dir(s["azimuth_deg"], s["elevation_deg"]) for s in seen]
    out: list[dict[str, Any]] = []

    def far_from_seen(d: np.ndarray, mirror: bool) -> bool:
        return all(_angle_deg(d, s, mirror) >= FAR_FROM_SEEN_DEG for s in dirs)

    def add(az: float, el: float, reason: str) -> None:
        d = _dir(az, el)
        if any(
            _angle_deg(d, _dir(o["azimuth_deg"], o["elevation_deg"]), False) < 10
            for o in out
        ):
            return
        out.append(
            {
                "azimuth_deg": az,
                "elevation_deg": el,
                "direction_object": d.tolist(),
                "reason": reason,
                "priority": len(out) + 1,
            }
        )

    if (
        fit["template"]["name"] in ("flat", "box")
        and max((abs(s["elevation_deg"]) for s in seen), default=0.0) < 60
    ):
        add(
            0.0,
            90.0,
            "top-down view: the thickness axis is unobserved; flat/boxy shapes are 3-D ambiguous from side views",
        )
    grid = [(a, e) for e in (-45.0, 0.0, 45.0) for a in range(-135, 181, 45)] + [
        (0.0, 90.0),
        (0.0, -90.0),
    ]
    scored = sorted(
        (
            (min((_angle_deg(_dir(a, e), s, True) for s in dirs), default=90.0), a, e)
            for a, e in grid
        ),
        key=lambda x: (-x[0], x[1], x[2]),
    )
    if scored and scored[0][0] >= FAR_FROM_SEEN_DEG:
        add(
            float(scored[0][1]),
            float(scored[0][2]),
            f"largest unseen direction ({scored[0][0]:.0f} deg from every view so far)",
        )
    best = max(
        (s for s in seen if s.get("iou") is not None),
        key=lambda s: s["iou"],
        default=None,
    )
    if best is not None:
        az, el = best["azimuth_deg"] + 180.0, -best["elevation_deg"]
        az = (az + 180.0) % 360.0 - 180.0
        if far_from_seen(_dir(az, el), False):
            add(
                az,
                el,
                "opposite side of the best-fitting view (mirror silhouette in orthographic projection; checks the unseen side)",
            )
    return out[:3]


def check(
    fit: dict[str, Any],
    views: list[dict[str, Any]] | None = None,
    flag_below: float = FLAG_BELOW_IOU,
) -> dict[str, Any]:
    """Score `fit` against extra views with the independent hard rasteriser; see the module docstring (also single-photo mode)."""
    validate_fit(fit)
    if not (isinstance(flag_below, (int, float)) and 0.0 < flag_below <= 1.0):
        raise BadInput(f"flag_below must be in (0, 1], got {flag_below!r}")
    vs = validate_views(views) if views else []
    V, F = mesh_from_fit(fit)
    R0, t0 = (
        np.asarray(fit["pose"]["R0"], np.float64),
        np.asarray(fit["pose"]["t_mm"], np.float64),
    )
    rows = []
    for v in vs:
        iou = view_iou(v["mask"], V, F, v["K"], v["R"], v["t"])
        rows.append(
            {
                "name": v.get("name"),
                "iou": iou,
                "flag": bool(iou < flag_below),
                **object_view_angles(R0, t0, camera_center(v)),
            }
        )
    san = _sanity(fit)
    # with real extra views their IoU decides (the sanity block is still reported); alone, the sanity checks decide
    flagged = any(r["flag"] for r in rows) if rows else not san["ok"]
    status = "flagged" if flagged else ("ok" if rows else "unverified")
    seen = [
        {
            "azimuth_deg": s["azimuth_deg"],
            "elevation_deg": s["elevation_deg"],
            "iou": s.get("iou"),
        }
        for s in fit.get("views", [])
        if isinstance(s, dict) and "azimuth_deg" in s and "elevation_deg" in s
    ]
    seen += [
        {
            "azimuth_deg": r["azimuth_deg"],
            "elevation_deg": r["elevation_deg"],
            "iou": r["iou"],
        }
        for r in rows
    ]
    return {
        "mode": "heldout" if rows else "single_photo",
        "views": rows,
        "min_iou": float(min(r["iou"] for r in rows)) if rows else None,
        "flag_below": float(flag_below),
        "status": status,
        "needs_views": suggest_views(fit, seen) if status != "ok" else [],
        "sanity": san,
    }


def render_overlays(
    fit: dict[str, Any], views: list[dict[str, Any]], pad: int = 16
) -> list[np.ndarray]:
    """One RGB review image per view (cropped to the mask/model bbox + pad): mask contour green, rendered fit red."""
    validate_fit(fit)
    V, F = mesh_from_fit(fit)
    out = []
    for v in validate_views(views):
        H, W = v["mask"].shape
        uv = project(V, v["K"], v["R"], v["t"])
        ys, xs = np.nonzero(v["mask"])
        x0 = int(max(min(xs.min(), np.floor(uv[:, 0].min())) - pad, 0))
        y0 = int(max(min(ys.min(), np.floor(uv[:, 1].min())) - pad, 0))
        x1 = int(min(max(xs.max(), np.ceil(uv[:, 0].max())) + pad, W))
        y1 = int(min(max(ys.max(), np.ceil(uv[:, 1].max())) + pad, H))
        model = hard_mask(V, F, v["K"], v["R"], v["t"], (W, H), roi=(x0, y0, x1, y1))
        out.append(overlay(None, model, v["mask"][y0:y1, x0:x1]))
    return out
