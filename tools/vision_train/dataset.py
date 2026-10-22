"""dataset.py — build the labelled frame set for tier (b) training, with publish-time sanitation.

Dev tool, not part of the `3d` runtime. Needs numpy + opencv-python-headless:
    uv venv ~/.cache/3d-cli/venv-vision-data && uv pip install numpy "opencv-python-headless<5" \\
        huggingface_hub     # (OpenCV 4.x: 5.x dropped the Haar cascades)

    dataset.py build OUT [--third-party] [--sources sources.json]
        own frames (sources.json "own") -> OUT/own/frames/<id>.jpg, sanitised; with --third-party
        also the public collection (sources.json "third_party") -> OUT/third_party/frames/ for
        server-side training and evaluation ONLY (never published: licences differ)
        -> OUT/labels.jsonl, OUT/sanitation.json (what was removed), OUT/DATASET_SHA256 (the
        published rows) and OUT/TRAINING_SHA256 (every row the server trains on); a frame the
        face/person detector refuses is kept only as OUT/private/frames (provenance own_private)
    dataset.py check OUT        re-run every sanitation check on the published part of OUT
    dataset.py sheets OUT       contact sheets of the published frames, for the human review
    dataset.py card OUT         OUT/README.md, the Hugging Face dataset card

SANITATION (every published frame; the dataset is PUBLIC):
  * decoded and re-encoded with OpenCV: EXIF, ICC and every other JPEG segment is gone;
  * cropped to the printer's `keep` rectangle (the bed and part area): the back wall of the U1
    chamber with its QR stickers, rooms, windows and screens outside it never leave the machine;
  * QR codes inside the crop are detected (OpenCV, native size and 2x) and painted over;
  * a face / person detector (OpenCV Haar frontal+profile faces, HOG pedestrian detector) runs on
    the published pixels; a frame with any hit is REFUSED (dropped and counted), never published;
  * file names are the sha256 of the published bytes: no timestamps, G-code or project names;
  * sanitation.json lists, per source, what was dropped and masked, and the tool versions.
`check` repeats the detectors and a metadata/text scan over what is about to be published; the
publish step (publish.py) refuses to run when it finds anything.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
MAX_SIDE = 1280
JPEG_QUALITY = 90
QR_PAD = 0.2  # grow each detected QR polygon by this fraction before painting it
FILL = (127, 127, 127)
# Text that must never appear in anything published: IPv4 addresses, .local hostnames, home dirs.
SECRET_PATTERNS = (
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    re.compile(r"\b[\w-]+\.local\b"),
    re.compile(r"/home/\w+"),
    re.compile(r"(?i)\b(?:token|password|passwd|secret|api[_-]?key)\b\s*[:=]"),
    re.compile(r"hf_[A-Za-z0-9]{20,}"),
)


def _cv2() -> Any:
    import cv2

    return cv2


# ---- sanitation ---------------------------------------------------------------------------------


def person_hits(bgr: Any) -> dict[str, int]:
    """Counts of face / person detections on a frame (all zero = clean). Conservative settings:
    a false hit only costs one frame, a miss would publish a person."""
    cv2 = _cv2()
    h, w = bgr.shape[:2]
    scale = 640.0 / w if w > 640 else 1.0
    small = cv2.resize(bgr, (int(w * scale), int(h * scale))) if scale != 1.0 else bgr
    gray = cv2.equalizeHist(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
    hits = {"face_front": 0, "face_profile": 0, "person": 0}
    for key, name in (
        ("face_front", "haarcascade_frontalface_default.xml"),
        ("face_profile", "haarcascade_profileface.xml"),
    ):
        cascade = cv2.CascadeClassifier(str(Path(cv2.data.haarcascades) / name))
        hits[key] = len(
            cascade.detectMultiScale(
                gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30)
            )
        )
    if small.shape[0] >= 128 and small.shape[1] >= 64:  # smaller than the 64x128 window: no person fits (and OpenCV 4.13 crashes)
        hog = cv2.HOGDescriptor()
        hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        _rects, weights = hog.detectMultiScale(
            small, winStride=(8, 8), padding=(8, 8), scale=1.05
        )
        hits["person"] = int(sum(1 for wgt in weights if float(wgt) >= 0.5))
    return hits


def qr_polygons(bgr: Any) -> list[Any]:
    """Corner arrays of every QR code found at native size and at 2x."""
    import numpy as np

    cv2 = _cv2()
    found: list[Any] = []
    det = cv2.QRCodeDetector()
    for factor in (1.0, 2.0):
        img = (
            bgr
            if factor == 1.0
            else cv2.resize(
                bgr, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC
            )
        )
        try:
            ok, pts = det.detectMulti(img)
        except cv2.error:
            continue
        if ok and pts is not None:
            found += [np.asarray(p, np.float32) / factor for p in pts]
    return found


def mask_qr(bgr: Any) -> tuple[Any, int]:
    """The frame with every QR code painted over (polygon grown by QR_PAD); and how many."""
    import numpy as np

    cv2 = _cv2()
    polys = qr_polygons(bgr)
    out = bgr.copy()
    for poly in polys:
        centre = poly.mean(axis=0)
        grown = centre + (poly - centre) * (1.0 + QR_PAD)
        cv2.fillPoly(out, [np.round(grown).astype(np.int32)], FILL)
    return out, len(polys)


def crop_keep(bgr: Any, keep: list[float]) -> Any:
    h, w = bgr.shape[:2]
    x1, y1, x2, y2 = keep
    return bgr[
        int(y1 * h) : max(int(y1 * h) + 1, int(y2 * h)),
        int(x1 * w) : max(int(x1 * w) + 1, int(x2 * w)),
    ]


def sanitise(bgr: Any, keep: list[float]) -> tuple[bytes, dict[str, Any]]:
    """(the JPEG bytes, report). Order: crop -> QR mask -> encode -> person check on the DECODED
    bytes (the detectors answer differently on pixels that went through JPEG once more, and the
    published pixels are what counts). `report["refused"]` is True when a face/person detector
    fired: such a frame must never be published (build keeps it as a private training frame)."""
    cv2 = _cv2()
    img = crop_keep(bgr, keep)
    h, w = img.shape[:2]
    if max(h, w) > MAX_SIDE:
        s = MAX_SIDE / max(h, w)
        img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    img, n_qr = mask_qr(img)
    data = encode(img)
    hits = person_hits(decode(data))
    report: dict[str, Any] = {"qr_masked": n_qr, "person_hits": hits, "refused": any(hits.values())}
    return data, report


def decode(data: bytes) -> Any:
    import numpy as np

    return _cv2().imdecode(np.frombuffer(data, np.uint8), _cv2().IMREAD_COLOR)


def encode(img: Any) -> bytes:
    cv2 = _cv2()
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        raise RuntimeError("jpeg encode failed")
    return bytes(buf)


def has_metadata(jpeg: bytes) -> list[str]:
    """Names of any metadata segments (APPn other than the JFIF APP0, COM) in a JPEG."""
    found, i = [], 2
    while i + 4 <= len(jpeg) and jpeg[i] == 0xFF:
        marker = jpeg[i + 1]
        if marker in (0xDA, 0xD9):  # scan data / end: no more header segments
            break
        length = int.from_bytes(jpeg[i + 2 : i + 4], "big")
        if marker == 0xFE or (0xE1 <= marker <= 0xEF):
            found.append(f"{'COM' if marker == 0xFE else f'APP{marker - 0xE0}'}")
        i += 2 + length
    return found


# ---- build --------------------------------------------------------------------------------------


def _expand(pattern: str) -> list[Path]:
    return [Path(p) for p in sorted(glob.glob(str(Path(pattern).expanduser())))]


def build(out: Path, sources: dict[str, Any], third_party: bool) -> None:
    cv2 = _cv2()
    own_dir = out / "own" / "frames"
    own_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "own": {},
        "third_party": {},
        "tools": {
            "opencv": [int(p) for p in cv2.__version__.split(".")],
            "max_side": MAX_SIDE,
            "jpeg_quality": JPEG_QUALITY,
        },
    }
    for src in sources["own"]:
        stat = {
            "found": 0,
            "published": 0,
            "refused_person_or_face": 0,
            "kept_private": 0,
            "frames_with_qr_masked": 0,
            "unreadable": 0,
            "duplicates": 0,
        }
        for n, path in enumerate(_expand(src["glob"])):
            if n % int(src.get("every", 1)):
                continue
            stat["found"] += 1
            img = cv2.imread(str(path))
            if img is None:
                stat["unreadable"] += 1
                continue
            data, rep = sanitise(img, src["keep"])
            refused = rep["refused"]
            stat["refused_person_or_face"] += refused
            stat["frames_with_qr_masked"] += bool(rep["qr_masked"])
            digest = hashlib.sha256(data).hexdigest()
            folder, provenance = ("private", "own_private") if refused else ("own", "own")
            dest = out / folder / "frames" / f"{digest[:16]}.jpg"
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                stat["duplicates"] += 1
                continue
            dest.write_bytes(data)
            h, w = decode(data).shape[:2]
            rows.append(
                {
                    "id": digest[:16],
                    "file": f"{folder}/frames/{digest[:16]}.jpg",
                    "label": src["label"],
                    "split": src["split"],
                    "group": src["group"],
                    "printer": src["printer"],
                    "provenance": provenance,
                    "source": src["id"],
                    "width": w,
                    "height": h,
                    "sha256": digest,
                    "qr_masked": rep["qr_masked"],
                }
            )
            stat["kept_private" if refused else "published"] += 1
        report["own"][src["id"]] = stat
        print(f"own {src['id']}: {stat}", flush=True)
    if third_party:
        rows += _build_third_party(out, sources["third_party"], report)
    rows.sort(key=lambda r: r["id"])
    (out / "labels.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    )

    def fingerprint(selected: list[dict[str, Any]]) -> str:
        lines = "".join(f"{r['id']}|{r['sha256']}|{r['label']}|{r['split']}\n" for r in selected)
        return hashlib.sha256(lines.encode()).hexdigest()

    digest = fingerprint([r for r in rows if r["provenance"] == "own"])  # what gets published
    (out / "DATASET_SHA256").write_text(digest + "\n")
    (out / "TRAINING_SHA256").write_text(fingerprint(rows) + "\n")  # everything the server trains on
    report["frames"] = {
        "own": sum(r["provenance"] == "own" for r in rows),
        "own_private": sum(r["provenance"] == "own_private" for r in rows),
        "third_party": sum(r["provenance"] == "third_party" for r in rows),
    }
    report["dataset_sha256"] = digest
    report["training_sha256"] = fingerprint(rows)
    (out / "sanitation.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    print(f"built {out}: {report['frames']}  dataset sha256 {digest[:16]}")


def _build_third_party(
    out: Path, spec: dict[str, Any], report: dict[str, Any]
) -> list[dict[str, Any]]:
    """Training/eval copies of the public collection, resized; labels mapped to tier (b)'s classes.
    These rows carry `provenance: third_party` and are excluded by publish.py."""
    cv2 = _cv2()
    root = Path(spec["root"]).expanduser()
    dest_dir = out / "third_party" / "frames"
    dest_dir.mkdir(parents=True, exist_ok=True)
    taken: dict[tuple[str, str], int] = {}
    rows = []
    for line in Path(spec["manifest"]).expanduser().read_text().splitlines():
        m = json.loads(line)
        label = spec["labels"].get(m["label"])
        if label is None:
            continue
        key = (m["source"], label)
        if taken.get(key, 0) >= spec["max_per_label_and_source"]:
            continue
        img = cv2.imread(str(root / m["path"]))
        if img is None:
            continue
        taken[key] = taken.get(key, 0) + 1
        h, w = img.shape[:2]
        if max(h, w) > MAX_SIDE:
            s = MAX_SIDE / max(h, w)
            img = cv2.resize(
                img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA
            )
        data = encode(img)
        digest = hashlib.sha256(data).hexdigest()
        (dest_dir / f"{digest[:16]}.jpg").write_bytes(data)
        h, w = img.shape[:2]
        rows.append(
            {
                "id": digest[:16],
                "file": f"third_party/frames/{digest[:16]}.jpg",
                "label": label,
                "split": m["split"],
                "group": f"{m['source']}",
                "printer": "other",
                "provenance": "third_party",
                "source": m["source"],
                "width": w,
                "height": h,
                "sha256": digest,
                "qr_masked": 0,
                "licence": m.get("licence", "unknown"),
            }
        )
    report["third_party"] = {f"{s}/{lab}": n for (s, lab), n in sorted(taken.items())}
    print(
        f"third_party: {len(rows)} frames from {len({r['source'] for r in rows})} sources (never published)",
        flush=True,
    )
    return rows


# ---- check / sheets / card ------------------------------------------------------------------------


def published_rows(out: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (out / "labels.jsonl").read_text().splitlines()
        if line.strip() and '"provenance": "own"' in line
    ]


def check(out: Path) -> list[str]:
    """Every finding that would stop a publish (empty list = clean)."""
    cv2 = _cv2()
    problems: list[str] = []
    rows = published_rows(out)
    if not rows:
        problems.append("no own frames to publish")
    for r in rows:
        path = out / r["file"]
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != r["sha256"]:
            problems.append(f"{r['file']}: sha256 differs from labels.jsonl")
        if segs := has_metadata(data):
            problems.append(f"{r['file']}: metadata segments {segs}")
        img = cv2.imdecode(
            __import__("numpy").frombuffer(data, __import__("numpy").uint8),
            cv2.IMREAD_COLOR,
        )
        if img is None:
            problems.append(f"{r['file']}: undecodable")
            continue
        if hits := {k: v for k, v in person_hits(img).items() if v}:
            problems.append(f"{r['file']}: face/person detector hit {hits}")
        if (n := len(qr_polygons(img))) and cv2.QRCodeDetector().detectAndDecode(img)[
            0
        ]:
            problems.append(f"{r['file']}: a decodable QR code remains ({n})")
    for text_file in (out / "labels.jsonl", out / "sanitation.json", out / "README.md"):
        if text_file.is_file():
            text = text_file.read_text()
            hits = [(pat, pat.search(text)) for pat in SECRET_PATTERNS]
            problems += [
                f"{text_file.name}: matches {pat.pattern!r}: {m.group(0)[:30]!r}"
                for pat, m in hits
                if m
            ]
    third = [
        r
        for r in (
            json.loads(line)
            for line in (out / "labels.jsonl").read_text().splitlines()
            if line.strip()
        )
        if r["provenance"] == "third_party"
    ]
    if third and not (out / "third_party").is_dir():
        problems.append(
            "labels.jsonl lists third-party rows but OUT/third_party is missing"
        )
    return problems


def cmd_sheets(out: Path) -> None:
    import numpy as np

    cv2 = _cv2()
    rows = published_rows(out)
    review = out / "review"
    review.mkdir(exist_ok=True)
    for stale in review.glob("sheet_*.jpg"):
        stale.unlink()
    per = 30
    for s in range(0, len(rows), per):
        tiles = []
        for r in rows[s : s + per]:
            im = cv2.resize(cv2.imread(str(out / r["file"])), (320, 180))
            cv2.putText(
                im,
                f"{r['label']} {r['source'][:22]}",
                (4, 14),
                0,
                0.42,
                (0, 255, 255),
                1,
            )
            tiles.append(im)
        while len(tiles) % 6:
            tiles.append(np.zeros_like(tiles[0]))
        sheet = np.vstack(
            [np.hstack(tiles[i : i + 6]) for i in range(0, len(tiles), 6)]
        )
        cv2.imwrite(str(review / f"sheet_{s // per:03d}.jpg"), sheet)
    print(f"{len(rows)} frames -> {review}")


CARD = """---
license: cc-by-4.0
task_categories:
- image-classification
tags:
- 3d-printing
- fdm
- print-failure
- snapmaker-u1
size_categories:
- n<1K
pretty_name: 3d-cli print-failure frames
---

# 3d-cli print-failure frames

Camera frames from two FDM printers (Snapmaker U1, Creality K1 SE), labelled for the tier (b)
whole-frame vision model of [3d-cli](https://github.com/alex-mextner/3d-cli) (`3d printer vision`;
code: `tools/vision_train`). Classes: `healthy`, `spaghetti`, `part_detached`, `empty_bed`.

**This set is small and unbalanced.** {n_healthy} healthy frames from printing and a handful of
failure frames ({counts}). It is meant to be extended with frames recorded by `3d printer vision record`; it is
not enough to train a detachment detector on its own. `part_detached` is one event (a large
honeycomb panel that peeled off the plate): do not read a metric computed on it as evidence.

## Files

- `own/frames/<id>.jpg`: sanitised frames (`<id>` = first 16 hex of the file's sha256).
- `labels.jsonl`: `id, file, label, split (train|test), group (one print or event: keep a group
  on one side of a split), printer, source, width, height, sha256, qr_masked`.
- `sanitation.json`: per source how many frames were found, published, refused or masked, and the
  tool versions. `DATASET_SHA256`: sha256 over the sorted `id|sha256|label|split` lines of the
  published rows (the id of the dataset revision a model card pins).

## Sanitation

Frames were decoded and re-encoded (EXIF/ICC removed), cropped to the printer's bed-and-part area
(rooms, windows, screens and the chamber's back wall with its QR stickers are outside the crop),
QR codes were painted over, and a face and person detector (OpenCV Haar + HOG) refused any frame
with a hit ({refused} frames refused). File names carry no timestamps or project names. The
automatic checks are no proof: contact sheets of every published frame were reviewed by a human
before publishing. If you find a frame that should not be here, open an issue on the GitHub repo.

## Licence and provenance

All frames are the owner's own captures: CC-BY-4.0. The prints on the bed are the owner's own
models and models downloaded from model sites; no model files are included. The public frame
collections used for evaluation next to this set (spaghetti, bed-adhesion failures, ...) carry
their own licences and are **not** redistributed here; their provenance is documented in the
3d-cli repository (docs/research/printer-vision-tuning.md).

## Revision

Dataset sha256 `{sha}`; built by `tools/vision_train/dataset.py` at 3d-cli commit `{commit}`.
"""


def cmd_card(out: Path, commit: str) -> None:
    rows = published_rows(out)
    rep = json.loads((out / "sanitation.json").read_text())
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["label"]] = counts.get(r["label"], 0) + 1
    refused = sum(s["refused_person_or_face"] for s in rep["own"].values())
    fail = ", ".join(
        f"{n} {lab}" for lab, n in sorted(counts.items()) if lab != "healthy"
    )
    text = CARD.format(
        n_healthy=counts.get("healthy", 0),
        counts=fail or "none",
        refused=refused,
        sha=rep["dataset_sha256"],
        commit=commit,
    )
    (out / "README.md").write_text(text)
    print(f"wrote {out / 'README.md'}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("out", type=Path)
    b.add_argument("--third-party", action="store_true")
    b.add_argument("--sources", type=Path, default=HERE / "sources.json")
    for name in ("check", "sheets"):
        sub.add_parser(name).add_argument("out", type=Path)
    c = sub.add_parser("card")
    c.add_argument("out", type=Path)
    c.add_argument("--commit", default="unknown")
    args = ap.parse_args(argv)
    if args.cmd == "build":
        build(args.out, json.loads(args.sources.read_text()), args.third_party)
    elif args.cmd == "check":
        problems = check(args.out)
        print(
            "\n".join(problems)
            if problems
            else "clean: no metadata, no face/person hit, no decodable QR, no secret-looking text"
        )
        return 1 if problems else 0
    elif args.cmd == "sheets":
        cmd_sheets(args.out)
    else:
        cmd_card(args.out, args.commit)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
