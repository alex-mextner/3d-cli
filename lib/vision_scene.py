"""vision_scene.py — what changed on the bed: foreign objects, moved/detached parts, stalled growth.

Pixel-side detectors of `3d printer vision` that need the camera calibration (vision_geometry.py)
to know where the bed and the expected part are in the picture at the current Z. They return
`printer_ops.vision.Finding`s; VisionMonitor turns them into edge-triggered events.

SceneWatch — "something appeared, vanished or moved on the bed". The U1 bed moves down with Z and
the toolhead sweeps the frame, so a single frame difference is useless. Instead:
  * every frame is shrunk to 320 px wide and converted to Lab, with the projected toolhead volume
    (from the reported XY, padded for the ~0.5 s status/frame skew) blanked out;
  * `current` = per-pixel median of the last 3 frames (30 s), `reference` = median of frames
    sampled once a minute that are 3-12 min old AND within `max_dz` of the current Z (so the bed
    has not visibly moved; new layers only add a sliver at the top band, which is excluded);
  * pixels whose colour moved by more than `diff` (Lab distance, after blurring) inside the bed
    area + expected part silhouette form regions; a region of at least `min_area` that persists
    for `persist` consecutive checks is a finding: mostly inside the silhouette -> `vision_part_moved`
    (detached, knocked over, shifted), else -> `vision_foreign_object` (tool, hand, debris, a
    blob that fell off). Once the change is older than the reference window it becomes the new
    normal and the finding clears (one event per episode).

GrowthWatch (experimental, info only) — "the part stopped growing": on the U1 the newest layer
always sits at the nozzle plane, so the top band of the expected silhouette (last `band_mm`) must
look like the part body just below it, not like the empty space just above it. When extrusion
stops (clog, runout, stripped filament) the bed keeps lowering and the band fills with
background. Reported as an info finding for an agent to look at; not validated on a real failure.
"""

from __future__ import annotations

import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

LIB = Path(__file__).resolve().parent
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from printer_ops.vision import INFO, WARNING, Finding  # noqa: E402

if TYPE_CHECKING:
    import numpy as np

WIDTH = 320  # working resolution: enough for a screwdriver on the bed, cheap to median


def shrink_lab(bgr: np.ndarray) -> np.ndarray:
    import cv2
    import numpy as np

    h, w = bgr.shape[:2]
    small = cv2.resize(bgr, (WIDTH, round(h * WIDTH / w)), interpolation=cv2.INTER_AREA)
    small = cv2.GaussianBlur(small, (5, 5), 0)
    return cv2.cvtColor(small, cv2.COLOR_BGR2LAB).astype(np.float32)


def shrink_mask(mask: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    import cv2

    return cv2.resize(mask, (shape[1], shape[0]), interpolation=cv2.INTER_NEAREST) > 0


def _median(stack: list[np.ndarray]) -> np.ndarray:
    import warnings

    import numpy as np

    with warnings.catch_warnings():
        warnings.simplefilter(
            "ignore", RuntimeWarning
        )  # all-NaN pixels (head) stay NaN
        return np.nanmedian(np.stack(stack), axis=0)


@dataclass(frozen=True)
class SceneParams:
    cur_len: int = 3
    ref_every_s: float = 60.0
    ref_min_age_s: float = 180.0
    ref_max_age_s: float = 720.0
    ref_min_frames: int = 4
    max_dz: float = 0.8  # mm of bed travel between reference and now
    diff: float = 28.0  # Lab distance
    min_area: float = 0.004  # share of the working image (~230 px at 320x180)
    persist: int = 3
    part_share: float = 0.5  # region inside the expected silhouette -> part moved


@dataclass
class _Sample:
    t: float
    z: float
    lab: Any  # float32 HxWx3 with NaN where the head was


@dataclass
class SceneWatch:
    params: SceneParams = field(default_factory=SceneParams)
    recent: deque[_Sample] = field(default_factory=lambda: deque(maxlen=3))
    history: deque[_Sample] = field(default_factory=lambda: deque(maxlen=16))
    streak: int = 0
    last_regions: list[tuple[int, int, int, int]] = field(default_factory=list)

    def reset(self) -> None:
        self.recent.clear()
        self.history.clear()
        self.streak = 0
        self.last_regions = []

    def update(
        self,
        bgr: np.ndarray,
        z: float,
        now: float,
        area: np.ndarray,
        silhouette: np.ndarray,
        exclude: np.ndarray | None = None,
    ) -> list[Finding]:
        """`area`: where a change matters (bed + expected part, full-res uint8); `silhouette`:
        the expected part; `exclude`: the toolhead volume and the growing top band."""
        import numpy as np

        prm = self.params
        lab = shrink_lab(bgr)
        hw = lab.shape[:2]
        if exclude is not None:
            lab[shrink_mask(exclude, hw)] = np.nan
        sample = _Sample(now, z, lab)
        self.recent.append(sample)
        if not self.history or now - self.history[-1].t >= prm.ref_every_s:
            self.history.append(sample)
        ref_frames = [
            s.lab
            for s in self.history
            if prm.ref_min_age_s <= now - s.t <= prm.ref_max_age_s
            and abs(s.z - z) <= prm.max_dz
        ]
        if len(self.recent) < prm.cur_len or len(ref_frames) < prm.ref_min_frames:
            self.streak = 0
            return []
        cur = _median([s.lab for s in self.recent])
        ref = _median(ref_frames)
        dist = np.sqrt(np.nansum((cur - ref) ** 2, axis=2))
        valid = ~(np.isnan(cur).any(axis=2) | np.isnan(ref).any(axis=2))
        where = shrink_mask(area, hw) & valid
        changed = (dist > prm.diff) & where
        regions = _regions(changed, prm.min_area * hw[0] * hw[1])
        if not regions:
            self.streak, self.last_regions = 0, []
            return []
        boxes = [r[0] for r in regions]
        same = any(_iou(a, b) > 0.3 for a in boxes for b in self.last_regions)
        self.streak = self.streak + 1 if same or not self.last_regions else 1
        self.last_regions = boxes
        if self.streak < prm.persist:
            return []
        sil = shrink_mask(silhouette, hw)
        findings = []
        for (x, y, w, h), comp in regions:
            inside = float((comp & sil).sum()) / max(1, int(comp.sum()))
            share = float(comp.sum()) / float(max(1, int(where.sum())))
            kind = (
                "vision_part_moved"
                if inside >= prm.part_share
                else "vision_foreign_object"
            )
            what = (
                "the printed part looks different from a few minutes ago (detached, "
                "knocked over or shifted?)"
                if kind == "vision_part_moved"
                else "something new on the bed outside the part (tool, debris, a fallen blob?)"
            )
            findings.append(
                Finding(
                    kind,
                    WARNING,
                    f"{what}; changed region {share * 100:.1f}% of the bed area",
                    {
                        "region": [
                            round(x / hw[1], 4),
                            round(y / hw[0], 4),
                            round((x + w) / hw[1], 4),
                            round((y + h) / hw[0], 4),
                        ],
                        "bed_share": round(share, 4),
                        "inside_part": round(inside, 3),
                    },
                )
            )
        best = _strongest_per_kind(findings)
        for f in best:
            f.image = evidence(ref, cur, f.data["region"])
        return best


def evidence(ref: np.ndarray, cur: np.ndarray, region: list[float]) -> bytes:
    """JPEG: the reference (minutes ago) | now, both with the changed region outlined; grey where
    the toolhead was masked out."""
    import cv2
    import numpy as np

    tiles = []
    for lab, title in ((ref, "minutes ago"), (cur, "now")):
        img = cv2.cvtColor(
            np.nan_to_num(lab, nan=128.0).clip(0, 255).astype(np.uint8),
            cv2.COLOR_LAB2BGR,
        )
        img = cv2.resize(img, (img.shape[1] * 2, img.shape[0] * 2))
        h, w = img.shape[:2]
        x1, y1, x2, y2 = region
        cv2.rectangle(
            img, (int(x1 * w), int(y1 * h)), (int(x2 * w), int(y2 * h)), (0, 0, 255), 2
        )
        cv2.putText(
            img, title, (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2
        )
        tiles.append(img)
    ok, buf = cv2.imencode(".jpg", np.hstack(tiles), [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes() if ok else b""


def _regions(
    changed: np.ndarray, min_px: float
) -> list[tuple[tuple[int, int, int, int], np.ndarray]]:
    import cv2
    import numpy as np

    m = cv2.morphologyEx(
        changed.astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8)
    )
    n, lab, st, _ = cv2.connectedComponentsWithStats(m)
    return [
        ((int(st[i, 0]), int(st[i, 1]), int(st[i, 2]), int(st[i, 3])), lab == i)
        for i in range(1, n)
        if st[i, 4] >= min_px
    ]


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax2, ay2, bx2, by2 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw = max(0, min(ax2, bx2) - max(a[0], b[0]))
    ih = max(0, min(ay2, by2) - max(a[1], b[1]))
    inter = iw * ih
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union else 0.0


def _strongest_per_kind(findings: list[Finding]) -> list[Finding]:
    best: dict[str, Finding] = {}
    for f in findings:
        if f.kind not in best or f.data["bed_share"] > best[f.kind].data["bed_share"]:
            best[f.kind] = f
    return list(best.values())


@dataclass(frozen=True)
class GrowthParams:
    every_mm: float = 1.0  # one check per mm of Z
    min_band_px: int = 150  # at full resolution: too little visible band -> no verdict
    stalled: float = 0.35  # band share that looks like the part below it
    persist: int = 3  # checks (mm) in a row


@dataclass
class GrowthWatch:
    params: GrowthParams = field(default_factory=GrowthParams)
    last_z: float | None = None
    streak: int = 0
    history: list[tuple[float, float]] = field(default_factory=list)  # (z, part share)

    def reset(self) -> None:
        self.last_z, self.streak, self.history = None, 0, []

    def update(
        self,
        bgr: np.ndarray,
        z: float,
        band: np.ndarray,
        below: np.ndarray,
        above: np.ndarray,
    ) -> list[Finding]:
        """`band`: top band of the expected silhouette (last few mm); `below`: the part body under
        it; `above`: the empty volume over it (all full-res uint8 masks, toolhead removed)."""
        import cv2
        import numpy as np

        prm = self.params
        if self.last_z is not None and z - self.last_z < prm.every_mm:
            return []
        self.last_z = z
        b, lo, hi = band > 0, below > 0, above > 0
        if (
            b.sum() < prm.min_band_px
            or lo.sum() < prm.min_band_px
            or hi.sum() < prm.min_band_px
        ):
            return []
        lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
        mu_lo = lab[lo].mean(axis=0)
        mu_hi = lab[hi].mean(axis=0)
        px = lab[b]
        part_like = np.linalg.norm(px - mu_lo, axis=1) < np.linalg.norm(
            px - mu_hi, axis=1
        )
        share = float(part_like.mean())
        self.history.append((round(z, 2), round(share, 3)))
        self.streak = self.streak + 1 if share < prm.stalled else 0
        if self.streak < prm.persist:
            return []
        return [
            Finding(
                "vision_no_growth",
                INFO,
                f"the top {prm.every_mm * prm.persist:.0f} mm of the expected part look empty "
                f"(only {share * 100:.0f}% of the band looks like the part below): under- or no "
                "extrusion? Experimental check, look at the frame",
                {"band_part_share": round(share, 3), "z": round(z, 2)},
            )
        ]
