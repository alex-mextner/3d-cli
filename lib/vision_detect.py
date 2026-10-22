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

from printer_ops.vision import (  # noqa: E402
    Box,
    FailureTracker,
    Finding,
    FrameScore,
    PrintContext,
    TrackerParams,
)

if TYPE_CHECKING:
    import numpy as np

    from vision_detach import DetachParams


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


class FrameScorer:
    """Stage 1 (Obico boxes) -> drop boxes outside the allowed area -> stage 2 (CLIP verifier),
    plus the calibrated bed-change detectors.

    `ignore`: static polygons (normalised) where print failures cannot be, e.g. the toolhead
    dock or the purge chute. With a camera calibration (`geometry`) and the live print position
    (`context`, set by the loop before each frame) the boxes must also lie in the bed volume,
    and vision_scene's detectors add Findings. A box counts as inside when its centre is.
    Without a verifier FrameScore.q stays None and the tracker uses the raw p."""

    def __init__(
        self,
        detector: ObicoScorer,
        verifier: Any = None,
        ignore: list[list[tuple[float, float]]] | None = None,
        geometry: PrintGeometry | None = None,
    ) -> None:
        self.detector = detector
        self.verifier = verifier
        self.ignore = ignore or []
        self.geometry = geometry
        self.context: PrintContext | None = None
        self.now: float | None = None  # frame time for replays; None = wall clock
        self.roi: np.ndarray | None = None

    def reset_print(self) -> None:
        """A new print: forget the bed-change references (no-op without geometry)."""
        if self.geometry is not None:
            self.geometry.reset_print()

    def _allowed(self, b: Box) -> bool:
        cx, cy = (b.x1 + b.x2) / 2, (b.y1 + b.y2) / 2
        if self.roi is not None:
            rh, rw = self.roi.shape[:2]
            if not self.roi[min(rh - 1, int(cy * rh)), min(rw - 1, int(cx * rw))]:
                return False
        return not any(_inside(cx, cy, poly) for poly in self.ignore)

    def score_bgr(self, bgr: np.ndarray) -> FrameScore:
        findings: list[Finding] = []
        if self.geometry is not None:
            if self.context is None:
                # No print context this frame (poll failed, frame missing from a replay): no
                # bed-volume mask at all, never the previous frame's (it may be for another Z).
                self.roi = None
            else:
                now = time.time() if self.now is None else self.now
                self.roi, findings = self.geometry.update(bgr, self.context, now)
        s = self.detector.score_bgr(bgr)
        kept = [b for b in s.boxes if self._allowed(b)]
        s = FrameScore(
            p=float(sum(b.conf for b in kept)),
            boxes=kept,
            luma=s.luma,
            masked=len(s.boxes) - len(kept),
            findings=findings,
        )
        if self.verifier is not None:
            from vision_verify import verified_score

            s.probs = self.verifier.box_probs(bgr, kept) if kept else []
            s.q = verified_score(kept, s.probs)
        return s

    def __call__(self, jpeg: bytes) -> FrameScore:
        return self.score_bgr(decode(jpeg))


# The U1 toolhead seen from the chamber camera: a ~60 mm block around the nozzle reaching
# ~100 mm up; the reported XY lags the frame by up to ~0.5 s of travel, hence the margin.
HEAD_HALF_MM = 30.0
HEAD_SKEW_MM = 40.0
HEAD_UP_MM = 100.0
ROI_PAD_MM = 15.0  # calibration error is up to ~30 px near the camera (9 px RMS)
ROI_ABOVE_MM = 40.0  # spaghetti tangles around the nozzle, above the part top
ROI_GROW_PX = 40  # at 1920 px: boxes just outside the projected volume still count
BAND_MM = (
    4.0  # the newest layers; excluded from the change detector, checked for growth
)


class PrintGeometry:
    """Camera calibration + live print position -> the box ROI and the bed-change Findings.
    Static masks are cached per (file, 0.1 mm of Z); the toolhead box is per frame."""

    def __init__(
        self,
        cam: Any,
        *,
        scene: bool = True,
        growth: bool = True,
        detach: DetachParams | None = None,
    ) -> None:
        from vision_detach import DetachWatch
        from vision_scene import GrowthWatch, SceneWatch

        self.cam = cam
        self.scene = SceneWatch() if scene else None
        self.growth = GrowthWatch() if growth else None
        self.detach = None if detach is None else DetachWatch(detach)
        self.file: str | None = None
        self._key: tuple[Any, ...] | None = None
        self._masks: dict[str, np.ndarray] = {}

    def masks(self, ctx: PrintContext, shape: tuple[int, int]) -> dict[str, np.ndarray]:
        import cv2
        import numpy as np
        import vision_geometry as vg

        key = (ctx.filename, round(ctx.z, 1), shape, ctx.objects)
        if key != self._key:
            objs = [[(float(x), float(y)) for x, y in o] for o in ctx.objects]
            z = ctx.z
            grow = max(1, round(ROI_GROW_PX * shape[1] / 1920))
            roi = vg.volume_mask(
                self.cam, z, shape, objs, pad_mm=ROI_PAD_MM, above_mm=ROI_ABOVE_MM
            )
            sil = vg.silhouette_mask(self.cam, z, shape, objs, pad_mm=3)
            self._masks = {
                "roi": cv2.dilate(roi, np.ones((grow, grow), np.uint8)),
                "silhouette": sil,
                "area": vg.bed_mask(self.cam, z, shape) | sil,
                # part-detached check (vision_detach.py): where the near walls should be, the
                # whole part volume, and the plate with a margin for the sheet beyond the bed
                "wall": vg.wall_mask(self.cam, z, shape, objs),
                "body": vg.prism_mask(self.cam, z, shape, objs, 0.0, max(z, 0.5)),
                "plate": vg.bed_mask(self.cam, z, shape, margin_mm=ROI_PAD_MM / 2),
                # growth check: the newest band vs. solid part well below it and empty space
                # above it; the one-band gaps keep calibration error (9-30 px) from mixing them
                "band": vg.prism_mask(self.cam, z, shape, objs, z - BAND_MM, z),
                "below": vg.prism_mask(
                    self.cam, z, shape, objs, z - 4 * BAND_MM, z - 2 * BAND_MM
                ),
                "above": vg.prism_mask(
                    self.cam, z, shape, objs, z + BAND_MM, z + 3 * BAND_MM
                ),
            }
            self._key = key
        head = [
            (
                ctx.x + dx * (HEAD_HALF_MM + HEAD_SKEW_MM),
                ctx.y + dy * (HEAD_HALF_MM + HEAD_SKEW_MM),
            )
            for dx, dy in ((-1, -1), (1, -1), (1, 1), (-1, 1))
        ]
        return {
            **self._masks,
            "head": vg.prism_mask(
                self.cam, ctx.z, shape, [head], ctx.z - 2.0, ctx.z + HEAD_UP_MM
            ),
        }

    def reset_print(self) -> None:
        """A new print started (possibly the same file again): drop every bed-change reference."""
        for w in (self.scene, self.growth, self.detach):
            if w is not None:
                w.reset()

    def update(
        self, bgr: np.ndarray, ctx: PrintContext, now: float
    ) -> tuple[np.ndarray, list[Finding]]:
        """(ROI for the stage-1 boxes, Findings of the bed-change detectors) for one frame."""
        if ctx.filename != self.file:  # another file: no reference frames from the old one
            self.file = ctx.filename
            self.reset_print()
        m = self.masks(ctx, bgr.shape[:2])
        findings: list[Finding] = []
        if (
            not ctx.objects
        ):  # no footprints (no exclude_object in the G-code): ROI = bed only
            return m["roi"], findings
        free = 255 - m["head"]
        if self.scene is not None:
            findings += self.scene.update(
                bgr, ctx.z, now, m["area"], m["silhouette"], m["head"] | m["band"]
            )
        if self.detach is not None:
            findings += self.detach.update(
                bgr, ctx.z, m["wall"], m["body"], m["plate"], m["head"]
            )
        if self.growth is not None:
            findings += self.growth.update(
                bgr,
                ctx.z,
                m["band"] & free,
                m["below"] & free,
                m["above"] & free,
            )
        return m["roi"], findings


def _inside(x: float, y: float, poly: list[tuple[float, float]]) -> bool:
    """Even-odd point-in-polygon (normalised coordinates)."""
    hit = False
    n = len(poly)
    for i in range(n):
        (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


class FrameDecodeError(ValueError):
    """A framed camera image cannot be decoded; it is not usable failure evidence."""


def decode(jpeg: bytes) -> np.ndarray:
    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise FrameDecodeError("not a decodable image")
    return img


def annotate(jpeg: bytes, score: FrameScore) -> bytes:
    """The frame with the detection boxes drawn (evidence for the event): red = the verifier
    says loose filament (>= 0.5) or did not run, yellow = it says texture/part; labels read
    `confidence/verified`."""
    import cv2

    img = decode(jpeg)
    h, w = img.shape[:2]
    for i, b in enumerate(score.boxes):
        prob = score.probs[i] if i < len(score.probs) else None
        colour = (0, 0, 255) if prob is None or prob >= 0.5 else (0, 215, 255)
        label = f"{b.conf:.2f}" + ("" if prob is None else f"/{prob:.2f}")
        p1, p2 = (int(b.x1 * w), int(b.y1 * h)), (int(b.x2 * w), int(b.y2 * h))
        cv2.rectangle(img, p1, p2, colour, max(2, w // 480))
        cv2.putText(
            img,
            label,
            (p1[0], max(20, p1[1] - 6)),
            cv2.FONT_HERSHEY_SIMPLEX,
            max(0.6, w / 1600),
            colour,
            2,
        )
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes() if ok else jpeg


# ---- live loop ----------------------------------------------------------------------------------


def _device_camera(device: Any, args: argparse.Namespace) -> Any:
    from errors import UsageError
    from printer_ops.camera import open_camera, require_camera

    if device.adapter != "moonraker":
        raise UsageError(
            f"{device.name}: no camera access for adapter {device.adapter}",
            command="printer vision",
            remediation=[
                "Only Moonraker printers are supported; a rooted K1 SE needs its "
                "Moonraker entry (adapter: moonraker, port 7125), not creality-stock."
            ],
        )
    spec = require_camera(device, "printer vision")  # watch.vision.camera / model default, never a guess
    return open_camera(
        device, spec, snapshot=args.snapshot, keepalive=False if args.no_keepalive else None
    )


def detach_params(setting: Any) -> DetachParams | None:
    """`watch.vision.detach`: true (defaults), false, or a mapping of DetachParams fields."""
    from errors import UsageError
    from vision_detach import DetachParams

    if setting is False or setting is None:
        return None
    if setting is True:
        return DetachParams()
    try:
        if not isinstance(setting, dict):
            raise TypeError("expected true, false or a mapping")
        return DetachParams(**setting)
    except TypeError as exc:
        raise UsageError(
            f"watch.vision.detach must be true, false or a mapping of "
            f"{', '.join(DetachParams.__dataclass_fields__)}: {exc}",
            command="printer vision",
        ) from exc


def build_scorer(
    cfg: dict[str, Any],
    *,
    verify: bool = True,
    camera: Any = None,
    scene: bool = False,
    detach: bool = True,
) -> FrameScorer:
    """The per-frame pipeline from a device's `watch: vision:` settings: `verify: false` turns
    stage 2 off, `ignore:` lists polygons (normalised [x, y] points) whose boxes are dropped,
    `geometry: false` ignores the camera calibration `camera` (vision_geometry.CameraModel).
    `scene: true` (or `scene=True`, the --scene flag) adds the bed-change detectors
    (foreign_object, part_moved, no_growth); off by default because on the U1 they still raise
    ~139 and ~50 false findings per healthy printing day (docs/research/printer-vision-tuning.md).
    The part-detached check (vision_detach.py) is on whenever the calibration is: `detach: false`
    (or `detach=False`, --no-detach) turns it off, `detach: {gap_abs: 0.3, ...}` tunes it."""
    from errors import UsageError

    try:
        polys = [
            [(float(x), float(y)) for x, y in poly] for poly in cfg.get("ignore") or []
        ]
    except (TypeError, ValueError) as exc:
        raise UsageError(
            f"watch.vision.ignore must be a list of polygons of [x, y] pairs (0..1): {exc}",
            command="printer vision",
        ) from exc
    # A typo such as [-1, -1]..[2, 2] would silently mask the whole frame and disable detection.
    bad = [
        poly
        for poly in polys
        if len(poly) < 3 or not all(0.0 <= v <= 1.0 for pt in poly for v in pt)
    ]
    if bad:
        raise UsageError(
            f"watch.vision.ignore polygons need >= 3 points with x, y in 0..1; got {bad[0]}",
            command="printer vision",
        )
    verifier = None
    if verify and cfg.get("verify", True):
        from vision_verify import ClipVerifier, ensure_clip

        verifier = ClipVerifier(ensure_clip(), threads=_threads())
    bed_change = scene or bool(cfg.get("scene", False))
    geometry = (
        PrintGeometry(
            camera,
            scene=bed_change,
            growth=bed_change,
            detach=detach_params(cfg.get("detach", True)) if detach else None,
        )
        if camera is not None and cfg.get("geometry", True)
        else None
    )
    if bed_change and geometry is None:
        raise UsageError(
            "the bed-change detectors (--scene / watch.vision.scene) need the camera "
            "calibration and `geometry: true`",
            command="printer vision",
            remediation=["Drop --scene, or see docs/research/printer-vision-geometry.md."],
        )
    return FrameScorer(ObicoScorer(ensure_model()), verifier, polys, geometry)


def _calibration(device: Any, args: argparse.Namespace) -> Any:
    if args.no_geometry:
        return None
    from vision_geometry import load_calibration

    return load_calibration(device.name)


def run_live(args: argparse.Namespace) -> int:
    from cli.paths import cache_dir
    from printer_ops import make_adapter
    from printer_ops.base import PrinterCommError
    from printer_ops.devices import get_device
    from printer_ops.vision import ContextTap, KlippyLogTap, VisionMonitor
    from printer_ops.vision_status import VisionStatus
    from printer_ops.watch import severity_rank
    from printer_ops.watch_sinks import Dispatcher

    device = get_device(args.printer)
    camera = _device_camera(device, args)
    adapter = make_adapter(device)
    from printer_ops.vision_config import monitor_limits, npu_enabled

    # the printer's own detector only where it has one (watch.vision.npu; default: a Snapmaker monitor camera)
    tap = KlippyLogTap(device.host, device.port) if npu_enabled(device) and not args.no_npu else None
    cfg = (getattr(device, "watch", None) or {}).get("vision") or {}
    cam = _calibration(device, args)
    scorer = build_scorer(
        cfg if isinstance(cfg, dict) else {},
        verify=not args.no_verify,
        camera=cam,
        scene=args.scene,
        detach=not args.no_detach,
    )
    context_tap = (
        ContextTap(device.host, device.port) if scorer.geometry else None
    )
    evidence = (
        Path(args.evidence) if args.evidence else cache_dir() / "vision" / "evidence"
    )
    monitor = VisionMonitor(
        device.name, scorer, annotate=annotate, evidence_dir=evidence,
        limits=monitor_limits(cfg if isinstance(cfg, dict) else {}),
    )
    status = None if args.once else VisionStatus(device.name)  # the file 3d-api serves to Home Assistant
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
            if monitor.set_job(st.state, st.filename):
                scorer.reset_print()  # the same file printed again is a new print too
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
            if context_tap is not None:
                try:
                    scorer.context = context_tap.poll()
                    report["z"] = round(scorer.context.z, 2)
                except PrinterCommError as exc:
                    scorer.context = None  # no masks this frame rather than stale ones
                    report["context_error"] = str(exc)
        score, events = monitor.step(frame)
        if tap is not None:
            try:
                events += monitor.npu(tap.poll(), time.time())
            except PrinterCommError as exc:
                report["npu_error"] = str(exc)
        if status is not None:
            status.update(
                state=report.get("state"),
                filename=report.get("file"),
                score=score,
                events=events,
                trend=monitor.tracker.adjusted,
                camera_error=report.get("camera_error"),
            )
        if args.once:
            if score is None and frame is not None:
                score = scorer(
                    frame.jpeg
                )  # not printing: still show what the model sees
            if score is not None:
                report.update(
                    p=round(score.p, 3),
                    q=None if score.q is None else round(score.q, 3),
                    luma=round(score.luma, 1),
                    masked=score.masked,
                    boxes=[
                        {
                            **{k: round(v, 3) for k, v in vars(b).items()},
                            "verified": round(score.probs[i], 3)
                            if i < len(score.probs)
                            else None,
                        }
                        for i, b in enumerate(score.boxes)
                    ],
                    findings=[f.kind for f in score.findings],
                )
            detach = scorer.geometry.detach if scorer.geometry is not None else None
            if detach is not None:
                # one frame cannot satisfy the persistence rule, so --once reports the raw reading
                gap = detach.last_gap
                report["detach_gap"] = None if gap is None else round(gap, 3)
                report["part_detached"] = gap is not None and gap >= detach.params.gap_abs
            report["trend"] = round(monitor.tracker.adjusted, 3)
            report["events"] = [e.to_json() for e in events]
            if frame is not None and args.save:
                out = Path(args.save).expanduser()
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(
                    annotate(frame.jpeg, score) if score and score.boxes else frame.jpeg
                )
                report["frame"] = str(out)
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
                        "q": None
                        if score is None or score.q is None
                        else round(score.q, 3),
                        "trend": round(monitor.tracker.adjusted, 3),
                    }
                ),
                file=sys.stderr,
                flush=True,
            )
        time.sleep(max(0.5, args.interval - (time.time() - t0)))


def _frame_score_errors() -> tuple[type[Exception], ...]:
    """Native pixel/runtime failures only; programming and model-contract errors propagate."""
    import cv2
    from onnxruntime.capi.onnxruntime_pybind11_state import EPFail, EngineError, Fail, RuntimeException

    return (FrameDecodeError, cv2.error, Fail, RuntimeException, EngineError, EPFail)


def run_guard(args: argparse.Namespace) -> int:
    """The explicit one-job writer. It has no Dispatcher, subscriber, agent or message sink."""
    from cli.paths import cache_dir, state_dir
    from errors import UsageError
    from printer_ops import make_adapter
    from printer_ops.base import PrinterCommError
    from printer_ops.guard import PauseGuard, _write_result, guard_device, validate_args
    from printer_ops.vision import ContextTap

    validate_args(args)
    device = guard_device(args.printer)
    cfg = (getattr(device, "watch", None) or {}).get("vision") or {}
    cfg = cfg if isinstance(cfg, dict) else {}
    if cfg.get("verify", True) is not True:
        raise UsageError("guard requires the CLIP verifier; watch.vision.verify is disabled", command="printer guard")
    camera_args = argparse.Namespace(snapshot=None, no_keepalive=False, no_geometry=False)
    camera = _device_camera(device, camera_args)
    scorer = build_scorer(cfg, camera=_calibration(device, camera_args))
    if scorer.verifier is None:
        raise UsageError("guard requires a loaded CLIP verifier", command="printer guard")
    score_errors = _frame_score_errors()
    context = ContextTap(device.host, device.port) if scorer.geometry else None
    root = state_dir() / "guard"
    guard = PauseGuard.arm(make_adapter(device), args.job_id, state_dir=root,
                           evidence_dir=Path(args.evidence).expanduser() if args.evidence else cache_dir() / "guard",
                           clock=time.time, sleep=time.sleep, dry_run=args.dry_run, interval=args.interval)
    root.mkdir(parents=True, exist_ok=True)

    def report(value: dict[str, Any]) -> None:
        _write_result(root / f"{device.name}.status.json", value)
        print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)

    report(guard._report("armed"))
    while True:
        started = time.time()
        frame, score, error, score_error = None, None, None, None
        try:
            camera.keepalive(started)
            frame = camera.grab()
            if context is not None:
                scorer.context = context.poll()
                if scorer.context.filename != guard.job.filename:
                    raise PrinterCommError("camera context belongs to another file", command="printer guard")
        except PrinterCommError as exc:
            error = str(exc)
        else:
            # Only scoring is recoverable here. Identity, state writes and PAUSE stay outside.
            try:
                score = scorer(frame.jpeg)
            except score_errors as exc:
                score_error = f"{type(exc).__name__}: {exc}"[:2000]
        result = guard.observe(frame, score)
        if error:
            result["camera_error"] = error
        if score_error:
            result["score_error"] = score_error
        report(result)
        if result["terminal"]:
            return 1 if result["outcome"] == "pause_unverified" else 0
        time.sleep(max(0.5, args.interval - (time.time() - started)))


def run_record(args: argparse.Namespace) -> int:
    """`vision record NAME DIR`: while a print is running OR paused (a paused printer is where a
    failure is still on the bed), save every frame plus where the print is (gcode Z, toolhead
    XY, object footprints, printer state) to DIR/meta.jsonl. Read-only, like the live loop. The
    folder is a test set for `vision eval`, which replays it with the calibrated detectors.
    Stops after --max frames (default: never) or, with --timeout S, gives up (exit 1) when no
    frame was saved within S seconds, so a script never waits forever on an idle printer."""
    from errors import GateFailure
    from printer_ops import make_adapter
    from printer_ops.base import PAUSED, PRINTING, PrinterCommError
    from printer_ops.devices import get_device
    from printer_ops.vision import ContextTap

    device = get_device(args.printer)
    camera = _device_camera(device, args)
    adapter = make_adapter(device)
    context_tap = ContextTap(device.host, device.port)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    n, objects, last_state = 0, None, None
    started = time.time()
    with (out / "meta.jsonl").open("a") as meta:
        while args.max is None or n < args.max:
            t0 = time.time()
            state, active = None, False
            try:
                state = adapter.status().state
                active = state in (PRINTING, PAUSED)
                if active:
                    camera.keepalive(t0)
                    frame = camera.grab()
                    ctx = context_tap.poll()
            except PrinterCommError as exc:
                print(
                    json.dumps({"t": round(t0, 1), "error": str(exc)}), file=sys.stderr
                )
                active = False
            if state != last_state:  # say why nothing is saved (an idle printer was silent)
                last_state = state
                print(
                    json.dumps({"t": round(t0, 1), "state": state, "recording": active}),
                    file=sys.stderr,
                    flush=True,
                )
            if active:
                # milliseconds: two frames inside one second must not overwrite each other
                stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(frame.ts))
                name = f"{stamp}.{int(frame.ts * 1000) % 1000:03d}Z.jpg"
                (out / name).write_bytes(frame.jpeg)
                row: dict[str, Any] = {
                    "frame": name,
                    "t": round(frame.ts, 2),
                    "file": ctx.filename,
                    "state": state,
                    "z": ctx.z,
                    "x": round(ctx.x, 2),
                    "y": round(ctx.y, 2),
                }
                if ctx.objects != objects:  # footprints only when they change
                    row["objects"] = objects = ctx.objects
                meta.write(json.dumps(row) + "\n")
                meta.flush()
                n += 1
            elif args.timeout is not None and n == 0 and time.time() - started >= args.timeout:
                raise GateFailure(
                    f"{device.name}: no frame saved in {args.timeout:g} s "
                    f"(printer state: {state or 'unreachable'})",
                    command="printer vision record",
                    remediation=[
                        "record saves frames only while the printer is printing or paused; "
                        "start or resume a print, or drop --timeout to wait for one."
                    ],
                )
            time.sleep(max(0.5, args.interval - (time.time() - t0)))
    return 0


def load_replay(path: Path) -> dict[str, tuple[PrintContext, float]]:
    """frame name -> (PrintContext, frame time) from a `vision record` folder's meta.jsonl;
    empty when the source is not such a folder."""
    meta = path / "meta.jsonl" if path.is_dir() else None
    if meta is None or not meta.is_file():
        return {}
    out: dict[str, tuple[PrintContext, float]] = {}
    objects: tuple[tuple[tuple[float, float], ...], ...] = ()
    for line in meta.read_text().splitlines():
        try:
            row = json.loads(line)
            if "objects" in row:
                objects = tuple(
                    tuple((float(x), float(y)) for x, y in o) for o in row["objects"]
                )
            if "frame" not in row:
                continue
            ctx = PrintContext(
                row.get("file"),
                float(row["z"]),
                float(row["x"]),
                float(row["y"]),
                objects,
            )
        except (ValueError, KeyError, TypeError):
            continue  # an error row or a torn last line
        out[row["frame"]] = (ctx, float(row["t"]))
    return out


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
    scores: list[float]  # raw stage-1 p
    levels: list[str | None]  # the live tracker on the signal (q when verified, else p)
    names: list[str] = field(default_factory=list)
    verified: list[float] | None = None  # stage-2 q per frame (None: no verifier)
    findings: list[tuple[str, ...]] = field(
        default_factory=list
    )  # Finding kinds per frame

    @property
    def first_alert(self) -> int | None:
        return next((i for i, lv in enumerate(self.levels) if lv is not None), None)


def evaluate(
    sources: list[tuple[Path, str]],
    score_bgr: Any,
    *,
    every: int = 1,
    params: TrackerParams | None = None,
    lead_in: int = 0,
    prepare: Any = None,
    new_print: Any = None,
) -> list[SeqResult]:
    """Score every frame of every source in order; each source is one print, so the tracker
    restarts per source (with a clean long-term baseline, the worst case) and `new_print()` runs
    first (the scorer's bed-change references). `lead_in` healthy
    frames (score 0) go first, as if the print had run fine before the clip starts: a failure
    clip then meets the tracker the way a live failure does, after warm-up. `prepare(source,
    frame name)` runs before each frame (replays set the print position there)."""
    out = []
    for path, label in sources:
        if new_print is not None:
            new_print()
        tracker = FailureTracker(params or TrackerParams())
        for _ in range(lead_in):
            tracker.update(0.0)
        scores, levels, names, verified, found = [], [], [], [], []
        for name, img in iter_frames(path, every):
            if prepare is not None:
                prepare(path, name)
            s = score_bgr(img)
            scores.append(s.p)
            if s.q is not None:
                verified.append(s.q)
            levels.append(tracker.update(s.signal))
            names.append(name)
            found.append(tuple(sorted(f.kind for f in s.findings)))
        out.append(
            SeqResult(
                str(path),
                label,
                scores,
                levels,
                names,
                verified if scores and len(verified) == len(scores) else None,
                found,
            )
        )
    return out


def _table(
    pos: list[float], neg: list[float], thresholds: tuple[float, ...]
) -> dict[str, Any]:
    table = {}
    for t in thresholds:
        tp = sum(p >= t for p in pos)
        fp = sum(p >= t for p in neg)
        table[str(t)] = {
            "tp": tp,
            "fp": fp,
            "fn": len(pos) - tp,
            "tn": len(neg) - fp,
            "precision": round(tp / (tp + fp), 4) if tp + fp else None,
            "recall": round(tp / len(pos), 4) if pos else None,
            "fpr": round(fp / len(neg), 5) if neg else None,
        }
    return table


def metrics(
    results: list[SeqResult],
    thresholds: tuple[float, ...] = (0.1, 0.2, 0.3, 0.5),
    interval_s: float = 10.0,
) -> dict[str, Any]:
    pos = [p for r in results if r.label == "pos" for p in r.scores]
    neg = [p for r in results if r.label == "neg" for p in r.scores]
    seqs = [
        {
            "source": r.source,
            "label": r.label,
            "frames": len(r.scores),
            "max_p": round(max(r.scores, default=0.0), 3),
            "max_q": None
            if r.verified is None
            else round(max(r.verified, default=0.0), 3),
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
    # False alarms per day of printing on healthy footage: every rising edge of the tracker.
    neg_alerts = sum(
        sum(
            1
            for i, lv in enumerate(r.levels)
            if lv is not None and (i == 0 or r.levels[i - 1] is None)
        )
        for r in results
        if r.label == "neg"
    )
    days = len(neg) * interval_s / 86400.0
    # Bed-change detectors: episodes (rising edges) per kind on healthy footage.
    finding_eps: dict[str, int] = {}
    for r in results:
        if r.label != "neg":
            continue
        prev: tuple[str, ...] = ()
        for kinds in r.findings:
            for k in set(kinds) - set(prev):
                finding_eps[k] = finding_eps.get(k, 0) + 1
            prev = kinds
    out = {
        "frames": {"pos": len(pos), "neg": len(neg)},
        "per_frame": _table(pos, neg, thresholds),
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
        "healthy_days": round(days, 3),
        "false_alarms_per_day": round(neg_alerts / days, 2) if days else None,
        "finding_alarms_per_day": {
            k: round(v / days, 2) for k, v in sorted(finding_eps.items())
        }
        if days
        else {},
    }
    if results and all(r.verified is not None for r in results):
        out["per_frame_verified"] = _table(
            [q for r in results if r.label == "pos" for q in r.verified or []],
            [q for r in results if r.label == "neg" for q in r.verified or []],
            thresholds,
        )
    return out


def _print_metrics(m: dict[str, Any]) -> None:
    print(f"frames: {m['frames']['pos']} positive, {m['frames']['neg']} negative")
    for key, name in (("per_frame", "p"), ("per_frame_verified", "q")):
        if key not in m:
            continue
        print(
            f"per frame ({name})  threshold  precision  recall   FPR      (tp/fp/fn/tn)"
        )
        for t, r in m[key].items():
            print(
                f"               {name}>={t:<6} {r['precision']!s:<10} {r['recall']!s:<8} "
                f"{r['fpr']!s:<8} ({r['tp']}/{r['fp']}/{r['fn']}/{r['tn']})"
            )
    print(
        "highest-scoring negative frames (p): "
        + ", ".join(f"{w['frame']} {w['p']}" for w in m["worst_negatives"])
    )
    print("sequences (tracker as in the live loop):")
    for s in m["sequences"]:
        q = "" if s["max_q"] is None else f", max q {s['max_q']}"
        print(
            f"  [{s['label']}] {Path(s['source']).name}: {s['frames']} frames, "
            f"max p {s['max_p']}{q}, first alert at frame {s['first_alert_frame']}, "
            f"alert frames {s['alert_frames']} (critical {s['critical_frames']})"
        )
    a = m["sequence_alerts"]
    print(
        f"alerts: {a['pos_detected']}/{a['pos_total']} failure sequences caught, "
        f"{a['neg_false_alarms']}/{a['neg_total']} healthy sequences with a false alarm; "
        f"{m['false_alarms_per_day']} false alarms per printing day "
        f"({m['healthy_days']} days of healthy frames at the live interval)"
    )
    if m["finding_alarms_per_day"]:
        print(
            "bed-change findings per healthy printing day: "
            + ", ".join(f"{k} {v}" for k, v in m["finding_alarms_per_day"].items())
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
    cam = None
    cfg: dict[str, Any] = {}
    if args.printer:
        from printer_ops.devices import get_device
        from vision_geometry import load_calibration

        cam = load_calibration(args.printer)
        if cam is None:
            raise UsageError(
                f"no camera calibration for printer {args.printer}",
                command="printer vision eval",
                remediation=[
                    "See docs/research/printer-vision-geometry.md; the U1 one ships "
                    "with 3d-cli for devices with `model: Snapmaker U1`."
                ],
            )
        # the device's live `watch: vision:` settings (ignore polygons, verify, scene), so eval
        # scores what the live loop would
        vision_cfg = (
            getattr(get_device(args.printer), "watch", None) or {}
        ).get("vision")
        cfg = vision_cfg if isinstance(vision_cfg, dict) else {}
    scorer = build_scorer(
        cfg, verify=not args.no_verify, camera=cam, scene=args.scene, detach=not args.no_detach
    )
    replays = {str(p): load_replay(p) for p, _ in sources}

    def prepare(path: Path, name: str) -> None:
        ctx, ts = replays[str(path)].get(name, (None, None))
        scorer.context, scorer.now = ctx, ts

    t0 = time.time()
    results = evaluate(
        sources,
        scorer.score_bgr,
        every=args.every,
        params=TrackerParams(warmup=args.warmup),
        lead_in=args.lead_in,
        prepare=prepare if cam is not None else None,  # replay times + print position
        new_print=scorer.reset_print,
    )
    # --every N keeps one frame in N, so each scored frame stands for N capture intervals
    m = metrics(results, interval_s=args.interval * args.every)
    n = m["frames"]["pos"] + m["frames"]["neg"]
    m["seconds_per_frame"] = round((time.time() - t0) / max(1, n), 3)
    if args.json:
        print(json.dumps(m, indent=2))
    else:
        _print_metrics(m)
        print(f"speed: {m['seconds_per_frame']} s/frame")
    return 0


def main(argv: list[str]) -> int:
    from printer_ops.guard import add_arguments

    ap = argparse.ArgumentParser(prog="3d printer vision")
    sub = ap.add_subparsers(dest="cmd", required=True)
    add_arguments(sub.add_parser("guard", help="explicit one-job automatic PAUSE; local evidence only"))
    live = sub.add_parser("run")
    live.add_argument("printer")
    live.add_argument("--interval", type=float, default=10.0)
    live.add_argument("--once", action="store_true")
    live.add_argument("--save")
    live.add_argument("--snapshot")
    live.add_argument("--no-keepalive", action="store_true")
    live.add_argument("--no-npu", action="store_true")
    live.add_argument("--no-verify", action="store_true")
    live.add_argument("--no-geometry", action="store_true")
    live.add_argument("--scene", action="store_true")
    live.add_argument("--no-detach", action="store_true")
    live.add_argument("--evidence")
    live.add_argument("--dry-run", action="store_true")
    live.add_argument("--min-severity")
    live.add_argument("--verbose", action="store_true")
    ev = sub.add_parser("eval")
    ev.add_argument("--pos", nargs="*", default=[])
    ev.add_argument("--neg", nargs="*", default=[])
    ev.add_argument("--every", type=int, default=1)
    ev.add_argument("--warmup", type=int, default=TrackerParams().warmup)
    ev.add_argument("--lead-in", type=int, default=0)
    ev.add_argument("--interval", type=float, default=10.0)
    ev.add_argument("--no-verify", action="store_true")
    ev.add_argument("--scene", action="store_true")
    ev.add_argument("--no-detach", action="store_true")
    ev.add_argument("--printer")
    ev.add_argument("--json", action="store_true")
    rec = sub.add_parser("record")
    rec.add_argument("printer")
    rec.add_argument("out")
    rec.add_argument("--interval", type=float, default=10.0)
    rec.add_argument("--max", type=int)
    rec.add_argument("--timeout", type=float)
    rec.add_argument("--snapshot")
    rec.add_argument("--no-keepalive", action="store_true")
    sub.add_parser("fetch-model")
    args = ap.parse_args(argv)
    if args.cmd == "guard":
        return run_guard(args)
    if args.cmd == "fetch-model":
        from vision_verify import ensure_clip

        print(ensure_model())
        print(ensure_clip())
        return 0
    if args.cmd == "eval":
        return run_eval(args)
    if args.cmd == "record":
        return run_record(args)
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
