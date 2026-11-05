"""bed_part.py — is the printed part still on the plate? The pixel half of `3d printer watch NAME --job`.

Run through cli.pyrun with numpy + OpenCV (BED_DEPS) by printer_ops/jobwatch.py, once per check:

    bed_part.py check NAME --frame F.jpg --baseline B.jpg --baseline-z Z0 --z Z \
        [--footprint P.png] [--save-footprint P.png] [--annotated OUT.jpg] [--min-view 0.5] [--z-tol 2]

It measures; it does not decide. The decision (armed, present, gone, partial, growing) is the pure state
machine of jobwatch.py, so the pixel tool stays stateless: everything it needs lives in the files it is given.

WHAT IS MEASURED: the changed share of the plate region of `frame` against `baseline`, an image of the empty
  plate taken at the start of the job (`baseline-z` = the toolhead Z it was taken at). Same light/exposure
  robustness as the bed check (bed_state.difference: per-channel gain/offset fit, detrend, shift tolerance,
  opening). With a camera calibration the baseline is warped to the current Z (a homography of the bed plane:
  the bed moves in Z, the picture of the plate moves with it). With a `--footprint` (the changed mask the part
  had when the watcher last saw it healthy) it also says how much of that footprint is still changed
  (`overlap`) and how much changed OUTSIDE it (`outside`, relative to the footprint: spaghetti, a part that slid).

CHAINED BASELINE (a printer without a calibration, a `watch.bed.roi` polygon): nothing says how the bed's move
  shows in the picture, so the plate is FOLLOWED instead. Every healthy reading saves, next to the footprint,
  the affine transform (`--save-track`) that carries the baseline's plate onto that frame. The next reading
  starts from it (`--track`) and refines it with ECC (cv2.findTransformECC, correlation based: a global
  light change does not move it) on the plate pixels only, WITHOUT the part's footprint neighbourhood. Layer
  to layer the bed moves 0.2 mm (a px or less), so the chain never has to jump: 30 layers (6 mm) or 100 are
  the same small steps, and every step is anchored to the true empty baseline, so errors do not add up (the
  footprint is carried along by the change of transform). A part that vanishes then reads as `overlap` ~0:
  its footprint matches the empty plate again whatever Z it is at. Within `--z-tol` mm of the baseline a
  failed tracking falls back to the baseline as it is; beyond it the answer is `blind`.
  LIMITS: the plate must show texture (a blank plate cannot be followed: `blind`); the transform may not
  jump more than TRACK_STEP_PX between two readings (the camera was off, the bed went far while nothing was
  healthy: `blind`); a correlation under TRACK_MIN_CORR is `blind`. The model is affine, so the perspective
  of a big move is followed only approximately (the shift tolerance of the comparison absorbs a pixel or
  two). Spaghetti covering most of the plate may make the plate untrackable too: `blind`, never gone.

BLIND, NOT WRONG: when the plate is not in the frame at this Z (`view` under `--min-view`), when the baseline
  cannot be brought to this Z, or the frame is another size, the answer is `blind` with the reason and no
  numbers: a camera that cannot see must never read as "the part is gone".

INVARIANTS: read-only (files only; no printer, no network); numpy/OpenCV imported lazily; frames decode at
  bed_state.PROC_W (480 px).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

LIB = Path(__file__).resolve().parent
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import bed_state as B  # noqa: E402

if TYPE_CHECKING:
    import numpy as np

    from printer_ops.devices import Device
    from vision_geometry import CameraModel

MIN_VIEW = 0.5  # share of the plate that must be in the frame for the part to be judged
Z_TOL_MM = 2.0  # an uncalibrated baseline stands this far from its Z
ROI_GROW_PX = 9  # the plate region is grown by this many px of the 480-px frame: a part stands above it
FOOTPRINT_GROW_PX = 15  # changes this close to the footprint are the part itself growing, not new things
TRACK_STEP_PX = 12.0  # the plate may move this far (px of the 480-px frame) between two healthy readings
TRACK_MIN_CORR = 0.6  # ECC correlation of the baseline's plate with the frame's under which it is not followed
TRACK_MIN_PX = 4000  # plate pixels (outside the footprint) needed to follow it
TRACK_ERODE_PX = 7  # the plate edge is not trusted: the bed's border is where the static machine begins


def blind(reason: str, **extra: Any) -> dict[str, Any]:
    return {"status": "blind", "reason": reason, **extra}


def warp_baseline(
    base: np.ndarray, cam: CameraModel, z0: float, z1: float
) -> np.ndarray:
    """`base` (the empty plate seen with the bed at Z `z0`) as the camera would see it at Z `z1`: the
    homography of the bed plane between the two views, fitted on a grid of plate points. The scene off the
    plate (the machine) is not on that plane and does not follow; judge only the plate region."""
    import cv2
    import numpy as np

    scale = base.shape[1] / cam.width
    pts = B._bed_points(cam, (0.0,), 9)
    a, b = cam.project(pts, z0) * scale, cam.project(pts, z1) * scale
    ok = np.isfinite(a).all(axis=1) & np.isfinite(b).all(axis=1)
    if int(ok.sum()) < 8:
        raise ValueError("the plate is behind the camera at one of the two Zs")
    h, _ = cv2.findHomography(a[ok].astype(np.float32), b[ok].astype(np.float32), 0)
    if h is None:
        raise ValueError("no homography between the two views of the plate")
    return cv2.warpPerspective(
        base, h, (base.shape[1], base.shape[0]), borderMode=cv2.BORDER_REPLICATE
    )


def baseline_at(
    base: np.ndarray, base_z: float, z: float, cam: CameraModel | None, z_tol: float
) -> tuple[np.ndarray | None, str | None]:
    """(the baseline as seen at Z `z`, None) or (None, why it cannot be)."""
    if abs(z - base_z) < 0.05:
        return base, None
    if cam is None:
        if abs(z - base_z) <= z_tol:
            return base, None
        return None, (
            f"the bed moved {abs(z - base_z):.1f} mm since the baseline (Z {base_z:.1f} -> {z:.1f}) and "
            "this printer has no camera calibration to warp it (a watch.bed.roi polygon only)"
        )
    try:
        return warp_baseline(base, cam, base_z, z), None
    except ValueError as exc:
        return None, str(exc)


def plate_region(
    device: Device, cfg: Any, z: float, shape: tuple[int, int]
) -> np.ndarray:
    """The plate surface at Z `z`, grown a little (a part stands on it), as the region to compare."""
    import cv2
    import numpy as np

    _, surface, _ = B.build_masks(device, cfg, z, shape)
    grown = (
        cv2.dilate(
            surface.astype(np.uint8), np.ones((2 * ROI_GROW_PX + 1,) * 2, np.uint8)
        )
        > 0
    )
    return grown


def plate_surface(
    device: Device, cfg: Any, z: float, shape: tuple[int, int]
) -> np.ndarray:
    """The plate surface at Z `z` as it is (not grown)."""
    _, surface, _ = B.build_masks(device, cfg, z, shape)
    return surface


# ---- following the plate (no camera calibration) ----------------------------------------------


def identity() -> np.ndarray:
    import numpy as np

    return np.eye(2, 3, dtype=np.float32)


def gray(img: np.ndarray) -> np.ndarray:
    import cv2
    import numpy as np

    return np.asarray(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), dtype=np.float32)


def move_mask(mask: np.ndarray, warp: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    """`mask` carried by the affine `warp` (src -> dst) onto an image of `shape`."""
    import cv2
    import numpy as np

    moved = cv2.warpAffine(
        mask.astype(np.uint8),
        warp,
        (shape[1], shape[0]),
        flags=cv2.INTER_NEAREST,
    )
    return np.asarray(moved > 0)


def carry_footprint(
    footprint: np.ndarray, old: np.ndarray | None, new: np.ndarray | None
) -> np.ndarray:
    """The footprint seen under transform `old` (baseline -> the frame it was learnt on) as it is under `new`."""
    import cv2
    import numpy as np

    if new is None:
        return footprint
    inv = cv2.invertAffineTransform(identity() if old is None else old)
    step = (np.vstack([new, [0, 0, 1]]) @ np.vstack([inv, [0, 0, 1]]))[:2]
    return move_mask(footprint, step.astype(np.float32), footprint.shape)


def step_px(surface: np.ndarray, a: np.ndarray, b: np.ndarray) -> float:
    """How far transforms `a` and `b` put the corners of the plate's bounding box from each other (px)."""
    import cv2
    import numpy as np

    x, y, w, h = cv2.boundingRect(surface.astype(np.uint8))
    pts = np.array([[x, y, 1], [x + w, y, 1], [x, y + h, 1], [x + w, y + h, 1]]).T
    return float(np.abs(a @ pts - b @ pts).max())


def tracking_mask(
    surface: np.ndarray, start: np.ndarray, footprint: np.ndarray | None
) -> np.ndarray:
    """Where the plate is expected on the frame (under the transform it started from), minus its borders
    and the part: the pixels that say how the bed moved."""
    import cv2
    import numpy as np

    moved = move_mask(surface, start, surface.shape).astype(np.uint8)
    plate = cv2.erode(moved, np.ones((TRACK_ERODE_PX,) * 2, np.uint8))
    if footprint is not None and footprint.shape == surface.shape:
        grow = np.ones((2 * FOOTPRINT_GROW_PX + 1,) * 2, np.uint8)
        plate[cv2.dilate(footprint.astype(np.uint8), grow) > 0] = 0
    return plate


def ecc_fit(
    base: np.ndarray, img: np.ndarray, mask: np.ndarray, start: np.ndarray
) -> tuple[np.ndarray, float] | str:
    """The affine transform that carries `base` onto `img` on the masked pixels, refined from `start`."""
    import cv2

    stop = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 100, 1e-6)
    try:
        corr, warp = cv2.findTransformECC(
            gray(base), gray(img), start.copy(), cv2.MOTION_AFFINE, stop, mask, 5
        )
    except cv2.error:
        return "the plate's picture does not converge onto the baseline's (no texture, or it moved too far)"
    if corr < TRACK_MIN_CORR:
        return f"the plate matches the baseline's picture only to {corr:.2f} (under {TRACK_MIN_CORR})"
    return warp, float(corr)


def follow_plate(
    base: np.ndarray,
    img: np.ndarray,
    plate: tuple[np.ndarray, np.ndarray],
    footprint: np.ndarray | None,
    track: np.ndarray | None,
) -> Aligned | str:
    """The baseline carried onto the frame by following the plate, starting from the last healthy transform
    (`track`, else none), or why the plate cannot be followed."""
    _, surface = plate
    start = identity() if track is None else track
    mask = tracking_mask(surface, start, footprint)
    if int(mask.sum()) < TRACK_MIN_PX:
        return "too little of the plate is left in view to follow it"
    fit = ecc_fit(base, img, mask, start)
    if isinstance(fit, str):
        return fit
    warp, corr = fit
    moved = step_px(surface, start, warp)
    if moved > TRACK_STEP_PX:
        return f"it moved {moved:.0f} px since the last healthy reading (more than {TRACK_STEP_PX:.0f})"
    import cv2

    size = (base.shape[1], base.shape[0])
    shifted = cv2.warpAffine(base, warp, size, borderMode=cv2.BORDER_REPLICATE)
    # the plate as it moved, NOT grown: the grown band holds the static machine, which the baseline's move
    # would drag over and report as new material once the drift is more than a few px
    return Aligned(shifted, move_mask(surface, warp, surface.shape), warp, corr)


def tracking_numbers(al: Aligned, surface: np.ndarray) -> dict[str, Any]:
    """`warp` (to be saved) and, when the plate was followed, its correlation and drift since the baseline."""
    out: dict[str, Any] = {}
    if al.warp is None:
        return out
    out["warp"] = [[round(float(v), 5) for v in row] for row in al.warp]
    if al.corr is not None:
        out["track_corr"] = round(al.corr, 3)
        out["drift_px"] = round(step_px(surface, identity(), al.warp), 1)
    return out


def read_track(path: str | None) -> np.ndarray | None:
    """The transform saved with the footprint (`--save-track`), None without."""
    import numpy as np

    if not path or not Path(path).is_file():
        return None
    return np.array(
        json.loads(Path(path).read_text(encoding="utf-8"))["warp"], np.float32
    )


def write_track(path: str, warp: list[list[float]]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps({"warp": warp}), encoding="utf-8")


def footprint_numbers(changed: np.ndarray, footprint: np.ndarray) -> dict[str, float]:
    """How much of `footprint` is still changed, and how much changed beyond it (both / footprint area)."""
    import cv2
    import numpy as np

    area = max(1, int(footprint.sum()))
    near = (
        cv2.dilate(
            footprint.astype(np.uint8),
            np.ones((2 * FOOTPRINT_GROW_PX + 1,) * 2, np.uint8),
        )
        > 0
    )
    return {
        "overlap": round(float((changed & footprint).sum()) / area, 4),
        "outside": round(float((changed & ~near).sum()) / area, 4),
        "footprint_px": int(footprint.sum()),
    }


def view_share(cam: CameraModel | None, z: float) -> float | None:
    return None if cam is None else B.bed_in_view(cam, round(z, 1))


@dataclass
class Aligned:
    """The baseline and the plate region brought onto the frame, and the transform that did it (None: none)."""

    base: np.ndarray
    region: np.ndarray
    warp: np.ndarray | None = None
    corr: float | None = None


def align_baseline(
    base: np.ndarray,
    img: np.ndarray,
    plate: tuple[np.ndarray, np.ndarray],
    *,
    cam: CameraModel | None,
    z: float,
    base_z: float,
    z_tol: float,
    footprint: np.ndarray | None,
    track: np.ndarray | None,
) -> Aligned | str:
    """The baseline as the frame sees it (an `Aligned`), or why it cannot be (a string). `plate` = (region,
    surface) in baseline coordinates. A calibrated camera warps the baseline to Z; without one the plate is
    followed on the picture (`follow_plate`), and the baseline as it stands is kept only within `z_tol`."""
    region, _ = plate
    if cam is not None or abs(z - base_z) < 0.05:
        shifted, why = baseline_at(base, base_z, z, cam, z_tol)
        if shifted is None:
            return why or "no baseline"
        return Aligned(shifted, region, None if cam is not None else identity())
    followed = follow_plate(base, img, plate, footprint, track)
    if not isinstance(followed, str):
        return followed
    if abs(z - base_z) <= z_tol:
        return Aligned(base, region, identity())
    return (
        f"the bed moved {abs(z - base_z):.1f} mm since the baseline (Z {base_z:.1f} -> {z:.1f}) and "
        f"the plate cannot be followed on the picture: {followed}"
    )


def measure(
    jpeg: bytes,
    base_jpeg: bytes,
    *,
    device: Device,
    z: float,
    base_z: float,
    footprint: np.ndarray | None = None,
    track: np.ndarray | None = None,
    min_view: float = MIN_VIEW,
    z_tol: float = Z_TOL_MM,
) -> tuple[dict[str, Any], np.ndarray | None, np.ndarray | None]:
    """(numbers, changed mask, region) for one frame against the job's baseline. Numbers `status` is `ok`
    or `blind` (with `reason`); `warp` (ok, no calibration) is the transform to save with the footprint."""
    cfg = B.bed_config(device)
    cam = B.camera_model(device, cfg)
    seen = view_share(cam, z)
    if seen is not None and seen < min_view:
        why = f"the camera sees {seen:.0%} of the plate at Z {z:.1f}, under {min_view:.0%}"
        return blind(why, view=seen), None, None
    img, base = B.decode_small(jpeg), B.decode_small(base_jpeg)
    if img.shape != base.shape:
        why = f"the frame is {img.shape[1]}x{img.shape[0]}, the baseline {base.shape[1]}x{base.shape[0]}"
        return blind(why), None, None
    plate = (
        plate_region(device, cfg, z, img.shape[:2]),
        plate_surface(device, cfg, z, img.shape[:2]),
    )
    al = align_baseline(
        base,
        img,
        plate,
        cam=cam,
        z=z,
        base_z=base_z,
        z_tol=z_tol,
        footprint=footprint,
        track=track,
    )
    if isinstance(al, str):
        return blind(al, view=seen), None, None
    th = B.thresholds_for(cfg)
    d = B.difference(
        al.base, img, al.region, th.level, th.detrend_px, th.shift_px, th.open_px
    )
    out: dict[str, Any] = {
        "status": "ok",
        "view": seen,
        "changed": round(d.frac, 4),
        "blob_px": round(d.blob * int(al.region.sum())),
        "changed_px": int(d.mask.sum()),
        "region_px": int(al.region.sum()),
        "z": z,
        "baseline_z": base_z,
    }
    out.update(tracking_numbers(al, plate[1]))
    if footprint is not None and footprint.shape == d.mask.shape:
        out.update(
            footprint_numbers(d.mask, carry_footprint(footprint, track, al.warp))
        )
    return out, d.mask, al.region


def annotate(
    jpeg: bytes,
    changed: np.ndarray | None,
    region: np.ndarray | None,
    label: str,
    path: Path,
) -> None:
    """The camera frame with the compared region (cyan) and the changed pixels (red), as the bed check draws it."""
    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        path.write_bytes(jpeg)
        return
    h, w = img.shape[:2]
    if changed is not None:
        red = (
            cv2.resize(
                changed.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST
            )
            > 0
        )
        img[red] = (0.45 * img[red] + np.array([0, 0, 140])).astype(np.uint8)
    if region is not None:
        roi = cv2.resize(
            region.astype(np.uint8), (w, h), interpolation=cv2.INTER_NEAREST
        )
        contours, _ = cv2.findContours(roi, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(img, contours, -1, (255, 255, 0), max(1, w // 640))
    cv2.putText(
        img,
        label,
        (12, 36),
        cv2.FONT_HERSHEY_SIMPLEX,
        max(0.6, w / 1600),
        (0, 255, 255),
        2,
    )
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    path.write_bytes(buf.tobytes() if ok else jpeg)


def read_mask(path: str | None) -> np.ndarray | None:
    import cv2

    if not path or not Path(path).is_file():
        return None
    m = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    return None if m is None else m > 0


def write_mask(path: str, mask: np.ndarray) -> None:
    import cv2

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(path, mask.astype("uint8") * 255)


USAGE = """bed_part.py check NAME --frame F.jpg --baseline B.jpg --baseline-z Z0 --z Z [--footprint P.png]
    [--track T.json] [--save-footprint P.png] [--save-track T.json] [--annotated OUT.jpg] [--label TEXT]
    [--min-view 0.5] [--z-tol 2] [--json]
  The pixel half of `3d printer watch NAME --job` (see lib/printer_ops/jobwatch.py): the changed share of
  the plate region of the frame against the job's empty-plate baseline, and with a footprint how much of it
  is still changed (overlap) and how much changed beyond it (outside). `status` is ok or blind (why).
  Without a camera calibration the plate is followed on the picture from the transform of --track (what
  --save-track wrote with the last healthy footprint), so the baseline survives the bed's move in Z."""


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="bed_part.py", add_help=False)
    ap.add_argument("sub", choices=("check",))
    ap.add_argument("name")
    ap.add_argument("--frame", required=True)
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--baseline-z", type=float, required=True)
    ap.add_argument("--z", type=float, required=True)
    ap.add_argument("--footprint")
    ap.add_argument("--track")
    ap.add_argument("--save-footprint")
    ap.add_argument("--save-track")
    ap.add_argument("--annotated")
    ap.add_argument("--label", default="")
    ap.add_argument("--min-view", type=float, default=MIN_VIEW)
    ap.add_argument("--z-tol", type=float, default=Z_TOL_MM)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("-h", "--help", action="store_true")
    return ap


def run_check(args: argparse.Namespace) -> dict[str, Any]:
    from printer_ops.devices import get_device

    device = get_device(args.name)
    jpeg, base = Path(args.frame).read_bytes(), Path(args.baseline).read_bytes()
    nums, changed, region = measure(
        jpeg,
        base,
        device=device,
        z=args.z,
        base_z=args.baseline_z,
        footprint=read_mask(args.footprint),
        track=read_track(args.track),
        min_view=args.min_view,
        z_tol=args.z_tol,
    )
    warp = nums.pop("warp", None)
    if args.annotated:
        annotate(
            jpeg,
            changed,
            region,
            args.label or f"{nums['status']} {nums.get('changed', '')}",
            Path(args.annotated),
        )
        nums["annotated"] = args.annotated
    if args.save_footprint and changed is not None:
        write_mask(args.save_footprint, changed)
        nums["footprint_saved"] = args.save_footprint
        if args.save_track and warp is not None:
            write_track(args.save_track, warp)
    return nums


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0 if argv else 1
    args = build_parser().parse_args(argv)
    if args.help:
        print(USAGE)
        return 0
    out = run_check(args)
    print(
        json.dumps(out, default=str)
        if args.json
        else json.dumps(out, indent=1, default=str)
    )
    return 0


if __name__ == "__main__":
    from errors import ThreeDError

    try:
        sys.exit(main(sys.argv[1:]))
    except ThreeDError as exc:
        print(exc.render(), file=sys.stderr)
        sys.exit(exc.exit_code)
