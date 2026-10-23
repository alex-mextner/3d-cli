"""Model-backed smoke tests for recognise / bank (need onnxruntime, tokenizers AND the clip-b32 + dinov2-s weights).

Skipped when any of them is absent (CI has none); run them with the vision venv:
  flock /tmp/hsw-vision-gpu.lock ~/.cache/3d-cli/vision/venv/bin/python -m pytest tests/test_vision_recognise_models.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")
pytest.importorskip("onnxruntime")
pytest.importorskip("tokenizers")
trimesh = pytest.importorskip("trimesh")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "threed-vision" / "src"))

from threed_vision import bank, models  # noqa: E402
from threed_vision.clip import ClipEncoder, DinoEncoder  # noqa: E402
from threed_vision.recognise import Recogniser  # noqa: E402


def _need() -> None:
    st = models.status()["models"]
    missing = [n for n in ("clip-b32", "dinov2-s") if not st[n]["installed"]]
    if missing:
        pytest.skip(
            f"model weights absent: {', '.join(missing)} (3d vision-models pull {' '.join(missing)})"
        )


def _photo() -> tuple[Any, list[dict[str, Any]]]:
    """Grey table with a red and a blue block (tall, 3:1)."""
    rgb = np.full((300, 400, 3), 128, np.uint8)
    masks = []
    for k, (x0, colour) in enumerate(((40, (210, 30, 30)), (240, (30, 40, 210)))):
        m = np.zeros(rgb.shape[:2], bool)
        m[60:240, x0 : x0 + 60] = True
        rgb[m] = colour
        masks.append({"id": k + 1, "mask": m})
    return rgb, masks


def test_real_clip_text_proposes_the_right_colour_class() -> None:
    _need()
    rgb, masks = _photo()
    rec = Recogniser("cpu")
    try:
        res = rec.classify(
            rgb,
            masks,
            [{"id": "red", "prompts": ["red"]}, {"id": "blue", "prompts": ["blue"]}],
            calibrate=False,
            topk=2,
        )
    finally:
        rec.unload()
    assert [r["text_topk"][0]["label"] for r in res] == ["red", "blue"]


def test_real_index_query_and_classify_with_a_bank(tmp_path: Path) -> None:
    _need()
    src = tmp_path / "meshes"
    src.mkdir()
    for name, colour in (
        ("red_block", (210, 30, 30, 255)),
        ("blue_block", (30, 40, 210, 255)),
    ):
        mesh = trimesh.creation.box(extents=(180, 60, 20))
        mesh.visual.face_colors = np.tile(
            np.array(colour, np.uint8), (len(mesh.faces), 1)
        )
        mesh.export(src / f"{name}.ply")
    (src / "categories.json").write_text(
        '{"red_block": "red", "blue_block": "blue"}', encoding="utf-8"
    )
    index_dir = bank.build_index(
        bank.build_thumbnails(src, tmp_path / "bank"), device="cpu"
    )
    assert (index_dir / "index.npz").is_file()
    rgb, masks = _photo()
    for k, expect in enumerate(("red_block", "blue_block")):
        hits = bank.query(index_dir, rgb, 2, mask=masks[k]["mask"], device="cpu")
        assert hits[0]["bank_id"] == expect and hits[0]["metric"] == "clip+dino"
    clip, dino = ClipEncoder("cpu"), DinoEncoder("cpu")
    rec = Recogniser("cpu", clip=clip, dino=dino)
    try:
        res = rec.classify(
            rgb,
            masks,
            ["red", "blue"],
            calibrate=False,
            bank=index_dir,
            rerank_classes=1,
        )
    finally:
        clip.unload()
        dino.unload()
    assert [r["bank_topk"][0]["bank_id"] for r in res] == ["red_block", "blue_block"]
