"""vision_detect.py — the pixel half of `3d printer vision`: spaghetti model, frame checks, live loop, eval.

Run by `3d printer vision` through cli.pyrun with numpy + OpenCV + onnxruntime (issue
https://github.com/alex-mextner/3d-cli/issues/150). The decision logic, camera access and the
printer's own NPU verdicts are stdlib code in printer_ops/vision.py; this file only turns
pixels into a `FrameScore` and wires the loop.

MODEL: Obico's open "failure" detector (YOLOv2, one class; TheSpaghettiDetective/obico-server,
AGPL-3.0). The ONNX weights are NOT shipped with 3d-cli: `ensure_model()` downloads them from
obico.io into ~/.cache/3d-cli/vision/models/ on first use and checks a pinned sha256. Inference
and post-processing here are 3d-cli's own code: plain resize to 416x416 RGB /255, boxes with
confidence > 0.08, greedy NMS IoU 0.45, frame score p = sum of kept confidences (what the Obico
server feeds its temporal logic). Box height uses the frame HEIGHT (obico ml_api lib/onnx.py
scales it by the width).

Measured on real Snapmaker U1 footage (2026-10-14): p stays <= 0.11 on 4,082 frames of six
healthy timelapses and is 0.49..3.2 on every frame of the 13:49-13:51 UTC spaghetti episode.
CPU cost ~0.1 s/frame with 2 threads. PrintGuard (GPL-2.0, ShuffleNet) was tried and scored
0/22 on the U1 failure frames, so it is not used.

CLASSIC CHECKS (OpenCV): mean brightness (lights off / covered lens) per frame; the frozen-frame
check is byte-level in printer_ops/vision.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

LIB = Path(__file__).resolve().parent
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from printer_ops.vision import Box, FailureTracker, FrameScore, TrackerParams  # noqa: E402

if TYPE_CHECKING:
    import numpy as np


@dataclass(frozen=True)
class ModelSpec:
    name: str
    url: str
    sha256: str
    size: int
    licence: str
    source: str


OBICO = ModelSpec(
    name="obico-failure-5a6b1be1fa.onnx",
    url="https://www.obico.io/static_pub/ml-models/model-weights-5a6b1be1fa.onnx",
    sha256="0a6ebd8e30dbf6a450c50f9c0a5406f04ba7eb1c99fd5996e888c78bb383b9aa",
    size=202_223_918,
    licence="AGPL-3.0 (part of obico-server; no separate weights licence)",
    source="https://github.com/TheSpaghettiDetective/obico-server",
)
INPUT = 416
CONF_MIN = 0.08
NMS_IOU = 0.45
WAKE_S = 5.0  # after camera.start_monitor the U1 writes a fresh monitor.jpg within ~4 s


def models_dir() -> Path:
    from cli.paths import cache_dir

    return cache_dir() / "vision" / "models"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_model(
    spec: ModelSpec = OBICO, *, fetch: bool = True, opener: Any = urllib.request.urlopen
) -> Path:
    """The verified local weights file; downloads it once (to .part, sha256-checked, then
    renamed) when missing. The weights stay in the user's cache and are never vendored."""
    from errors import MissingDependency, ThreeDError

    path = models_dir() / spec.name
    if path.is_file():
        if _sha256(path) == spec.sha256:
            return path
        # Corrupted or replaced cache file: never hand it to onnxruntime; fetch a fresh copy.
        print(f"vision: {path} fails its sha256 check; re-downloading", file=sys.stderr)
    if not fetch:
        raise MissingDependency(
            f"spaghetti model {spec.name}",
            install="3d printer vision fetch-model",
            degrades="spaghetti detection cannot run",
            command="printer vision",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    # A per-process temp name: concurrent first runs / fetch-model must not share one .part.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{spec.name}.", suffix=".part")
    os.close(fd)
    part = Path(tmp)
    print(
        f"vision: downloading {spec.url} ({spec.size / 1e6:.0f} MB, {spec.licence}; {spec.source})",
        file=sys.stderr,
        flush=True,
    )
    try:
        with opener(spec.url, timeout=60) as r, open(part, "wb") as out:
            shutil.copyfileobj(r, out, 1 << 20)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    got = _sha256(part)
    if got != spec.sha256:
        part.unlink(missing_ok=True)
        raise ThreeDError(
            f"downloaded {spec.name} has sha256 {got}, expected {spec.sha256}",
            command="printer vision",
            remediation=["Retry; if it persists the upstream file changed."],
        )
    os.replace(part, path)
    return path


def _threads() -> int:
    from cli.resources import max_workers

    return max(1, min(2, max_workers()))


def nms(boxes: np.ndarray, confs: np.ndarray, iou: float = NMS_IOU) -> list[int]:
    """Greedy non-maximum suppression; boxes are x1,y1,x2,y2. Returns kept indices, best first."""
    import numpy as np

    order = confs.argsort()[::-1]
    area = np.clip(boxes[:, 2] - boxes[:, 0], 0, None) * np.clip(
        boxes[:, 3] - boxes[:, 1], 0, None
    )
    keep: list[int] = []
    while order.size:
        i = int(order[0])
        keep.append(i)
        rest = order[1:]
        w = np.clip(
            np.minimum(boxes[i, 2], boxes[rest, 2])
            - np.maximum(boxes[i, 0], boxes[rest, 0]),
            0,
            None,
        )
        h = np.clip(
            np.minimum(boxes[i, 3], boxes[rest, 3])
            - np.maximum(boxes[i, 1], boxes[rest, 1]),
            0,
            None,
        )
        inter = w * h
        order = rest[inter / (area[i] + area[rest] - inter + 1e-9) <= iou]
    return keep


def luma(bgr: np.ndarray) -> float:
    """Mean brightness 0..255 on a 1/8 thumbnail (lights off, covered lens)."""
    import cv2

    small = cv2.resize(
        bgr,
        (max(1, bgr.shape[1] // 8), max(1, bgr.shape[0] // 8)),
        interpolation=cv2.INTER_AREA,
    )
    return float(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).mean())


class ObicoScorer:
    """Frame -> FrameScore with the Obico ONNX model on the CPU."""

    def __init__(self, model: Path, threads: int | None = None) -> None:
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads or _threads()
        opts.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(model), opts, providers=["CPUExecutionProvider"]
        )
        self.input = self.session.get_inputs()[0].name

    def score_bgr(self, bgr: np.ndarray) -> FrameScore:
        import cv2
        import numpy as np

        x = cv2.cvtColor(cv2.resize(bgr, (INPUT, INPUT)), cv2.COLOR_BGR2RGB)
        x = (x.transpose(2, 0, 1)[None].astype(np.float32)) / 255.0
        boxes, confs = self.session.run(None, {self.input: x})
        c = confs.reshape(-1)
        b = boxes.reshape(-1, 4)
        sel = c > CONF_MIN
        b, c = b[sel], c[sel]
        kept = nms(b, c) if c.size else []
        out = []
        for i in kept:
            x1, y1, x2, y2 = (float(v) for v in np.clip(b[i], 0.0, 1.0))
            out.append(Box(x1, y1, x2, y2, float(c[i])))
        return FrameScore(
            p=float(sum(bx.conf for bx in out)), boxes=out, luma=luma(bgr)
        )

    def __call__(self, jpeg: bytes) -> FrameScore:
        return self.score_bgr(decode(jpeg))


def decode(jpeg: bytes) -> np.ndarray:
    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("not a decodable image")
    return img


def annotate(jpeg: bytes, score: FrameScore) -> bytes:
    """The frame with the detection boxes drawn (evidence for the event)."""
    import cv2

    img = decode(jpeg)
    h, w = img.shape[:2]
    for b in score.boxes:
        p1, p2 = (int(b.x1 * w), int(b.y1 * h)), (int(b.x2 * w), int(b.y2 * h))
        cv2.rectangle(img, p1, p2, (0, 0, 255), max(2, w // 480))
        cv2.putText(
            img,
            f"{b.conf:.2f}",
            (p1[0], max(20, p1[1] - 6)),
            cv2.FONT_HERSHEY_SIMPLEX,
            max(0.6, w / 1600),
            (0, 0, 255),
            2,
        )
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes() if ok else jpeg


# ---- live loop ----------------------------------------------------------------------------------


def _device_camera(device: Any, args: argparse.Namespace) -> Any:
    from errors import UsageError
    from printer_ops.vision import U1_MONITOR, MoonrakerCamera

    if device.adapter != "moonraker":
        raise UsageError(
            f"{device.name}: no camera access for adapter {device.adapter}",
            command="printer vision",
            remediation=[
                "Only Moonraker printers are supported (the K1 SE has no camera "
                "without Creality's AI camera accessory)."
            ],
        )
    cfg = (getattr(device, "watch", None) or {}).get("vision") or {}
    cfg = cfg if isinstance(cfg, dict) else {}
    snapshot = args.snapshot or str(cfg.get("snapshot") or U1_MONITOR)
    keep = False if args.no_keepalive else cfg.get("keepalive")
    return MoonrakerCamera(
        device.host,
        device.port,
        snapshot=snapshot,
        keepalive=None if keep is None else bool(keep),
    )


def run_live(args: argparse.Namespace) -> int:
    from cli.paths import cache_dir
    from printer_ops import make_adapter
    from printer_ops.base import PrinterCommError
    from printer_ops.devices import get_device
    from printer_ops.vision import KlippyLogTap, VisionMonitor
    from printer_ops.watch import severity_rank
    from printer_ops.watch_sinks import Dispatcher

    device = get_device(args.printer)
    camera = _device_camera(device, args)
    adapter = make_adapter(device)
    tap = None if args.no_npu else KlippyLogTap(device.host, device.port)
    scorer = ObicoScorer(ensure_model())
    evidence = (
        Path(args.evidence) if args.evidence else cache_dir() / "vision" / "evidence"
    )
    monitor = VisionMonitor(
        device.name, scorer, annotate=annotate, evidence_dir=evidence
    )
    dispatcher = None
    if not args.once:
        dispatcher = Dispatcher.for_devices([device], dry_run=args.dry_run)
        if args.min_severity:
            severity_rank(args.min_severity)
            dispatcher.min_severity[device.name] = args.min_severity
    while True:
        t0 = time.time()
        report: dict[str, Any] = {"printer": device.name}
        try:
            st = adapter.status()
            monitor.set_job(st.state, st.filename)
            report.update(state=st.state, file=st.filename)
        except PrinterCommError as exc:
            report["error"] = str(exc)
        frame = None
        # Keep the U1 camera awake and fetch frames only while printing (or for --once): the
        # monitor costs the printer a capture every ~0.5 s for as long as it is awake.
        if monitor.printing or args.once:
            try:
                if camera.keepalive(t0) and args.once:
                    time.sleep(
                        WAKE_S
                    )  # a sleeping monitor writes its first fresh frame within ~4 s
                frame = camera.grab()
            except PrinterCommError as exc:
                report["camera_error"] = str(exc)
        score, events = monitor.step(frame)
        if tap is not None:
            try:
                events += monitor.npu(tap.poll(), time.time())
            except PrinterCommError as exc:
                report["npu_error"] = str(exc)
        if args.once:
            if score is None and frame is not None:
                score = scorer(
                    frame.jpeg
                )  # not printing: still show what the model sees
            if score is not None:
                report.update(
                    p=round(score.p, 3),
                    luma=round(score.luma, 1),
                    boxes=[
                        {k: round(v, 3) for k, v in vars(b).items()}
                        for b in score.boxes
                    ],
                )
            report["trend"] = round(monitor.tracker.adjusted, 3)
            report["events"] = [e.to_json() for e in events]
            if frame is not None and args.save:
                Path(args.save).write_bytes(
                    annotate(frame.jpeg, score) if score and score.boxes else frame.jpeg
                )
                report["frame"] = args.save
            print(json.dumps(report, ensure_ascii=False))
            return 0
        if events and dispatcher is not None:
            dispatcher.process(events)
        if args.verbose:
            print(
                json.dumps(
                    {
                        **report,
                        "p": None if score is None else round(score.p, 3),
                        "trend": round(monitor.tracker.adjusted, 3),
                    }
                ),
                file=sys.stderr,
                flush=True,
            )
        time.sleep(max(0.5, args.interval - (time.time() - t0)))


# ---- evaluation on recorded footage ------------------------------------------------------------

IMAGE_EXT = {".jpg", ".jpeg", ".png"}


def iter_frames(path: Path, every: int = 1) -> Any:
    """(name, bgr) for a directory of images (sorted), one image, or a video file."""
    import cv2

    if path.is_dir():
        files = sorted(p for p in path.iterdir() if p.suffix.lower() in IMAGE_EXT)
        for i, p in enumerate(files):
            if i % every == 0:
                img = cv2.imread(str(p))
                if img is not None:
                    yield p.name, img
    elif path.suffix.lower() in IMAGE_EXT:
        img = cv2.imread(str(path))
        if img is not None:
            yield path.name, img
    else:
        cap = cv2.VideoCapture(str(path))
        i = 0
        try:
            while True:
                ok, img = cap.read()
                if not ok:
                    break
                if i % every == 0:
                    yield f"{path.name}#{i}", img
                i += 1
        finally:
            cap.release()


@dataclass
class SeqResult:
    source: str
    label: str  # "pos" | "neg"
    scores: list[float]
    levels: list[str | None]
    names: list[str] = field(default_factory=list)

    @property
    def first_alert(self) -> int | None:
        return next((i for i, lv in enumerate(self.levels) if lv is not None), None)


def evaluate(
    sources: list[tuple[Path, str]],
    score_bgr: Any,
    *,
    every: int = 1,
    params: TrackerParams | None = None,
) -> list[SeqResult]:
    """Score every frame of every source in order; each source is one print, so the tracker
    restarts per source (with a clean long-term baseline, the worst case)."""
    out = []
    for path, label in sources:
        tracker = FailureTracker(params or TrackerParams())
        scores, levels, names = [], [], []
        for name, img in iter_frames(path, every):
            p = score_bgr(img).p
            scores.append(p)
            levels.append(tracker.update(p))
            names.append(name)
        out.append(SeqResult(str(path), label, scores, levels, names))
    return out


def metrics(
    results: list[SeqResult], thresholds: tuple[float, ...] = (0.1, 0.2, 0.3, 0.5)
) -> dict[str, Any]:
    pos = [p for r in results if r.label == "pos" for p in r.scores]
    neg = [p for r in results if r.label == "neg" for p in r.scores]
    per_frame = {}
    for t in thresholds:
        tp = sum(p >= t for p in pos)
        fp = sum(p >= t for p in neg)
        per_frame[str(t)] = {
            "tp": tp,
            "fp": fp,
            "fn": len(pos) - tp,
            "tn": len(neg) - fp,
            "precision": round(tp / (tp + fp), 4) if tp + fp else None,
            "recall": round(tp / len(pos), 4) if pos else None,
            "fpr": round(fp / len(neg), 5) if neg else None,
        }
    seqs = [
        {
            "source": r.source,
            "label": r.label,
            "frames": len(r.scores),
            "max_p": round(max(r.scores, default=0.0), 3),
            "first_alert_frame": r.first_alert,
            "alert_frames": sum(lv is not None for lv in r.levels),
            "critical_frames": sum(lv == "critical" for lv in r.levels),
        }
        for r in results
    ]
    neg_seq = [s for s in seqs if s["label"] == "neg"]
    pos_seq = [s for s in seqs if s["label"] == "pos"]
    worst = sorted(
        (
            (p, r.names[i] if i < len(r.names) else f"{r.source}#{i}")
            for r in results
            if r.label == "neg"
            for i, p in enumerate(r.scores)
        ),
        reverse=True,
    )[:5]
    return {
        "frames": {"pos": len(pos), "neg": len(neg)},
        "per_frame": per_frame,
        "worst_negatives": [{"frame": n, "p": round(p, 3)} for p, n in worst],
        "sequences": seqs,
        "sequence_alerts": {
            "pos_detected": sum(s["first_alert_frame"] is not None for s in pos_seq),
            "pos_total": len(pos_seq),
            "neg_false_alarms": sum(
                s["first_alert_frame"] is not None for s in neg_seq
            ),
            "neg_total": len(neg_seq),
        },
    }


def _print_metrics(m: dict[str, Any]) -> None:
    print(f"frames: {m['frames']['pos']} positive, {m['frames']['neg']} negative")
    print("per frame  threshold  precision  recall   FPR      (tp/fp/fn/tn)")
    for t, r in m["per_frame"].items():
        print(
            f"           p>={t:<6} {r['precision']!s:<10} {r['recall']!s:<8} {r['fpr']!s:<8} "
            f"({r['tp']}/{r['fp']}/{r['fn']}/{r['tn']})"
        )
    print(
        "highest-scoring negative frames: "
        + ", ".join(f"{w['frame']} {w['p']}" for w in m["worst_negatives"])
    )
    print("sequences (tracker as in the live loop):")
    for s in m["sequences"]:
        print(
            f"  [{s['label']}] {Path(s['source']).name}: {s['frames']} frames, max p {s['max_p']}, "
            f"first alert at frame {s['first_alert_frame']}, alert frames {s['alert_frames']} "
            f"(critical {s['critical_frames']})"
        )
    a = m["sequence_alerts"]
    print(
        f"alerts: {a['pos_detected']}/{a['pos_total']} failure sequences caught, "
        f"{a['neg_false_alarms']}/{a['neg_total']} healthy sequences with a false alarm"
    )


def run_eval(args: argparse.Namespace) -> int:
    from errors import UsageError

    sources = [(Path(p), "pos") for p in args.pos] + [
        (Path(p), "neg") for p in args.neg
    ]
    missing = [str(p) for p, _ in sources if not p.exists()]
    if not sources or missing:
        raise UsageError(
            "eval needs existing --pos/--neg paths"
            + (f"; missing: {', '.join(missing)}" if missing else ""),
            command="printer vision eval",
        )
    scorer = ObicoScorer(ensure_model())
    t0 = time.time()
    results = evaluate(
        sources,
        scorer.score_bgr,
        every=args.every,
        params=TrackerParams(warmup=args.warmup),
    )
    m = metrics(results)
    n = m["frames"]["pos"] + m["frames"]["neg"]
    m["seconds_per_frame"] = round((time.time() - t0) / max(1, n), 3)
    if args.json:
        print(json.dumps(m, indent=2))
    else:
        _print_metrics(m)
        print(f"speed: {m['seconds_per_frame']} s/frame")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="3d printer vision")
    sub = ap.add_subparsers(dest="cmd", required=True)
    live = sub.add_parser("run")
    live.add_argument("printer")
    live.add_argument("--interval", type=float, default=10.0)
    live.add_argument("--once", action="store_true")
    live.add_argument("--save")
    live.add_argument("--snapshot")
    live.add_argument("--no-keepalive", action="store_true")
    live.add_argument("--no-npu", action="store_true")
    live.add_argument("--evidence")
    live.add_argument("--dry-run", action="store_true")
    live.add_argument("--min-severity")
    live.add_argument("--verbose", action="store_true")
    ev = sub.add_parser("eval")
    ev.add_argument("--pos", nargs="*", default=[])
    ev.add_argument("--neg", nargs="*", default=[])
    ev.add_argument("--every", type=int, default=1)
    ev.add_argument("--warmup", type=int, default=TrackerParams().warmup)
    ev.add_argument("--json", action="store_true")
    sub.add_parser("fetch-model")
    args = ap.parse_args(argv)
    if args.cmd == "fetch-model":
        print(ensure_model())
        return 0
    if args.cmd == "eval":
        return run_eval(args)
    return run_live(args)


if __name__ == "__main__":
    from errors import ThreeDError

    try:
        sys.exit(main(sys.argv[1:]))
    except ThreeDError as exc:
        print(exc.render(), file=sys.stderr)
        sys.exit(exc.exit_code)
    except KeyboardInterrupt:
        sys.exit(130)
