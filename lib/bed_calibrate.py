"""bed_calibrate.py — `3d printer bed calibrate NAME`: how much of the plate the camera sees at each Z (#723).

A printer with a camera calibration (the U1's ships with 3d-cli) knows where the bed is in the frame at every
Z: `bed_state.bed_in_view`. One without (the K1 SE) judged every Z as "the plate is in the frame by definition",
so the check never moved the bed to a better view and never knew that a low view was a poor one. This learns it.

WHAT: on an IDLE printer the bed is moved in Z ONLY (bed_view.excursion: the guarded move of `bed check`, the
  clearance floor and ceiling of `bed_view.move_limits`, the per-printer lock, the displaced-Z marker, SIGTERM and
  exceptions unwinding, the bed restored in a `finally`) through a grid of Zs, with the printer's light on; one
  fresh frame per Z. Then, without touching the printer, the plate is FOLLOWED over the frames:
    seed     the plate polygon in a frame of known Z: `watch.bed.plate_mask` (= `roi`) drawn on the nearest
             empty-bed reference, else that reference's own bed surface; with neither, the largest dark,
             low-saturation region of the grid frame that has most of it (rough: the result says so).
    follow   from the seed outwards in both directions, frame to frame: the affine transform (ECC, coarse to
             fine, correlation based so a light change does not move it) that carries the plate of the last
             followed frame onto the next one, fitted on the plate pixels in view. The polygon moves with it,
             so the part of the plate that has left the frame is still counted.
    share    visible area of the polygon in the frame / its whole area, per Z.
  A plate that cannot be followed (too little of it in view, no texture, a correlation under ECC_MIN_CORR) ends
  the chain in that direction: those Zs are listed as `unfollowed` and are not stored, so no view is invented.

STORED (~/.local/share/3d-cli/bed/NAME/calibration.json, printer_ops.bed.Calibration): `views` [[Z, share]],
  `plate_mask` (the polygon where most of the plate shows, 0..1 of the frame), `method`, the grid, the seed.
  `bed_state.z_match` reads it when the printer has no camera calibration and no `watch.bed.roi`;
  `watch.bed.views` overrides it.

LIMITS: the model is affine (the perspective of a big move is followed approximately); the plate must show
  texture; the share is relative to the plate's area at that Z; the grid step has to be small enough for the
  plate to move a few tens of px between two Zs. `--json` has every number.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import bed_state as B
import bed_view
from errors import UsageError
from printer_ops.bed import AUTO_VIEW_SETTLE_S, BedConfig, write_calibration
from printer_ops.bed_light import LightSession, write_block

if TYPE_CHECKING:
    import numpy as np

    from printer_ops.devices import Device

DEFAULT_STEP_MM = 20.0
MAX_POINTS = (
    60  # frames of one calibration: each is a move, a settle wait and a camera frame
)
MIN_VISIBLE = (
    0.05  # a plate with less of it in view than this cannot be followed any further
)
ECC_MIN_CORR = (
    0.5  # correlation of the followed plate under which the step is not trusted
)
GROW_PX = 15  # the fine fit looks this far beyond where the plate is expected: a step moves it a few px
HEURISTIC_MIN_SHARE = (
    0.05  # the largest dark region must cover this share of the frame to be a plate
)


@dataclass
class PlateAt:
    z: float
    share: float
    polygon: list[tuple[float, float]]  # px of the frame the plate is followed in
    corr: float | None = None


@dataclass
class Tracked:
    method: str  # "plate_mask" | "reference" | "heuristic"
    seed_z: float
    seed_source: str
    at: list[PlateAt] = field(default_factory=list)
    unfollowed: list[dict[str, Any]] = field(default_factory=list)


# ---- geometry (pure python: a polygon is a list of (x, y)) ---------------------------------------


def area(poly: list[tuple[float, float]]) -> float:
    """Shoelace area of a simple polygon."""
    n = len(poly)
    return (
        abs(
            sum(
                poly[i][0] * poly[(i + 1) % n][1] - poly[(i + 1) % n][0] * poly[i][1]
                for i in range(n)
            )
        )
        / 2
    )


def _clip_edge(
    poly: list[tuple[float, float]],
    inside: Callable[[tuple[float, float]], bool],
    cut: Callable[[tuple[float, float], tuple[float, float]], tuple[float, float]],
) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for i, cur in enumerate(poly):
        prev = poly[i - 1]
        if inside(cur):
            if not inside(prev):
                out.append(cut(prev, cur))
            out.append(cur)
        elif inside(prev):
            out.append(cut(prev, cur))
    return out


def clip_to_frame(
    poly: list[tuple[float, float]], shape: tuple[int, int]
) -> list[tuple[float, float]]:
    """`poly` cut to the frame (Sutherland-Hodgman against the four edges: x 0..w-1, y 0..h-1)."""
    h, w = shape
    x1, y1 = float(w - 1), float(h - 1)

    def at_x(
        x: float,
    ) -> Callable[[tuple[float, float], tuple[float, float]], tuple[float, float]]:
        return lambda a, b: (x, a[1] + (b[1] - a[1]) * (x - a[0]) / (b[0] - a[0]))

    def at_y(
        y: float,
    ) -> Callable[[tuple[float, float], tuple[float, float]], tuple[float, float]]:
        return lambda a, b: (a[0] + (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]), y)

    for inside, cut in (
        (lambda p: p[0] >= 0, at_x(0.0)),
        (lambda p: p[0] <= x1, at_x(x1)),
        (lambda p: p[1] >= 0, at_y(0.0)),
        (lambda p: p[1] <= y1, at_y(y1)),
    ):
        if not poly:
            break
        poly = _clip_edge(poly, inside, cut)
    return poly


def visible_share(poly: list[tuple[float, float]], shape: tuple[int, int]) -> float:
    """The visible area of `poly` in a frame of `shape` over its whole area."""
    whole = area(poly)
    if whole < 1e-6:
        return 0.0
    clipped = clip_to_frame(poly, shape)
    return min(1.0, area(clipped) / whole) if len(clipped) >= 3 else 0.0


def to_px(
    poly_norm: tuple[tuple[float, float], ...], shape: tuple[int, int]
) -> list[tuple[float, float]]:
    h, w = shape
    return [(x * (w - 1), y * (h - 1)) for x, y in poly_norm]


def to_norm(
    poly: list[tuple[float, float]], shape: tuple[int, int]
) -> list[list[float]]:
    h, w = shape
    return [[round(x / (w - 1), 4), round(y / (h - 1), 4)] for x, y in poly]


def transform(
    poly: list[tuple[float, float]], warp: np.ndarray
) -> list[tuple[float, float]]:
    """`poly` carried by the 2x3 affine `warp`."""
    return [
        (
            float(warp[0, 0] * x + warp[0, 1] * y + warp[0, 2]),
            float(warp[1, 0] * x + warp[1, 1] * y + warp[1, 2]),
        )
        for x, y in poly
    ]


# ---- following the plate -----------------------------------------------------------------------


def _mask(
    poly: list[tuple[float, float]], shape: tuple[int, int], grow_px: int = GROW_PX
) -> np.ndarray:
    """Where the plate is expected in the frame, grown by `grow_px`: the pixels the fine fit looks at."""
    import cv2
    import numpy as np

    m = np.zeros(shape, np.uint8)
    cv2.fillPoly(m, [np.round(np.array(poly, np.float32)).astype(np.int32)], 1)
    return np.asarray(cv2.dilate(m, np.ones((2 * grow_px + 1,) * 2, np.uint8)))


def _ecc(
    prev: np.ndarray,
    cur: np.ndarray,
    mask: np.ndarray | None,
    warp: np.ndarray,
    scale: int,
) -> tuple[np.ndarray, float]:
    """One ECC fit at 1/`scale` size, from `warp` (full-size coordinates); the result is in full-size ones.
    `mask` (valid pixels of `cur`) is optional: a tight mask makes the coarse fits fail to converge."""
    import cv2
    import numpy as np

    def small(a: np.ndarray, interp: int) -> np.ndarray:
        if scale == 1:
            return a
        return np.asarray(
            cv2.resize(
                a, (a.shape[1] // scale, a.shape[0] // scale), interpolation=interp
            )
        )

    start = warp.copy()
    start[:, 2] /= scale
    stop = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 100, 1e-6)
    corr, fitted = cv2.findTransformECC(
        small(prev, cv2.INTER_AREA),
        small(cur, cv2.INTER_AREA),
        start,
        cv2.MOTION_AFFINE,
        stop,
        small(np.ones(prev.shape, np.uint8) if mask is None else mask, cv2.INTER_NEAREST),
        5,
    )
    fitted[:, 2] *= scale
    return fitted, float(corr)


def follow(
    prev: np.ndarray, cur: np.ndarray, poly: list[tuple[float, float]]
) -> tuple[np.ndarray, float] | str:
    """The affine transform that carries the plate of `prev` (outlined by `poly`) onto `cur`, with its
    correlation; or why the plate cannot be followed. Coarse to fine: the frames at 1/4 and 1/2 size find the
    move of the picture (the plate is most of it), then the full-size fit looks only where that move puts the plate."""
    import cv2
    import numpy as np

    g1, g2 = (
        np.asarray(cv2.cvtColor(i, cv2.COLOR_BGR2GRAY), np.float32) for i in (prev, cur)
    )
    warp = np.eye(2, 3, dtype=np.float32)
    try:
        for scale in (4, 2):
            warp, _ = _ecc(g1, g2, None, warp, scale)
        mask = _mask(transform(poly, warp), g1.shape)
        if visible_share(transform(poly, warp), g1.shape) < MIN_VISIBLE:
            return "too little of the plate is in the frame to follow it"
        warp, corr = _ecc(g1, g2, mask, warp, 1)
    except cv2.error:
        return "the plate's picture does not converge onto the next frame's (no texture, or it moved too far)"
    if corr < ECC_MIN_CORR:
        return f"the plate matches the next frame only to {corr:.2f} (under {ECC_MIN_CORR})"
    return warp, corr


def _unfollowed(
    frames: list[tuple[float, np.ndarray]], why: str
) -> list[dict[str, Any]]:
    return [{"z": z, "why": why} for z, _ in frames]


def _chain(
    frames: list[tuple[float, np.ndarray]],
    seed_img: np.ndarray,
    seed_poly: list[tuple[float, float]],
    out: Tracked,
) -> None:
    """Follow the plate from the seed through `frames` (nearest Z first); record each Z, stop at the first the
    plate cannot be followed to (the Zs beyond it are listed, not stored)."""
    prev, poly = seed_img, seed_poly
    for i, (z, img) in enumerate(frames):
        step = follow(prev, img, poly)
        if isinstance(step, str):
            out.unfollowed.extend(_unfollowed(frames[i:], f"at Z {z:g}: {step}"))
            return
        warp, corr = step
        poly = transform(poly, warp)
        share = visible_share(poly, img.shape[:2])
        out.at.append(PlateAt(z, share, poly, corr))
        if share < MIN_VISIBLE:
            out.unfollowed.extend(
                _unfollowed(frames[i + 1 :], f"the plate has left the frame at Z {z:g}")
            )
            return
        prev = img


def track(
    seed_img: np.ndarray,
    seed_poly: list[tuple[float, float]],
    seed_z: float,
    frames: dict[float, np.ndarray],
    method: str,
    source: str,
) -> Tracked:
    """Follow the plate of the seed frame over the grid `frames` (Z -> image), outwards in both directions."""
    out = Tracked(method, seed_z, source)
    out.at.append(
        PlateAt(seed_z, visible_share(seed_poly, seed_img.shape[:2]), seed_poly)
    )
    below = sorted(((z, i) for z, i in frames.items() if z < seed_z), reverse=True)
    above = sorted((z, i) for z, i in frames.items() if z >= seed_z)
    for side in (above, below):
        _chain(side, seed_img, seed_poly, out)
    # one point per Z: a grid frame at the seed's own Z (followed from the seed) replaces the seed's point
    out.at = sorted({p.z: p for p in out.at}.values(), key=lambda p: p.z)
    return out


# ---- the seed -------------------------------------------------------------------------------------


def heuristic_polygon(img: np.ndarray) -> list[tuple[float, float]] | None:
    """The outline of the largest dark, low-saturation region of `img` (a black PEI plate against a room): rough,
    and merged with a black frame it touches. None when no region is a twentieth of the frame."""
    import cv2
    import numpy as np

    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    dark = ((hsv[..., 1] < 80) & (hsv[..., 2] < 120)).astype(np.uint8)
    opened = cv2.morphologyEx(dark, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(opened)
    if n < 2:
        return None
    best = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    if (
        stats[best, cv2.CC_STAT_AREA]
        < HEURISTIC_MIN_SHARE * img.shape[0] * img.shape[1]
    ):
        return None
    hull = cv2.convexHull(np.argwhere(lab == best)[:, ::-1].astype(np.int32))
    approx = cv2.approxPolyDP(hull, 0.02 * cv2.arcLength(hull, True), True)
    return [(float(x), float(y)) for x, y in approx[:, 0, :]]


@dataclass
class Seed:
    img: np.ndarray
    z: float
    polygon: list[tuple[float, float]]
    method: str
    source: str


def reference_seed(cfg: BedConfig, refs: list[B.Reference]) -> Seed | None:
    """The plate in an empty-bed reference: `watch.bed.plate_mask`/`roi` drawn on it, else its bed surface."""
    import cv2
    import numpy as np

    plates = [r for r in refs if r.kind == "plate" and r.z is not None]
    if not plates:
        return None
    ref = max(plates, key=lambda r: float(r.surface.mean()))
    assert ref.z is not None
    if cfg.roi is not None:
        return Seed(
            ref.image,
            ref.z,
            to_px(cfg.roi, ref.image.shape[:2]),
            "plate_mask",
            f"watch.bed.plate_mask on the reference at Z {ref.z:g}",
        )
    contours, _ = cv2.findContours(
        ref.surface.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    if not contours:
        return None
    big = max(contours, key=cv2.contourArea)
    approx = cv2.approxPolyDP(big, 0.01 * cv2.arcLength(big, True), True)
    return Seed(
        ref.image,
        ref.z,
        [(float(x), float(y)) for x, y in approx[:, 0, :]],
        "reference",
        f"the bed surface of the reference at Z {ref.z:g}",
    )


def heuristic_seed(frames: dict[float, np.ndarray]) -> Seed | None:
    """The grid frame with the largest dark region, outlined; the rough fallback without a reference."""
    best: Seed | None = None
    for z, img in frames.items():
        poly = heuristic_polygon(img)
        if poly is not None and (best is None or area(poly) > area(best.polygon)):
            best = Seed(
                img,
                z,
                poly,
                "heuristic",
                f"the largest dark low-saturation region of the frame at Z {z:g}",
            )
    return best


def analyse(frames: dict[float, np.ndarray], seed: Seed | None) -> Tracked:
    """The plate's visible share per Z over `frames` from `seed` (None: the heuristic seed). Pure: no printer."""
    seed = seed or heuristic_seed(frames)
    if seed is None:
        raise UsageError(
            "no plate to start from: no empty-bed reference, no watch.bed.plate_mask, and no frame "
            "shows a dark region of at least a twentieth of it",
            command="printer bed",
            remediation=[
                "Take a reference first: `3d printer bed reference NAME --capture`, or draw the "
                "plate in watch.bed.plate_mask: [[x, y], ...] (0..1 of the frame)."
            ],
        )
    return track(seed.img, seed.polygon, seed.z, frames, seed.method, seed.source)


# ---- the grid ----------------------------------------------------------------------------------------


def plan_grid(
    limits: bed_view.Limits,
    z_from: float | None,
    z_to: float | None,
    z_step: float | None,
) -> list[float]:
    """The Zs to look from, ascending, within the clearance floor and the axis ceiling; UsageError otherwise."""
    lo = limits.z_floor if z_from is None else z_from
    hi = limits.z_ceiling if z_to is None else z_to
    step = DEFAULT_STEP_MM if z_step is None else z_step
    if not all(math.isfinite(v) for v in (lo, hi, step)) or step <= 0:
        raise UsageError(
            "the Z grid needs finite --z-from/--z-to and a positive --z-step",
            command="printer bed",
        )
    if lo < limits.z_floor or hi > limits.z_ceiling:
        raise UsageError(
            f"Z {lo:g}..{hi:g} is outside what may be moved to: {limits.z_floor:g}..{limits.z_ceiling:g} "
            f"(the tallest thing on the bed is {limits.part_mm:g} mm, {limits.part_source}; the ceiling "
            "is the axis limit)",
            command="printer bed",
            remediation=[
                "Lower the floor with a smaller --part-height only if nothing stands on the bed."
            ],
        )
    if hi < lo:
        raise UsageError(
            f"--z-to {hi:g} is below --z-from {lo:g}", command="printer bed"
        )
    n = int((hi - lo) / step + 1e-9) + 1
    if n > MAX_POINTS:
        raise UsageError(
            f"{n} Zs is too many (at most {MAX_POINTS}): raise --z-step",
            command="printer bed",
        )
    return [round(lo + i * step, 3) for i in range(n)]


# ---- the flow ------------------------------------------------------------------------------------------


def _collect(
    device: Device,
    adapter: Any,
    camera: Any,
    m: bed_view.Motion,
    limits: bed_view.Limits,
    grid: list[float],
    light: LightSession,
    sleep: Callable[[float], None],
    settle_s: float,
) -> tuple[dict[float, np.ndarray], dict[str, Any]]:
    """Move through `grid`, one frame per Z; the bed is put back (and the marker cleared) however this ends."""
    frames: dict[float, np.ndarray] = {}
    notes: dict[str, Any] = {"moved": [], "refused": None}
    with bed_view.excursion(device, adapter, m, limits) as trip:
        light.on()  # before the first move: its settle overlaps the Z travel
        for z in grid:
            try:
                trip.go(z)
            except bed_view.MoveRefused as exc:
                notes["refused"] = f"stopped at Z {z:g}: {exc}"
                break
            notes["moved"].append(z)
            sleep(settle_s)
            jpeg, _ = B._fresh_frame(device, camera, sleep)
            frames[z] = B._decode_frame(device, jpeg)
    notes["restore_error"] = trip.restore_error
    return frames, notes


def _document(
    device: Device,
    tracked: Tracked,
    grid: list[float],
    shape: tuple[int, int],
    now: float,
) -> dict[str, Any]:
    best = max(tracked.at, key=lambda p: p.share)
    return {
        "printer": device.name,
        "ts": now,
        "method": tracked.method,
        "seed": {"z": tracked.seed_z, "source": tracked.seed_source},
        "grid": grid,
        "views": [[p.z, round(p.share, 3)] for p in tracked.at],
        "plate_mask": to_norm(best.polygon, shape),
        "plate_mask_z": best.z,
        "unfollowed": tracked.unfollowed,
        "corr": {
            f"{p.z:g}": None if p.corr is None else round(p.corr, 3) for p in tracked.at
        },
    }


def calibrate(
    device: Device,
    cfg: BedConfig,
    *,
    z_from: float | None = None,
    z_to: float | None = None,
    z_step: float | None = None,
    part_height: float | None = None,
    adapter: Any = None,
    camera: Any = None,
    now: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    settle_s: float | None = None,
) -> dict[str, Any]:
    """Look at the plate from every Z of the grid and store how much of it shows at each. An idle, homed
    printer only; the bed ends where it was found. The result is the stored document plus `light`, `view`
    (what the bed did) and `path`."""
    from printer_ops import make_adapter

    adapter = adapter if adapter is not None else make_adapter(device, timeout=10.0)
    camera = camera if camera is not None else B.camera_for(device, cfg)
    m = bed_view.read_motion(adapter)
    limits = bed_view._may_move(device, adapter, cfg, m, part_height)
    if not isinstance(limits, bed_view.Limits):
        raise UsageError(
            f"{device.name}: not calibrated, nothing was moved: {limits}",
            command="printer bed",
            remediation=[
                "`bed calibrate` needs an idle (idle / complete / cancelled), homed, writable "
                "printer whose part height is known (--part-height MM)."
            ],
        )
    grid = plan_grid(limits, z_from, z_to, z_step)
    light = LightSession(
        adapter,
        cfg.light,
        note="the calibration frames",
        blocked=write_block(device),
        sleep=sleep,
    )
    with light:
        frames, notes = _collect(
            device,
            adapter,
            camera,
            m,
            limits,
            grid,
            light,
            sleep,
            AUTO_VIEW_SETTLE_S if settle_s is None else settle_s,
        )
    if len(frames) < 2:
        raise UsageError(
            f"{device.name}: only {len(frames)} frame(s) taken ({notes['refused'] or 'no grid'}): "
            "nothing was stored",
            command="printer bed",
        )
    tracked = analyse(frames, reference_seed(cfg, B.RefStore(device.name).load()))
    if len(tracked.at) < 2:
        raise UsageError(
            f"{device.name}: the plate could not be followed over the frames "
            f"({'; '.join(str(u['why']) for u in tracked.unfollowed[:2])}): nothing was stored",
            command="printer bed",
            remediation=[
                "Smaller --z-step, or a plate_mask / a reference to start from."
            ],
        )
    now = time.time() if now is None else now
    doc = _document(device, tracked, grid, next(iter(frames.values())).shape[:2], now)
    path = write_calibration(device.name, doc)
    return {
        **doc,
        "path": str(path),
        "light": light.report.to_dict(),
        "view": {**notes, "z_before": m.z, "restored": notes["restore_error"] is None},
    }


def _text(device: Device, res: dict[str, Any]) -> str:
    rows = "\n".join(
        f"  Z {z:6.1f}  {share:>4.0%} of the plate in the frame"
        for z, share in res["views"]
    )
    lost_zs = ", ".join(f"{u['z']:g}" for u in res["unfollowed"])
    lost = f"\n  not followed (not stored): {lost_zs}" if lost_zs else ""
    rough = (
        "\n  method heuristic: the plate was outlined from the largest dark region — check it"
        if res["method"] == "heuristic"
        else ""
    )
    view = res["view"]
    back = (
        "bed put back"
        if view["restored"]
        else f"BED NOT PUT BACK: {view['restore_error']}"
    )
    stopped = f"\n  {view['refused']}" if view.get("refused") else ""
    light = (res.get("light") or {}).get("text")
    return (
        f"{device.name}: calibrated from {res['seed']['source']} ({res['method']}); {back}\n{rows}{lost}{rough}"
        f"{stopped}\n  {res['path']}" + (f"\n  light: {light}" if light else "")
    )


def cmd_calibrate(args: argparse.Namespace, device: Device, cfg: BedConfig) -> int:
    """`bed calibrate NAME`: exit 0 when the views were stored (and the bed is back), 1 when the bed is not back."""
    res = calibrate(
        device,
        cfg,
        z_from=args.z_from,
        z_to=args.z_to,
        z_step=args.z_step,
        part_height=args.part_height,
    )
    print(json.dumps(res, indent=1, default=str) if args.json else _text(device, res))
    return 0 if res["view"]["restored"] else 1
