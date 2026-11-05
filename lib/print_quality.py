"""print_quality.py — how good does the print look? The pixel half of the print-quality monitor.

Run through cli.pyrun with numpy + OpenCV (BED_DEPS) by `3d printer watch NAME --job` (once per camera check),
and imported by vision_quality.py inside `3d printer vision` (the frame is already decoded there):

    print_quality.py check NAME --frame F.jpg --z Z [--roi x,y;x,y;...] [--annotated OUT.jpg] [--json]

It measures; it does not decide. The decision (notice / warning / severe, per issue, against the job's own
first-layer baseline, with persistence and one alert per level) is the pure state machine of
printer_ops/quality.py, so this tool stays stateless: a frame in, numbers out.

WHAT IS MEASURED (all inside the PLATE region of the frame — the plate surface at the print's own Z from the
camera calibration, a `watch.bed.roi` polygon, `--roi`, or the whole frame; frames are resized to PROC_W = 640
px wide, so every pixel constant below is "px at 640", and the same frame at any camera resolution reads the same):
  `fleck_frac`  share of the region covered by small bright NEUTRAL specks and strands: the top-hat (SE
                TOPHAT_K px) of the minimum colour channel, divided by the local mean brightness, above
                FLECK_THR, connected pieces of FLECK_AMIN..FLECK_AMAX px. The minimum channel is high only
                where ALL of R, G and B are high, i.e. on white: stringing debris, loose strands, blobs of
                natural/white filament. The beige part itself, golden specular grain (blue is low) and the
                blue-lit toolhead shadow do not reach it. `fleck_per10k` is the same as a count.
  `order`       how regular the surface pattern is: the structure-tensor coherence (1 = every neighbourhood has
                ONE dominant line direction, 0 = isotropic mush), averaged with gradient energy as weights. Clean
                parallel beads read 0.8+; loose debris, stringing webs, rough or striped-and-broken lines pull it
                down; an empty plate is isotropic. It is meaningful as a CHANGE against the same job's own first
                layers (the decision side does exactly that), and as an absolute floor for a first layer.
  `luma`        mean brightness of the region (the exposure the numbers were taken at).

CALIBRATION (docs/research/print-quality-monitor.md): 12 U1 frames of one 10 h job (f1 empty plate, f2-f5 clean
first layer, f6 first strings, f7-f12 heavy stringing debris on layers 2-4) and 4 phone photos of the failed
first attempt. Clean f2-f5: fleck_frac 0.011-0.025, order 0.83-0.85; f7-f12: fleck_frac 0.039-0.087, order
0.63-0.72. The constants are for ONE camera view family (a low oblique plate view); another camera needs its own
calibration run (see the research doc) before its thresholds are trusted.

BLIND, NOT CLEAN: no region (the plate is not in the frame), a region under MIN_REGION_PX, or an undecodable
frame is `status: blind` with the reason and no numbers: a camera that cannot see must never read as "good".

INVARIANTS: read-only (files only; no printer, no network); numpy/OpenCV imported lazily.
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

if TYPE_CHECKING:
    import numpy as np

PROC_W = (
    640  # processing width: every constant below is in pixels of a PROC_W-wide frame
)
REGION_ERODE_PX = (
    7  # the region is eroded by this much: its rim is the plate edge, not the print
)
MIN_REGION_PX = 4000  # under this the plate is too small in the frame to judge (640x360 frame: ~1.7 %)
TOPHAT_K = 17
FLECK_THR = 0.18
FLECK_AMIN = 5
FLECK_AMAX = 130
MIN_CHANNEL_SIGMA = 0.55
LOCAL_MEAN_SIGMA = 8.0
ORDER_GRAD_SIGMA = 0.7
ORDER_WIN_SIGMA = 5.5
ANNOTATE_RED = (40, 40, 255)

USAGE = """print_quality.py check NAME --frame F.jpg --z Z [--roi "x,y;x,y;..."] [--annotated OUT.jpg] [--json]
  Measure ONE frame of a running print: fleck_frac (white specks/strands = stringing debris), order (how regular
  the bead pattern is), luma, inside the plate region at Z (camera calibration of NAME, a watch.bed.roi polygon,
  --roi with normalised 0..1 points, or the whole frame with `--roi whole`). Prints one JSON object:
  {"status": "ok", "fleck_frac": .., "fleck_per10k": .., "order": .., "luma": .., "region_px": .., ...} or
  {"status": "blind", "reason": ".."}. It decides nothing: printer_ops/quality.py compares it with the job's own
  first layers."""


def blind(reason: str) -> dict[str, Any]:
    return {"status": "blind", "reason": reason}


# ---- decode and region ------------------------------------------------------------------------


def decode(data: bytes) -> np.ndarray:
    """BGR frame PROC_W wide (INTER_AREA down from anything wider; a narrower frame is used as it is)."""
    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(data.rstrip(b"\x00"), np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("not a decodable image")
    return prepare(img)


def prepare(img: np.ndarray) -> np.ndarray:
    """A decoded BGR frame at the processing width (INTER_AREA down from anything wider)."""
    import cv2
    import numpy as np

    if img.shape[1] <= PROC_W:
        return img
    h = max(1, round(img.shape[0] * PROC_W / img.shape[1]))
    return np.asarray(cv2.resize(img, (PROC_W, h), interpolation=cv2.INTER_AREA))


def parse_roi(text: str) -> list[tuple[float, float]]:
    """`x,y;x,y;...` (normalised 0..1) -> points. ValueError names the bad part."""
    pts = []
    for item in (x for x in text.split(";") if x.strip()):
        x, y = (float(v) for v in item.split(","))
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise ValueError(f"--roi point {item!r} is outside 0..1")
        pts.append((x, y))
    if len(pts) < 3:
        raise ValueError("--roi needs at least 3 points (or `whole`)")
    return pts


def polygon_region(
    points: list[tuple[float, float]], shape: tuple[int, int]
) -> np.ndarray:
    import cv2
    import numpy as np

    h, w = shape
    mask = np.zeros((h, w), np.uint8)
    pts = np.array(
        [[round(x * (w - 1)), round(y * (h - 1))] for x, y in points], np.int32
    )
    cv2.fillPoly(mask, [pts], 1)
    return mask.astype(bool)


def plate_region(name: str, z: float, shape: tuple[int, int]) -> tuple[np.ndarray, str]:
    """(the plate surface at Z of printer NAME in a frame of `shape`, where it came from)."""
    from printer_ops.devices import get_device

    return device_region(
        get_device(name), z, shape
    ), f"plate surface of {name} at Z {z:g}"


def device_region(device: Any, z: float, shape: tuple[int, int]) -> np.ndarray:
    """The plate surface at Z of a configured printer (camera calibration or `watch.bed.roi`)."""
    import bed_state as B
    import numpy as np

    _, surface, _ = B.build_masks(device, B.bed_config(device), z, shape)
    return np.asarray(surface)


def shrink(region: np.ndarray) -> np.ndarray:
    import cv2
    import numpy as np

    k = np.ones((2 * (REGION_ERODE_PX // 2) + 1,) * 2, np.uint8)
    return np.asarray(cv2.erode(region.astype(np.uint8), k) > 0)


# ---- the measures -----------------------------------------------------------------------------


def fleck_map(img: np.ndarray) -> np.ndarray:
    """Per pixel: how much brighter the WHITE-ness (min channel) is than the local mean brightness."""
    import cv2
    import numpy as np

    f = img.astype(np.float32)
    whiteness = cv2.GaussianBlur(f.min(axis=2), (0, 0), MIN_CHANNEL_SIGMA)
    local = cv2.GaussianBlur(f.mean(axis=2), (0, 0), LOCAL_MEAN_SIGMA) + 10.0
    se = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (TOPHAT_K, TOPHAT_K))
    return np.asarray(cv2.morphologyEx(whiteness, cv2.MORPH_TOPHAT, se) / local)


def fleck_pieces(top: np.ndarray, region: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(labels, kept-piece mask): connected pieces of FLECK_AMIN..FLECK_AMAX px over the threshold."""
    import cv2
    import numpy as np

    over = ((top > FLECK_THR) & region).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(over, connectivity=8)
    area = stats[:, cv2.CC_STAT_AREA]
    keep = (area >= FLECK_AMIN) & (area <= FLECK_AMAX)
    keep[0] = False
    return np.asarray(labels), np.asarray(keep[labels]) if n else np.zeros_like(region)


def order_of(img: np.ndarray, region: np.ndarray) -> float:
    """Energy-weighted structure-tensor coherence inside the region (0..1)."""
    import cv2
    import numpy as np

    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    g /= max(float(g[region].mean()), 1.0)
    b = cv2.GaussianBlur(g, (0, 0), ORDER_GRAD_SIGMA)
    gx, gy = cv2.Sobel(b, cv2.CV_32F, 1, 0), cv2.Sobel(b, cv2.CV_32F, 0, 1)
    jxx = cv2.GaussianBlur(gx * gx, (0, 0), ORDER_WIN_SIGMA)
    jyy = cv2.GaussianBlur(gy * gy, (0, 0), ORDER_WIN_SIGMA)
    jxy = cv2.GaussianBlur(gx * gy, (0, 0), ORDER_WIN_SIGMA)
    coherence = np.sqrt((jxx - jyy) ** 2 + 4 * jxy**2) / (jxx + jyy + 1e-6)
    weight = (gx * gx + gy * gy)[region]
    total = float(weight.sum())
    return float((coherence[region] * weight).sum() / total) if total > 0 else 0.0


def measure(
    img: np.ndarray, region: np.ndarray, source: str = ""
) -> tuple[dict[str, Any], np.ndarray | None]:
    """(numbers, the mask of the fleck pixels) for a PROC_W-wide frame and its (already shrunk) region."""
    import cv2

    px = int(region.sum())
    if px < MIN_REGION_PX:
        return blind(
            f"the plate covers {px} px of the {img.shape[1]}x{img.shape[0]} frame, under {MIN_REGION_PX}"
        ), None
    top = fleck_map(img)
    _, flecks = fleck_pieces(top, region)
    count = cv2.connectedComponents(flecks.astype("uint8"), connectivity=8)[0] - 1
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    out: dict[str, Any] = {
        "status": "ok",
        "region_px": px,
        "region_share": round(px / (img.shape[0] * img.shape[1]), 4),
        "fleck_frac": round(float(flecks.sum()) / px, 4),
        "fleck_per10k": round(count * 1e4 / px, 2),
        "order": round(order_of(img, region), 4),
        "luma": round(float(gray[region].mean()), 1),
    }
    if source:
        out["region_source"] = source
    return out, flecks


def annotated_jpeg(
    img: np.ndarray, region: np.ndarray, flecks: np.ndarray | None, label: str
) -> bytes:
    """The frame with the region outline and the fleck pixels in red, `label` on top: the evidence picture."""
    import cv2

    vis = img.copy()
    contours, _ = cv2.findContours(
        region.astype("uint8"), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    cv2.drawContours(vis, contours, -1, (0, 200, 0), 1)
    if flecks is not None:
        vis[flecks] = ANNOTATE_RED
    cv2.putText(
        vis, label, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3, cv2.LINE_AA
    )
    cv2.putText(
        vis,
        label,
        (8, 20),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (255, 255, 255),
        1,
        cv2.LINE_AA,
    )
    ok, buf = cv2.imencode(".jpg", vis, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        raise ValueError("could not encode the annotated frame")
    return bytes(buf)


def annotate(
    img: np.ndarray,
    region: np.ndarray,
    flecks: np.ndarray | None,
    label: str,
    out: Path,
) -> None:
    """`annotated_jpeg` written to `out`."""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(annotated_jpeg(img, region, flecks, label))


def quality_numbers(
    jpeg: bytes,
    *,
    name: str | None,
    z: float,
    roi: list[tuple[float, float]] | None = None,
    annotated: Path | None = None,
    label: str = "",
) -> dict[str, Any]:
    """The numbers of one frame. Region: `roi` when given, else printer `name`'s plate surface at Z, else the
    whole frame. A frame that cannot be decoded or a plate that is out of view is a `blind` answer."""
    try:
        img = decode(jpeg)
    except ValueError as exc:
        return blind(str(exc))
    shape = (img.shape[0], img.shape[1])
    if roi is not None:
        region, source = polygon_region(roi, shape), "--roi polygon"
    elif name is not None:
        region, source = plate_region(name, z, shape)
    else:
        import numpy as np

        region, source = np.ones(shape, bool), "whole frame"
    numbers, flecks = measure(img, shrink(region), source)
    if annotated is not None and numbers["status"] == "ok":
        annotate(
            img,
            shrink(region),
            flecks,
            label or f"fleck {numbers['fleck_frac']:.3f} order {numbers['order']:.2f}",
            annotated,
        )
        numbers["annotated"] = str(annotated)
    return numbers


# ---- command line -----------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="print_quality.py", add_help=False)
    ap.add_argument("sub", choices=("check",))
    ap.add_argument("name")
    ap.add_argument("--frame", required=True)
    ap.add_argument("--z", type=float, default=0.0)
    ap.add_argument("--roi")
    ap.add_argument("--annotated")
    ap.add_argument("--label", default="")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("-h", "--help", action="store_true")
    return ap


def run_check(args: argparse.Namespace) -> dict[str, Any]:
    whole = args.roi == "whole"
    roi = None if args.roi is None or whole else parse_roi(args.roi)
    return quality_numbers(
        Path(args.frame).read_bytes(),
        name=None if whole else args.name,
        z=args.z,
        roi=roi,
        annotated=Path(args.annotated) if args.annotated else None,
        label=args.label,
    )


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0 if argv else 1
    args = build_parser().parse_args(argv)
    if args.help:
        print(USAGE)
        return 0
    out = run_check(args)
    print(json.dumps(out) if args.json else json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    from errors import ThreeDError

    try:
        sys.exit(main(sys.argv[1:]))
    except ThreeDError as exc:
        print(exc.render(), file=sys.stderr)
        sys.exit(exc.exit_code)
