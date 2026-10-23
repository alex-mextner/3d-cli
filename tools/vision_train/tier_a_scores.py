"""tier_a_scores.py — tier (a)'s verified spaghetti score q for every frame of a dataset directory.

Dev tool for experiment.py's comparison column: it runs the SHIPPED tier (a) pipeline
(lib/vision_detect.FrameScorer: Obico YOLOv2 boxes + the CLIP B/32 crop verifier, no camera
calibration, no ROI) on exactly the frames the other methods see, and writes DATASET/tier_a.json
{frame id: q}. Run it where the weights are cached (`3d printer vision fetch-model`), with
onnxruntime + opencv; `--cuda` uses the CUDA provider when onnxruntime-gpu is installed (the shipped
scorers are CPU-only; this only speeds the dev run up, the numbers are the same up to rounding).

    tier_a_scores.py DATASET [--cuda]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "lib"))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dataset", type=Path)
    ap.add_argument("--cuda", action="store_true")
    args = ap.parse_args(argv)
    import cv2
    import onnxruntime as ort

    if args.cuda:
        ort.preload_dlls()
        real = ort.InferenceSession

        def cuda_session(
            path: str, opts: object = None, providers: object = None, **kw: object
        ) -> object:
            return real(
                path,
                opts,
                providers=[
                    ("CUDAExecutionProvider", {"gpu_mem_limit": 2 << 30}),
                    "CPUExecutionProvider",
                ],
                **kw,
            )

        ort.InferenceSession = cuda_session  # type: ignore[misc,assignment]
    from vision_detect import FrameScorer, ObicoScorer, ensure_model
    from vision_verify import ClipVerifier, ensure_clip

    scorer = FrameScorer(
        ObicoScorer(ensure_model(fetch=False)), ClipVerifier(ensure_clip(fetch=False))
    )
    rows = [
        json.loads(line)
        for line in (args.dataset / "labels.jsonl").read_text().splitlines()
        if line.strip()
    ]
    out: dict[str, float] = {}
    t0 = time.time()
    for i, r in enumerate(rows):
        img = cv2.imread(str(args.dataset / r["file"]))
        out[r["id"]] = round(float(scorer.score_bgr(img).q or 0.0), 5)
        if i % 500 == 0:
            print(f"{i}/{len(rows)} {time.time() - t0:.0f} s", flush=True)
    (args.dataset / "tier_a.json").write_text(json.dumps(out) + "\n")
    print(
        f"wrote {args.dataset / 'tier_a.json'} ({len(out)} frames, {time.time() - t0:.0f} s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
