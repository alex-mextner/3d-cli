"""Synthetic honeycomb panel renderer with a KNOWN camera, shared by the threed_vision calibrate/register tests.

The panel follows the HSW lattice (flat-top hexagonal holes, neighbour distance 23.6 mm, column pitch 20.4382 mm, odd
columns half a row up). Holes are darker than the walls and carry a one-sided shadow gradient; the whole image gets a
smooth illumination ramp, a slight blur and Gaussian noise, so it behaves like the real recessed-cell photo.
"""

from __future__ import annotations

import math

import numpy as np

PITCH = (20.4382, 23.6)


def rot_x(deg: float) -> np.ndarray:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], float)


def rot_y(deg: float) -> np.ndarray:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], float)


def rot_z(deg: float) -> np.ndarray:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], float)


def camera(
    f: float,
    size: tuple[int, int],
    tilt_x: float,
    tilt_y: float,
    roll: float,
    depth_mm: float,
    centre_mm: tuple[float, float],
) -> dict[str, np.ndarray]:
    """K, R, t (grid frame -> camera) looking at `centre_mm` of the panel from `depth_mm`, then tilted."""
    w, h = size
    K = np.array([[f, 0, w / 2], [0, f, h / 2], [0, 0, 1.0]])
    R = (
        rot_z(roll) @ rot_x(tilt_x) @ rot_y(tilt_y) @ np.diag([1.0, -1.0, -1.0])
    )  # grid y up / z out -> OpenCV
    c = np.array([centre_mm[0], centre_mm[1], 0.0])
    t = np.array([0.0, 0.0, depth_mm]) - R @ c
    return {"K": K, "R": R, "t": t}


def _nearest_hex_dist(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Hex-metric distance (max |projection on the 3 face normals|, mm) to the nearest lattice node."""
    pc, pr = PITCH
    m = x / pc
    n = y / pr - 0.5 * m
    m0, n0 = np.floor(m), np.floor(n)
    best = np.full(x.shape, np.inf)
    normals = [
        (math.cos(math.radians(a)), math.sin(math.radians(a)))
        for a in (30.0, 90.0, 150.0)
    ]
    for dm in (-1, 0, 1, 2):
        for dn in (-1, 0, 1, 2):
            mm_, nn_ = m0 + dm, n0 + dn
            cx, cy = mm_ * pc, (nn_ + 0.5 * mm_) * pr
            d = np.maximum.reduce(
                [np.abs((x - cx) * nx + (y - cy) * ny) for nx, ny in normals]
            )
            best = np.minimum(best, d)
    return best


def render(
    cam: dict[str, np.ndarray],
    size: tuple[int, int],
    cols: int = 22,
    rows: int = 16,
    wall_mm: float = 2.4,
    noise: float = 2.0,
    seed: int = 0,
    ss: int = 2,
) -> np.ndarray:
    """Render the panel (cols x rows cells at the grid origin) to an RGB uint8 image."""
    import cv2

    w, h = size
    pc, pr = PITCH
    H = cam["K"] @ np.column_stack([cam["R"][:, 0], cam["R"][:, 1], cam["t"]])
    Hi = np.linalg.inv(H)
    ys, xs = np.mgrid[0 : h * ss, 0 : w * ss].astype(np.float64)
    q = (
        np.stack(
            [(xs + 0.5) / ss - 0.5, (ys + 0.5) / ss - 0.5, np.ones_like(xs)], axis=-1
        )
        @ Hi.T
    )
    X, Y = q[..., 0] / q[..., 2], q[..., 1] / q[..., 2]
    d = _nearest_hex_dist(X, Y)
    inside = (X > 0) & (cols * pc > X) & (Y > 0) & (rows * pr + 0.5 * pr > Y)
    hole = d < (0.5 * pr - wall_mm / 2)
    # one-sided shadow inside the hole (light from the upper left of the wall)
    shade = np.clip(
        0.5 + 0.5 * ((X % pc) / pc - 0.5) * 2 - 0.3 * ((Y % pr) / pr - 0.5), 0, 1
    )
    img = np.full(X.shape, 0.78)
    img[inside] = 0.92
    img[inside & hole] = (0.42 + 0.14 * shade)[inside & hole]
    ramp = 1.0 + 0.12 * (xs / (w * ss) - 0.5) - 0.08 * (ys / (h * ss) - 0.5)
    img = img * ramp
    img = cv2.resize(img.astype(np.float32), (w, h), interpolation=cv2.INTER_AREA)
    img = cv2.GaussianBlur(img, (0, 0), 0.7)
    rng = np.random.default_rng(seed)
    out = np.clip(img * 255 + rng.normal(0, noise, img.shape), 0, 255).astype(np.uint8)
    return np.repeat(out[:, :, None], 3, axis=2)


def lattice_projection_error(
    res: dict, cam: dict[str, np.ndarray]
) -> tuple[float, float]:
    """Median and max pixel distance between the recovered model and the TRUE camera over the recovered cells.

    The recovered lattice labelling has an arbitrary origin, so each recovered cell is matched to the true lattice node
    that its true projection lands on (phase of the true lattice estimated by a circular mean), then both cameras project
    that node."""
    from threed_vision import calibrate as C

    pc, pr = PITCH
    Ht = cam["K"] @ np.column_stack([cam["R"][:, 0], cam["R"][:, 1], cam["t"]])
    P = np.asarray(res["inlier_cells_px"])
    q = C.apply_homography(np.linalg.inv(Ht), P)
    m = q[:, 0] / pc
    n = q[:, 1] / pr - 0.5 * m
    am = np.angle(np.exp(2j * np.pi * m).mean()) / (2 * np.pi)
    an = np.angle(np.exp(2j * np.pi * n).mean()) / (2 * np.pi)
    node = np.column_stack([np.rint(m - am) + am, np.rint(n - an) + an])
    mm = np.column_stack([node[:, 0] * pc, (node[:, 1] + 0.5 * node[:, 0]) * pr])
    truth = C.apply_homography(Ht, mm)
    lab = np.asarray(res["inlier_cells"])
    X = np.column_stack(
        [(lab[:, 0] + 0.5) * pc, (lab[:, 1] + 0.5 + 0.5 * (lab[:, 0] & 1)) * pr]
    )
    mine = C.apply_homography(res["homography_grid_mm_to_px"], X)
    d = np.linalg.norm(mine - truth, axis=1)
    return float(np.median(d)), float(d.max())


TEXTURE_SIZE = (800, 600)


def textured(size: tuple[int, int] = TEXTURE_SIZE, seed: int = 0) -> np.ndarray:
    """Multi-scale noise plus random discs/rectangles: plenty of SIFT corners, no repeating structure."""
    import cv2

    w, h = size
    rng = np.random.default_rng(seed)
    img = np.zeros((h, w), np.float32)
    for s, amp in ((4, 0.5), (8, 1.0), (16, 1.5), (40, 2.0)):
        img += amp * cv2.resize(
            rng.random((h // s + 2, w // s + 2)).astype(np.float32),
            (w, h),
            interpolation=cv2.INTER_CUBIC,
        )
    img = (255 * (img - img.min()) / (img.max() - img.min())).astype(np.uint8)
    rgb = np.repeat(img[:, :, None], 3, axis=2).copy()
    for _ in range(60):
        c = tuple(int(v) for v in rng.integers(0, 255, 3))
        x, y = int(rng.integers(0, w)), int(rng.integers(0, h))
        if rng.random() < 0.5:
            cv2.circle(rgb, (x, y), int(rng.integers(6, 30)), c, -1)
        else:
            cv2.rectangle(
                rgb,
                (x, y),
                (x + int(rng.integers(8, 50)), y + int(rng.integers(8, 50))),
                c,
                -1,
            )
    return rgb


def plane_homography(
    K: np.ndarray, R: np.ndarray, t: np.ndarray, n: np.ndarray, d: float = 1.0
) -> np.ndarray:
    """H = K (R + t n^T / d) K^-1: the homography induced by the plane n.X = d between two views."""
    H = K @ (R + np.outer(t, n) / d) @ np.linalg.inv(K)
    return H / H[2, 2]


def view_pair(
    yaw_deg: float = 12.0,
    pitch_deg: float = 3.0,
    t: tuple[float, float, float] = (0.15, 0.02, 0.05),
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(A, B, true H A->B, true relative R): B is A warped by the plane homography of a camera moved by (R, t)."""
    import cv2

    a = textured()
    w, h = TEXTURE_SIZE
    f = 0.375 * math.hypot(w, h)  # default_intrinsics
    K = np.array([[f, 0, w / 2], [0, f, h / 2], [0, 0, 1.0]])
    R = rot_y(yaw_deg) @ rot_x(pitch_deg)
    H = plane_homography(K, R, np.array(t), np.array([0.0, 0.0, 1.0]))
    b = cv2.warpPerspective(
        a, H, TEXTURE_SIZE, flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT
    )
    return a, b, H, R


def write_wall_photo(
    path: str,
    size: tuple[int, int] = (1200, 900),
    f: float = 900.0,
    tilt_x: float = 25.0,
    tilt_y: float = 12.0,
    roll: float = 4.0,
    depth_mm: float = 900.0,
) -> dict[str, np.ndarray]:
    """Render a synthetic honeycomb wall photo to `path` (PNG/JPEG) and return the true camera."""
    import cv2

    cam = camera(
        f, size, tilt_x, tilt_y, roll, depth_mm, (11 * PITCH[0], 8.25 * PITCH[1])
    )
    cv2.imwrite(path, cv2.cvtColor(render(cam, size), cv2.COLOR_RGB2BGR))
    return cam


def write_png(path: str, img: np.ndarray) -> None:
    import cv2

    ok = cv2.imwrite(
        path, img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    )
    assert ok, path
