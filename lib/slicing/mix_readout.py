"""mix_readout.py — a photo of the tester card → the measured colour of every swatch (`mix-readout/1`).

WHAT: `read_photo()` takes the tester manifest (printops.mix_tester/1 with its `readout` card
  layout, slicing/mix_card.py) and one photo of the printed card with the swatches lying on it:
  1. finds the card's ArUco fiducials (calib_rectify.detect, DICT_4X4_50) and fits the homography
     image px → card mm (needs 3 of 4); the photo is warped to a straight 10 px/mm image;
  2. decodes the card's QR and refuses a card that names another manifest (or none);
  3. white balance on the blank paper regions: a per-channel gain in linear light that maps the
     paper to `white_level` (as `3d mix target`), fitted as a plane over the regions so an uneven
     light across the card is corrected; or, with `white=N`, on the pure reference swatch N, which
     is mapped to its own spool colour;
  4. per swatch, the median in CIELAB of its patch (the face without its border; the number lies
     outside the face, so edges and label are never read), the measured hex, ΔE2000 to the
     predicted colour and to the target;
  5. an annotated PNG (every patch, the measured colour next to the predicted and the target).
WHY: epic #410 / issue #416: a number per swatch is what the model fit and the records (#417) need;
  by eye in chat the tester result reaches neither. A top-camera frame and a phone photo go through
  the same card, so both give the same numbers.
INVARIANTS: cv2, numpy and Pillow are imported inside the functions; every box comes from the
  manifest (nothing is guessed from the picture); a photo that cannot be trusted (fewer than 3
  fiducials, too coarse, another manifest's QR, a dark paper region) raises ReadoutError and never
  returns a guess.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from printops.color import hex_delta_e
from slicing import mix_card
from slicing.mix_target import (
    DEFAULT_WHITE_LEVEL,
    MIN_WHITE_CHANNEL,
    MixTargetError,
    decode_srgb,
    lab_pixels,
    lab_to_hex,
    load_image,
    median_lab,
    white_gain,
)

SCHEMA = "mix-readout/1"
MIN_FIDUCIALS = 3
MIN_PX_PER_MM = 2.5  # coarser than this and a 3 mm patch is under 8 px
WARN_FIDUCIAL_RMS_MM = 0.5
MAX_FIDUCIAL_RMS_MM = 1.5  # the patch inset: a worse fit puts a patch off its face
VALID_ALPHA = 250  # of 255: a patch must lie wholly inside the photo
MAX_PATCH_SPREAD = 8.0  # Lab distance of the 90th-percentile pixel from the median: a swatch must be flat
MAX_WORK_FACTOR = 4  # the photo is warped at most 4x finer than the card's 10 px/mm, then averaged down


class ReadoutError(ValueError):
    """The photo cannot be read as this tester's card."""


def _px_box(lay: dict[str, Any], box: list[float]) -> tuple[int, int, int, int]:
    """Card-mm box (Y up) → rectified-image pixels (x0, y0, x1, y1), Y down."""
    ppm, h_mm = lay["px_per_mm"], lay["card_mm"][1]
    return (
        round(box[0] * ppm),
        round((h_mm - box[3]) * ppm),
        round(box[2] * ppm),
        round((h_mm - box[1]) * ppm),
    )


def _rectify(img: Any, lay: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """The photo warped to the card at px_per_mm, and what the fiducials said about the photo."""
    import calib_rectify as cr
    import cv2
    import numpy as np

    grey = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    found = cr.detect(grey, lay["dictionary"])
    wanted = {int(f["id"]): f for f in lay["fiducials"]}
    ids = sorted(i for i in wanted if i in found)
    if len(ids) < MIN_FIDUCIALS:
        raise ReadoutError(
            f"{len(ids)} of {len(wanted)} card fiducials found (need {MIN_FIDUCIALS}): "
            "reshoot with the whole card in view, the four corner squares sharp and no glare on them"
        )
    src = np.vstack([found[i] for i in ids])
    dst = np.vstack(
        [
            cr.marker_corners_mm(
                tuple(wanted[i]["center_mm"]), float(wanted[i]["size_mm"])
            )
            for i in ids
        ]
    )
    h, _ = cv2.findHomography(src, dst, 0)
    if h is None or not np.isfinite(h).all() or abs(np.linalg.det(h)) < 1e-12:
        raise ReadoutError(
            "the card fiducials are degenerate (collinear or coincident): reshoot the whole card"
        )
    resid = np.linalg.norm(cr.apply_h(h, src) - dst, axis=1)
    rms = float(np.sqrt((resid**2).mean()))
    if rms > MAX_FIDUCIAL_RMS_MM:
        raise ReadoutError(
            f"the card fiducials fit a flat card with {rms:.2f} mm rms (over {MAX_FIDUCIAL_RMS_MM:g}): "
            "is another plate or card with the same markers in the frame, or the card bent or crumpled? "
            "lay it flat and shoot again"
        )
    marker_mm = float(next(iter(wanted.values()))["size_mm"])
    edges = [
        np.linalg.norm(found[i][(k + 1) % 4] - found[i][k])
        for i in ids
        for k in range(4)
    ]
    photo_ppm = float(np.mean(edges)) / marker_mm
    if photo_ppm < MIN_PX_PER_MM:
        raise ReadoutError(
            f"the card is {photo_ppm:.1f} px/mm in the photo, under {MIN_PX_PER_MM:g}: "
            "move the camera closer so the card fills the frame"
        )
    ppm = int(lay["px_per_mm"])
    work = ppm * max(1, min(MAX_WORK_FACTOR, int(photo_ppm // ppm)))
    w_mm, h_mm = lay["card_mm"]
    canvas = np.array(
        [[work, 0, 0], [0, -work, h_mm * work], [0, 0, 1]], dtype=np.float64
    )
    # a 4th channel of 255 says where the photo is; the warp leaves 0 where the card lies outside it
    img4 = np.dstack([img, np.full(img.shape[:2], 255, np.uint8)])
    out = cv2.warpPerspective(
        img4,
        canvas @ h,
        (round(w_mm * work), round(h_mm * work)),
        flags=cv2.INTER_CUBIC,
    )
    if work != ppm:
        out = cv2.resize(
            out, (round(w_mm * ppm), round(h_mm * ppm)), interpolation=cv2.INTER_AREA
        )
    info = {
        "found": len(ids),
        "of": len(wanted),
        "ids": ids,
        "missing": sorted(set(wanted) - set(ids)),
        "rms_mm": round(rms, 3),
        "photo_px_per_mm": round(photo_ppm, 2),
        "homography": h.tolist(),
    }
    if rms > WARN_FIDUCIAL_RMS_MM:
        info["warning"] = (
            f"fiducials fit the card with {rms:.2f} mm rms (over {WARN_FIDUCIAL_RMS_MM:g}): "
            "the card is bent or the lens distorts"
        )
    return out, info


def _decode_qr(img: Any, rect: Any, lay: dict[str, Any]) -> str:
    import cv2
    import numpy as np

    rect = np.ascontiguousarray(rect[:, :, :3])

    det = cv2.QRCodeDetector()
    x0, y0, x1, y1 = _px_box(lay, lay["qr"]["box_mm"])
    pad = round(mix_card.QR_QUIET * mix_card.QR_MODULE * lay["px_per_mm"])
    crop = rect[max(0, y0 - pad) : y1 + pad, max(0, x0 - pad) : x1 + pad]
    for frame in (crop, rect, img):
        text, _, _ = det.detectAndDecode(cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY))
        if text:
            return str(text)
    return ""


class _PaperLight:
    """The white reference: the blank paper regions, as a gain per channel at any card point."""

    def __init__(self, rect: Any, lay: dict[str, Any], white_level: int) -> None:
        import numpy as np

        self.level = white_level
        self.boxes: list[dict[str, Any]] = []
        pts, lin = [], []
        for box in lay["white_boxes"]:
            px = _patch_pixels(rect, lay, box)
            try:
                _, rgb = white_gain(px, white_level, "paper region")
            except MixTargetError as e:
                raise ReadoutError(
                    f"{e}: a paper region of the card is dark or shaded, reshoot in even light"
                ) from e
            self.boxes.append({"box_mm": box, "rgb": list(rgb)})
            pts.append([(box[0] + box[2]) / 2, (box[1] + box[3]) / 2])
            lin.append(decode_srgb(np.median(px, axis=0) / 255.0))
        a = np.hstack([np.ones((len(pts), 1)), np.array(pts)])
        self._lin = np.array(lin)
        self.model = "constant"
        self._coef = None
        if len(pts) >= 3 and np.linalg.matrix_rank(a) == 3:
            self._coef, *_ = np.linalg.lstsq(a, self._lin, rcond=None)
            self.model = "plane"
        self._target = decode_srgb(np.full(3, white_level / 255.0))

    def gain(self, x_mm: float, y_mm: float) -> Any:
        import numpy as np

        if self._coef is None:
            paper = self._lin.mean(axis=0)
        else:
            paper = np.array([1.0, x_mm, y_mm]) @ self._coef
        floor = decode_srgb(np.full(3, MIN_WHITE_CHANNEL / 255.0))
        return self._target / np.maximum(paper, floor)

    def describe(self) -> dict[str, Any]:
        return {
            "mode": "card",
            "model": self.model,
            "level": self.level,
            "regions": self.boxes,
        }


def _patch_pixels(rect: Any, lay: dict[str, Any], box: list[float]) -> Any:
    x0, y0, x1, y1 = _px_box(lay, box)
    h, w = rect.shape[:2]
    if x0 < 0 or y0 < 0 or x1 > w or y1 > h or x1 <= x0 or y1 <= y0:
        raise ReadoutError(f"a box {box} of the card lies outside the card image")
    part = rect[y0:y1, x0:x1]
    if part[:, :, 3].min() < VALID_ALPHA:
        raise ReadoutError(
            f"the part of the card at {box} (mm) is outside the photo: "
            "reshoot with the whole card in the frame"
        )
    return part[:, :, :3].reshape(-1, 3)


def _spread(px: Any, gain: Any, lab: tuple[float, float, float]) -> float:
    """90th percentile of the Lab distance of the balanced pixels of a patch from its median."""
    import numpy as np

    d = np.linalg.norm(lab_pixels(px, gain) - np.array(lab), axis=1)
    return float(np.percentile(d, 90))


def read_photo(
    manifest: dict[str, Any],
    photo: Path,
    out: Path,
    *,
    white: str = "card",
    white_level: int = DEFAULT_WHITE_LEVEL,
    check_qr: bool = True,
    manifest_name: str = "",
) -> dict[str, Any]:
    """Read the photo, write OUT.rectified.png, OUT.readout.png, OUT.readout.json; return the record."""
    import numpy as np
    from PIL import Image

    lay = manifest["readout"]
    mid = manifest["id"]
    if not 1 <= white_level <= 255:
        raise ReadoutError(f"white level {white_level} must be 1..255")
    try:
        img, sha = load_image(photo)
    except MixTargetError as e:
        raise ReadoutError(f"{e}: send a JPEG or PNG of the card") from e
    rect, fid = _rectify(img, lay)

    qr: dict[str, Any] = {"expected": mix_card.payload(mid)}
    if check_qr:
        text = _decode_qr(img, rect, lay)
        qr["read"] = text or None
        if not text:
            raise ReadoutError(
                "no QR code found on the card: reshoot with the QR sharp, or pass --no-qr-check "
                "to read the card without confirming which tester it belongs to"
            )
        if text != mix_card.payload(mid):
            named = text.removeprefix(mix_card.PAYLOAD_PREFIX)
            raise ReadoutError(
                f"the photo's QR names {named!r}, not this manifest ({mid}): "
                "it is the card of another tester"
            )
        qr["status"] = "match"
    else:
        qr["status"] = "skipped"

    entries: list[dict[str, Any]] = [
        {"kind": "candidate", **c} for c in manifest["candidates"]
    ] + [{"kind": "reference", **r} for r in manifest["references"]]
    by_number = {e["number"]: e for e in entries}
    cells = {c["number"]: c for c in lay["cells"]}

    paper: _PaperLight | None = None
    swatch_gain: Any = None
    if white == "card":
        paper = _PaperLight(rect, lay, white_level)
        white_info = paper.describe()
    else:
        try:
            num = int(white)
        except ValueError:
            raise ReadoutError(
                f"--white {white!r}: card, or the number of a pure reference swatch"
            ) from None
        ref = by_number.get(num)
        if ref is None or ref["kind"] != "reference":
            raise ReadoutError(
                f"--white {num}: not a pure reference swatch of this tester "
                f"(references: {sorted(n for n, e in by_number.items() if e['kind'] == 'reference')})"
            )
        try:
            px = _patch_pixels(rect, lay, cells[num]["patch_box"])
            _, ref_rgb = white_gain(px, white_level, f"swatch {num}")
        except MixTargetError as e:
            raise ReadoutError(f"{e}: swatch {num} is too dark to balance on") from e
        want = decode_srgb(
            np.array([int(ref["hex"][i : i + 2], 16) for i in (1, 3, 5)]) / 255.0
        )
        swatch_gain = want / decode_srgb(np.median(px, axis=0) / 255.0)
        white_info = {
            "mode": "swatch",
            "number": num,
            "rgb": list(ref_rgb),
            "maps_to": ref["hex"],
        }

    target = manifest["target"]
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    if "warning" in fid:
        warnings.append(fid["warning"])
    for num in sorted(by_number):
        entry, cell = by_number[num], cells[num]
        box = cell["patch_box"]
        px = _patch_pixels(rect, lay, box)
        gain = (
            paper.gain((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
            if paper is not None
            else swatch_gain
        )
        lab = median_lab(px, gain)
        measured = lab_to_hex(lab)
        spread = _spread(px, gain, lab)
        if spread > MAX_PATCH_SPREAD:
            warnings.append(
                f"swatch {num}: the patch is not flat (spread {spread:.1f}): "
                "is it lying in its frame, without glare or a shadow?"
            )
        rows.append(
            {
                "number": num,
                "kind": entry["kind"],
                "pattern": entry.get("pattern"),
                "slot": entry.get("slot"),
                "predicted_hex": entry["hex"],
                "measured_hex": measured,
                "measured_lab": [round(v, 2) for v in lab],
                "delta_e_predicted": round(hex_delta_e(measured, entry["hex"]), 2),
                "delta_e_target": round(hex_delta_e(measured, target), 2),
                "samples": len(px),
                "spread": round(spread, 2),
                "patch_box_mm": box,
                "calibration": entry.get("calibration"),
            }
        )
    mixes = [r for r in rows if r["kind"] == "candidate"]
    summary: dict[str, Any] = {
        "swatches": len(rows),
        "mean_delta_e_predicted": round(
            sum(r["delta_e_predicted"] for r in rows) / len(rows), 2
        ),
        "max_delta_e_predicted": max(r["delta_e_predicted"] for r in rows),
    }
    if mixes:
        best = min(mixes, key=lambda r: r["delta_e_target"])
        summary["closest_to_target"] = {
            "number": best["number"],
            "pattern": best["pattern"],
            "measured_hex": best["measured_hex"],
            "delta_e_target": best["delta_e_target"],
        }

    out.parent.mkdir(parents=True, exist_ok=True)
    files = {
        "rectified": out.with_name(out.name + ".rectified.png"),
        "annotated": out.with_name(out.name + ".readout.png"),
        "json": out.with_name(out.name + ".readout.json"),
    }
    Image.fromarray(rect[:, :, :3]).save(files["rectified"])
    annotate(rect, lay, rows, target).save(files["annotated"])
    record: dict[str, Any] = {
        "schema": SCHEMA,
        "manifest": {
            "id": mid,
            "schema": manifest["schema"],
            "file": manifest_name,
            "target": target,
        },
        "photo": {
            "file": str(photo),
            "sha256": sha,
            "size": [int(img.shape[1]), int(img.shape[0])],
        },
        "fiducials": fid,
        "qr": qr,
        "white": white_info,
        "swatches": rows,
        "summary": summary,
        "warnings": warnings,
        "files": {k: v.name for k, v in files.items()},
    }
    files["json"].write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    return record


def annotate(
    rect: Any, lay: dict[str, Any], rows: list[dict[str, Any]], target: str
) -> Any:
    """The rectified card with every patch boxed, beside a table: predicted | measured | target."""
    from PIL import Image, ImageDraw, ImageFont

    ppm = lay["px_per_mm"]
    h_px, w_px = rect.shape[:2]
    font = ImageFont.load_default(size=22)
    small = ImageFont.load_default(size=16)
    row_h, table_w = 56, 640
    canvas = Image.new(
        "RGB", (w_px + table_w, max(h_px, 70 + row_h * len(rows))), "white"
    )
    canvas.paste(Image.fromarray(rect[:, :, :3]), (0, 0))
    d = ImageDraw.Draw(canvas)
    for r in rows:
        x0, y0, x1, y1 = _px_box(lay, r["patch_box_mm"])
        d.rectangle((x0, y0, x1, y1), outline=(0, 200, 60), width=2)
        d.text((x0 + 2, y0 - 26), str(r["number"]), fill=(0, 140, 40), font=font)
    x = w_px + 16
    d.text((x, 10), "n   predicted  measured  target", fill="black", font=small)
    d.text(
        (x, 34),
        f"dE2000 to predicted / to target ({target}); card at {ppm} px/mm",
        fill=(80, 80, 80),
        font=small,
    )
    for i, r in enumerate(rows):
        y = 64 + i * row_h
        d.text((x, y + 12), str(r["number"]), fill="black", font=font)
        for k, hex_ in enumerate((r["predicted_hex"], r["measured_hex"], target)):
            cx = x + 40 + k * 70
            d.rectangle((cx, y, cx + 60, y + row_h - 8), fill=hex_, outline="black")
        d.text(
            (x + 40 + 3 * 70 + 8, y + 4),
            f"{r['measured_hex']}  dE {r['delta_e_predicted']:.1f} / {r['delta_e_target']:.1f}",
            fill="black",
            font=small,
        )
        d.text(
            (x + 40 + 3 * 70 + 8, y + 24),
            r["pattern"] or f"T{r['slot']} pure",
            fill=(80, 80, 80),
            font=small,
        )
    return canvas
