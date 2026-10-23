"""threed_vision segment / detect / clip contracts with fake engines (numpy + OpenCV + Pillow only, CI-runnable)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("cv2")
pytest.importorskip("PIL")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "threed-vision" / "src"))

from threed_vision import cli, clip, detect, io, segment  # noqa: E402
from threed_vision.errors import BadInput, MissingDependency  # noqa: E402
from threed_vision.segment import Segmenter  # noqa: E402

H, W = 120, 200


def _blob(x: int, y: int, w: int, h: int, shape: tuple[int, int] = (H, W)) -> Any:
    m = np.zeros(shape, bool)
    m[y : y + h, x : x + w] = True
    return m


def _inst(iid: int, mask: Any, **kw: Any) -> dict[str, Any]:
    return segment.make_instance(
        mask,
        iid,
        kw.pop("source", "amg"),
        kw.pop("bg_prob", 0.1),
        kw.pop("stability", 0.9),
    )


# --- commands are discoverable ------------------------------------------------------------------------------


def test_new_commands_are_discovered_and_documented() -> None:
    cmds = cli.discover()
    for name in ("vision-models", "segment", "detect"):
        assert name in cmds and cmds[name].SUMMARY


# --- clip helpers ---------------------------------------------------------------------------------------------


def test_letterbox_preserves_aspect_and_centres() -> None:
    img = np.full((40, 100, 3), 200, np.uint8)
    out = clip.letterbox(img, 224)
    assert out.shape == (224, 224, 3)
    assert tuple(out[0, 0]) == clip.PAD_RGB  # padding at the top
    assert tuple(out[112, 112]) == (200, 200, 200)  # content in the middle
    rows = np.nonzero((out != np.array(clip.PAD_RGB, np.uint8)).any(axis=(1, 2)))[0]
    assert abs(len(rows) - 90) <= 2  # 40/100 * 224 = 89.6 rows of content


def test_prep_shape_dtype_and_normalisation() -> None:
    x = clip.prep([np.zeros((10, 30, 3), np.uint8)], 224, clip.CLIP_MEAN, clip.CLIP_STD)
    assert (
        x.shape == (1, 3, 224, 224)
        and x.dtype == np.float32
        and x.flags["C_CONTIGUOUS"]
    )


def test_crop_masked_blanks_the_background_unless_asked() -> None:
    rgb = np.full((H, W, 3), 50, np.uint8)
    mask = _blob(60, 40, 30, 20)
    crop, (x0, y0, x1, y1) = clip.crop_masked(rgb, mask)
    assert x0 < 60 < 90 < x1 and y0 < 40 < 60 < y1
    assert tuple(crop[0, 0]) == clip.PAD_RGB and tuple(
        crop[crop.shape[0] // 2, crop.shape[1] // 2]
    ) == (50, 50, 50)
    kept, _ = clip.crop_masked(rgb, mask, keep_bg=True)
    assert (kept == 50).all()
    with pytest.raises(ValueError):
        clip.crop_masked(rgb, np.zeros((H, W), bool))


def test_encoder_without_models_is_a_missing_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("onnxruntime")
    monkeypatch.setenv("THREED_VISION_MODELS_DIR", str(tmp_path))
    enc = clip.ClipEncoder("cpu")
    with pytest.raises(MissingDependency) as exc:
        enc.load()
    assert "3d vision-models pull clip-b32" in exc.value.render()
    assert not enc.loaded


# --- detect ----------------------------------------------------------------------------------------------------


def test_owl_boxes_are_scaled_by_the_longest_side_not_by_w_and_h() -> None:
    # a box covering the left half of the padded square of a 200x100 (WxH) image: cx=.25, cy=.25 (-> y=50 of 200), w=.5, h=.5
    norm = np.array([[0.25, 0.25, 0.5, 0.5]], np.float32)
    xyxy = detect.boxes_from_square(norm, h=100, w=200)
    assert xyxy.tolist() == [
        [0.0, 0.0, 100.0, 100.0]
    ]  # scaled by 200 (max side), clipped to the 100 px height
    # a box entirely in the padding (below the image) clips to zero height
    pad = detect.boxes_from_square(
        np.array([[0.5, 0.9, 0.2, 0.1]], np.float32), h=100, w=200
    )
    assert pad[0, 3] - pad[0, 1] == 0


def test_nms_keeps_the_best_of_overlapping_boxes() -> None:
    boxes = np.array([[0, 0, 10, 10], [1, 1, 11, 11], [50, 50, 60, 60]], np.float32)
    assert detect.nms_indices(boxes, np.array([0.5, 0.9, 0.4]), 0.5) == [1, 2]
    assert detect.nms_indices(boxes, np.array([0.5, 0.9, 0.4]), 0.99) == [1, 0, 2]
    assert detect.nms_indices(np.zeros((0, 4)), np.zeros(0), 0.5) == []


def test_select_boxes_applies_area_cap_nms_and_labels() -> None:
    xyxy = np.array(
        [[0, 0, 100, 100], [10, 10, 30, 30], [11, 11, 31, 31], [150, 80, 170, 100]],
        np.float32,
    )
    scores = np.array([0.9, 0.4, 0.6, 0.3])
    out = detect.select_boxes(
        xyxy, scores, ["a", "b", "c", "d"], h=100, w=200, nms=0.5, max_area_frac=0.05
    )
    assert [b["label"] for b in out] == [
        "c",
        "d",
    ]  # the 100x100 box (50 % of the image) and the lower-scored duplicate go
    assert out[0]["xyxy"] == [11.0, 11.0, 31.0, 31.0] and out[0]["score"] == 0.6


# --- instance bookkeeping --------------------------------------------------------------------------------------


def test_make_instance_fields() -> None:
    i = _inst(3, _blob(10, 20, 5, 4))
    assert (
        i["id"] == 3
        and i["bbox_xywh"] == [10, 20, 5, 4]
        and i["area_px"] == 20
        and i["mask"].dtype == bool
    )


def test_delete_merge_pick_and_labelmap() -> None:
    a, b, c = (
        _inst(1, _blob(0, 0, 20, 20)),
        _inst(2, _blob(15, 0, 20, 20)),
        _inst(3, _blob(100, 50, 10, 10)),
    )
    insts = [a, b, c]
    assert [i["id"] for i in segment.delete_instances(insts, [2])] == [1, 3]
    with pytest.raises(BadInput):
        segment.delete_instances(insts, [9])
    merged = segment.merge_instances(insts, [2, 1])
    assert [i["id"] for i in merged] == [1, 3] and merged[0]["source"] == "merged"
    assert merged[0]["area_px"] == 20 * 35 and merged[0]["bbox_xywh"] == [0, 0, 35, 20]
    assert len(insts) == 3  # inputs untouched
    with pytest.raises(BadInput):
        segment.merge_instances(insts, [1])
    assert segment.pick_instance(insts, 5, 5) == 1
    assert (
        segment.pick_instance(insts, 17, 5) == 1
    )  # overlap of a and b: equal areas -> first wins
    assert segment.pick_instance(insts, 105, 55) == 3
    assert segment.pick_instance(insts, 90, 90) is None
    small = _inst(4, _blob(2, 2, 4, 4))
    assert (
        segment.pick_instance([a, small], 3, 3) == 4
    )  # the smallest containing instance is the one meant
    lab = segment.instances_to_labelmap([a, small], (H, W))
    assert (
        lab.dtype == np.uint16
        and lab[3, 3] == 4
        and lab[10, 10] == 1
        and lab[100, 100] == 0
    )
    assert (
        segment.Segmenter.merge is segment.merge_instances
        and segment.next_id(insts) == 4
    )


def test_bg_probability_is_summed_background_softmax() -> None:
    img = np.array([[1.0, 0.0], [0.0, 1.0]], np.float32)
    txt = np.array(
        [[1.0, 0.0], [0.0, 1.0]], np.float32
    )  # prompt 0 = background, prompt 1 = foreground
    bg = segment.bg_probability(img, txt, n_bg=1)
    assert bg[0] > 0.99 and bg[1] < 0.01


# --- Segmenter orchestration with fake engines ------------------------------------------------------------------


class FakeDetector:
    def __init__(self, boxes: list[list[float]]) -> None:
        self.boxes, self.calls = boxes, 0
        self.shapes: list[tuple[int, ...]] = []

    def detect(
        self, rgb: Any, queries: Any, threshold: float = 0.15, **kw: Any
    ) -> dict[str, Any]:
        self.calls += 1
        self.shapes.append(rgb.shape)
        self.kwargs = kw
        return {
            "boxes": [
                {"xyxy": b, "score": 0.5, "label": "a hand tool"} for b in self.boxes
            ],
            "timing_s": 0.0,
            "objectness": np.full((4, 4), 0.9, np.float32),
        }

    def unload(self) -> None:
        raise AssertionError(
            "an injected detector must not be unloaded by the Segmenter"
        )


class FakeClip:
    """Rejects masks whose crop is entirely dark (value < 40) by returning a background-ish embedding."""

    def images(self, crops: list[Any]) -> Any:
        return np.array(
            [[1.0, 0.0] if c.mean() > 60 else [0.0, 1.0] for c in crops], np.float32
        )

    def texts(self, prompts: list[str]) -> Any:
        return np.array(
            [
                [0.0, 1.0]
                if "carpet" in p or "floor" in p or "cabinet" in p or "shadow" in p
                else [1.0, 0.0]
                for p in prompts
            ],
            np.float32,
        )

    def unload(self) -> None:
        raise AssertionError(
            "an injected clip encoder must not be unloaded by the Segmenter"
        )


class FakeSegmenter(Segmenter):
    """Segmenter whose SAM2 stages are canned; everything else (orchestration, filters, ids, scaling) is real."""

    def __init__(self, amg: list[Any], fill_masks: list[Any], **kw: Any) -> None:
        kw.setdefault(
            "quality", "accurate"
        )  # the stock generator path is the one `_amg` / `_fill` below replace
        super().__init__(**kw)
        self._amg_masks, self._fill_masks, self.amg_calls = amg, fill_masks, 0
        self.seen_shapes: list[tuple[int, ...]] = []

    def load(self) -> None:
        self.device = "cpu" if self.requested == "cpu" or self._cpu_only else "cuda"
        self.loaded = True

    def _amg(self, rgb: Any, pps: int) -> list[dict[str, Any]]:
        self.amg_calls += 1
        self.seen_shapes.append(rgb.shape)
        if self.device == "cuda" and getattr(self, "oom_once", False):
            self.oom_once = False
            raise RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB")
        self.pps = pps
        return [{"mask": m, "source": "amg", "stability": 0.9} for m in self._amg_masks]

    def _fill(
        self, rgb: Any, cands: Any, boxes: Any, min_px: float, max_overlap: float = 0.3
    ) -> list[dict[str, Any]]:
        self.fill_boxes = boxes
        return [
            {"mask": m, "source": "owl", "stability": 0.8} for m in self._fill_masks
        ]


def _rgb() -> Any:
    rgb = np.full((H, W, 3), 120, np.uint8)
    rgb[:, 150:] = 20  # a dark "carpet" strip on the right
    return rgb


def test_run_orchestrates_stages_filters_and_numbers_instances() -> None:
    good, good2 = _blob(10, 10, 20, 20), _blob(50, 10, 20, 20)
    dark_bg = _blob(155, 10, 20, 20)  # CLIP-rejected (dark crop)
    too_big = _blob(0, 40, 190, 70)  # 55 % of the image > max_area_frac 0.5
    tiny = _blob(100, 100, 5, 5)  # < min_area
    owl_added = _blob(90, 10, 20, 20)
    det = FakeDetector([[88, 8, 112, 32]])
    seg = FakeSegmenter(
        [good, good2, dark_bg, too_big, tiny],
        [owl_added],
        device="cuda",
        detector=det,
        clip=FakeClip(),
        max_area_frac=0.5,
        min_area=50,
    )
    stages: list[tuple[str, float]] = []
    res = seg.run(_rgb(), progress=lambda s, f: stages.append((s, f)))
    assert [i["id"] for i in res["instances"]] == [1, 2, 3]
    assert [i["source"] for i in res["instances"]] == ["amg", "amg", "owl"]
    assert res["method"] == "sam2+owlv2" and res["device"] == "cuda"
    assert set(res["timing_s"]) >= {
        "load",
        "amg",
        "detect",
        "fill",
        "bg_reject",
        "total",
    }
    order = [s for s, f in stages if f == 0.0]
    assert order == ["load", "amg", "detect", "fill", "bg_reject"]
    assert [s for s, f in stages if f == 1.0] == order  # every stage reports completion
    assert det.calls == 1
    assert seg.fill_boxes == [[88, 8, 112, 32]]
    for inst in res["instances"]:
        assert (
            inst["mask"].shape == (H, W)
            and inst["mask"].dtype == bool
            and inst["bg_prob"] < 0.5
        )


def test_fill_none_and_no_reject_skip_those_stages() -> None:
    det = FakeDetector([[0, 0, 5, 5]])
    seg = FakeSegmenter(
        [_blob(10, 10, 20, 20), _blob(155, 10, 20, 20)],
        [],
        device="cpu",
        fill="none",
        reject_bg=False,
        detector=det,
        clip=FakeClip(),
        max_area_frac=0.5,
    )
    res = seg.run(_rgb())
    assert (
        det.calls == 0
        and "detect" not in res["timing_s"]
        and "bg_reject" not in res["timing_s"]
    )
    assert len(res["instances"]) == 2 and res["method"] == "sam2"


def test_cpu_default_skips_the_grid_and_prompts_owl_boxes_on_a_downscaled_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(segment, "CPU_MAX_SIDE", 100)
    small = _blob(
        5, 5, 10, 10, shape=(60, 100)
    )  # at the processing resolution (half of 200x120)
    det = FakeDetector([[4, 4, 16, 16]])
    seg = FakeSegmenter(
        [],
        [small],
        device="cpu",
        reject_bg=False,
        detector=det,
        max_area_frac=0.5,
        min_area=50,
    )
    res = seg.run(_rgb())
    assert seg.amg_calls == 0 and "amg" not in res["timing_s"]
    assert det.shapes == [(60, 100, 3)]  # OWLv2 sees the downscaled copy
    inst = res["instances"][0]
    assert inst["source"] == "owl" and inst["mask"].shape == (H, W)
    assert (
        abs(inst["area_px"] - 400) <= 40
    )  # 10x10 at half scale -> ~20x20 at full resolution
    assert (
        res["process_scale"] == 0.5
        and res["points_per_side"] == segment.CPU_POINTS_PER_SIDE == 0
    )


def test_cpu_with_fill_none_or_explicit_grid_runs_a_coarse_amg_on_the_downscaled_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(segment, "CPU_MAX_SIDE", 100)
    small = _blob(5, 5, 10, 10, shape=(60, 100))
    seg = FakeSegmenter(
        [small],
        [],
        device="cpu",
        fill="none",
        reject_bg=False,
        max_area_frac=0.5,
        min_area=50,
    )
    res = seg.run(_rgb())
    assert (
        seg.seen_shapes == [(60, 100, 3)]
        and seg.pps == segment.CPU_AMG_ONLY_POINTS_PER_SIDE == 16
    )
    assert res["instances"][0]["mask"].shape == (H, W) and res["process_scale"] == 0.5
    explicit = FakeSegmenter(
        [small],
        [],
        device="cpu",
        fill="owlv2",
        points_per_side=24,
        reject_bg=False,
        detector=FakeDetector([]),
        max_area_frac=0.5,
        min_area=50,
    )
    explicit.run(_rgb())
    assert explicit.pps == 24
    with pytest.raises(BadInput):  # no grid and no boxes: nothing to run
        FakeSegmenter([], [], device="cpu", fill="none", points_per_side=0).run(_rgb())


def test_gpu_default_uses_the_full_grid_at_full_resolution() -> None:
    seg = FakeSegmenter(
        [_blob(10, 10, 20, 20)],
        [],
        device="cuda",
        fill="none",
        reject_bg=False,
        max_area_frac=0.5,
    )
    res = seg.run(_rgb())
    assert seg.seen_shapes == [(H, W, 3)] and seg.pps == 32 and res["device"] == "cuda"
    dense = FakeSegmenter(
        [], [], device="cuda", method="sam2-dense", fill="none", reject_bg=False
    )
    dense.run(_rgb())
    assert dense.pps == 64


def test_oom_on_auto_device_falls_back_to_cpu_and_still_returns() -> None:
    seg = FakeSegmenter(
        [_blob(10, 10, 20, 20)],
        [],
        device="auto",
        fill="none",
        reject_bg=False,
        max_area_frac=0.5,
    )
    seg.oom_once = True  # type: ignore[attr-defined]
    res = seg.run(_rgb())
    assert res["device"] == "cpu" and len(res["instances"]) == 1 and seg.amg_calls == 2


def test_oom_on_explicit_cuda_is_not_swallowed() -> None:
    seg = FakeSegmenter(
        [_blob(10, 10, 20, 20)], [], device="cuda", fill="none", reject_bg=False
    )
    seg.oom_once = True  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="out of memory"):
        seg.run(_rgb())


def test_bad_arguments_are_bad_input() -> None:
    with pytest.raises(BadInput):
        Segmenter(method="nope")
    with pytest.raises(BadInput):
        Segmenter(fill="nope")
    with pytest.raises(BadInput):
        FakeSegmenter([], [], device="cpu").run(np.zeros((10, 10), np.uint8))
    with pytest.raises(BadInput):
        Segmenter(device="cpu").add_by_point(_rgb(), [], [])
    with pytest.raises(BadInput):
        Segmenter(method="u2net", device="cpu").add_by_box(_rgb(), [], [0, 0, 5, 5])


def test_segmenter_import_is_cheap_and_load_without_model_names_the_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("THREED_VISION_MODELS_DIR", str(tmp_path))
    with pytest.raises(MissingDependency) as exc:
        Segmenter(device="cpu").load()
    assert "3d vision-models pull sam2-tiny" in exc.value.render()


def test_importing_segment_does_not_import_heavy_dependencies() -> None:
    import subprocess

    code = (
        f"import sys; sys.path.insert(0, {str(ROOT / 'packages' / 'threed-vision' / 'src')!r}); "
        "import threed_vision.segment, threed_vision.detect, threed_vision.clip, threed_vision.models;"
        "heavy = [m for m in ('torch', 'sam2', 'transformers', 'onnxruntime', 'rembg') if m in sys.modules]; print(heavy)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout.strip()
    assert out == "[]"


# --- command output writer ---------------------------------------------------------------------------------------


def test_write_result_artefacts_match_the_documented_schema(tmp_path: Path) -> None:
    from threed_vision.outputs import write_result

    rgb = _rgb()
    insts = [
        _inst(1, _blob(10, 10, 20, 20)),
        _inst(7, _blob(60, 30, 15, 25), source="owl", bg_prob=0.02, stability=0.8),
    ]
    result = {
        "instances": insts,
        "method": "sam2+owlv2",
        "device": "cpu",
        "timing_s": {"amg": 1.0, "total": 1.5},
        "vram_mb": 0,
    }
    doc = write_result(tmp_path, "photo.jpg", rgb, result)
    on_disk = json.loads((tmp_path / "instances.json").read_text())
    assert on_disk == json.loads(json.dumps(doc))
    assert on_disk["image"] == {"path": "photo.jpg", "w": W, "h": H}
    assert {"method", "device", "timing_s", "vram_mb", "instances"} <= set(on_disk)
    rec = on_disk["instances"][1]
    assert rec["id"] == 7 and rec["source"] == "owl" and rec["mask"] == "masks/007.png"
    assert (
        rec["bbox_xywh"] == [60, 30, 15, 25]
        and rec["area_px"] == 375
        and len(rec["polygon"]) >= 4
    )
    assert set(rec) == {
        "id",
        "source",
        "bbox_xywh",
        "area_px",
        "bg_prob",
        "stability",
        "mask",
        "polygon",
    }
    lab = io.load_mask(tmp_path / "masks" / "007.png")
    assert lab.sum() == 375
    from PIL import Image

    labels = np.array(Image.open(tmp_path / "labels.png"))
    assert (
        labels.dtype == np.uint16
        and labels[35, 65] == 7
        and labels[15, 15] == 1
        and labels[0, 0] == 0
    )
    assert (tmp_path / "overlay.jpg").stat().st_size > 0


@pytest.mark.parametrize(
    ("free_mb", "requested", "expected"),
    [
        (500, "auto", "cpu"),
        (1999, "auto", "cpu"),
        (2000, "auto", "cuda"),
        (None, "auto", "cpu"),
        (500, "cuda", "cuda"),
        (9000, "cpu", "cpu"),
    ],
)
def test_auto_device_needs_enough_free_vram(
    monkeypatch: pytest.MonkeyPatch, free_mb: int | None, requested: str, expected: str
) -> None:
    from threed_vision import device as dev

    monkeypatch.setattr(dev, "free_vram_mb", lambda: free_mb)
    seg = Segmenter(device=requested)
    monkeypatch.setattr(seg, "_load_sam", lambda: None)
    monkeypatch.setattr(segment.models, "model_path", lambda name, rel=None: Path(name))
    monkeypatch.setattr(segment, "require_torch", lambda: None)
    monkeypatch.setattr(segment, "require_cuda_if_requested", lambda device: None)
    seg.load()
    assert seg.device == expected and Segmenter.vram_estimate_mb == 2000


# --- `detect --masks`: SAM2 box prompts for the detector boxes -----------------------------------------------------


class BoxPromptSegmenter(Segmenter):
    """Replaces only the SAM2 predictor: the mask is the prompt box itself; zero-area boxes yield no mask."""

    def load(self) -> None:
        self.device = "cpu"
        self.loaded = True

    def _prompt_masks(self, rgb: Any, prompts: Any) -> list[tuple[Any, float] | None]:
        out: list[tuple[Any, float] | None] = []
        for p in prompts:
            x0, y0, x1, y1 = (int(v) for v in p["box"][0])
            mask = np.zeros(rgb.shape[:2], bool)
            mask[y0:y1, x0:x1] = True
            out.append((mask, 0.9) if mask.any() else None)
        return out


def test_masks_for_boxes_labels_each_instance_and_skips_empty_prompts() -> None:
    boxes = [
        {"xyxy": [10, 10, 30, 30], "score": 0.7, "label": "pliers"},
        {"xyxy": [50, 50, 50, 50], "score": 0.6, "label": "void"},
        {"xyxy": [60, 20, 90, 40], "score": 0.4, "label": "a box"},
    ]
    out = BoxPromptSegmenter(device="cpu").masks_for_boxes(_rgb(), boxes)
    assert [i["id"] for i in out] == [1, 2]
    assert [i["label"] for i in out] == ["pliers", "a box"]
    assert [i["source"] for i in out] == ["owl", "owl"]
    assert out[1]["score"] == 0.4 and out[1]["box_xyxy"] == [60.0, 20.0, 90.0, 40.0]
    assert out[0]["area_px"] == 400 and out[0]["mask"].shape == (H, W)
    assert BoxPromptSegmenter(device="cpu").masks_for_boxes(_rgb(), []) == []


def test_detect_masks_writes_a_segment_compatible_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse
    from PIL import Image

    from threed_vision.commands import detect as detect_cmd

    class StubOwl:
        device = "cpu"

        def __init__(self, device: str) -> None:
            pass

        def detect(self, rgb: Any, queries: Any, **kw: Any) -> dict[str, Any]:
            return {
                "boxes": [
                    {"xyxy": [10, 10, 30, 30], "score": 0.7, "label": queries[0]}
                ],
                "timing_s": 0.0,
            }

        def unload(self) -> None:
            pass

    monkeypatch.setattr(detect_cmd, "OwlDetector", StubOwl)
    monkeypatch.setattr(segment, "Segmenter", BoxPromptSegmenter)
    photo = tmp_path / "p.png"
    Image.fromarray(_rgb()).save(photo)
    parser = argparse.ArgumentParser()
    detect_cmd.add_arguments(parser)
    out_dir = tmp_path / "out"
    args = parser.parse_args(
        [str(photo), "--queries", "pliers", "--masks", str(out_dir), "--json"]
    )
    assert detect_cmd.run(args) == 0
    doc = json.loads((out_dir / "instances.json").read_text())
    assert doc["method"] == "owlv2+sam2-box" and doc["image"]["w"] == W
    (rec,) = doc["instances"]
    assert rec["label"] == "pliers" and rec["source"] == "owl" and rec["score"] == 0.7
    assert rec["mask"] == "masks/001.png" and rec["area_px"] == 400
    assert io.load_mask(out_dir / "masks" / "001.png").sum() == 400
    assert (out_dir / "labels.png").exists() and (out_dir / "overlay.jpg").exists()


def test_detect_without_masks_never_loads_the_segmenter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse
    from PIL import Image

    from threed_vision.commands import detect as detect_cmd

    class StubOwl:
        device = "cpu"

        def __init__(self, device: str) -> None:
            pass

        def detect(self, rgb: Any, queries: Any, **kw: Any) -> dict[str, Any]:
            return {"boxes": [], "timing_s": 0.0}

        def unload(self) -> None:
            pass

    def boom(*a: Any, **k: Any) -> None:
        raise AssertionError("boxes-only detect must not build a Segmenter")

    monkeypatch.setattr(detect_cmd, "OwlDetector", StubOwl)
    monkeypatch.setattr(segment, "Segmenter", boom)
    photo = tmp_path / "p.png"
    Image.fromarray(_rgb()).save(photo)
    parser = argparse.ArgumentParser()
    detect_cmd.add_arguments(parser)
    assert (
        detect_cmd.run(parser.parse_args([str(photo), "--queries", "x", "--json"])) == 0
    )
    assert "masks" not in json.loads(capsys.readouterr().out)


def test_detect_masks_with_no_box_needs_no_sam2_weights(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse
    from PIL import Image

    from threed_vision.commands import detect as detect_cmd

    class StubOwl:
        device = "cpu"

        def __init__(self, device: str) -> None:
            pass

        def detect(self, rgb: Any, queries: Any, **kw: Any) -> dict[str, Any]:
            return {"boxes": [], "timing_s": 0.0}

        def unload(self) -> None:
            pass

    class NoWeights(Segmenter):
        def load(self) -> None:
            raise AssertionError("an empty detection must not load SAM2")

    monkeypatch.setattr(detect_cmd, "OwlDetector", StubOwl)
    monkeypatch.setattr(segment, "Segmenter", NoWeights)
    photo = tmp_path / "p.png"
    Image.fromarray(_rgb()).save(photo)
    parser = argparse.ArgumentParser()
    detect_cmd.add_arguments(parser)
    out_dir = tmp_path / "out"
    args = parser.parse_args([str(photo), "--queries", "x", "--masks", str(out_dir)])
    assert detect_cmd.run(args) == 0
    assert json.loads((out_dir / "instances.json").read_text())["instances"] == []


# --- ClipEncoder.load: missing model is reported before a missing package -------------------------------------------


def test_clip_load_reports_the_missing_model_even_without_tokenizers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("THREED_VISION_MODELS_DIR", str(tmp_path))
    monkeypatch.setitem(
        sys.modules, "tokenizers", None
    )  # `import tokenizers` -> ImportError
    monkeypatch.setattr(clip.ClipEncoder, "_resolve", lambda self: None)
    with pytest.raises(MissingDependency) as exc:
        clip.ClipEncoder("cpu").load()
    assert "3d vision-models pull clip-b32" in exc.value.render()


def test_clip_load_without_tokenizers_is_a_missing_dependency_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "tokenizers", None)
    monkeypatch.setattr(clip.ClipEncoder, "_resolve", lambda self: None)
    monkeypatch.setattr(clip.ClipEncoder, "_session", lambda self, rel: object())
    monkeypatch.setattr(
        clip.models, "model_path", lambda name, rel=None: tmp_path / "t.json"
    )
    enc = clip.ClipEncoder("cpu")
    with pytest.raises(MissingDependency, match="tokenizers"):
        enc.load()
    assert not enc.loaded and enc._sessions == {}


# --- device handling: engines get the user's request, OOM fallback is per call ---------------------------------------


def test_owned_engines_receive_the_request_not_the_resolved_device() -> None:
    seg = Segmenter(device="auto")
    seg.device = "cuda"  # what load() resolved
    assert seg._get_clip().requested == "auto"
    assert seg._get_detector().requested == "auto"
    explicit = Segmenter(device="cuda")
    assert explicit._get_clip().requested == "cuda"


def test_oom_fallback_is_local_to_one_run_and_keeps_the_request() -> None:
    seg = FakeSegmenter(
        [_blob(10, 10, 20, 20)], [], device="auto", fill="none", reject_bg=False
    )
    seg.oom_once = True  # type: ignore[attr-defined]
    res = seg.run(_rgb())
    assert res["device"] == "cpu" and seg.requested == "auto"
    assert not seg._cpu_only and not seg.loaded  # the next run re-evaluates auto
    assert seg.run(_rgb())["device"] == "cuda"


class OomOncePromptSegmenter(BoxPromptSegmenter):
    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.devices: list[str] = []

    def load(self) -> None:
        self.device = "cpu" if self._cpu_only else "cuda"
        self.loaded = True

    def _prompt_masks(self, rgb: Any, prompts: Any) -> Any:
        self.load()
        self.devices.append(self.device)
        if self.device == "cuda":
            raise RuntimeError("CUDA out of memory. Tried to allocate 20.00 MiB")
        return super()._prompt_masks(rgb, prompts)


def test_box_prompts_fall_back_to_cpu_on_oom_for_auto_only() -> None:
    boxes = [{"xyxy": [10, 10, 30, 30], "score": 0.7, "label": "pliers"}]
    seg = OomOncePromptSegmenter(device="auto")
    seg.load()
    out = seg.masks_for_boxes(_rgb(), boxes)
    assert len(out) == 1 and seg.devices == ["cuda", "cpu"] and seg.requested == "auto"
    strict = OomOncePromptSegmenter(device="cuda")
    strict.load()
    with pytest.raises(RuntimeError, match="out of memory"):
        strict.masks_for_boxes(_rgb(), boxes)


def test_masks_for_boxes_rejects_u2net_instead_of_returning_nothing() -> None:
    boxes = [{"xyxy": [10, 10, 30, 30], "score": 0.7, "label": "x"}]
    with pytest.raises(BadInput, match="SAM2"):
        Segmenter(method="u2net", device="cpu").masks_for_boxes(_rgb(), boxes)


def test_write_result_clears_masks_of_a_previous_run(tmp_path: Path) -> None:
    from threed_vision.outputs import write_result

    base = {"method": "m", "device": "cpu", "timing_s": {"total": 0}, "vram_mb": 0}
    two = [_inst(1, _blob(10, 10, 20, 20)), _inst(2, _blob(60, 30, 15, 25))]
    write_result(tmp_path, "p.jpg", _rgb(), {**base, "instances": two})
    write_result(tmp_path, "p.jpg", _rgb(), {**base, "instances": two[:1]})
    assert sorted(p.name for p in (tmp_path / "masks").iterdir()) == ["001.png"]


def test_min_area_zero_still_drops_empty_masks() -> None:
    seg = FakeSegmenter(
        [_blob(10, 10, 20, 20), np.zeros((H, W), bool)],
        [],
        device="cpu",
        fill="none",
        reject_bg=False,
        min_area=0,
        max_area_frac=0.5,
    )
    assert len(seg.run(_rgb())["instances"]) == 1


# --- review round 2 ------------------------------------------------------------------------------------------------


def test_write_result_only_removes_masks_the_previous_run_listed(
    tmp_path: Path,
) -> None:
    from threed_vision.outputs import write_result

    base = {"method": "m", "device": "cpu", "timing_s": {"total": 0}, "vram_mb": 0}
    (tmp_path / "masks").mkdir()
    (tmp_path / "masks" / "reference.png").write_bytes(b"user file")
    two = [_inst(1, _blob(10, 10, 20, 20)), _inst(2, _blob(60, 30, 15, 25))]
    write_result(tmp_path, "p.jpg", _rgb(), {**base, "instances": two})
    write_result(tmp_path, "p.jpg", _rgb(), {**base, "instances": two[:1]})
    assert sorted(p.name for p in (tmp_path / "masks").iterdir()) == [
        "001.png",
        "reference.png",
    ]


def test_empty_amg_candidate_does_not_crash_background_rejection() -> None:
    seg = FakeSegmenter(
        [_blob(10, 10, 20, 20), np.zeros((H, W), bool)],
        [],
        device="cpu",
        fill="none",
        clip=FakeClip(),
        max_area_frac=0.5,
    )
    assert len(seg.run(_rgb())["instances"]) == 1


def test_explicit_cuda_without_a_cuda_ort_provider_is_missing_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from threed_vision import device as dev

    monkeypatch.setattr(dev, "free_vram_mb", lambda: 9000)
    monkeypatch.setattr(clip, "setup_ort_cuda", lambda: False)
    monkeypatch.setattr(clip, "ort_missing_cuda_libs", lambda: [])
    with pytest.raises(MissingDependency, match="CUDAExecutionProvider"):
        clip.ClipEncoder("cuda")._resolve()
    enc = clip.ClipEncoder("auto")
    enc._resolve()
    assert enc.device == "cpu"


def test_explicit_cuda_without_cuda_torch_is_missing_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    torch = pytest.importorskip("torch")
    from threed_vision import torchutil

    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    with pytest.raises(MissingDependency, match="CUDA GPU"):
        torchutil.require_cuda_if_requested("cuda")
    torchutil.require_cuda_if_requested("cpu")


def test_segmenter_load_names_the_missing_model_before_the_missing_torch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("THREED_VISION_MODELS_DIR", str(tmp_path))
    monkeypatch.setitem(sys.modules, "torch", None)  # `import torch` -> ImportError
    with pytest.raises(MissingDependency) as exc:
        Segmenter(device="cpu").load()
    assert "3d vision-models pull sam2-tiny" in exc.value.render()


def test_fill_stage_lets_large_owl_boxes_through_when_max_area_frac_is_raised() -> None:
    seen: list[float] = []

    class RecordingDetector(FakeDetector):
        def detect(
            self, rgb: Any, queries: Any, threshold: float = 0.15, **kw: Any
        ) -> Any:
            seen.append(kw["max_area_frac"])
            return super().detect(rgb, queries, threshold, **kw)

    for frac in (0.2, 0.03):
        FakeSegmenter(
            [],
            [],
            device="cuda",
            fill="owlv2",
            reject_bg=False,
            detector=RecordingDetector([]),
            max_area_frac=frac,
        ).run(_rgb())
    assert seen == [0.2, segment.OWL_MIN_AREA_FRAC]


# --- prompts, option validation, CUDA-free CPU runs, fast quality (#479 #480 #508 #478) ---------------------------


class PromptClip(FakeClip):
    """Text embeddings follow the prompt's brightness word: "dark" prompts look like dark crops, "bright" ones like bright crops."""

    def texts(self, prompts: list[str]) -> Any:
        return np.array(
            [[0.0, 1.0] if "dark" in p else [1.0, 0.0] for p in prompts], np.float32
        )


def _prompt_run(**kw: Any) -> list[tuple[int, int]]:
    bright, dark = _blob(10, 10, 20, 20), _blob(155, 10, 20, 20)  # bright / dark crops
    seg = FakeSegmenter(
        [bright, dark],
        [],
        device="cpu",
        fill="none",
        detector=FakeDetector([]),
        clip=PromptClip(),
        max_area_frac=0.5,
        **kw,
    )
    return [tuple(i["bbox_xywh"][:2]) for i in seg.run(_rgb())["instances"]]


def test_bg_and_fg_prompts_change_which_masks_clip_rejects() -> None:
    # defaults ("carpet / floor ..." are background): FakeClip's texts() is not used here, PromptClip maps by the word "dark"
    dark_is_bg = _prompt_run(bg_prompts=["a dark carpet"], fg_prompts=["a bright tool"])
    bright_is_bg = _prompt_run(bg_prompts=["a bright wall"], fg_prompts=["a dark tool"])
    assert dark_is_bg == [(10, 10)]  # the dark patch is rejected
    assert bright_is_bg == [(155, 10)]  # swapping the prompts swaps the survivor


def test_default_prompts_are_the_documented_lists_and_are_copied() -> None:
    seg = Segmenter(device="cpu")
    assert seg.bg_prompts == segment.BG_PROMPTS and seg.fg_prompts == segment.FG_PROMPTS
    seg.bg_prompts.append("x")
    assert "x" not in segment.BG_PROMPTS


def test_prompts_without_a_bg_or_fg_entry_are_bad_input_unless_rejection_is_off() -> (
    None
):
    with pytest.raises(BadInput, match="at least one bg prompt and one fg prompt"):
        Segmenter(device="cpu", bg_prompts=[])
    with pytest.raises(BadInput):
        Segmenter(device="cpu", fg_prompts=[])
    Segmenter(device="cpu", bg_prompts=[], reject_bg=False)


def test_load_prompts_reads_a_comma_list_or_a_file(tmp_path: Path) -> None:
    assert segment.load_prompts("a wooden table, a shadow ,") == [
        "a wooden table",
        "a shadow",
    ]
    f = tmp_path / "bg.txt"
    f.write_text("# bench\na steel bench\n\na cable  # trailing\n", encoding="utf-8")
    assert segment.load_prompts(str(f)) == ["a steel bench", "a cable"]
    with pytest.raises(BadInput):
        segment.load_prompts(" , ")


def test_a_mistyped_prompt_file_is_an_error_not_a_prompt(tmp_path: Path) -> None:
    for typo in ("bg-prompts.txt", str(tmp_path / "nope" / "bg"), "prompts.md"):
        with pytest.raises(BadInput, match="does not exist"):
            segment.load_prompts(typo)
    bad = tmp_path / "latin1.txt"
    bad.write_bytes("caf\xe9\n".encode("latin-1"))
    with pytest.raises(BadInput, match="cannot read prompt file"):
        segment.load_prompts(str(bad))


@pytest.mark.parametrize(
    "kw",
    [
        {"max_side": -5},
        {"max_side": 10},
        {"max_side": 100000},
        {"points_per_side": -1},
        {"points_per_side": 500},
        {"points_per_batch": 0},
        {"min_area": -1},
        {"max_area_frac": 0.0},
        {"max_area_frac": 1.5},
        {"owl_threshold": 2.0},
        {"owl_tiles": 0},
        {"owl_tiles": 9},
        {"grid_gate": -0.1},
        {"points_per_side": 0, "fill": "none"},
        {"quality": "turbo"},
    ],
)
def test_invalid_options_are_bad_input_at_construction(kw: dict[str, Any]) -> None:
    with pytest.raises(BadInput):
        Segmenter(device="cpu", **kw)


def test_valid_edge_options_are_accepted() -> None:
    Segmenter(
        device="cpu", max_side=0, points_per_side=0
    )  # boxes only, never downscale
    Segmenter(
        device="cpu", points_per_side=128, max_side=64, owl_tiles=4, grid_gate=1.0
    )


def test_cpu_sam2_dense_runs_its_64_grid_instead_of_silently_running_none() -> None:
    seg = FakeSegmenter(
        [_blob(10, 10, 20, 20)],
        [],
        device="cpu",
        method="sam2-dense",
        fill="none",
        reject_bg=False,
        max_area_frac=0.5,
    )
    seg.run(_rgb())
    assert seg.pps == 64 and seg.amg_calls == 1


class _NoCuda:
    """A `torch.cuda` stand-in: anything that would initialise or query a CUDA context raises."""

    @staticmethod
    def is_initialized() -> bool:
        return False  # asking is allowed: it never creates a context

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"torch.cuda.{name} touched on a --device cpu run")


def test_cpu_runs_never_touch_cuda(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    from threed_vision import torchutil

    fake = types.ModuleType("torch")
    fake.cuda = _NoCuda()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "torch", fake)
    seg = FakeSegmenter(
        [_blob(10, 10, 20, 20)],
        [_blob(90, 10, 20, 20)],
        device="cpu",
        detector=FakeDetector([[88, 8, 112, 32]]),
        clip=FakeClip(),
        max_area_frac=0.5,
        min_area=50,
    )
    res = seg.run(_rgb())
    seg.unload()  # free_cuda() on the way out
    assert res["device"] == "cpu" and res["vram_mb"] == 0
    torchutil.reset_peak("cpu")
    assert torchutil.peak_mb("cpu") == 0 and torchutil.cuda_allocated_mb() == 0
    torchutil.free_cuda()


def test_fast_quality_with_a_gate_runs_owl_before_the_grid_and_hands_it_the_objectness() -> (
    None
):
    order: list[str] = []
    seen: dict[str, Any] = {}

    class GateDetector(FakeDetector):
        def detect(
            self, rgb: Any, queries: Any, threshold: float = 0.15, **kw: Any
        ) -> dict[str, Any]:
            order.append("detect")
            return super().detect(rgb, queries, threshold, **kw)

    class FastFake(FakeSegmenter):
        def _grid(
            self, rgb: Any, pps: int, objectness: Any, gate: float
        ) -> list[dict[str, Any]]:
            order.append("grid")
            seen.update(pps=pps, gate=gate, shape=objectness.shape)
            return [{"mask": _blob(10, 10, 20, 20), "source": "amg", "stability": 0.9}]

    seg = FastFake(
        [],
        [],
        device="cuda",
        quality="fast",
        grid_gate=0.1,
        detector=GateDetector([[0, 0, 9, 9]]),
        reject_bg=False,
        max_area_frac=0.5,
        min_area=50,
    )
    res = seg.run(_rgb())
    assert order == ["detect", "grid"] and seen == {
        "pps": 32,
        "gate": 0.1,
        "shape": (4, 4),
    }
    assert [i["source"] for i in res["instances"]] == ["amg"]
    assert list(res["timing_s"])[:3] == ["load", "detect", "amg"]


def test_gate_points_keep_only_points_near_an_object_patch() -> None:
    from threed_vision import sam_grid

    obj = np.zeros((10, 10), np.float32)
    obj[2, 3] = (
        0.9  # one hot patch: x in [3,4)/10 * 100 px, y in [2,3)/10 * 100 px (square 100 x 100 image)
    )
    pts = np.array([[35.0, 25.0], [45.0, 35.0], [95.0, 95.0], [5.0, 5.0]], np.float32)
    kept = sam_grid.gate_points(pts, obj, (100, 100), 0.5)
    # the hot patch and its 3x3 neighbourhood (patches x 2..4, y 1..3) pass; the far points do not
    assert kept.tolist() == [[35.0, 25.0], [45.0, 35.0]]
    assert sam_grid.gate_points(pts, obj, (100, 100), 0.0).shape == (4, 2)


def test_gate_points_use_the_padded_square_frame_of_a_wide_image() -> None:
    from threed_vision import sam_grid

    obj = np.zeros((10, 10), np.float32)
    obj[0, 9] = 1.0  # top-right patch of the padded square (side = max(H, W) = 200)
    pts = np.array([[195.0, 5.0], [195.0, 90.0]], np.float32)  # 200 x 100 image
    kept = sam_grid.gate_points(pts, obj, (100, 200), 0.5)
    assert kept.tolist() == [[195.0, 5.0]]


def test_clean_mask_fills_small_holes_and_drops_small_islands_but_keeps_the_frame_edge() -> (
    None
):
    from threed_vision import sam_grid

    m = np.zeros((80, 80), bool)
    m[10:60, 10:60] = True
    m[30:34, 30:34] = False  # a 16 px hole
    m[70:73, 70:73] = True  # a 9 px island
    out = sam_grid.clean_mask(m, min_area=20)
    assert out[30:34, 30:34].all() and not out[70:73, 70:73].any()
    assert out[10:60, 10:60].all() and int(out.sum()) == 50 * 50
    notch = np.zeros((40, 40), bool)
    notch[0:30, 0:30] = True
    notch[0:3, 10:13] = (
        False  # 9 px of background open to the frame edge: the outside, not a hole
    )
    assert (sam_grid.clean_mask(notch, min_area=20) == notch).all()
    assert not sam_grid.clean_mask(np.zeros((9, 9), bool)).any()
    # every island small: the largest survives (SAM's remove_small_regions), as a mask is never emptied by cleaning
    small = np.zeros((40, 40), bool)
    small[5:12, 5:12] = True  # 49 px
    small[30:33, 30:33] = True  # 9 px
    kept = sam_grid.clean_mask(small, min_area=100)
    assert kept[5:12, 5:12].all() and not kept[30:33, 30:33].any()


def test_mask_bbox_matches_nonzero_bbox_and_handles_empty() -> None:
    rng = np.random.default_rng(3)
    for _ in range(20):
        m = rng.random((40, 60)) > 0.97
        ys, xs = np.nonzero(m)
        want = (
            [0, 0, 0, 0]
            if xs.size == 0
            else [
                int(xs.min()),
                int(ys.min()),
                int(xs.max() - xs.min() + 1),
                int(ys.max() - ys.min() + 1),
            ]
        )
        assert io.mask_bbox_xywh(m) == want
    assert io.mask_bbox_xywh(np.zeros((5, 5), bool)) == [0, 0, 0, 0]


def test_tile_windows_cover_the_image_and_cut_boxes_are_flagged() -> None:
    wins = detect.tile_windows(100, 200, 2, 0.25)
    assert (
        wins[0] == (0, 0, 200, 100) and len(wins) == 5
    )  # the whole image + 2 x 2 tiles
    cover = np.zeros((100, 200), bool)
    for x0, y0, x1, y1 in wins[1:]:
        assert 0 <= x0 < x1 <= 200 and 0 <= y0 < y1 <= 100
        cover[y0:y1, x0:x1] = True
    assert cover.all()
    assert detect.tile_windows(100, 200, 1, 0.25) == [(0, 0, 200, 100)]
    win = wins[1]  # top-left tile
    boxes = np.array(
        [[10, 10, 40, 40], [10, 10, win[2] - 1, 40], [10, 10, 40, win[3] - 1]],
        np.float32,
    )
    cut = detect.cut_by_inner_edge(boxes, win, (100, 200))
    assert cut.tolist() == [False, True, True]
    # a tile edge that IS the image edge never cuts
    assert not detect.cut_by_inner_edge(
        np.array([[0, 0, 30, 30]], np.float32), win, (100, 200)
    ).any()


def test_detect_rejects_bad_tiles() -> None:
    det = detect.OwlDetector("cpu")
    with pytest.raises(BadInput):
        det.detect(np.zeros((8, 8, 3), np.uint8), tiles=0)
    with pytest.raises(BadInput):
        det.detect(np.zeros((8, 8, 3), np.uint8), tiles=2, tile_overlap=0.9)


def test_owl_tiles_option_reaches_the_detector() -> None:
    det = FakeDetector([])
    seg = FakeSegmenter(
        [_blob(10, 10, 20, 20)],
        [],
        device="cpu",
        owl_tiles=3,
        detector=det,
        reject_bg=False,
        max_area_frac=0.5,
        min_area=50,
    )
    seg.run(_rgb())
    assert det.kwargs["tiles"] == 3 and det.kwargs["with_objectness"] is True


def test_cpu_default_is_the_fast_grid_on_the_full_resolution_photo() -> None:
    seen: dict[str, Any] = {}

    class CpuFast(FakeSegmenter):
        def _grid(
            self, rgb: Any, pps: int, objectness: Any, gate: float
        ) -> list[dict[str, Any]]:
            seen.update(shape=rgb.shape, pps=pps, gate=gate)
            return [{"mask": _blob(10, 10, 20, 20), "source": "amg", "stability": 0.9}]

    seg = CpuFast(
        [],
        [],
        device="cpu",
        quality=None,
        detector=FakeDetector([]),
        reject_bg=False,
        max_area_frac=0.5,
        min_area=50,
    )
    assert seg.effective_quality == "fast"
    res = seg.run(_rgb())
    assert seen == {"shape": (H, W, 3), "pps": 32, "gate": segment.DEFAULT_GRID_GATE}
    assert res["process_scale"] == 1.0 and res["points_per_side"] == 32


def test_quality_defaults_to_accurate_on_gpu_and_fast_on_cpu_and_is_overridable() -> (
    None
):
    gpu, cpu = Segmenter(device="cuda"), Segmenter(device="cpu")
    gpu.device, cpu.device = "cuda", "cpu"
    assert gpu.effective_quality == "accurate" and cpu.effective_quality == "fast"
    assert Segmenter(device="cpu", quality="accurate").effective_quality == "accurate"
    forced = Segmenter(device="cuda", quality="fast")
    forced.device = "cuda"
    assert forced.effective_quality == "fast"


class CudaThenCpuEncoder(clip.OrtEncoder):
    """An onnxruntime encoder whose CUDA session fails once; records the device of every build attempt."""

    model_name = "clip-b32"

    def __init__(self, device: str, error: Exception) -> None:
        super().__init__(device)
        self.error = error
        self.built_on: list[str] = []

    def _resolve(self) -> None:
        self.device = "cuda"

    def _build(self) -> None:
        self.built_on.append(self.device)
        if self.device == "cuda":
            raise self.error


def test_auto_retries_on_cpu_when_the_cuda_session_cannot_initialise_but_not_for_other_errors() -> (
    None
):
    enc = CudaThenCpuEncoder("auto", clip.CudaSessionFailed("cuBLAS failed"))
    enc.load()
    assert enc.built_on == ["cuda", "cpu"] and enc.device == "cpu" and enc.loaded
    other = CudaThenCpuEncoder("auto", MissingDependency("tokenizers is missing"))
    with pytest.raises(MissingDependency, match="tokenizers"):
        other.load()
    assert other.built_on == ["cuda"]  # a missing package is not retried on CPU
    explicit = CudaThenCpuEncoder("cuda", clip.CudaSessionFailed("cuBLAS failed"))
    with pytest.raises(clip.CudaSessionFailed):
        explicit.load()
    assert explicit.built_on == ["cuda"]  # an explicit cuda request reports it
