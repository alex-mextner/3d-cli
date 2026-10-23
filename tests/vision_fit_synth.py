"""Synthetic ground-truth scenes for the threed_vision fit tests and the benchmark (numpy + OpenCV only).

Camera = the calibrated HSW phone (f 720.8 px, 1152x1536, refit/photo_calibration.json), object ~650 mm away, cameras on a ring
around it. Masks are rendered by the independent hard rasteriser from a high-resolution GT mesh (16-gon sections, 40 stations) --
NOT from the fitted template -- so a fit has to cope with discretisation mismatch like on real photos.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any

import numpy as np

SRC = Path(__file__).resolve().parents[1] / "packages" / "threed-vision" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from threed_vision.loft import build_faces, loft_vertices, rodrigues  # noqa: E402
from threed_vision.render import hard_mask  # noqa: E402

KF = np.array([[720.8221, 0, 576.0], [0, 720.8221, 768.0], [0, 0, 1.0]])
SIZE = (1152, 1536)
GT_K = 40
GT_RING = 16


def recorded_mask(name: str) -> np.ndarray:
    """A bbox-cropped real SAM2 carpet silhouette (bool), recorded from the hsw-editor spike pipeline (tests/data/vision/carpet_masks.npz)."""
    with np.load(Path(__file__).resolve().parent / "data" / "vision" / "carpet_masks.npz") as z:
        return np.asarray(z[name], bool)


def prof(stops: list[tuple[float, float]], K: int = GT_K) -> np.ndarray:
    t = np.linspace(0, 1, K)
    return np.interp(t, [s[0] for s in stops], [s[1] for s in stops])


GT: dict[str, dict[str, Any]] = {
    "screwdriver": {
        "L": 190.0,
        "w": [(0, 8), (0.05, 11), (0.3, 11), (0.38, 6), (0.4, 2.6), (0.95, 2.6), (1, 1.6)],
        "aspect": 1.0,
        "p": 2.0,
        "template": "lathe",
    },
    "pliers_flat": {
        "L": 170.0,
        "w": [(0, 7), (0.35, 9), (0.5, 12), (0.6, 12), (0.75, 7), (1, 3)],
        "aspect": 0.22,
        "p": 4.0,
        "template": "flat",
    },
    "box_elongated": {
        "L": 240.0, "w": [(0, 15), (1, 15)], "aspect": 0.5, "p": 8.0, "template": "box"
    },
    "box_near_square": {
        "L": 146.0, "w": [(0, 44), (1, 44)], "aspect": 0.5, "p": 8.0, "template": "box"
    },
}


def lookat(
    eye: np.ndarray, target: np.ndarray | None = None, up: np.ndarray | None = None
) -> tuple[np.ndarray, np.ndarray]:
    target = np.zeros(3) if target is None else target
    up = np.array([0.0, 0.0, 1.0]) if up is None else up
    z = target - eye
    z = z / np.linalg.norm(z)
    x = np.cross(z, up)
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    R = np.stack([x, y, z])
    return R, -R @ eye


def cam_ring(
    az_deg: float, el_deg: float = 20.0, dist: float = 650.0
) -> tuple[np.ndarray, np.ndarray]:
    a, e = math.radians(az_deg), math.radians(el_deg)
    return lookat(
        dist
        * np.array([math.sin(a) * math.cos(e), -math.cos(a) * math.cos(e), math.sin(e)])
    )


def gt_mesh(
    spec: dict[str, Any], rotvec: np.ndarray, t: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    w = prof(spec["w"])
    V = loft_vertices(spec["L"], np.zeros(GT_K), w, spec["aspect"], spec["p"], GT_RING)
    R = rodrigues(rotvec)
    return V @ R.T + t, build_faces(GT_K, GT_RING), R


def scene(
    name: str,
    seed: int = 0,
    azimuths: tuple[float, ...] = (0.0, 55.0, -50.0),
    elevations: tuple[float, ...] = (20.0, 20.0, 20.0),
    heldout: tuple[float, float] | None = (100.0, 35.0),
) -> dict[str, Any]:
    """Random pose of GT shape `name` seen by cameras on a ring; returns views (training), heldout view, GT numbers."""
    spec = GT[name]
    rng = np.random.RandomState(1000 + seed)
    rotvec = rng.randn(3) * 0.8
    t = rng.randn(3) * 8
    V, F, R = gt_mesh(spec, rotvec, t)
    cams = [cam_ring(a, e) for a, e in zip(azimuths, elevations, strict=True)]
    views = [
        {
            "mask": hard_mask(V, F, KF, Rc, tc, SIZE),
            "K": KF,
            "R": Rc,
            "t": tc,
            "name": f"az{a:g}",
        }
        for (Rc, tc), a in zip(cams, azimuths, strict=True)
    ]
    out: dict[str, Any] = {
        "spec": spec,
        "views": views,
        "V": V,
        "F": F,
        "R": R,
        "t": t,
        "depth": float(np.linalg.norm(t - (-cams[0][0].T @ cams[0][1]))),
    }
    if heldout:
        Rc, tc = cam_ring(*heldout)
        out["heldout"] = [
            {
                "mask": hard_mask(V, F, KF, Rc, tc, SIZE),
                "K": KF,
                "R": Rc,
                "t": tc,
                "name": "heldout",
            }
        ]
    return out


def topdown_scene(
    name: str, angle_deg: float = 30.0, depth: float = 650.0
) -> dict[str, Any]:
    """Object lying flat on a plane parallel to the image (identity camera), rotated in-plane by `angle_deg`."""
    spec = GT[name]
    rz = math.radians(angle_deg)
    V, F, R = gt_mesh(spec, np.array([0.0, 0.0, rz]), np.array([20.0, -15.0, depth]))
    Rc, tc = np.eye(3), np.zeros(3)
    view = {
        "mask": hard_mask(V, F, KF, Rc, tc, SIZE),
        "K": KF,
        "R": Rc,
        "t": tc,
        "name": "top",
    }
    return {"spec": spec, "views": [view], "V": V, "F": F, "R": R, "depth": depth}
