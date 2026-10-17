"""calib_rectify.py — top photo of a calibration plate → straight, mm-scaled bed image.

WHAT: finds the plate's ArUco fiducials (plan §8.3.4: DICT_4X4_50, ids 0–3, 10 mm, one layer;
  positions from the plate manifest `calib-plate/1` or --fiducials), fits the homography from
  image pixels to bed millimetres (>= 3 markers = 12 corners, least squares), warps the photo
  to a rectified image at --px-per-mm (+Y of the bed points up), and draws the detections.
  With --scale-ref, an external reference card (markers at caliper-measured true positions)
  is measured in bed millimetres and gives the true-mm-per-bed-mm factor per axis — the
  printer's own fiducials carry its scale error, so they cannot reveal it themselves.
WHY: issue #342 (photo mode) feeding the vision readout #273.
RUN BY: `3d calibrate photo` and `3d calibrate rectify` through cli.pyrun (needs OpenCV ≥ 4.7
  with the aruco module: opencv-python-headless, `uv sync --extra vision`).

Marker convention: a fiducial with id i, centre (cx, cy) and size s lies axis-aligned with its
  top edge towards bed +Y, so its ArUco corners TL, TR, BR, BL sit at (cx−s/2, cy+s/2),
  (cx+s/2, cy+s/2), (cx+s/2, cy−s/2), (cx−s/2, cy−s/2). `rotation_deg` (counter-clockwise,
  multiples of 90) rotates that for a marker printed turned.

Output (stdout JSON, `calib-rectify/1`): fiducials_found, ids, missing, homography (image px →
  bed mm), residual_mm {rms, max}, px_per_mm, area_mm, rectified, annotated, scale (or null).
Exit 0 = rectified; 1 = fewer than 3 fiducials or a scale reference that was asked for but not
  seen (JSON still printed with "status": "failed" and the reason); 2 = bad arguments.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import cv2
import numpy as np

MIN_FIDUCIALS = 3


def parse_fiducials(text: str, size: float) -> list[dict[str, Any]]:
    """`0:12,12;1:208,12` → [{"id": 0, "center_mm": [12, 12], "size_mm": size}, ...]."""
    out = []
    for part in filter(None, (p.strip() for p in text.split(";"))):
        ident, _, xy = part.partition(":")
        x, _, y = xy.partition(",")
        out.append(
            {"id": int(ident), "center_mm": [float(x), float(y)], "size_mm": size}
        )
    if not out:
        raise ValueError("no fiducials given")
    return out


def marker_corners_mm(
    center: tuple[float, float], size: float, rotation_deg: float = 0.0
) -> np.ndarray:
    cx, cy = center
    h = size / 2.0
    pts = np.array([[-h, h], [h, h], [h, -h], [-h, -h]], dtype=np.float64)
    if rotation_deg:
        a = math.radians(rotation_deg)
        rot = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
        pts = pts @ rot.T
    return pts + np.array([cx, cy])


def dictionary(name: str) -> Any:
    attr = name if name.startswith("DICT_") else f"DICT_{name}"
    if not hasattr(cv2.aruco, attr):
        raise ValueError(f"unknown ArUco dictionary {name!r}")
    return cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, attr))


def detect(grey: np.ndarray, dict_name: str) -> dict[int, np.ndarray]:
    """{id: 4×2 sub-pixel corners (TL, TR, BR, BL in the marker's frame)}."""
    params = cv2.aruco.DetectorParameters()
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    params.cornerRefinementWinSize = 3
    detector = cv2.aruco.ArucoDetector(dictionary(dict_name), params)
    corners, ids, _ = detector.detectMarkers(grey)
    found: dict[int, np.ndarray] = {}
    if ids is None:
        return found
    for c, i in zip(corners, ids.flatten(), strict=True):
        found[int(i)] = c.reshape(4, 2).astype(np.float64)
    return found


def to_grey8(image: np.ndarray) -> np.ndarray:
    img = image
    if img.dtype == np.uint16:
        img = (img / 257.0).round().astype(np.uint8)
    if img.ndim == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return img


def apply_h(h: np.ndarray, pts: np.ndarray) -> np.ndarray:
    pts = np.asarray(pts, dtype=np.float64).reshape(-1, 2)
    hom = np.hstack([pts, np.ones((len(pts), 1))]) @ h.T
    return hom[:, :2] / hom[:, 2:3]


def fit_scale(true_mm: np.ndarray, measured_mm: np.ndarray) -> dict[str, Any]:
    """Map the reference card's true marker centres onto their measured bed-mm centres.

    measured = D·R·true + t with D = diag(1/ex, 1/ey): a printer that moves ex true mm per
    commanded mm puts its fiducials ex too far apart, so true lengths look 1/ex as long in bed
    mm. Returns ex, ey (true mm per bed mm along bed X/Y) and the fit residual."""
    n = len(true_mm)
    tc, mc = true_mm.mean(axis=0), measured_mm.mean(axis=0)
    t0, m0 = true_mm - tc, measured_mm - mc
    if n >= 3:
        a, *_ = np.linalg.lstsq(t0, m0, rcond=None)  # m0 ≈ t0 @ a  →  A = aᵀ
        amat = a.T
        aat = amat @ amat.T
        ex, ey = 1.0 / math.sqrt(aat[0, 0]), 1.0 / math.sqrt(aat[1, 1])
        pred = t0 @ a + mc
        model = "affine"
    else:
        s = float(np.linalg.norm(m0[1] - m0[0]) / np.linalg.norm(t0[1] - t0[0]))
        ex = ey = 1.0 / s
        d = m0[1] - m0[0]
        ang = math.atan2(d[1], d[0]) - math.atan2(
            t0[1][1] - t0[0][1], t0[1][0] - t0[0][0]
        )
        rot = np.array(
            [[math.cos(ang), -math.sin(ang)], [math.sin(ang), math.cos(ang)]]
        )
        pred = (t0 @ rot.T) * s + mc
        model = "isotropic (2 markers)"
    resid = np.linalg.norm(pred - measured_mm, axis=1)
    return {
        "ex": ex,
        "ey": ey,
        "model": model,
        "residual_mm": {
            "rms": float(np.sqrt((resid**2).mean())),
            "max": float(resid.max()),
        },
    }


def scale_reference(
    ref: dict[str, Any],
    found: dict[int, np.ndarray],
    grey: np.ndarray,
    h: np.ndarray,
    camera_height_mm: float | None,
    plate_dict: str = "DICT_4X4_50",
) -> dict[str, Any]:
    markers = {int(k): v for k, v in ref["markers"].items()}
    dict_name = ref.get("dictionary", "DICT_4X4_50")
    seen = found if dict_name == plate_dict else detect(grey, dict_name)
    ids = sorted(i for i in markers if i in seen)
    out: dict[str, Any] = {
        "name": ref.get("name"),
        "markers_found": ids,
        "markers_missing": sorted(set(markers) - set(ids)),
    }
    if len(ids) < 2:
        out.update(
            status="failed",
            reason=f"scale reference: {len(ids)} of {len(markers)} markers seen (need 2)",
        )
        return out
    true_mm = np.array([markers[i] for i in ids], dtype=np.float64)
    measured = np.array([apply_h(h, seen[i]).mean(axis=0) for i in ids])
    spread = np.linalg.svd(true_mm - true_mm.mean(axis=0), compute_uv=False)
    if spread[0] < 1.0 or (len(ids) >= 3 and spread[1] < 1.0):  # coincident, or collinear for the affine fit
        out.update(status="failed", reason="scale reference: the seen markers are coincident or collinear")
        return out
    try:
        fit = fit_scale(true_mm, measured)
    except (ValueError, ZeroDivisionError, np.linalg.LinAlgError) as exc:
        out.update(status="failed", reason=f"scale reference: degenerate fit on the measured markers ({exc})")
        return out
    if not all(math.isfinite(fit[k]) and fit[k] > 0 for k in ("ex", "ey")):
        out.update(status="failed", reason="scale reference: degenerate fit on the measured markers")
        return out
    thickness = float(ref.get("thickness_mm", 0.0))
    magnify = 1.0
    corrected = thickness == 0.0
    if camera_height_mm is not None and not (math.isfinite(camera_height_mm) and camera_height_mm > 0):
        out.update(status="failed", reason=f"camera height {camera_height_mm} mm is not a positive number")
        return out
    if thickness and camera_height_mm:
        if camera_height_mm <= thickness:
            out.update(status="failed", reason=f"camera height {camera_height_mm} mm must exceed the card "
                       f"thickness {thickness} mm")
            return out
        magnify = camera_height_mm / (camera_height_mm - thickness)
        corrected = True
    ex, ey = fit["ex"] * magnify, fit["ey"] * magnify
    out.update(
        status="ok",
        true_mm_per_bed_mm={"x": ex, "y": ey},
        printer_scale_error_pct={"x": (ex - 1) * 100, "y": (ey - 1) * 100},
        fit_model=fit["model"],
        fit_residual_mm=fit["residual_mm"],
        thickness_mm=thickness,
        parallax_corrected=corrected,
    )
    if not corrected:
        out["warning"] = (
            f"card is {thickness} mm thick and camera_height_mm is unknown: the scale "
            f"is uncorrected by ~{thickness / 300 * 100:.2f}% at 300 mm"
        )
    return out


def rectify(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    image = cv2.imread(args.image, cv2.IMREAD_UNCHANGED)
    if image is None:
        return 2, {"status": "failed", "reason": f"cannot read {args.image}"}
    if args.manifest:
        try:
            manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
            fiducials = manifest.get("fiducials") or []
            dict_name = str(manifest.get("fiducial_dictionary", "DICT_4X4_50"))
            if not isinstance(fiducials, list) or not all(
                    isinstance(f, dict) and "id" in f and "center_mm" in f for f in fiducials):
                raise ValueError("fiducials must be a list of {id, center_mm, ...}")
        except (OSError, ValueError, AttributeError) as exc:
            return 2, {"status": "failed", "reason": f"bad manifest {args.manifest}: {exc}"}
    else:
        fiducials = parse_fiducials(args.fiducials, args.fiducial_size)
        dict_name = args.dictionary
    grey = to_grey8(image)
    found = detect(grey, dict_name)
    wanted = {int(f["id"]): f for f in fiducials}
    ids = sorted(i for i in wanted if i in found)
    result: dict[str, Any] = {
        "schema": "calib-rectify/1",
        "image": args.image,
        "image_size": [int(image.shape[1]), int(image.shape[0])],
        "dictionary": dict_name,
        "fiducials_found": len(ids),
        "ids": ids,
        "missing": sorted(set(wanted) - set(ids)),
        "other_markers": sorted(set(found) - set(wanted)),
        "opencv": cv2.__version__,
    }
    if len(ids) < MIN_FIDUCIALS:
        result.update(
            status="failed",
            reason=f"{len(ids)} of {len(wanted)} fiducials found; need {MIN_FIDUCIALS}",
        )
        return 1, result
    src = np.vstack([found[i] for i in ids])
    dst = np.vstack(
        [
            marker_corners_mm(
                tuple(wanted[i]["center_mm"]),
                float(wanted[i]["size_mm"]),
                float(wanted[i].get("rotation_deg", 0)),
            )
            for i in ids
        ]
    )
    h, _ = cv2.findHomography(src, dst, 0)
    if h is None or not np.isfinite(h).all() or abs(np.linalg.det(h)) < 1e-12:
        result.update(status="failed", reason="the fiducials are degenerate (collinear or coincident): no homography")
        return 1, result
    resid = np.linalg.norm(apply_h(h, src) - dst, axis=1)
    centers = np.array([w["center_mm"] for w in wanted.values()], dtype=np.float64)
    if args.area:
        try:
            x0, y0, x1, y1 = (float(v) for v in args.area.split(","))
        except ValueError:
            return 2, {**result, "status": "failed", "reason": f"--area {args.area!r}: want X0,Y0,X1,Y1 in mm"}
    else:
        m = args.margin_mm
        x0, y0 = centers.min(axis=0) - m
        x1, y1 = centers.max(axis=0) + m
    ppm = args.px_per_mm
    span = ((x1 - x0) * ppm, (y1 - y0) * ppm)
    if not (math.isfinite(ppm) and ppm > 0 and all(math.isfinite(v) and 1 <= round(v) <= 20000 for v in span)):
        return 2, {**result, "status": "failed",
                   "reason": f"bad output geometry: area {x0:g},{y0:g},{x1:g},{y1:g} mm at {ppm:g} px/mm"}
    width, height = int(round(span[0])), int(round(span[1]))
    to_canvas = np.array(
        [[ppm, 0, -x0 * ppm], [0, -ppm, y1 * ppm], [0, 0, 1]], dtype=np.float64
    )
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rectified = cv2.warpPerspective(
        image, to_canvas @ h, (width, height), flags=cv2.INTER_CUBIC
    )
    rect_path = out_dir / "rectified.png"
    if not cv2.imwrite(str(rect_path), rectified):
        return 2, {**result, "status": "failed", "reason": f"cannot write {rect_path}"}
    annotated = cv2.cvtColor(grey, cv2.COLOR_GRAY2BGR)
    cv2.aruco.drawDetectedMarkers(
        annotated,
        [found[i].reshape(1, 4, 2).astype(np.float32) for i in ids],
        np.array(ids, dtype=np.int32),
    )
    expected_px = apply_h(
        np.linalg.inv(h), np.array([wanted[i]["center_mm"] for i in ids])
    )
    for pt in expected_px:
        cv2.circle(annotated, (int(round(pt[0])), int(round(pt[1]))), 6, (0, 0, 255), 2)
    ann_path = out_dir / "detections.png"
    if not cv2.imwrite(str(ann_path), annotated):
        return 2, {**result, "status": "failed", "reason": f"cannot write {ann_path}"}
    result.update(
        status="ok",
        homography=h.tolist(),
        residual_mm={
            "rms": float(np.sqrt((resid**2).mean())),
            "max": float(resid.max()),
        },
        px_per_mm=ppm,
        area_mm=[float(x0), float(y0), float(x1), float(y1)],
        rectified=str(rect_path),
        rectified_size=[width, height],
        annotated=str(ann_path),
        scale=None,
    )
    if args.scale_ref:
        try:
            ref = json.loads(Path(args.scale_ref).read_text(encoding="utf-8"))
            if not isinstance(ref, dict) or not isinstance(ref.get("markers"), dict):
                raise ValueError("want a scale-ref/1 object with a markers mapping")
            for v in ref["markers"].values():
                if not (isinstance(v, list) and len(v) == 2 and all(isinstance(c, (int, float)) for c in v)):
                    raise ValueError(f"marker position {v!r} is not [x, y] in mm")
        except (OSError, ValueError) as exc:
            return 2, {**result, "status": "failed", "reason": f"bad scale reference {args.scale_ref}: {exc}"}
        scale = scale_reference(ref, found, grey, h, args.camera_height_mm, dict_name)
        result["scale"] = scale
        if scale["status"] != "ok":
            result.update(status="failed", reason=scale["reason"])
            return 1, result
    return 0, result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="calib_rectify")
    ap.add_argument("image")
    ap.add_argument("--out-dir", required=True)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--manifest")
    src.add_argument("--fiducials")
    ap.add_argument("--fiducial-size", type=float, default=10.0)
    ap.add_argument("--dictionary", default="DICT_4X4_50")
    ap.add_argument("--px-per-mm", type=float, default=10.0)
    ap.add_argument("--area")
    ap.add_argument("--margin-mm", type=float, default=10.0)
    ap.add_argument("--scale-ref")
    ap.add_argument("--camera-height-mm", type=float)
    args = ap.parse_args(argv)
    try:
        code, result = rectify(args)
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as exc:
        code, result = 2, {"status": "failed", "reason": str(exc)}
    print(json.dumps(result, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
