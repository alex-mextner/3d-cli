"""bed_pose.py — has the camera moved since the reference frame was taken, and by how much (bed_state.py).

A reference stands for the picture the camera sees. The K1 SE's camera rides with the toolhead, so after a
job (or a home, or a Z move) the same empty plate is seen from another place: the whole picture, background
included, is shifted and zoomed by tens of pixels. Compared pixel by pixel with a reference taken from
elsewhere that is "70 % of the bed surface changed" — a plate-missing FAIL for a plate that is on
(2026-11-13, K1 SE start preflight). The pixels of a part or a missing plate change a REGION of an otherwise
unchanged scene; a camera that moved changes the whole scene. So before judging, the frame is located against
the reference, by the scene OUTSIDE the bed region (what a plate or a part cannot change):

  same    the scene round the bed lines up (edge strength correlates, or the frames differ in light only and
          phase correlation finds no shift, or the SIFT homography moves the bed under `SAME_PX`): the camera
          stands where it stood; judged as is (the fixed U1 camera: always this).
  moved   the scene is shifted but a homography from matched SIFT features explains it (inliers, a plausible
          quad) and after warping the scene round the bed lines up again (`MIN_FIT`) with `MIN_VISIBLE` of
          the bed region still in the frame: the frame is warped onto the reference and judged there.
  lost    the scene round the bed correlates under `LOST_NCC` and no homography is found, or the scene lines up
          only far away (the camera sits lower or closer: parallax), or too little of the bed region is in the
          frame. Nothing about the plate or parts can be concluded from pixels.
  blind   too little texture in either frame, no scene round the bed region (a whole-frame ROI), or no match
          although the scene round the bed still correlates (a night light, a big part on a fixed camera): to
          tell nothing; judged as is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    import numpy as np

# thresholds tuned on real K1 SE frames (tests/data/bed) and the U1's light-change frames
SAME_NCC = 0.85        # scene correlation at which the camera surely stands where it stood (a 3 px shift: 0.87)
LIGHT_RESPONSE = 0.25  # a light change or parts lower the correlation (U1 night vs day 0.5-0.65): phase correlation
                       # of the two frames then at least this sure (real pans 0.8-1.0, other views 0.07-0.19) ...
SAME_PX = 4.0          # ... that the scene moved under this many px of the 480-px frame (also for the homography; the judge
                       # itself forgives 3 px: `shift_px`)
LOST_NCC = 0.3         # a frame is `lost` only when the scene round the bed also differs this much; a scene that still
                       # correlates (a night light, a part, spaghetti on a fixed camera) is judged as it always was
MIN_SCENE_PX = 1000    # fewer scene pixels outside the bed region: nothing to locate the camera by
RING_PX = 15           # the bed region is grown by this much before it is left out of the scene
MIN_KEYPOINTS = 40     # fewer SIFT features in either frame: nothing to locate it by
MIN_INLIERS = 15       # a homography backed by fewer matched features is chance, not a pose
MIN_SHARE = 0.25       # ... and by less than this share of the ratio-test matches
RATIO = 0.75           # Lowe ratio test
REPROJ_PX = 3.0        # RANSAC reprojection tolerance at PROC_W
AREA_RANGE = (0.25, 4.0)   # the frame's area after the move: more than 4x or under a quarter is a mismatch
MAX_STRETCH = 1.6          # singular value ratio of the homography's linear part (shear + aspect)
MIN_FIT = 0.65         # scene correlation after aligning (real frames: aligned 0.8-0.96, not aligned 0.3-0.55)
MIN_VISIBLE = 0.9      # share of the bed region that must stay inside the moved frame (the rest is not judged)

State = Literal["same", "moved", "lost", "blind"]
SAME: State = "same"
MOVED: State = "moved"
LOST: State = "lost"
BLIND: State = "blind"


@dataclass(frozen=True)
class Pose:
    state: State
    why: str
    ncc: float = 1.0                      # correlation of the scene round the bed, unaligned
    inliers: int = 0
    shift_px: float = 0.0                 # how far the bed region moved (PROC_W px)
    visible: float = 1.0                  # share of the bed region the frame covers (moved)
    h: np.ndarray | None = None           # current frame -> reference frame (moved)
    covered: np.ndarray | None = None     # bool: reference pixels the moved frame covers (moved)

    def info(self) -> dict[str, object]:
        return {"state": self.state, "ncc": round(self.ncc, 3), "shift_px": round(self.shift_px, 1),
                "inliers": self.inliers, "visible": round(self.visible, 3)}


class _Blind(Exception):
    """Too little texture to match by."""


def _structure(img: np.ndarray) -> np.ndarray:
    import cv2
    import numpy as np

    grey = cv2.GaussianBlur(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (5, 5), 0).astype(np.float32)
    mag = np.hypot(cv2.Sobel(grey, cv2.CV_32F, 1, 0), cv2.Sobel(grey, cv2.CV_32F, 0, 1))
    return cv2.GaussianBlur(mag, (9, 9), 0)


def _ncc(a: np.ndarray, b: np.ndarray, mask: np.ndarray) -> float:
    """Normalised correlation of two structure maps inside `mask`: 1 the same picture, ~0 another view."""
    import numpy as np

    x, y = a[mask], b[mask]
    x, y = x - float(x.mean()), y - float(y.mean())
    norm = float(np.sqrt((x * x).sum() * (y * y).sum()))
    return float((x * y).sum() / norm) if norm > 0 else 0.0


def _phase(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """(shift in px, how sure) of the translation between two structure maps: light-proof, exact for a pan."""
    import cv2
    import numpy as np

    window = cv2.createHanningWindow((a.shape[1], a.shape[0]), cv2.CV_32F)
    (dx, dy), response = cv2.phaseCorrelate(a.astype(np.float32), b.astype(np.float32), window)
    return float(np.hypot(dx, dy)), float(response)


def _plausible(h: np.ndarray, shape: tuple[int, int]) -> str | None:
    """Why the homography `h` cannot be a camera move (the frame's corners must stay a convex quad of about the
    same area and aspect, same orientation), or None."""
    import cv2
    import numpy as np

    height, width = shape
    corners = np.array([[[0, 0], [width, 0], [width, height], [0, height]]], np.float32)
    quad = cv2.perspectiveTransform(corners, h)[0]
    edges = np.roll(quad, -1, axis=0) - quad
    turns = edges[:, 0] * np.roll(edges[:, 1], -1) - edges[:, 1] * np.roll(edges[:, 0], -1)
    if not np.isfinite(quad).all() or float(turns.min()) <= 0:
        return "the matched features imply a folded or mirrored view"
    area = float(cv2.contourArea(quad)) / (width * height)
    if not AREA_RANGE[0] <= area <= AREA_RANGE[1]:
        return f"the matched features imply an implausible zoom (area x{area:.2f})"
    s = np.linalg.svd(h[:2, :2], compute_uv=False)
    if s[1] <= 0 or s[0] / s[1] > MAX_STRETCH:
        return "the matched features imply an implausible shear of the view"
    return None


def _matches(ref: np.ndarray, cur: np.ndarray) -> tuple[np.ndarray, np.ndarray, int]:
    """(points in `cur`, their matches in `ref`, features found in `cur`): SIFT + the ratio test.
    Raises _Blind when either frame has too few features."""
    import cv2
    import numpy as np

    sift = cv2.SIFT_create(1500)
    kr, dr = sift.detectAndCompute(cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY), None)
    kc, dc = sift.detectAndCompute(cv2.cvtColor(cur, cv2.COLOR_BGR2GRAY), None)
    if dr is None or dc is None or min(len(kr), len(kc)) < MIN_KEYPOINTS:
        raise _Blind
    pairs = cv2.BFMatcher().knnMatch(dc, dr, k=2)
    good = [m for m, n in (p for p in pairs if len(p) == 2) if m.distance < RATIO * n.distance]
    src = np.array([kc[m.queryIdx].pt for m in good], np.float32).reshape(-1, 2)
    dst = np.array([kr[m.trainIdx].pt for m in good], np.float32).reshape(-1, 2)
    return src, dst, len(kc)


def _homography(ref: np.ndarray, cur: np.ndarray) -> tuple[np.ndarray | None, int, str]:
    """(H mapping `cur` onto `ref`, inliers, why not): matches + RANSAC; H None when there is no fit.
    Raises _Blind when either frame has too few features."""
    import cv2

    src, dst, found = _matches(ref, cur)
    if len(src) < MIN_INLIERS:
        return None, 0, f"only {len(src)} of {found} features of the frame match the reference's"
    h, inl = cv2.findHomography(src, dst, cv2.RANSAC, REPROJ_PX)
    n = 0 if inl is None else int(inl.sum())
    if h is None or n < max(MIN_INLIERS, MIN_SHARE * len(src)):
        return None, n, f"only {n} of {len(src)} matched features agree on one view"
    h = h / h[2, 2]
    bad = _plausible(h, ref.shape[:2])
    return (None, n, bad) if bad else (h, n, "")


def _lost(why: str, ncc: float, inliers: int, shift: float = 0.0, visible: float = 1.0) -> Pose:
    """`lost` when the scene round the bed does not correlate with the reference's; else nobody can tell."""
    if ncc < LOST_NCC:
        return Pose(LOST, why, ncc, inliers, shift, visible)
    return Pose(BLIND, f"cannot tell whether the camera moved ({why}), but the scene round the bed still correlates "
                f"({ncc:.2f})", ncc, inliers, shift, visible)


def locate(ref: np.ndarray, cur: np.ndarray, roi: np.ndarray) -> Pose:
    """Where is `cur` (BGR, same size as `ref`) relative to `ref`; `roi` is the reference's bed mask."""
    import cv2
    import numpy as np

    if not roi.any():  # classify reports an empty ROI itself
        return Pose(BLIND, "the bed region of the reference is empty: nothing to locate the camera by")
    scene = ~cv2.dilate(roi.astype(np.uint8), np.ones((RING_PX, RING_PX), np.uint8)).astype(bool)
    if int(scene.sum()) < MIN_SCENE_PX:
        return Pose(BLIND, "the bed region of the reference fills the frame: no scene round it to locate the camera by")
    sr, sc = _structure(ref), _structure(cur)
    c = _ncc(sr, sc, scene)
    if c >= SAME_NCC:
        return Pose(SAME, "the camera stands where the reference was taken", c)
    shift, response = _phase(sr, sc)  # the frames differ in light, or in what is on the bed, not in view?
    if response >= LIGHT_RESPONSE and shift < SAME_PX:
        return Pose(SAME, "the camera stands where the reference was taken (the picture differs)", c, 0, shift)
    try:
        h, inliers, why = _homography(ref, cur)
    except _Blind:
        return Pose(BLIND, "too little texture to tell whether the camera moved", c)
    if h is None:
        return _lost(f"the camera does not see the scene of the reference ({why})", c, inliers)
    height, width = roi.shape
    ys, xs = np.nonzero(roi)
    # how far the bed region's corners (and centre) moved: a zoom or a turn about the centre moves its border, not it
    box = np.array([[[xs.min(), ys.min()], [xs.max(), ys.min()], [xs.max(), ys.max()], [xs.min(), ys.max()],
                     [xs.mean(), ys.mean()]]], np.float32)
    shift = float(np.linalg.norm(cv2.perspectiveTransform(box, np.linalg.inv(h))[0] - box[0], axis=1).max())
    if shift < SAME_PX:
        return Pose(SAME, "the camera stands where the reference was taken", c, inliers, shift)
    covered = cv2.warpPerspective(np.ones(cur.shape[:2], np.uint8), h, (width, height), flags=cv2.INTER_NEAREST) > 0
    covered = cv2.erode(covered.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    seen = float((covered & roi).sum()) / int(roi.sum())
    if seen < MIN_VISIBLE:  # a trusted homography (plausible, many inliers) says so: no `LOST_NCC` doubt applies
        return Pose(LOST, f"the camera moved ~{shift:.0f} px and only {seen:.0%} of the bed region of the reference "
                    "is still in its frame", c, inliers, shift, seen)
    warped = cv2.warpPerspective(cur, h, (width, height), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    # how well the scene lines up after the move, measured where a plate or a part cannot have changed it. A frame
    # that aligns on the far scene but not near the bed (parallax: the camera sits lower or closer than it did)
    # leaves the bed unaligned, and the diff along every edge of it reads as parts.
    around = covered & scene
    fit = _ncc(sr, _structure(warped), around if int(around.sum()) >= MIN_SCENE_PX else covered)
    if fit < MIN_FIT:
        return _lost(f"the camera moved ~{shift:.0f} px and the scene does not line up with the reference after "
                     f"aligning it (fit {fit:.2f}, needs {MIN_FIT:.2f}: it sees the bed from another height or "
                     "angle)", c, inliers, shift, seen)
    return Pose(MOVED, f"the camera moved ~{shift:.0f} px since the reference ({inliers} features matched, fit "
                f"{fit:.2f}): the frame was aligned to it", c, inliers, shift, seen, h, covered)
