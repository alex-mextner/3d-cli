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
  the bed moves in Z, the picture of the plate moves with it); without one (a `watch.bed.roi` printer) the
  baseline stands only within `--z-tol` mm. With a `--footprint` (the changed mask the part had when the watcher
  last saw it healthy) it also says how much of that footprint is still changed (`overlap`) and how much changed
  OUTSIDE it (`outside`, relative to the footprint: spaghetti, a part that slid).

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


def measure(
    jpeg: bytes,
    base_jpeg: bytes,
    *,
    device: Device,
    z: float,
    base_z: float,
    footprint: np.ndarray | None = None,
    min_view: float = MIN_VIEW,
    z_tol: float = Z_TOL_MM,
) -> tuple[dict[str, Any], np.ndarray | None, np.ndarray | None]:
    """(numbers, changed mask, region) for one frame against the job's baseline. Numbers `status` is `ok`
    or `blind` (with `reason`)."""
    cfg = B.bed_config(device)
    cam = B.camera_model(device, cfg)
    seen = view_share(cam, z)
    if seen is not None and seen < min_view:
        return (
            blind(
                f"the camera sees {seen:.0%} of the plate at Z {z:.1f}, under {min_view:.0%}",
                view=seen,
            ),
            None,
            None,
        )
    img, base = B.decode_small(jpeg), B.decode_small(base_jpeg)
    if img.shape != base.shape:
        return (
            blind(
                f"the frame is {img.shape[1]}x{img.shape[0]}, the baseline {base.shape[1]}x{base.shape[0]}"
            ),
            None,
            None,
        )
    shifted, why = baseline_at(base, base_z, z, cam, z_tol)
    if shifted is None:
        return blind(why or "no baseline", view=seen), None, None
    region = plate_region(device, cfg, z, img.shape[:2])
    th = B.thresholds_for(cfg)
    d = B.difference(
        shifted, img, region, th.level, th.detrend_px, th.shift_px, th.open_px
    )
    out: dict[str, Any] = {
        "status": "ok",
        "view": seen,
        "changed": round(d.frac, 4),
        "blob_px": round(d.blob * int(region.sum())),
        "changed_px": int(d.mask.sum()),
        "region_px": int(region.sum()),
        "z": z,
        "baseline_z": base_z,
    }
    if footprint is not None and footprint.shape == d.mask.shape:
        out.update(footprint_numbers(d.mask, footprint))
    return out, d.mask, region


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
    [--save-footprint P.png] [--annotated OUT.jpg] [--label TEXT] [--min-view 0.5] [--z-tol 2] [--json]
  The pixel half of `3d printer watch NAME --job` (see lib/printer_ops/jobwatch.py): the changed share of
  the plate region of the frame against the job's empty-plate baseline, and with a footprint how much of it
  is still changed (overlap) and how much changed beyond it (outside). `status` is ok or blind (why)."""


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="bed_part.py", add_help=False)
    ap.add_argument("sub", choices=("check",))
    ap.add_argument("name")
    ap.add_argument("--frame", required=True)
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--baseline-z", type=float, required=True)
    ap.add_argument("--z", type=float, required=True)
    ap.add_argument("--footprint")
    ap.add_argument("--save-footprint")
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
        min_view=args.min_view,
        z_tol=args.z_tol,
    )
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
