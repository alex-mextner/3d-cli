"""bed_state.py — the pixel half of the printer bed state (issue #303): is the bed clear, is the plate on.

Run through cli.pyrun with numpy + OpenCV by `3d printer bed` (status / reference) and by
`3d printer watch` (one `monitor` child per printer with `watch.bed.enabled`). Config, the
state file, the watch child, the start gate and the clear acknowledgement are stdlib code in
printer_ops/bed.py.

HOW IT DECIDES: against a reference frame of the EMPTY bed WITH the plate, taken by
  `3d printer bed reference NAME --capture` at a toolhead Z the printer rests at after a job
  (U1: Z 200 after PRINT_END/CANCEL_PRINT, Z 10 after a G28; K1 SE END_PRINT: half of Z max).
  The bed moves in Z on both printers, so a reference only stands for the bed at a Z where the
  camera sees the same picture: with a camera calibration, where the projected bed (and the
  volume above it) moved under `match_px` pixels of the 480-px frame (`ZMatch`; the U1 parks at
  toolhead Z 199.7 or 200.3 depending on the job's mesh and Z offset), else within `z_tol_mm`.
  Both frames are decoded at reduced size (480 px wide), blurred, the current one is mapped onto
  the reference colour by a robust per-channel gain/offset fit inside the ROI (auto-exposure and
  room light change the whole frame, a part changes a region), and the pixels still off by more
  than `level` count as changed.
    view:  with a calibration, under `view_min` of the bed footprint in the frame at this Z (the
           U1 bed at Z >= ~215, Z 275 after a home: none of it) -> unknown, "out of the camera's
           view" — no reference can fix that, and none is captured there.
    bed:   changed share of the bed ROI <= clear_max -> clear; >= occupied_min -> occupied;
           in between -> unknown (a small part or a shadow: look at the frame).
    plate: with a `--no-plate` reference too, whichever reference the bed surface matches
           better; with the plate reference only, most of the surface changed -> missing
           (plate off, or the bed covered); surface under 2% of the frame -> unknown.
  ROI: fleet `watch.bed.roi` (a polygon of the bed in the frame at the reference Z) if given,
  else the camera calibration (vision_geometry: bed surface and the volume above it up to
  `volume_height_mm`, projected at the reference Z), else the whole frame.

CHEAP (monitor): nothing is grabbed while the printer prints. Otherwise one snapshot per
  `interval_s`; identical bytes are skipped (the U1 rewrites its monitor.jpg every ~4 s); a new
  frame costs one reduced JPEG decode and one masked difference against the previous frame.
  The classifier runs only when frames changed and then stayed still for `settle_frames`
  frames (a hand in the chamber is not judged), and once at start.

OUTPUT (monitor): one JSON object per stdout line — `{"event": {...}}` on a transition
  (bed_clear, bed_occupied, plate_present, plate_missing, and the warning bed_unknown when the
  first verdict after a job cannot tell; printer_ops.bed.transitions) and `{"state": {...}}`
  after each verdict; logs on stderr. Evidence frames (ROI outlined, changed pixels red) go to
  ~/.cache/3d-cli/vision/bed/.

READ-ONLY except `watch.bed.clear_ack` (printer_ops/bed.py WRITES).
"""

from __future__ import annotations

import argparse
import functools
import json
import math
import re
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

LIB = Path(__file__).resolve().parent
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from printer_ops.bed import (  # noqa: E402
    CLEAR,
    MISSING,
    OCCUPIED,
    PRESENT,
    PRINTING,
    QUIET_STATES,
    UNKNOWN,
    BedConfig,
    bed_config,
    clear_ack,
    evidence_dir,
    read_state,
    refs_dir,
    state_path,
    transitions,
    write_state,
)

if TYPE_CHECKING:
    import numpy as np

    from printer_ops.devices import Device
    from vision_geometry import CameraModel

PROC_W = 480  # processing width; a 1920x1080 frame is decoded at 1/4, a 1280x720 one at 1/2
ACTIVE = ("printing", "paused", "busy")  # base.py states in which the bed belongs to the job
_REDUCED = {1: "IMREAD_COLOR", 2: "IMREAD_REDUCED_COLOR_2", 4: "IMREAD_REDUCED_COLOR_4", 8: "IMREAD_REDUCED_COLOR_8"}


def _log(msg: str) -> None:
    print(f"bed: {msg}", file=sys.stderr, flush=True)


@dataclass(frozen=True)
class Thresholds:
    """Tuned on real frames (tests/data/bed): the U1 removal of 2026-10-14 at Z 200 (parts: 20 %
    of the bed view changed, largest region ~2260 px; 25 min of drifting light after the removal:
    0 px) and the K1 SE empty bed in daylight vs a dark room (largest region 74 px, a glare at the
    plate edge). A 16 px square (a ~15 mm part on the K1 SE) is ~300 px, a 12 px one ~190 px."""

    level: float = 25.0          # per-pixel colour difference after alignment that counts as changed
    detrend_px: int = 61         # objects: median window removing smooth light gradients (odd px)
    edge_px: int = 3             # objects are judged this far inside the ROI border
    clear_blob_px: int = 100     # clear: the largest changed region is smaller than this ...
    clear_max: float = 0.03      # ... and at most this share of the bed ROI changed
    occupied_blob_px: int = 200  # occupied: a changed region at least this big ...
    occupied_min: float = 0.08   # ... or at least this share of the bed ROI changed
    plate_missing_min: float = 0.6  # plate reference only: changed share of the surface = plate off
    plate_tie: float = 0.02       # both references: |with-plate - bare-bed| below this -> plate unknown
    surface_min: float = 0.02    # bed surface share of the frame below which the plate is not judged
    change_min: float = 0.01     # frame-to-frame changed share that counts as "something moved"
    settle_frames: int = 3       # unchanged frames after a change before the bed is judged
    match_px: float = 1.5        # calibrated: a reference stands for a Z where the bed view moved less
    view_min: float = 0.10       # calibrated: bed footprint share in the frame below which nothing is judged


TH = Thresholds()


def thresholds_for(cfg: BedConfig) -> Thresholds:
    """The thresholds of one printer: the tuned defaults with its `watch.bed.thresholds` applied."""
    return replace(TH, **{k: type(getattr(TH, k))(v) for k, v in cfg.thresholds})


# ---- pixels ---------------------------------------------------------------------------------

def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) from the first SOFn marker, or None."""
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg = int.from_bytes(data[i + 2:i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        i += 2 + seg
    return None


def decode_small(data: bytes) -> np.ndarray:
    """BGR frame PROC_W wide, decoded at the largest JPEG reduction that keeps >= PROC_W.
    The K1 SE mjpg-streamer pads the entropy data with zeros before EOI (#250): dropped."""
    import cv2
    import numpy as np

    body = data.rstrip(b"\x00")
    if body.endswith(b"\xff\xd9"):
        body = body[:-2].rstrip(b"\x00") + b"\xff\xd9"
    size = jpeg_size(body)
    reduce = next((r for r in (8, 4, 2) if size and size[0] // r >= PROC_W), 1)
    img = cv2.imdecode(np.frombuffer(body, np.uint8), getattr(cv2, _REDUCED[reduce]))
    if img is None:
        raise ValueError("not a decodable JPEG")
    if img.shape[1] != PROC_W:
        h = max(1, round(img.shape[0] * PROC_W / img.shape[1]))
        img = cv2.resize(img, (PROC_W, h), interpolation=cv2.INTER_AREA)
    return img


def align(cur: np.ndarray, ref: np.ndarray, roi: np.ndarray) -> np.ndarray:
    """`cur` (float32) mapped onto `ref`'s colours: per channel a*x + b fitted inside `roi` on
    the best-matching 60 % of the pixels (three refits), so a part or a hand does not bend the
    fit and a global exposure/white-balance change disappears."""
    import numpy as np

    out = np.empty(cur.shape, np.float32)
    for c in range(cur.shape[2]):
        x, y = cur[..., c][roi], ref[..., c][roi]
        a, b = 1.0, 0.0
        keep = np.ones(x.shape, bool)
        for _ in range(3):
            xs, ys = x[keep], y[keep]
            if xs.size < 16:
                break
            var = float(xs.var())
            # least squares y = a*x + b; a flat region keeps the gain and fits the offset only
            a = min(max(float(((xs - xs.mean()) * (ys - ys.mean())).mean()) / var, 0.25), 4.0) if var > 1.0 else 1.0
            b = float(ys.mean()) - a * float(xs.mean())
            r = np.abs(a * x + b - y)
            keep = r <= np.quantile(r, 0.6)
        out[..., c] = a * cur[..., c] + b
    return out


@dataclass
class Diff:
    frac: float           # changed share of the ROI
    blob: float           # largest connected changed region, share of the ROI
    mask: np.ndarray      # bool, changed pixels


def difference(ref: np.ndarray, cur: np.ndarray, roi: np.ndarray, level: float = TH.level,
               detrend: int = 0) -> Diff:
    """Changed pixels of `cur` against `ref` inside `roi` after `align`. `detrend` (odd px > 0)
    also subtracts the median of the signed difference over that window: a light gradient or a
    reflection that moved with the room light is smooth and goes, a part narrower than half the
    window stays (used for objects; the plate test keeps the plain difference)."""
    import cv2
    import numpy as np

    area = int(roi.sum())
    if area == 0:
        return Diff(0.0, 0.0, np.zeros(roi.shape, bool))
    r = cv2.GaussianBlur(ref, (5, 5), 0).astype(np.float32)
    c = cv2.GaussianBlur(cur, (5, 5), 0).astype(np.float32)
    signed = align(c, r, roi) - r
    if detrend > 1:
        signed[~roi] = 0.0
        packed = np.clip(signed / 2 + 128, 0, 255).astype(np.uint8)
        signed -= (cv2.medianBlur(packed, detrend | 1).astype(np.float32) - 128) * 2
    d = np.abs(signed).max(axis=2)
    changed = ((d > level) & roi).astype(np.uint8)
    changed = cv2.morphologyEx(changed, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(changed)
    blob = int(stats[1:, cv2.CC_STAT_AREA].max()) if n > 1 else 0
    return Diff(float(changed.sum()) / area, blob / area, changed.astype(bool))


# ---- references -----------------------------------------------------------------------------

@dataclass
class Reference:
    kind: str             # "plate" | "noplate"
    z: float | None
    ts: float
    roi_source: str
    image: np.ndarray     # PROC_W-wide BGR
    volume: np.ndarray    # bool: where objects on the bed show
    surface: np.ndarray   # bool: the bed surface (plate)
    path: Path


def polygon_mask(poly: tuple[tuple[float, float], ...], shape: tuple[int, int]) -> np.ndarray:
    import cv2
    import numpy as np

    h, w = shape
    mask = np.zeros((h, w), np.uint8)
    pts = np.array([[round(x * (w - 1)), round(y * (h - 1))] for x, y in poly], np.int32)
    cv2.fillPoly(mask, [pts], 1)
    return mask.astype(bool)


def camera_model(device: Device, cfg: BedConfig) -> CameraModel | None:
    """The camera calibration the ROI comes from (None with a `watch.bed.roi` polygon or none)."""
    if cfg.roi is not None:
        return None
    from vision_geometry import bundled_calibration, load_calibration

    return load_calibration(device.name) or (bundled_calibration(device.model) if device.model else None)


def build_masks(device: Device, cfg: BedConfig, z: float, shape: tuple[int, int]) -> tuple[Any, Any, str]:
    """(volume, surface, where the ROI came from) for a reference taken at gcode Z `z`."""
    import numpy as np

    if cfg.roi is not None:
        m = polygon_mask(cfg.roi, shape)
        return m, m, f"{device.name}.watch.bed.roi"
    from vision_geometry import bed_mask, prism_mask

    cam = camera_model(device, cfg)
    if cam is not None:
        x0, y0, x1, y1 = cam.bed
        bed = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        surface = bed_mask(cam, z, shape) > 0
        # the whole volume_height_mm above the bed at any Z (volume_mask stops at min(z, height),
        # the print's height while printing: at the U1's Z 10 rest that would be 10 mm)
        volume = surface | (prism_mask(cam, z, shape, [bed], 0.0, cfg.volume_height_mm) > 0)
        return volume, surface, f"camera calibration, bed + {cfg.volume_height_mm:g} mm above it at Z {z:.1f}"
    full = np.ones(shape, bool)
    return full, full, "whole frame (no camera calibration and no watch.bed.roi)"


def _bed_points(cam: CameraModel, heights: tuple[float, ...], n: int) -> np.ndarray:
    """An n x n grid over the bed rectangle at each height above the bed (mm)."""
    import numpy as np

    x0, y0, x1, y1 = cam.bed
    gx, gy = np.meshgrid(np.linspace(x0, x1, n), np.linspace(y0, y1, n))
    return np.concatenate([np.c_[gx.ravel(), gy.ravel(), np.full(gx.size, h)] for h in heights])


def _in_frame(cam: CameraModel, px: np.ndarray) -> np.ndarray:
    import numpy as np

    return (np.isfinite(px).all(axis=1) & (px[:, 0] >= 0) & (px[:, 0] < cam.width)
            & (px[:, 1] >= 0) & (px[:, 1] < cam.height))


@functools.lru_cache(maxsize=512)
def bed_in_view(cam: CameraModel, z: float) -> float:
    """Share of the bed footprint inside the camera frame with the bed at gcode Z `z`."""
    return float(_in_frame(cam, cam.project(_bed_points(cam, (0.0,), 41), z)).mean())


@functools.lru_cache(maxsize=512)
def view_shift_px(cam: CameraModel, z0: float, z1: float, height_mm: float) -> float:
    """Largest move, in pixels of the PROC_W frame, of a bed point or a point `height_mm` above it
    that is in the frame at either Z, when the bed goes from Z `z0` to `z1` (inf: nothing in view)."""
    import numpy as np

    pts = _bed_points(cam, (0.0, height_mm), 9)
    a, b = cam.project(pts, z0), cam.project(pts, z1)
    seen = np.isfinite(a).all(axis=1) & np.isfinite(b).all(axis=1) & (_in_frame(cam, a) | _in_frame(cam, b))
    if not seen.any():
        return math.inf
    d = np.linalg.norm(a[seen] - b[seen], axis=1)
    return float(d.max()) * PROC_W / cam.width if np.isfinite(d).all() else math.inf


@dataclass(frozen=True)
class ZMatch:
    """When a reference taken at one Z stands for the bed at another, and whether the camera sees
    the bed at a Z at all: from the camera calibration when there is one (the picture moved under
    `max_px`), else the plain distance `z_tol` (a `watch.bed.roi` polygon knows nothing of Z)."""

    z_tol: float = 0.5
    cam: CameraModel | None = None
    height_mm: float = 80.0
    max_px: float = TH.match_px

    def __call__(self, ref_z: float | None, z: float | None) -> bool:
        if ref_z is None or z is None:
            return False
        if self.cam is None:
            return abs(ref_z - z) <= self.z_tol
        return view_shift_px(self.cam, round(ref_z, 2), round(z, 2), self.height_mm) <= self.max_px

    def in_view(self, z: float | None) -> float | None:
        """Share of the bed in the frame at `z`; None without a calibration or a Z."""
        return None if self.cam is None or z is None else bed_in_view(self.cam, round(z, 1))

    def nearest_view(self, z: float, view_min: float) -> float | None:
        """The highest Z (5 mm steps) at or below `z` where at least `view_min` of the bed is in view."""
        return next((z - 5 * i for i in range(int(z // 5) + 1) if (self.in_view(z - 5 * i) or 0.0) >= view_min),
                    None)


def z_match(device: Device, cfg: BedConfig) -> ZMatch:
    return ZMatch(cfg.z_tol_mm, camera_model(device, cfg), cfg.volume_height_mm, thresholds_for(cfg).match_px)


def out_of_view(match: ZMatch, z: float | None, th: Thresholds = TH) -> str | None:
    """Why the camera cannot judge the bed at `z` (calibrated bed out of the frame), or None."""
    seen = match.in_view(z)
    if seen is None or seen >= th.view_min:
        return None
    assert z is not None
    back = match.nearest_view(z, th.view_min)
    where = f"; it sees the bed again at Z {back:.0f} or below" if back is not None else ""
    return (f"the bed is out of the camera's view at Z {z:.1f} (camera calibration: {seen:.0%} of it in the "
            f"frame, under {th.view_min:.0%}){where}")


class RefStore:
    """The references of one printer, re-read when the directory changes."""

    def __init__(self, printer: str) -> None:
        self.dir = refs_dir(printer)
        self._sig: tuple[tuple[str, int], ...] | None = None
        self._refs: list[Reference] = []

    def signature(self) -> tuple[tuple[str, int], ...]:
        try:
            return tuple(sorted((p.name, p.stat().st_mtime_ns) for p in self.dir.glob("*.json")))
        except OSError:
            return ()

    def load(self) -> list[Reference]:
        sig = self.signature()
        if sig != self._sig:
            loaded = [_load_reference(p) for p in sorted(self.dir.glob("*.json"))]
            self._sig, self._refs = sig, [r for r in loaded if r is not None]
        return self._refs


def _sibling(meta_path: Path, ext: str) -> Path:
    """`plate-z200.0.json` -> `plate-z200.0<ext>` (the stem holds a dot: no with_suffix)."""
    return meta_path.with_name(meta_path.name[: -len(".json")] + ext)


def _load_reference(meta_path: Path) -> Reference | None:
    import cv2

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        image = decode_small(_sibling(meta_path, ".jpg").read_bytes())
        vol = cv2.imread(str(_sibling(meta_path, ".volume.png")), cv2.IMREAD_GRAYSCALE)
        surf = cv2.imread(str(_sibling(meta_path, ".surface.png")), cv2.IMREAD_GRAYSCALE)
        if vol is None or surf is None:
            raise ValueError("mask missing")
        if vol.shape != image.shape[:2] or surf.shape != image.shape[:2]:
            raise ValueError(f"masks {vol.shape}/{surf.shape} do not match the frame {image.shape[:2]}")
        return Reference(str(meta["kind"]), None if meta.get("z") is None else float(meta["z"]),
                         float(meta.get("ts", 0)), str(meta.get("roi_source", "")), image, vol > 0, surf > 0,
                         meta_path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _log(f"skipping reference {meta_path}: {exc}")
        return None


def nearest(refs: list[Reference], kind: str, z: float | None, match: ZMatch) -> Reference | None:
    cands = [r for r in refs if r.kind == kind and match(r.z, z)]
    return min(cands, key=lambda r: (abs((r.z or 0) - (z or 0)), -r.ts)) if cands else None


def _fz(z: float | None) -> str:
    return "?" if z is None else f"{z:.1f}"


# ---- the classifier -------------------------------------------------------------------------

@dataclass
class Verdict:
    bed: str
    plate: str
    reason: str
    metrics: dict[str, float] = field(default_factory=dict)
    changed: Any = None          # bool mask of the changed pixels (evidence), or None
    roi: Any = None              # bool mask of the bed ROI (evidence), or None


def _judge_plate(img: np.ndarray, plate_ref: Reference | None, noplate_ref: Reference | None,
                 z: float | None, th: Thresholds) -> tuple[str, str, dict[str, float]]:
    base = plate_ref or noplate_ref
    assert base is not None
    cover = float(base.surface.mean())
    if cover < th.surface_min:
        return UNKNOWN, f"plate not judged: the bed surface is out of view at Z {_fz(z)} ({cover:.1%} of the frame)", {}
    if plate_ref is not None and noplate_ref is not None:
        ds = difference(plate_ref.image, img, plate_ref.surface, th.level).frac
        dn = difference(noplate_ref.image, img, noplate_ref.surface, th.level).frac
        if abs(ds - dn) < th.plate_tie:  # equally far from both: a human must look
            return UNKNOWN, (f"plate unclear: bed surface {ds:.0%} off the with-plate reference, "
                             f"{dn:.0%} off the bare-bed one"), {"surface_vs_plate": ds, "surface_vs_noplate": dn}
        plate = MISSING if dn < ds else PRESENT
        return plate, (f"bed surface {ds:.0%} off the with-plate reference, {dn:.0%} off the bare-bed one"), \
            {"surface_vs_plate": ds, "surface_vs_noplate": dn}
    if plate_ref is not None:
        ds = difference(plate_ref.image, img, plate_ref.surface, th.level).frac
        if ds >= th.plate_missing_min:
            return MISSING, (f"{ds:.0%} of the bed surface differs from the with-plate reference "
                             "(plate off, or the bed covered)"), {"surface_vs_plate": ds}
        return PRESENT, f"bed surface {ds:.0%} off the with-plate reference", {"surface_vs_plate": ds}
    assert noplate_ref is not None
    dn = difference(noplate_ref.image, img, noplate_ref.surface, th.level).frac
    if dn >= th.plate_missing_min:
        return PRESENT, (f"{dn:.0%} of the bed surface differs from the bare-bed reference "
                         "(plate on, or the bed covered)"), {"surface_vs_noplate": dn}
    return MISSING, f"bed surface matches the bare-bed reference ({dn:.0%} off)", {"surface_vs_noplate": dn}


def classify(img: np.ndarray, refs: list[Reference], z: float | None, printer: str,
             th: Thresholds = TH, match: ZMatch | None = None) -> Verdict:
    import cv2
    import numpy as np

    match = match or ZMatch()
    hidden = out_of_view(match, z, th)
    if hidden is not None:  # before the references: no reference can show what the camera cannot see
        return Verdict(UNKNOWN, UNKNOWN, f"{hidden}: the camera cannot tell whether it is clear",
                       {"bed_in_view": match.in_view(z) or 0.0})
    plate_ref, noplate_ref = nearest(refs, "plate", z, match), nearest(refs, "noplate", z, match)
    if plate_ref is not None and noplate_ref is not None and noplate_ref.image.shape != plate_ref.image.shape:
        noplate_ref = None  # taken at another resolution: judge against the with-plate one only
    if plate_ref is None and noplate_ref is None:
        have = ", ".join(f"{r.kind} at Z {_fz(r.z)}" for r in refs) or "none"
        return Verdict(UNKNOWN, UNKNOWN, f"no empty-bed reference at Z {_fz(z)} (have: {have}); with the bed "
                       f"clear and the plate on: 3d printer bed reference {printer} --capture")
    base = plate_ref or noplate_ref
    assert base is not None
    if img.shape != base.image.shape:
        return Verdict(UNKNOWN, UNKNOWN, f"frame is {img.shape[1]}x{img.shape[0]} but the reference "
                       f"{base.path.name} is {base.image.shape[1]}x{base.image.shape[0]} (camera changed?): "
                       f"capture it again: 3d printer bed reference {printer} --capture")
    plate, plate_why, metrics = _judge_plate(img, plate_ref, noplate_ref, z, th)
    ref = noplate_ref if plate == MISSING else plate_ref
    if ref is None:
        why = ("plate missing and no bare-bed reference to judge objects against" if plate == MISSING
               else "only a bare-bed (--no-plate) reference at this Z")
        return Verdict(UNKNOWN, plate, f"{why}; {plate_why}", metrics)
    # the outer band of the ROI is where a slightly different plate edge or a detrend border
    # shows up; parts are judged inside it
    inner = cv2.erode(ref.volume.astype(np.uint8), np.ones((2 * th.edge_px + 1,) * 2, np.uint8)) > 0
    if inner.sum() < 100:  # a degenerate ROI inspects nothing: never call that clear
        return Verdict(UNKNOWN, plate, f"the bed ROI of {ref.path.name} is empty or tiny ({int(inner.sum())} px): "
                       f"fix watch.bed.roi and capture the reference again; {plate_why}", metrics)
    d = difference(ref.image, img, inner, th.level, th.detrend_px)
    blob_px = d.blob * int(inner.sum())
    metrics.update(changed=d.frac, blob=d.blob, blob_px=blob_px)
    seen = (f"{d.frac:.1%} of the bed view differs from the empty-bed reference "
            f"(largest region {blob_px:.0f} px of {PROC_W}-px frame)")
    in_view = match.in_view(z)
    if in_view is not None:
        metrics["bed_in_view"] = in_view
        if in_view < 0.5:  # e.g. the U1 at Z 200: the front of the bed and what stands tall
            seen += f"; the camera sees {in_view:.0%} of the bed at this Z, low parts at the back may not show"
    if blob_px >= th.occupied_blob_px or d.frac >= th.occupied_min:
        bed = OCCUPIED
    elif blob_px < th.clear_blob_px and d.frac <= th.clear_max:
        bed = CLEAR
    else:
        bed, seen = UNKNOWN, f"{seen}: a small part or a shadow? look at the frame"
    return Verdict(bed, plate, f"{seen}; {plate_why}", metrics, d.mask, inner)


# ---- the change gate ------------------------------------------------------------------------

class Monitor:
    """Decides WHEN to classify: after a change has settled, and once at start (pure: feed
    frames, get a Verdict or None; `classified` counts classifier runs)."""

    def __init__(self, classify_fn: Callable[[Any, float | None], Verdict], th: Thresholds = TH,
                 z_tol: float = 0.5) -> None:
        self.classify_fn = classify_fn
        self.th = th
        self.z_tol = z_tol
        self.prev: Any = None
        self.z: float | None = None
        self.static = 0
        self.dirty = True
        self.classified = 0

    def reset(self) -> None:
        """The printer is printing: forget the scene, judge again after the job."""
        self.prev, self.static, self.dirty = None, 0, True

    def invalidate(self) -> None:
        """References changed: judge again at the next settled frame."""
        self.dirty = True

    def feed(self, img: np.ndarray, z: float | None, roi: Any = None) -> Verdict | None:
        import numpy as np

        moved_z = (z is None) != (self.z is None) or (z is not None and self.z is not None
                                                      and abs(z - self.z) > self.z_tol)
        if self.prev is None or self.prev.shape != img.shape or moved_z:
            changed = True
        else:
            mask = roi if roi is not None and roi.shape == img.shape[:2] else np.ones(img.shape[:2], bool)
            changed = difference(self.prev, img, mask, self.th.level).frac >= self.th.change_min
        self.prev, self.z = img, z
        if changed:
            self.static, self.dirty = 0, True
            return None
        self.static += 1
        if self.dirty and self.static >= self.th.settle_frames:
            self.dirty = False
            self.classified += 1
            return self.classify_fn(img, z)
        return None


# ---- printer + camera -----------------------------------------------------------------------

def read_printer(device: Device) -> tuple[str, float | None]:
    """(normalized state, toolhead Z) — one read-only Moonraker query."""
    from printer_ops.base import UNKNOWN as STATE_UNKNOWN
    from printer_ops.base import PrinterCommError, http_json
    from printer_ops.moonraker import _STATE

    url = (f"http://{device.host}:{device.port}/printer/objects/query"
           "?print_stats=state&toolhead=position&webhooks=state")
    try:
        body = http_json("GET", url, timeout=10.0)
    except (OSError, ValueError) as exc:
        raise PrinterCommError(f"{device.name}: printer state unreadable: {exc}", command="printer bed") from exc
    status = (body.get("result") or {}).get("status") if isinstance(body, dict) else None
    if not isinstance(status, dict):
        raise PrinterCommError(f"{device.name}: no status in {str(body)[:200]}", command="printer bed")
    if (status.get("webhooks") or {}).get("state", "ready") != "ready":
        return STATE_UNKNOWN, None
    raw = str((status.get("print_stats") or {}).get("state", ""))
    pos = (status.get("toolhead") or {}).get("position")
    z = float(pos[2]) if isinstance(pos, list) and len(pos) > 2 and isinstance(pos[2], (int, float)) else None
    if z is not None and not math.isfinite(z):
        z = None  # a NaN Z would slip past every `z < min` guard
    return _STATE.get(raw, STATE_UNKNOWN), z


def camera_for(device: Device) -> Any:
    """The same snapshot source as `3d printer vision` (printer_ops.camera: `watch.vision.camera`,
    else the printer model's default camera); a printer without one is refused."""
    from errors import UsageError
    from printer_ops.camera import open_camera, require_camera

    if device.adapter != "moonraker":
        raise UsageError(f"{device.name}: no camera or Z access for adapter {device.adapter}", command="printer bed",
                         remediation=["The bed check needs a Moonraker printer (adapter: moonraker)."])
    return open_camera(device, require_camera(device, "printer bed"))


def grab(camera: Any) -> bytes:
    camera.keepalive()
    return bytes(camera.grab().jpeg)


# ---- evidence -------------------------------------------------------------------------------

def save_evidence(printer: str, jpeg: bytes, verdict: Verdict, label: str, now: float, keep: int = 100) -> str:
    """The full frame with the ROI outlined (cyan) and the changed pixels (red); returns the path."""
    import cv2
    import numpy as np

    out_dir = evidence_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    path = out_dir / f"{printer}-{stamp}-{label}.jpg"
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        path.write_bytes(jpeg)
        return str(path)
    h, w = img.shape[:2]
    if verdict.changed is not None:
        red = cv2.resize(verdict.changed.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST) > 0
        img[red] = (0.45 * img[red] + np.array([0, 0, 140])).astype(np.uint8)
    if verdict.roi is not None:
        roi = cv2.resize(verdict.roi.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST)
        contours, _ = cv2.findContours(roi, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(img, contours, -1, (255, 255, 0), max(1, w // 640))
    cv2.putText(img, f"{verdict.bed} / plate {verdict.plate}", (12, 36), cv2.FONT_HERSHEY_SIMPLEX,
                max(0.6, w / 1600), (0, 255, 255), 2)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    path.write_bytes(buf.tobytes() if ok else jpeg)
    own = re.compile(rf"{re.escape(printer)}-\d{{8}}T\d{{6}}Z-")  # not another printer named "{printer}-…"
    for old in sorted(p for p in out_dir.glob(f"{printer}-*.jpg") if own.match(p.name))[:-keep]:
        old.unlink(missing_ok=True)
    return str(path)


# ---- monitor --------------------------------------------------------------------------------

_TEXT = {
    "bed_clear": "{p}: the bed is clear (camera, Z {z}) — the parts were taken off; ready for the next job",
    "bed_occupied": "{p}: objects on the bed (camera, Z {z}): {why}",
    "plate_missing": "{p}: build plate missing? (camera, Z {z}): {why}",
    "plate_present": "{p}: build plate is back on the bed (camera, Z {z})",
    "bed_unknown": "{p}: cannot determine the bed state after the job (camera, Z {z}): {why}",
}


def _emit(out: Any, obj: dict[str, Any]) -> None:
    out.write(json.dumps(obj, default=str) + "\n")
    out.flush()


def _state_doc(printer: str, v: Verdict, z: float | None, now: float, frame: str | None,
               prev: dict[str, Any]) -> dict[str, Any]:
    doc = {k: prev[k] for k in ("last_ack", "ack") if k in prev}
    doc.update(printer=printer, bed=v.bed, plate=v.plate, z=z, ts=now, checked=now, reason=v.reason,
               metrics={k: round(x, 4) for k, x in v.metrics.items()}, frame=frame)
    return doc


@contextmanager
def _flock(path: Path) -> Iterator[None]:
    """Blocking exclusive lock on `path` (created) for the duration of the block."""
    import fcntl

    with open(path, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


@contextmanager
def _monitor_lock(name: str) -> Iterator[None]:
    """One bed monitor per printer: a second one (another `printer watch`, a manual `bed
    monitor`) would double the clear ack and overwrite the first one's verdicts."""
    import fcntl

    from errors import UsageError

    path = state_path(name).with_suffix(".monitor.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UsageError(f"{name}: a bed monitor for this printer is already running", command="printer bed",
                             remediation=["`3d printer watch` runs it when watch.bed.enabled is set; "
                                          "read its verdict with `3d printer bed status NAME --max-age 60`."]) from None
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _touch_checked(name: str, prev: dict[str, Any], now: float, last_write: float) -> float:
    """A new frame shows the scene unchanged since the verdict: still current (at most every 10 s)."""
    if now - last_write >= 10.0 and prev.get("bed") not in (None, PRINTING):
        prev["checked"] = now
        write_state(name, prev)
        return now
    return last_write


def run_monitor(device: Device, cfg: BedConfig, **kw: Any) -> Monitor:
    """The watch child's loop (see CHEAP / OUTPUT above), at most one per printer."""
    with _monitor_lock(device.name):
        return _run_monitor(device, cfg, **kw)


def _run_monitor(device: Device, cfg: BedConfig, *, duration: float | None = None, out: Any = sys.stdout,
                camera: Any = None, printer_fn: Callable[[Device], tuple[str, float | None]] = read_printer,
                ack_fn: Callable[..., tuple[bool, str]] = clear_ack, th: Thresholds | None = None,
                clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                busy_interval: float = 10.0) -> Monitor:
    from printer_ops.base import PrinterCommError

    name = device.name
    th = thresholds_for(cfg) if th is None else th
    camera = camera if camera is not None else camera_for(device)
    store = RefStore(name)
    prev = read_state(name) or {}
    known_bed, known_plate = prev.get("known_bed", prev.get("bed")), prev.get("known_plate", prev.get("plate"))
    refs_sig = store.signature()
    match = z_match(device, cfg)
    mon = Monitor(lambda img, z: classify(img, store.load(), z, name, th, match), th, cfg.z_tol_mm)
    last_jpeg = b""
    last_img: Any = None
    last_write = 0.0
    last_err = ""
    end = None if duration is None else clock() + duration
    _log(f"{name}: watching the bed every {cfg.interval_s:g} s while the printer is not printing "
         f"(references: {store.dir}; last verdict: {prev.get('bed', 'none')})")
    while end is None or clock() < end:
        t0 = clock()
        try:
            state, z = printer_fn(device)
            if state in ACTIVE:
                if known_bed != PRINTING:
                    known_bed = PRINTING
                    prev = {**prev, "printer": name, "bed": PRINTING, "known_bed": PRINTING, "z": z, "ts": t0,
                            "checked": t0, "reason": f"printer is {state}", "frame": None}
                    write_state(name, prev)
                    _emit(out, {"state": {"bed": PRINTING, "plate": known_plate, "z": z}})
                mon.reset()
                last_jpeg, last_img = b"", None
                sleep(busy_interval)
                continue
            jpeg = grab(camera)
            last_err = ""
        except PrinterCommError as exc:
            if exc.message != last_err:
                _log(exc.message)
                last_err = exc.message
            sleep(max(cfg.interval_s, 5.0))
            continue
        sig = store.signature()
        if sig != refs_sig:
            refs_sig = sig
            mon.invalidate()
        # identical bytes (a static or frozen snapshot) still count as unchanged frames while a
        # verdict is pending, so the monitor settles; the cached decode keeps that cheap
        jpeg_new = jpeg != last_jpeg
        if jpeg_new or mon.dirty:
            ref = nearest(store.load(), "plate", z, match) or nearest(store.load(), "noplate", z, match)
            if jpeg_new:
                last_jpeg = jpeg
                try:
                    last_img = decode_small(jpeg)
                except ValueError as exc:
                    _log(f"{name}: {exc}")
                    last_img = None
            img = last_img
            verdict = mon.feed(img, z, ref.volume if ref is not None else None) if img is not None else None
            now = clock()
            if verdict is not None:
                was_occupied = known_bed == OCCUPIED
                kinds = transitions(known_bed, known_plate, verdict.bed, verdict.plate)
                frame = save_evidence(name, jpeg, verdict, kinds[0] if kinds else verdict.bed, now) if kinds else None
                # bed_unknown (right after a job) also leaves "printing": it is said once per job,
                # and a later clear/occupied verdict is news again
                if verdict.bed in (CLEAR, OCCUPIED) or "bed_unknown" in kinds:
                    known_bed = verdict.bed
                if verdict.plate in (PRESENT, MISSING):
                    known_plate = verdict.plate
                prev = _state_doc(name, verdict, z, now, frame, prev)
                prev.update(known_bed=known_bed, known_plate=known_plate)
                write_state(name, prev)
                last_write = now
                _log(f"{name}: bed {verdict.bed}, plate {verdict.plate} at Z {_fz(z)}: {verdict.reason}")
                for kind in kinds:
                    why = verdict.reason
                    _emit(out, {"event": {
                        "kind": kind, "ts": now, "key": f"{name}:{kind}:{int(now)}",
                        "text": _TEXT[kind].format(p=name, z=_fz(z), why=why),
                        "data": {"bed": verdict.bed, "plate": verdict.plate, "z": z, "reason": why,
                                 "metrics": prev["metrics"], "frame": frame}}})
                _emit(out, {"state": {"bed": verdict.bed, "plate": verdict.plate, "z": z}})
                # the ack says "the parts were taken off": not on a first-ever clear or right after a job
                if "bed_clear" in kinds and cfg.clear_ack is not None and was_occupied:
                    try:
                        sent, what = ack_fn(device, cfg.clear_ack, z=z, last_ack=prev.get("last_ack"), now=now)
                    except Exception as exc:  # a failed ack never stops the monitor
                        sent, what = False, f"failed: {exc}"
                    _log(f"{name}: clear ack {what}")
                    prev["ack"] = what
                    if sent:
                        prev["last_ack"] = now
                    write_state(name, prev)
            elif not mon.dirty and jpeg_new:
                last_write = _touch_checked(name, prev, now, last_write)
        # byte-identical frames never refresh `checked`: a frozen camera must age out of --max-age
        sleep(max(0.0, cfg.interval_s - (clock() - t0)))
    return mon


# ---- one-shot status and references ---------------------------------------------------------

FROZEN_CHECK_S = 6.0  # one_shot waits this long for a new frame (the U1 rewrites monitor.jpg every ~4 s)


def one_shot(device: Device, cfg: BedConfig, *, max_age: float | None, camera: Any = None,
             printer_fn: Callable[[Device], tuple[str, float | None]] = read_printer,
             th: Thresholds | None = None, now: float | None = None,
             sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """The bed now: the monitor's last verdict when it is fresher than `max_age` at this Z,
    else one fresh camera frame classified here (never written to the monitor's state)."""
    th = thresholds_for(cfg) if th is None else th
    now = time.time() if now is None else now
    state, z = printer_fn(device)
    if state in ACTIVE:
        return {"printer": device.name, "bed": PRINTING, "plate": UNKNOWN, "z": z, "source": "printer",
                "age_s": 0.0, "reason": f"printer is {state}: the bed belongs to the job", "frame": None}
    last = read_state(device.name)
    match = z_match(device, cfg)
    if (max_age is not None and last and isinstance(last.get("checked"), (int, float))
            and now - float(last["checked"]) <= max_age and last.get("bed") not in (None, PRINTING)
            and isinstance(last.get("z"), (int, float)) and match(float(last["z"]), z)):
        return {**last, "source": "watch", "age_s": round(now - float(last["checked"]), 1)}
    camera = camera if camera is not None else camera_for(device)
    first = jpeg = grab(camera)
    waited = 0.0
    while jpeg == first and waited < FROZEN_CHECK_S:  # a live camera never repeats a frame byte for byte
        sleep(1.0)
        waited += 1.0
        jpeg = grab(camera)  # the newer frame is the one judged
    frozen = jpeg == first
    try:
        img = decode_small(jpeg)
    except ValueError as exc:
        from printer_ops.base import PrinterCommError

        raise PrinterCommError(f"{device.name}: camera frame unusable: {exc}", command="printer bed") from exc
    v = classify(img, RefStore(device.name).load(), z, device.name, th, match)
    # a possibly stale frame may still show parts or a missing plate (a FAIL), never a clear bed
    if frozen and v.bed == CLEAR:
        v = Verdict(UNKNOWN, v.plate if v.plate == MISSING else UNKNOWN,
                    f"the camera returned the same frame over {FROZEN_CHECK_S:g} s: frozen camera? "
                    f"(`3d printer vision NAME --once` to check); the stale frame looked clear", v.metrics,
                    v.changed, v.roi)
    frame = save_evidence(device.name, jpeg, v, f"status-{v.bed}", now)
    return {"printer": device.name, "bed": v.bed, "plate": v.plate, "z": z, "source": "camera now", "age_s": 0.0,
            "reason": v.reason, "metrics": {k: round(x, 4) for k, x in v.metrics.items()}, "frame": frame}


def capture_reference(device: Device, cfg: BedConfig, *, plate: bool, camera: Any = None,
                      printer_fn: Callable[[Device], tuple[str, float | None]] = read_printer,
                      now: float | None = None, frame: bytes | None = None,
                      frame_z: float | None = None) -> dict[str, Any]:
    """Store the current camera frame (or `frame`, a saved JPEG of the empty bed taken at gcode
    Z `frame_z`, e.g. an earlier snapshot) as the printer's reference at that Z."""
    from errors import UsageError

    now = time.time() if now is None else now
    if frame is not None:
        if frame_z is None or not math.isfinite(frame_z):
            raise UsageError("a reference from a file needs the Z it was taken at (--z, a finite number)",
                             command="printer bed")
        z: float | None = frame_z
        jpeg = frame
    else:
        state, z = printer_fn(device)
        if state not in QUIET_STATES:
            raise UsageError(f"{device.name} is {state}: a reference needs the empty bed of an idle printer",
                             command="printer bed",
                             remediation=["Capture when `3d printer status` shows idle, complete or cancelled."])
        if z is None:
            raise UsageError(f"{device.name}: toolhead Z unknown (Klipper not ready?)", command="printer bed",
                             remediation=["Retry when `3d printer status` reads the printer."])
        jpeg = grab(camera if camera is not None else camera_for(device))
    assert z is not None
    match = z_match(device, cfg)
    hidden = out_of_view(match, z, thresholds_for(cfg))
    if hidden is not None:  # a reference there would read every low part as a clear bed
        raise UsageError(f"{device.name}: {hidden}; a reference there cannot show parts on the bed",
                         command="printer bed",
                         remediation=["Capture with the bed at a Z where the camera sees it: the Z the printer rests at "
                                      "after a job or a home (`3d printer bed reference NAME` lists the references)."])
    try:
        img = decode_small(jpeg)
    except ValueError as exc:
        raise UsageError(f"{'the --from frame' if frame is not None else 'the camera frame'} is unusable: {exc}",
                         command="printer bed") from exc
    volume, surface, source = build_masks(device, cfg, z, img.shape[:2])
    kind = "plate" if plate else "noplate"
    d = refs_dir(device.name)
    d.mkdir(parents=True, exist_ok=True)
    with _flock(d / ".capture.lock"):  # two captures must not interleave their sibling files
        return _store_reference(d, kind, z, jpeg, img, volume, surface, source, match, now)


def _store_reference(d: Path, kind: str, z: float, jpeg: bytes, img: Any, volume: Any, surface: Any, source: str,
                     match: ZMatch, now: float) -> dict[str, Any]:
    import cv2

    for old in d.glob(f"{kind}-z*.json"):  # one reference per kind and view (ZMatch)
        try:
            oz = float(json.loads(old.read_text(encoding="utf-8")).get("z"))
        except (OSError, ValueError, TypeError):
            continue
        if match(oz, z):
            for p in d.glob(old.stem + ".*"):
                p.unlink(missing_ok=True)
    meta_path = d / f"{kind}-z{z:.1f}.json"
    _sibling(meta_path, ".jpg").write_bytes(jpeg)
    cv2.imwrite(str(_sibling(meta_path, ".volume.png")), volume.astype("uint8") * 255)
    cv2.imwrite(str(_sibling(meta_path, ".surface.png")), surface.astype("uint8") * 255)
    meta = {"kind": kind, "z": z, "ts": now, "roi_source": source, "width": img.shape[1], "height": img.shape[0],
            "volume_share": round(float(volume.mean()), 4), "surface_share": round(float(surface.mean()), 4)}
    meta_path.write_text(json.dumps(meta, indent=1), encoding="utf-8")  # last: RefStore keys on the .json
    return {**meta, "path": str(_sibling(meta_path, ".jpg"))}


def list_references(printer: str) -> list[dict[str, Any]]:
    out = []
    for p in sorted(refs_dir(printer).glob("*.json")):
        try:
            meta = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(meta, dict) or meta.get("kind") not in ("plate", "noplate") \
                    or not math.isfinite(float(meta["z"])):
                raise ValueError("not a reference (kind, finite z)")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            _log(f"skipping reference {p}: {exc}")
            continue
        out.append({**meta, "path": str(_sibling(p, ".jpg"))})
    return out


# ---- CLI ------------------------------------------------------------------------------------

USAGE = """3d printer bed status NAME [--json] [--max-age S]
3d printer bed reference NAME [--capture [--no-plate] [--from FRAME.jpg --z Z]] [--json]
3d printer bed monitor NAME [--duration S]
  Bed state from the printer's camera (issue #303): is the bed clear, is the build plate on.
  Needs one reference frame of the EMPTY bed WITH the plate per printer and resting Z, taken
  where the printer rests after a job (U1: Z 200, and Z 10 after a G28; K1 SE: half of Z
  max) — the bed moves in Z. With a camera calibration a reference stands for any Z where the
  bed looks the same (moved < 1.5 px), and where the bed is out of the camera's view (U1 Z 275
  after a home) the state is unknown and no reference is taken; without one (watch.bed.roi)
  a reference is used only within watch.bed.z_tol_mm (default 0.5 mm) of the current Z.

  status      the bed now: clear | occupied | unknown, plate present | missing | unknown,
              with the reason and an evidence frame (~/.cache/3d-cli/vision/bed/). With
              --max-age S the `3d printer watch` monitor's last verdict is used when it was
              confirmed within S seconds at the same Z; otherwise one fresh frame is judged
              (a frozen camera, no new frame within 6 s, never reads as a clear bed).
  reference   list the references; --capture takes one now (printer not printing, bed clear,
              plate on); --capture --no-plate takes the bare-bed one (plate off), which makes
              plate detection reliable. --from FRAME.jpg --z Z stores a saved frame of the
              empty bed taken at gcode Z instead of grabbing one. Stored in
              ~/.local/share/3d-cli/bed/NAME/.
  monitor     the loop `3d printer watch` runs per printer with `watch.bed.enabled: true`:
              JSON lines on stdout (events bed_clear, bed_occupied, plate_present,
              plate_missing; the warning bed_unknown when the first verdict after a job
              cannot tell). Nothing is grabbed while the printer prints.

Config (fleet/printers.yaml or printer-devices.yaml, `<name>.watch.bed`):
  enabled: true            run the monitor in `3d printer watch` and the `bed` start gate
  roi: [[x, y], ...]       bed polygon (0..1 of the frame) at the reference Z; default: the
                           camera calibration, else the whole frame
  interval_s, stale_s, volume_height_mm
  z_tol_mm                 reference Z tolerance for a watch.bed.roi (uncalibrated) camera
  thresholds: {clear_max: 0.05, level: 30}   per-printer overrides of the tuned pixel thresholds
                           (level, detrend_px, edge_px, clear_blob_px, clear_max, occupied_blob_px,
                           occupied_min, plate_missing_min, plate_tie, surface_min, change_min,
                           settle_frames, match_px, view_min)
  The camera is the printer's watch.vision.camera (a printer without one cannot enable the bed check).
  clear_ack: {enabled: true, command: STEPPER_BUZZ STEPPER=stepper_z, debounce_minutes: 10,
              min_z_mm: 10}   one motor sound after bed_clear (the command must be in
                           trusted_macros; only while the printer is idle/complete/cancelled)

Examples:
  3d printer bed reference u1 --capture
  3d printer bed status u1
  3d printer bed status k1se --json | jq .bed"""


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0 if argv else 1
    from printer_ops.devices import get_device

    ap = argparse.ArgumentParser(prog="3d printer bed", add_help=False)
    ap.add_argument("sub", choices=("status", "reference", "monitor"))
    ap.add_argument("name")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--max-age", type=float)
    ap.add_argument("--capture", action="store_true")
    ap.add_argument("--no-plate", action="store_true")
    ap.add_argument("--duration", type=float)
    ap.add_argument("--from", dest="from_file")
    ap.add_argument("--z", type=float)
    ap.add_argument("-h", "--help", action="store_true")
    args, rest = ap.parse_known_args(argv)
    if args.help:
        print(USAGE)
        return 0
    if rest:
        from errors import UsageError

        raise UsageError(f"unknown arguments for `printer bed {args.sub}`: {' '.join(rest)}", command="printer bed",
                         remediation=["See `3d printer bed --help`."])
    allowed = {"status": {"json", "max_age"}, "monitor": {"duration"},
               "reference": {"json", "capture"} | ({"no_plate", "from_file", "z"} if args.capture else set())}[args.sub]
    stray = [f"--{k.replace('_', '-')}" if k != "from_file" else "--from" for k, v in vars(args).items()
             if k not in {"sub", "name", "help"} | allowed and v is not None and v is not False]
    if stray:
        from errors import UsageError

        raise UsageError(f"`printer bed {args.sub}` does not take {', '.join(stray)}", command="printer bed",
                         remediation=["See `3d printer bed --help`."])
    device = get_device(args.name)
    cfg = bed_config(device)
    if args.sub in ("monitor", "status") or (args.sub == "reference" and args.capture and not args.from_file):
        camera_for(device)  # a printer without a camera is refused here, before any printer request
    if args.sub == "monitor":
        run_monitor(device, cfg, duration=args.duration)
        return 0
    if args.sub == "reference":
        if args.capture:
            frame = None
            if args.from_file:
                path = Path(args.from_file).expanduser()
                if not path.is_file():
                    from errors import InputNotFound

                    raise InputNotFound(str(path), command="printer bed")
                frame = path.read_bytes()
            ref = capture_reference(device, cfg, plate=not args.no_plate, frame=frame, frame_z=args.z)
            print(json.dumps(ref, indent=1) if args.json else
                  f"{ref['kind']} reference of {device.name} at Z {ref['z']:.1f}: {ref['path']}\n"
                  f"  ROI: {ref['roi_source']} ({ref['volume_share']:.1%} of the frame; "
                  f"bed surface {ref['surface_share']:.1%})")
            return 0
        refs = list_references(device.name)
        if args.json:
            print(json.dumps(refs, indent=1))
        elif not refs:
            print(f"no references for {device.name}; with the bed clear and the plate on: "
                  f"3d printer bed reference {device.name} --capture")
        else:
            for r in refs:
                stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(r.get("ts", 0))))
                print(f"{r['kind']:<8} Z {float(r['z']):6.1f}  {stamp}  {r['path']}  ROI: {r.get('roi_source')}")
        return 0
    v = one_shot(device, cfg, max_age=args.max_age)
    if args.json:
        print(json.dumps(v, indent=1, default=str))
    else:
        print(f"{device.name}: bed {v['bed']}, plate {v['plate']} (Z {_fz(v.get('z'))}; {v['source']}, "
              f"{v.get('age_s', 0):.0f} s ago)\n  {v.get('reason', '')}" + (f"\n  frame: {v['frame']}" if v.get("frame") else ""))
    return 0


if __name__ == "__main__":
    from errors import ThreeDError

    try:
        sys.exit(main(sys.argv[1:]))
    except ThreeDError as exc:
        print(exc.render(), file=sys.stderr)
        sys.exit(exc.exit_code)
    except KeyboardInterrupt:
        sys.exit(130)
