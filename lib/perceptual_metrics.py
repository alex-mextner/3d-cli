#!/usr/bin/env python3
"""perceptual_metrics.py -- perceptual / semantic image metrics with explicit senses.

ACCESSED VIA: `3d metrics perceptual <image_a> <image_b> [options]`
(lib/commands/metrics.py runs THIS file through cli.pyrun; the command module stays
stdlib-only at its top level).

WHAT IT COMPUTES (APPLY-RESEARCH P1.2 / benchmarks-and-metrics §2):
  - PSNR   (dB, higher better) -- pixel-level, always available (ImageMagick / numpy).
  - LPIPS  (>=0, 0 best)       -- perceptual deep-feature distance (Zhang CVPR 2018).
  - CLIP   (0..100, higher best) -- semantic image-image similarity (Hessel 2021).

WHY SENSES ARE STORED EXPLICITLY:
  benchmarks-and-metrics §2.6 flags the "0-best vs high-best" mix (LPIPS/DSSIM are
  0-best while PSNR/CLIP are high-best) as a silent footgun: a later weighted score
  can invert a channel if the sense drifts. So every metric here carries a `sense`
  field, and every result records the convention (net, model, cap).

GRACEFUL DEGRADATION (not a silent false score):
  LPIPS and CLIP need heavy wheels (torch + the model). When a wheel is absent the
  metric function raises a structured `MissingDependency` (exit 127) naming the exact
  install command. In battery mode each missing metric is recorded as
  `{"available": false, ...}` -- never a fabricated number.

INVARIANTS:
  - Module top level is stdlib-only; numpy / torch / lpips / clip / PIL are lazy-imported
    inside the functions that use them.
  - `psnr_arrays` is pure numpy so unit tests can assert exact known answers
    (identical -> capped; a constant offset -> a known dB).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Sequence
from typing import Any

from errors import MissingDependency

SENSE_LOWER = "lower_better"
SENSE_HIGHER = "higher_better"

# Identical images give MSE 0 -> PSNR = +inf, which is not JSON-serialisable and
# meaningless as a delta. Cap it at a large finite dB (benchmarks §2 "inf-capped").
PSNR_CAP_DB = 100.0


# --------------------------------------------------------------------------- #
# PSNR -- pure numpy core (testable) + an ImageMagick path for files.
# --------------------------------------------------------------------------- #
def psnr_arrays(a: Any, b: Any, *, max_value: float = 255.0, cap_db: float = PSNR_CAP_DB) -> dict[str, Any]:
    """PSNR between two equal-shaped pixel arrays. Identical -> `cap_db` (inf-capped)."""
    import numpy as np

    arr_a = np.asarray(a, dtype=np.float64)
    arr_b = np.asarray(b, dtype=np.float64)
    if arr_a.shape != arr_b.shape:
        raise ValueError(f"PSNR needs equal shapes, got {arr_a.shape} vs {arr_b.shape}")
    mse = float(np.mean((arr_a - arr_b) ** 2))
    if mse <= 0.0:
        return {"value": float(cap_db), "sense": SENSE_HIGHER, "unit": "dB", "mse": 0.0, "capped": True}
    psnr = 10.0 * float(np.log10((max_value**2) / mse))
    return {"value": min(cap_db, psnr), "sense": SENSE_HIGHER, "unit": "dB", "mse": mse,
            "capped": psnr > cap_db}


def psnr_images(path_a: str, path_b: str) -> dict[str, Any]:
    """PSNR between two image FILES via ImageMagick `compare -metric PSNR` (dB).

    ImageMagick reports the metric on stderr; identical images give `inf`, capped to
    PSNR_CAP_DB. `path_b` is resized to `path_a` so a size mismatch does not abort.
    """
    magick = _find_magick()
    if magick is None:
        raise MissingDependency(
            "ImageMagick", install="brew install imagemagick",
            degrades="PSNR (image files) cannot be computed", command="metrics",
        )
    resized, is_temp = _resize_to_match(magick, path_a, path_b)
    try:
        cmp_cmd = [magick, "compare"] if magick.endswith("magick") or magick == "magick" else [magick]
        proc = subprocess.run([*cmp_cmd, "-metric", "PSNR", path_a, resized, "null:"],
                              capture_output=True, text=True)
        raw = (proc.stderr or proc.stdout).strip()
    finally:
        if is_temp:
            try:
                os.remove(resized)
            except OSError:
                pass
    value = _parse_psnr(raw)
    return {"value": value, "sense": SENSE_HIGHER, "unit": "dB", "capped": value >= PSNR_CAP_DB}


def _parse_psnr(raw: str) -> float:
    """Extract the PSNR dB value from ImageMagick output; map inf -> the cap."""
    token = raw.split()[0] if raw.split() else ""
    if token.lower() in ("inf", "1.#inf", "+inf") or "inf" in token.lower():
        return PSNR_CAP_DB
    try:
        return min(PSNR_CAP_DB, float(token))
    except ValueError as exc:
        raise ValueError(f"could not parse PSNR from ImageMagick output: {raw!r}") from exc


def _find_magick() -> str | None:
    import shutil

    if shutil.which("magick"):
        return "magick"
    for p in ("/opt/homebrew/bin/magick", "/usr/local/bin/magick"):
        if os.access(p, os.X_OK):
            return p
    if shutil.which("compare"):
        return "compare"
    return None


def _resize_to_match(magick: str, ref: str, other: str) -> tuple[str, bool]:
    """Resize `other` to `ref`'s pixel geometry.

    Returns `(path, is_temp)`. A per-call unique temp file (not a shared static name)
    avoids cross-user / concurrent collisions and lets the caller unlink it. Falls back
    to the original `other` (is_temp False) if the resize did not produce a file.
    """
    import tempfile

    ident = [magick, "identify"] if magick == "magick" else ["identify"]
    dims = subprocess.run([*ident, "-format", "%wx%h", ref], capture_output=True, text=True).stdout.strip()
    fd, out = tempfile.mkstemp(prefix="_psnr_resized_", suffix=".png")
    os.close(fd)
    subprocess.run([magick, other, "-resize", f"{dims}!", out], capture_output=True, text=True)
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        return out, True
    try:
        os.remove(out)
    except OSError:
        pass
    return other, False


# --------------------------------------------------------------------------- #
# LPIPS -- perceptual deep-feature distance (lazy torch + lpips).
# --------------------------------------------------------------------------- #
def lpips_distance(path_a: str, path_b: str, *, net: str = "alex") -> dict[str, Any]:
    """LPIPS distance between two image files. Range >=0, 0 best. Needs `lpips`+`torch`."""
    try:
        import lpips  # type: ignore[import-not-found]
        import torch  # noqa: F401
    except ImportError as exc:
        raise MissingDependency(
            "lpips", install="pip install lpips torch pillow",
            degrades="LPIPS perceptual channel unavailable", command="metrics",
        ) from exc
    tensor_a = _load_lpips_tensor(path_a)
    tensor_b = _load_lpips_tensor(path_b)
    model = lpips.LPIPS(net=net, verbose=False)
    with _no_grad():
        value = float(model(tensor_a, tensor_b).item())
    return {"value": value, "sense": SENSE_LOWER, "net": net}


def _no_grad() -> Any:
    import torch

    return torch.no_grad()


def _load_lpips_tensor(path: str) -> Any:
    """Load an image as a [1,3,H,W] tensor scaled to [-1, 1] (LPIPS convention)."""
    import numpy as np
    import torch
    from PIL import Image

    with Image.open(path) as img:
        arr = np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0
    chw = np.transpose(arr, (2, 0, 1))[None, ...]
    return torch.from_numpy(chw * 2.0 - 1.0)


# --------------------------------------------------------------------------- #
# CLIP-similarity -- semantic image-image cosine (lazy open_clip / clip).
# --------------------------------------------------------------------------- #
def clip_similarity(path_a: str, path_b: str, *, model_name: str = "ViT-B-32",
                    pretrained: str = "openai") -> dict[str, Any]:
    """CLIP image-image similarity: 100 * cosine of the two image embeddings.

    Range effectively 0..100, higher better (benchmarks §2.4). Needs `open_clip_torch`.
    """
    try:
        import open_clip  # type: ignore[import-not-found]
        import torch  # noqa: F401
    except ImportError as exc:
        raise MissingDependency(
            "open_clip_torch", install="pip install open_clip_torch torch pillow",
            degrades="CLIP semantic-similarity channel unavailable", command="metrics",
        ) from exc
    model, _, preprocess = open_clip.create_model_and_transforms(model_name, pretrained=pretrained)
    model.eval()
    emb_a = _clip_embed(model, preprocess, path_a)
    emb_b = _clip_embed(model, preprocess, path_b)
    import torch

    cos = float(torch.nn.functional.cosine_similarity(emb_a, emb_b).item())
    return {"value": max(0.0, 100.0 * cos), "sense": SENSE_HIGHER, "range": "0..100",
            "model": model_name, "pretrained": pretrained}


def _clip_embed(model: Any, preprocess: Any, path: str) -> Any:
    import torch
    from PIL import Image

    with Image.open(path) as img:
        tensor = preprocess(img.convert("RGB")).unsqueeze(0)
    with torch.no_grad():
        feats = model.encode_image(tensor)
    return feats / feats.norm(dim=-1, keepdim=True)


# --------------------------------------------------------------------------- #
# Battery: run the requested channels; record each missing one honestly.
# --------------------------------------------------------------------------- #
_ALL_METRICS = ("psnr", "lpips", "clip")


def perceptual_battery(path_a: str, path_b: str, *, metrics: Sequence[str] = _ALL_METRICS) -> dict[str, Any]:
    """Compute the requested perceptual channels. Missing wheels -> `available: false`."""
    computers = {
        "psnr": lambda: psnr_images(path_a, path_b),
        "lpips": lambda: lpips_distance(path_a, path_b),
        "clip": lambda: clip_similarity(path_a, path_b),
    }
    senses = {"psnr": SENSE_HIGHER, "lpips": SENSE_LOWER, "clip": SENSE_HIGHER}
    report: dict[str, Any] = {"convention": {"psnr_cap_db": PSNR_CAP_DB, "clip_range": "0..100"}}
    for name in metrics:
        if name not in computers:
            raise ValueError(f"unknown perceptual metric: {name}")
        try:
            entry = computers[name]()
            entry["available"] = True
            report[name] = entry
        except MissingDependency as exc:
            report[name] = {"available": False, "sense": senses[name],
                            "install": exc.install, "reason": exc.message}
    return report


# --------------------------------------------------------------------------- #
# CLI entry point (invoked by `3d metrics perceptual` via cli.pyrun).
# --------------------------------------------------------------------------- #
_USAGE = """usage: perceptual_metrics.py <image_a> <image_b> [options]
  Perceptual battery: PSNR (dB, high best), LPIPS (0 best), CLIP-sim (0..100, high best).

Options:
  --metrics LIST    comma-separated subset of psnr,lpips,clip (default all)
  --json            print the full JSON report (senses + convention)
  --no-store        do not append a record to the metrics store"""


def _parse_args(argv: list[str]) -> dict[str, Any]:
    opts: dict[str, Any] = {"positional": [], "metrics": _ALL_METRICS, "json": False, "store": True}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--json":
            opts["json"] = True
            i += 1
        elif a == "--no-store":
            opts["store"] = False
            i += 1
        elif a == "--metrics":
            if i + 1 >= len(argv):
                raise ValueError("option --metrics needs a value")
            opts["metrics"] = tuple(m.strip() for m in argv[i + 1].split(",") if m.strip())
            i += 2
        elif a.startswith("-") and a not in ("-h", "--help"):
            raise ValueError(f"unknown option {a}")
        else:
            opts["positional"].append(a)
            i += 1
    return opts


def _print_report(report: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, sort_keys=True, indent=2))
        return
    for name in ("psnr", "lpips", "clip"):
        entry = report.get(name)
        if entry is None:
            continue
        key = name.upper()
        if entry.get("available"):
            print(f"{key}={entry['value']:.4f}")
            print(f"{key}_SENSE={entry['sense']}")
        else:
            print(f"{key}=unavailable")
            print(f"{key}_INSTALL={entry['install']}")


def _store(report: dict[str, Any], opts: dict[str, Any]) -> None:
    try:
        from registries.metrics import append_record

        append_record(
            command="perceptual", tool="perceptual_metrics",
            inputs={"image_a": opts["positional"][0], "image_b": opts["positional"][1],
                    "metrics": list(opts["metrics"])},
            metrics=report,
        )
    except Exception:  # a store failure must never fail the measurement itself.
        pass


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(_USAGE)
        return 0 if argv else 1
    try:
        opts = _parse_args(argv)
    except ValueError as exc:
        print(f"perceptual_metrics: {exc}", file=sys.stderr)
        return 2
    if len(opts["positional"]) != 2:
        print(_USAGE, file=sys.stderr)
        return 2
    for path in opts["positional"]:
        if not os.path.isfile(path):
            print(f"perceptual_metrics: file not found: {path}", file=sys.stderr)
            return 2
    try:
        report = perceptual_battery(opts["positional"][0], opts["positional"][1], metrics=opts["metrics"])
    except ValueError as exc:
        print(f"perceptual_metrics: {exc}", file=sys.stderr)
        return 2
    _print_report(report, bool(opts["json"]))
    if opts["store"]:
        _store(report, opts)
    # Exit 127 only when EVERY requested channel was unavailable (nothing measured).
    available = [v for k, v in report.items() if k != "convention" and isinstance(v, dict) and v.get("available")]
    return 0 if available else 127


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
