"""vision_detach.py — "the part is no longer where the printer says it is": daylight under a part.

Tier (a) of `3d printer vision` (cheap OpenCV, no model): one geometric check per frame, no
reference frame from "a few minutes ago" (that is why vision_scene's part_moved cannot see a
part that lifts slowly: the lifted state becomes the reference after 12 min).

Idea. The camera calibration (vision_geometry.py) projects the part's footprint prism, bed to
the current layer, into the frame. From the low U1/K1-style chamber camera the near side wall of
that prism is a band between the base edge (on the plate) and the top edge (the newest layer).
While the print holds on the plate the band is part. When the part peels off (warped corner,
lifted slab, knocked off, dragged away) bare build plate shows in the band. So: learn the plate
colour from the bed pixels outside the part (the same frame, so exposure and white balance
cancel), and measure the share of the (eroded) wall band that has the plate colour:

  * `gap` >= `gap_abs`                                  -> warning (absolute: catches a part that
                                                           lifted before any baseline existed)
  * `gap` - lowest gap of this print >= `gap_rise`      -> warning (the part's own baseline: a
                                                           concave part whose convex-hull footprint
                                                           always shows some plate is not flagged)
  * the rise stays >= `gap_crit` for `persist_crit` checks -> critical

The check abstains (no finding, never a false alarm) when the plate cannot be told from the part:
too few plate pixels outside the part, a plate chroma below `min_chroma` (grey plate under a
cold light), or a volume too small once eroded by the calibration error (~25 px at 1920).
Never pauses anything: VisionMonitor turns the Finding into an event; printer guard only pauses on
confirmed spaghetti.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

LIB = Path(__file__).resolve().parent
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from printer_ops.vision import CRITICAL, WARNING, Finding  # noqa: E402

if TYPE_CHECKING:
    import numpy as np

WIDTH = 480  # working resolution
ERODE_PX_1920 = 25  # calibration residual near the camera: 9 px RMS, up to ~30 px
MIN_LIGHT = 15.0  # CIE L below this is shadow: hue is noise


@dataclass(frozen=True)
class DetachParams:
    min_z: float = 1.5  # below this the prism is a sliver the calibration error swallows
    min_body_share: float = 0.006  # wall band, share of the frame, after erosion
    min_ref_share: float = 0.01  # plate pixels outside the part, share of the frame
    min_chroma: float = 25.0  # CIE (a, b) distance from grey of the plate; below: abstain
    tol_floor: float = 14.0  # plate class: (a, b) within max(tol_floor, tol_ratio x chroma)
    tol_ratio: float = 0.35
    gap_abs: float = 0.25  # share of the part volume showing bare plate
    gap_floor: float = 0.08  # the relative rule never fires below this
    gap_rise: float = 0.12  # above the print's own lowest gap
    gap_crit: float = 0.25  # rise for the critical level
    persist: int = 3  # consecutive checks (frames) for a warning
    persist_crit: int = 12  # ... for critical


@dataclass
class DetachWatch:
    params: DetachParams = field(default_factory=DetachParams)
    base: float | None = None  # lowest smoothed gap seen in this print
    recent: list[float] = field(default_factory=list)
    streak: int = 0
    crit_streak: int = 0
    last_gap: float | None = None  # the latest measurement (None: abstained), for `--once`

    def reset(self) -> None:
        self.base = None
        self.last_gap = None
        self.recent = []
        self.streak = self.crit_streak = 0

    def update(
        self,
        bgr: np.ndarray,
        z: float,
        wall: np.ndarray,
        body: np.ndarray,
        bed: np.ndarray,
        exclude: np.ndarray | None = None,
    ) -> list[Finding]:
        """`wall`: the expected near side walls (vision_geometry.wall_mask); `body`: the part
        footprint prisms bed..z; `bed`: the build plate area at this Z; `exclude`: toolhead
        volume (all full-res uint8). -> a Finding while the condition holds."""
        gap = self.last_gap = self.measure(bgr, z, wall, body, bed, exclude)
        return self.judge(gap, z)

    def judge(self, gap: float | None, z: float | None, *, low_confidence: bool = False) -> list[Finding]:
        """The persistence rules on one measurement (`None` = abstained)."""
        if gap is None:
            self.streak = self.crit_streak = 0
            return []
        prm = self.params
        self.recent = (self.recent + [gap])[-3:]
        smooth = sorted(self.recent)[len(self.recent) // 2]  # median of the last three
        self.base = smooth if self.base is None else min(self.base, smooth)
        rise = smooth - self.base
        absolute = smooth >= prm.gap_abs
        relative = smooth >= prm.gap_floor and rise >= prm.gap_rise
        self.streak = self.streak + 1 if absolute or relative else 0
        self.crit_streak = self.crit_streak + 1 if rise >= prm.gap_crit else 0
        if self.streak < prm.persist:
            return []
        level = CRITICAL if self.crit_streak >= prm.persist_crit and not low_confidence else WARNING
        why = (
            f"{smooth * 100:.0f}% of the part's volume shows bare build plate"
            if absolute
            else f"bare build plate under the part grew from {self.base * 100:.0f}% "
            f"to {smooth * 100:.0f}%"
        )
        return [
            Finding(
                "vision_part_detached",
                level,
                f"the printed part looks detached from the plate: {why} "
                "(peeled/warped corner, lifted or knocked off?)"
                + (" [uncalibrated check, low confidence]" if low_confidence else "")
                + "; look at the frame and pause if it is real",
                {
                    "gap": round(smooth, 3),
                    "baseline": round(self.base, 3),
                    "rise": round(rise, 3),
                    **({"z": round(z, 2)} if z is not None else {}),
                    **({"confidence": "low"} if low_confidence else {}),
                },
            )
        ]

    def measure(
        self,
        bgr: np.ndarray,
        z: float,
        wall: np.ndarray,
        body: np.ndarray,
        bed: np.ndarray,
        exclude: np.ndarray | None = None,
    ) -> float | None:
        """Share (0..1) of the eroded wall band that has the plate colour; None = abstain."""
        import cv2
        import numpy as np

        prm = self.params
        if z < prm.min_z:
            return None
        light, ab, chroma = _planes(bgr)
        shape = light.shape

        def shrink(mask: np.ndarray) -> np.ndarray:
            return cv2.resize(mask, (shape[1], shape[0]), interpolation=cv2.INTER_NEAREST) > 0

        k = max(3, round(ERODE_PX_1920 * WIDTH / 1920)) | 1
        kernel = np.ones((k, k), np.uint8)
        inner = cv2.erode(shrink(wall).astype(np.uint8), kernel) > 0
        outer = cv2.dilate(shrink(body).astype(np.uint8), np.ones((3 * k, 3 * k), np.uint8)) > 0
        free = np.ones(shape, bool) if exclude is None else ~shrink(exclude)
        px = float(shape[0] * shape[1])
        inner &= free
        if inner.sum() < prm.min_body_share * px:
            return None
        ref = shrink(bed) & ~outer & free & (light >= MIN_LIGHT * 2)
        if ref.sum() < prm.min_ref_share * px:
            return None
        is_plate = _plate_map(light, ab, chroma, ref, prm)
        if is_plate is None:
            return None
        return float(is_plate[inner].sum()) / float(inner.sum())


def _planes(bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(lightness 0..100, (a, b) offsets from grey, chroma) at the working resolution."""
    import cv2
    import numpy as np

    h, w = bgr.shape[:2]
    small = cv2.resize(bgr, (WIDTH, round(h * WIDTH / w)), interpolation=cv2.INTER_AREA)
    lab = cv2.cvtColor(cv2.GaussianBlur(small, (5, 5), 0), cv2.COLOR_BGR2LAB)
    light = lab[..., 0].astype(np.float32) * 100.0 / 255.0
    ab = lab[..., 1:].astype(np.float32) - 128.0
    return light, ab, np.hypot(ab[..., 0], ab[..., 1])


def _plate_map(
    light: np.ndarray, ab: np.ndarray, chroma: np.ndarray, ref: np.ndarray, prm: DetachParams
) -> np.ndarray | None:
    """Pixels that have the plate's colour, the plate colour being the median over the `ref`
    pixels; None when that plate is too grey to be told from the part."""
    import numpy as np

    plate = np.median(ab[ref], axis=0)
    plate_c = float(np.hypot(*plate))
    if plate_c < prm.min_chroma:
        return None
    tol = max(prm.tol_floor, prm.tol_ratio * plate_c)
    near = np.hypot(*(ab - plate).transpose(2, 0, 1)) <= tol
    # same hue and a real share of the plate's saturation: grey shadow and white parts out
    return near & (chroma >= 0.5 * plate_c) & (light >= MIN_LIGHT)  # type: ignore[no-any-return]


@dataclass
class FixedBedDetach:
    """UNCALIBRATED fallback of the same idea, for a printer whose camera and bed do not move
    against each other (a gantry-moves-Z printer such as the Creality K1 SE: the plate sits at
    the same pixels at every Z) and that has no camera calibration, so no footprint prism.

    The footprint is LEARNT instead: a few frames `baseline_gap_s` apart at the start of the
    print; what is not plate colour inside `roi` in ALL of them is the part (the moving
    toolhead is not in all of them). From then on `gap` is the share of that baseline that
    shows bare plate; a part that peeled, shifted or was knocked off leaves plate where it was.
    Same persistence rules, always `low_confidence` (warning, never critical).

    UNVALIDATED on a real failure: there is no K1 frame of a detached part, only synthetic
    ones (tests/test_vision_detach.py). It is therefore opt-in (`watch.vision.fixed_bed`)."""

    roi: list[tuple[float, float]]  # the plate in the picture, normalised 0..1
    params: DetachParams = field(default_factory=DetachParams)
    baseline_frames: int = 6
    baseline_gap_s: float = 20.0
    judge: DetachWatch = field(init=False)
    part: Any = None  # bool array at the working resolution once the baseline is learnt
    _seen: list[Any] = field(default_factory=list)
    _last_t: float = float("-inf")
    last_gap: float | None = None

    def __post_init__(self) -> None:
        self.judge = DetachWatch(self.params)

    def reset(self) -> None:
        self.judge.reset()
        self.part = None
        self._seen = []
        self._last_t = float("-inf")
        self.last_gap = None

    def update(self, bgr: np.ndarray, now: float) -> list[Finding]:
        import cv2
        import numpy as np

        prm = self.params
        light, ab, chroma = _planes(bgr)
        shape = light.shape
        roi = np.zeros(shape, np.uint8)
        pts = np.array([[x * shape[1], y * shape[0]] for x, y in self.roi], np.int32)
        cv2.fillPoly(roi, [pts], 1)
        inside = roi > 0
        learning = self.part is None
        if learning and now - self._last_t < self.baseline_gap_s:
            return []
        px = float(shape[0] * shape[1])
        k = max(3, round(ERODE_PX_1920 * WIDTH / 1920)) | 1
        ref = inside & (light >= MIN_LIGHT * 2)
        if self.part is not None:
            grown = cv2.dilate(self.part.astype(np.uint8), np.ones((3 * k, 3 * k), np.uint8)) > 0
            ref = ref & ~grown
        if ref.sum() < prm.min_ref_share * px:
            return []
        is_plate = _plate_map(light, ab, chroma, ref, prm)
        if is_plate is None:
            self.last_gap = None
            return self.judge.judge(None, None, low_confidence=True)
        if learning:
            self._last_t = now
            self._seen.append(inside & ~is_plate & (light >= MIN_LIGHT))
            if len(self._seen) < self.baseline_frames:
                return []
            part = np.logical_and.reduce(self._seen)
            part = cv2.erode(part.astype(np.uint8), np.ones((k, k), np.uint8)) > 0
            self._seen = []
            if part.sum() < prm.min_body_share * px:  # nothing printed yet: learn again later
                return []
            self.part = part
            return []
        gap = float(is_plate[self.part].sum()) / float(self.part.sum())
        self.last_gap = gap
        return self.judge.judge(gap, None, low_confidence=True)
