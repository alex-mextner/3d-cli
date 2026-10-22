"""Model-backed smoke tests for clip / detect / segment (need torch, sam2, transformers, onnxruntime AND the weights).

Skipped when any of them is absent (CI has none); run them with the vision venv:
  flock /tmp/hsw-vision-gpu.lock ~/.cache/3d-cli/vision/venv/bin/python -m pytest tests/test_vision_segment_models.py -q
"""

from __future__ import annotations

import sys
from typing import Any
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
torch = pytest.importorskip("torch")
pytest.importorskip("sam2")
pytest.importorskip("transformers")
pytest.importorskip("onnxruntime")
cv2 = pytest.importorskip("cv2")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "threed-vision" / "src"))

from threed_vision import clip, device, io, models  # noqa: E402
from threed_vision.detect import OwlDetector  # noqa: E402
from threed_vision.segment import Segmenter, segment_single_object  # noqa: E402

CARPET = Path(
    "/home/ultra/dev/garage-band/projects/HSW/docs/photos/tools-on-carpet-2026-10-21.heic"
)
GPU = torch.cuda.is_available()


def _need(*names: str) -> None:
    st = models.status()["models"]
    missing = [n for n in names if not st[n]["installed"]]
    if missing:
        pytest.skip(
            f"model weights absent: {', '.join(missing)} (3d vision-models pull {' '.join(missing)})"
        )


def _scene() -> Any:
    """Gray 'carpet' with two clearly separated coloured blobs."""
    rng = np.random.RandomState(0)
    img = np.clip(rng.normal(120, 6, (240, 320, 3)), 0, 255).astype(np.uint8)
    cv2.rectangle(img, (40, 60), (120, 130), (200, 30, 30), -1)
    cv2.ellipse(img, (230, 150), (50, 30), 20, 0, 360, (30, 60, 200), -1)
    return img


def _wants_cuda() -> bool:
    return GPU and (device.free_vram_mb() or 0) > 2500


def test_clip_encoders_shapes_norms_and_real_providers() -> None:
    _need("clip-b32", "dinov2-s")
    crops = [np.full((40, 90, 3), c, np.uint8) for c in (30, 200)]
    c = clip.ClipEncoder("auto")
    c.load()
    emb = c.images(crops)
    assert emb.shape == (2, 512) and emb.dtype == np.float32
    assert np.allclose(np.linalg.norm(emb, axis=1), 1, atol=1e-4)
    txt = c.texts(["a photo of a screwdriver", "a photo of a carpet"])
    assert txt.shape == (2, 512) and np.allclose(
        np.linalg.norm(txt, axis=1), 1, atol=1e-4
    )
    assert not np.allclose(txt[0], txt[1])
    assert c.images([]).shape == (0, 512)
    if (
        c.device == "cuda"
    ):  # the CUDA provider must really be the active one, not a silent CPU fallback
        assert c.providers[0] == "CUDAExecutionProvider"
    else:
        assert c.providers == ["CPUExecutionProvider"]
    c.unload()
    assert not c.loaded
    d = clip.DinoEncoder("cpu")
    assert d.images(crops).shape == (2, 768)


def test_ort_cuda_is_really_active_when_a_gpu_is_present() -> None:
    _need("clip-b32")
    if not (GPU and device.setup_ort_cuda()):
        pytest.skip("no usable ORT CUDA provider on this machine")
    c = clip.ClipEncoder("cuda")
    c.load()
    assert c.device == "cuda" and c.providers[0] == "CUDAExecutionProvider"
    c.unload()


def test_clip_text_prefers_the_matching_class() -> None:
    _need("clip-b32")
    c = clip.ClipEncoder("cpu")
    red = np.zeros((100, 100, 3), np.uint8)
    red[:] = (220, 20, 20)
    green = np.zeros((100, 100, 3), np.uint8)
    green[:] = (20, 200, 20)
    img = c.images([red, green])
    txt = c.texts(["a photo of a red surface", "a photo of a green surface"])
    sim = img @ txt.T
    assert sim[0, 0] > sim[0, 1] and sim[1, 1] > sim[1, 0]


def test_u2net_matte_finds_a_single_salient_object() -> None:
    _need("u2net")
    img = np.full((240, 320, 3), 235, np.uint8)
    cv2.ellipse(img, (160, 120), (90, 45), 0, 0, 360, (190, 40, 40), -1)
    cv2.ellipse(img, (140, 110), (30, 15), 0, 0, 360, (80, 10, 10), -1)
    mask = segment_single_object(img, "cpu")
    truth = np.zeros((240, 320), np.uint8)
    cv2.ellipse(truth, (160, 120), (90, 45), 0, 0, 360, 255, -1)
    assert mask.dtype == bool and io.mask_iou(mask, truth > 0) > 0.8


def test_owl_boxes_live_in_the_real_image_not_the_padded_square() -> None:
    _need("owlv2-base")
    if not CARPET.is_file():
        pytest.skip("carpet photo absent")
    rgb = io.load_rgb(CARPET)
    h, w = rgb.shape[:2]
    det = OwlDetector("cuda" if _wants_cuda() else "cpu")
    out = det.detect(rgb, ["a hand tool", "a box", "pliers"])
    det.unload()
    boxes = np.array([b["xyxy"] for b in out["boxes"]])
    assert len(boxes) > 20
    assert (
        boxes[:, 0].min() >= 0
        and boxes[:, 1].min() >= 0
        and boxes[:, 2].max() <= w
        and boxes[:, 3].max() <= h
    )
    # scaled by max(H, W): objects exist all the way down the (shorter) vertical axis, so boxes reach the lower third
    assert boxes[:, 3].max() > 0.8 * h
    assert all(
        0.15 <= b["score"] <= 1 and b["label"] in ("a hand tool", "a box", "pliers")
        for b in out["boxes"]
    )


def test_segmenter_on_a_synthetic_scene_and_click_helpers() -> None:
    _need("sam2-tiny", "owlv2-base", "clip-b32")
    rgb = _scene()
    seg = Segmenter(
        device="cpu",
        fill="none",
        reject_bg=False,
        points_per_side=16,
        max_area_frac=0.5,
        min_area=200,
    )
    res = seg.run(rgb)
    assert res["device"] == "cpu" and res["instances"]
    truth = np.zeros(rgb.shape[:2], bool)
    truth[60:131, 40:121] = True
    best = max(io.mask_iou(i["mask"], truth) for i in res["instances"])
    assert best > 0.85
    added = seg.add_by_point(rgb, [], [[230, 150]])
    ell = np.zeros(rgb.shape[:2], np.uint8)
    cv2.ellipse(ell, (230, 150), (50, 30), 20, 0, 360, 255, -1)
    assert (
        len(added) == 1
        and added[0]["source"] == "click"
        and io.mask_iou(added[0]["mask"], ell > 0) > 0.85
    )
    boxed = seg.add_by_box(rgb, added, [30, 50, 130, 140])
    assert [i["id"] for i in boxed] == [1, 2] and io.mask_iou(
        boxed[1]["mask"], truth
    ) > 0.85
    seg.unload()
    assert not seg.loaded


@pytest.mark.skipif(not GPU, reason="no CUDA device")
def test_unload_really_frees_vram() -> None:
    _need("sam2-tiny", "owlv2-base", "clip-b32")
    if (device.free_vram_mb() or 0) < 2500:
        pytest.skip("less than 2.5 GB VRAM free")
    base = torch.cuda.memory_allocated()
    free_before = torch.cuda.mem_get_info()[0]
    seg = Segmenter(device="cuda")
    seg.run(_scene())  # loads SAM2 + OWLv2 + CLIP and runs every stage
    assert torch.cuda.memory_allocated() - base > 100 * (1 << 20)
    seg.unload()
    assert torch.cuda.memory_allocated() - base < 16 * (1 << 20)
    free_after = torch.cuda.mem_get_info()[0]
    assert free_before - free_after < 300 * (
        1 << 20
    )  # the driver got the memory back (torch + onnxruntime)


@pytest.mark.skipif(not CARPET.is_file(), reason="private carpet photo absent")
def test_segmenter_on_the_carpet_photo_default_device() -> None:
    _need("sam2-tiny", "owlv2-base", "clip-b32")
    rgb = io.load_rgb(CARPET)
    seg = Segmenter(device="auto")
    res = seg.run(rgb)
    seg.unload()
    n = len(res["instances"])
    assert 40 <= n <= 160, (res["device"], n)
    assert res["timing_s"]["total"] > 0
    assert [i["id"] for i in res["instances"]] == list(range(1, n + 1))
    assert all(
        i["mask"].shape == rgb.shape[:2] and i["mask"].dtype == bool
        for i in res["instances"]
    )


def test_detect_masks_on_the_carpet_photo(tmp_path: Path) -> None:
    import argparse
    import json

    from threed_vision.commands import detect as detect_cmd

    _need("sam2-tiny", "owlv2-base")
    if not CARPET.is_file():
        pytest.skip("carpet photo absent")
    parser = argparse.ArgumentParser()
    detect_cmd.add_arguments(parser)
    out = tmp_path / "masks"
    dev = "cuda" if _wants_cuda() else "cpu"
    args = parser.parse_args(
        [
            str(CARPET),
            "--queries",
            "a hand tool,pliers,a box",
            "--masks",
            str(out),
            "--device",
            dev,
            "--json",
        ]
    )
    assert detect_cmd.run(args) == 0
    doc = json.loads((out / "instances.json").read_text())
    n = len(doc["instances"])
    assert n >= 15 and doc["method"] == "owlv2+sam2-box"
    h, w = doc["image"]["h"], doc["image"]["w"]
    for rec in doc["instances"]:
        mask = io.load_mask(out / rec["mask"])
        assert mask.shape == (h, w) and mask.sum() == rec["area_px"] > 0
        x0, y0, x1, y1 = rec["box_xyxy"]
        ys, xs = np.nonzero(mask)
        # SAM2 may spill past its prompt box, but the object it returns is centred in it and not box-sized x several
        assert x0 <= xs.mean() <= x1 and y0 <= ys.mean() <= y1
        assert rec["area_px"] <= 2.0 * (x1 - x0) * (y1 - y0)
