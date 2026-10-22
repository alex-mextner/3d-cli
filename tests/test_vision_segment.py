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
        return {
            "boxes": [
                {"xyxy": b, "score": 0.5, "label": "a hand tool"} for b in self.boxes
            ],
            "timing_s": 0.0,
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
        super().__init__(**kw)
        self._amg_masks, self._fill_masks, self.amg_calls = amg, fill_masks, 0
        self.seen_shapes: list[tuple[int, ...]] = []

    def load(self) -> None:
        self.device = "cpu" if self.requested == "cpu" else "cuda"
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
    from threed_vision.commands import segment as seg_cmd

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
    doc = seg_cmd.write_result(tmp_path, "photo.jpg", rgb, result)
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
    seg.load()
    assert seg.device == expected and Segmenter.vram_estimate_mb == 2000


# --- `detect --masks`: SAM2 box prompts for the detector boxes -----------------------------------------------------


class BoxPromptSegmenter(Segmenter):
    """Replaces only the SAM2 predictor call: the mask is the prompt box itself; zero-area boxes yield an empty mask."""

    def load(self) -> None:
        self.device = "cpu"
        self.loaded = True

    def _add_prompt(
        self, rgb: Any, instances: Any, source: str, **prompt: Any
    ) -> list[dict[str, Any]]:
        x0, y0, x1, y1 = (int(v) for v in prompt["box"][0])
        mask = np.zeros(rgb.shape[:2], bool)
        mask[y0:y1, x0:x1] = True
        if not mask.any():
            raise BadInput("the prompt produced an empty mask")
        return [
            *instances,
            segment.make_instance(mask, segment.next_id(instances), source, 0.0, 0.9),
        ]


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
