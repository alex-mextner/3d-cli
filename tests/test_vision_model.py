"""Tier (b) of `3d printer vision` (lib/vision_model.py, lib/vision_fit.py): the whole-frame CLIP
model, its device choice, cadence and debounce, the Findings it feeds VisionMonitor, the fitted
head and `vision eval --model`.

No real weights: `tests/data/vision/tiny_tier_b.onnx` is a 6 KB stand-in with CLIP's input/output
contract (regenerate: `tools/vision_model_bench.py tiny-onnx`), run through the real onnxruntime
CPU path; scripted fakes stand in for the scorer wherever the logic, not the pixels, is tested.
The CUDA test runs only where onnxruntime has the CUDA provider."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from errors import InvalidArgument, MissingDependency, ThreeDError, UsageError
from printer_ops.vision import WARNING, Frame, FrameScore, PrintContext, VisionMonitor

TINY = Path(__file__).parent / "data" / "vision" / "tiny_tier_b.onnx"
JPEG = b"\xff\xd8" + b"\x00" * 64 + b"\xff\xd9"


def _np() -> Any:
    return pytest.importorskip("numpy")


def _cv2() -> Any:
    return pytest.importorskip("cv2")


def _ort() -> Any:
    return pytest.importorskip("onnxruntime")


def _frame(level: int, w: int = 64, h: int = 36) -> Any:
    return _np().full((h, w, 3), level, _np().uint8)


# ---- configuration -------------------------------------------------------------------------------


def test_defaults_are_conservative_and_cadenced() -> None:
    import vision_model as vm

    cfg = vm.parse_config(None)
    assert (cfg.enabled, cfg.device, cfg.every_s, cfg.confirm, cfg.clear) == (
        None,
        "auto",
        30.0,
        3,
        2,
    )
    assert vm.parse_config(True).enabled is True
    assert vm.parse_config({"enabled": False}).enabled is False
    # the CLI flag beats the config
    assert vm.parse_config({"enabled": False}, flag=True).enabled is True
    assert vm.parse_config({"enabled": True}, flag=False).enabled is False


def test_a_threshold_number_applies_to_every_class_and_a_mapping_to_its_own() -> None:
    import vision_model as vm

    assert vm.parse_config({"threshold": 0.6}).thresholds == dict.fromkeys(
        vm.RULES, 0.6
    )
    assert vm.parse_config({"threshold": {"spaghetti": 0.7}}).thresholds == {
        "spaghetti": 0.7
    }


@pytest.mark.parametrize(
    "bad",
    [
        "yes",
        {"colour": 1},
        {"enabled": "yes"},
        {"every_s": 0},
        {"every_s": "30"},
        {"confirm": 0},
        {"threshold": 0},
        {"threshold": 1.5},
        {"threshold": {"cats": 0.5}},
        {"crop": [0, 0, 1]},
        {"crop": [0.5, 0, 0.2, 1]},
        {"crop": [0, 0, 1.2, 1]},
    ],
)
def test_bad_config_is_a_usage_error_naming_the_key(bad: object) -> None:
    import vision_model as vm

    with pytest.raises(UsageError, match="watch.vision.model|must be a mapping"):
        vm.parse_config(bad)


def test_unknown_device_lists_the_accepted_ones() -> None:
    import vision_model as vm

    with pytest.raises(InvalidArgument, match="auto, cpu, cuda"):
        vm.parse_config({"device": "tpu"})


def test_crop_cuts_the_bed_area_out_of_the_frame() -> None:
    import vision_model as vm

    cfg = vm.parse_config({"crop": [0.25, 0.5, 0.75, 1.0]})
    assert cfg.crop == (0.25, 0.5, 0.75, 1.0)
    img = _np().zeros((100, 200, 3), _np().uint8)
    assert vm.crop_frame(img, cfg.crop).shape == (50, 100, 3)
    assert vm.crop_frame(img, None) is img


# ---- device choice -------------------------------------------------------------------------------

CUDA = ["CUDAExecutionProvider", "CPUExecutionProvider"]
CPU_ONLY = ["CPUExecutionProvider"]


@pytest.mark.parametrize(
    ("requested", "providers", "free", "expected"),
    [
        ("cpu", CUDA, 8000, "cpu"),
        ("auto", CUDA, 4000, "cuda"),
        ("auto", CUDA, 2048, "cuda"),
        ("auto", CUDA, 1500, "cpu"),  # the desktop holds the card: not enough VRAM left
        ("auto", CUDA, None, "cpu"),  # no NVIDIA driver / nvidia-smi
        ("auto", CPU_ONLY, 8000, "cpu"),
        ("cuda", CUDA, 100, "cuda"),  # an explicit request is honoured
    ],
)
def test_device_resolution(
    requested: str, providers: list[str], free: int | None, expected: str
) -> None:
    import vision_model as vm

    assert vm.resolve_device(requested, providers=providers, free_mib=free) == expected


def test_explicit_cuda_without_the_provider_is_a_structured_error_not_a_silent_cpu_run() -> (
    None
):
    import vision_model as vm

    with pytest.raises(MissingDependency) as err:
        vm.resolve_device("cuda", providers=CPU_ONLY, free_mib=8000)
    assert "onnxruntime-gpu" in err.value.install


def test_env_overrides_the_configured_device(monkeypatch: pytest.MonkeyPatch) -> None:
    import vision_model as vm

    cfg = vm.parse_config({"device": "cuda"})
    assert vm.requested_device(cfg) == "cuda"
    monkeypatch.setenv("THREED_VISION_DEVICE", "CPU")
    assert vm.requested_device(cfg) == "cpu"
    monkeypatch.setenv("THREED_VISION_DEVICE", "gpu")
    with pytest.raises(InvalidArgument, match="THREED_VISION_DEVICE"):
        vm.requested_device(cfg)


def test_free_vram_is_none_without_a_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    import vision_model as vm

    def no_smi(*a: Any, **k: Any) -> None:
        raise FileNotFoundError("nvidia-smi")

    monkeypatch.setattr(subprocess, "run", no_smi)
    assert vm.free_vram_mib() is None

    class Done:
        stdout = "4068\n"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Done())
    assert vm.free_vram_mib() == 4068


# ---- the tier: cadence, debounce, findings -------------------------------------------------------


@dataclass
class FakeScorer:
    """Scripted per-run class probabilities; the last script entry repeats."""

    script: list[dict[str, float]]
    device: str = "cpu"
    head: Any = None
    calls: int = 0
    closed: bool = False
    seen_shapes: list[tuple[int, ...]] = field(default_factory=list)

    def score_bgr(self, bgr: Any) -> dict[str, float]:
        self.seen_shapes.append(tuple(bgr.shape))
        row = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return {
            "spaghetti": 0.0,
            "part_detached": 0.0,
            "empty_bed": 0.0,
            "healthy": 1.0,
            **row,
        }

    def close(self) -> None:
        self.closed = True


def _tier(script: list[dict[str, float]], **cfg: Any) -> tuple[Any, FakeScorer]:
    import vision_model as vm

    scorer = FakeScorer(script)
    return vm.ModelTier(scorer, vm.parse_config(cfg)), scorer  # type: ignore[arg-type]


CTX = PrintContext("part.gcode", 25.0, 100.0, 100.0)


def test_the_model_runs_every_every_s_seconds_and_not_per_frame() -> None:
    tier, scorer = _tier([{}], every_s=30)
    img = _frame(100)
    for t in range(0, 125, 10):  # a frame every 10 s for 2 minutes
        tier.update(img, CTX, float(t))
    assert scorer.calls == 5  # t = 0, 30, 60, 90, 120
    assert tier.runs == 5


def test_a_finding_needs_confirm_runs_in_a_row_and_stays_alive_between_runs() -> None:
    tier, _ = _tier([{"spaghetti": 0.9}], every_s=30, confirm=3)
    img = _frame(100)
    out = [tier.update(img, CTX, float(t)) for t in range(0, 91, 10)]
    first = next(i for i, f in enumerate(out) if f)
    assert first == 6  # the third run at/above the threshold is t = 60 s
    assert all(
        out[i] for i in range(first, len(out))
    )  # held on the frames between runs
    f = out[-1][0]
    assert (f.kind, f.severity) == ("vision_model_spaghetti", WARNING)
    assert (
        f.data["class"] == "spaghetti"
        and f.data["score"] == 0.9
        and f.data["device"] == "cpu"
    )
    assert "second opinion" in f.text


def test_a_single_spike_never_raises_a_finding() -> None:
    tier, _ = _tier([{}, {"spaghetti": 0.95}, {}, {"spaghetti": 0.95}, {}], every_s=1)
    assert [tier.update(_frame(90), CTX, float(t)) for t in range(5)] == [[]] * 5


def test_the_finding_clears_after_clear_quiet_runs_not_on_one_dip() -> None:
    hot, cold = {"spaghetti": 0.9}, {"spaghetti": 0.1}
    tier, _ = _tier(
        [hot, hot, hot, cold, hot, cold, cold], every_s=1, confirm=3, clear=2
    )
    active = [bool(tier.update(_frame(90), CTX, float(t))) for t in range(7)]
    assert active == [False, False, True, True, True, True, False]


def test_empty_bed_is_off_by_default_and_judged_only_once_the_print_is_high_enough() -> None:
    hot = [{"empty_bed": 0.95}]
    low = PrintContext("part.gcode", 4.0, 0, 0)
    tier, _ = _tier(hot, every_s=1, confirm=1)
    assert tier.update(_frame(90), CTX, 0.0) == []  # zero-shot is not trusted for this class
    for ctx, expect in ((None, False), (low, False), (CTX, True)):
        tier, _ = _tier(hot, every_s=1, confirm=1, threshold={"empty_bed": 0.8})
        found = tier.update(_frame(90), ctx, 0.0)
        assert bool(found) is expect, ctx
        if expect:
            assert (
                found[0].kind == "vision_model_empty_bed" and found[0].data["z"] == 25.0
            )
    tier, _ = _tier(hot, every_s=1, confirm=1, min_z=2.0, threshold={"empty_bed": 0.8})
    assert tier.update(
        _frame(90), low, 0.0
    )  # the per-device min_z beats the rule's 10 mm


def test_part_detached_is_off_until_a_threshold_or_a_fitted_head_enables_it() -> None:
    import vision_model as vm

    hot = [{"part_detached": 0.99}]
    tier, _ = _tier(hot, every_s=1, confirm=1)
    assert (
        tier.update(_frame(90), CTX, 0.0) == []
    )  # zero-shot has no skill on this class
    tier, _ = _tier(hot, every_s=1, confirm=1, threshold={"part_detached": 0.9})
    assert [f.kind for f in tier.update(_frame(90), CTX, 0.0)] == [
        "vision_model_part_detached"
    ]
    # a head's fitted threshold enables it too
    np = _np()
    head = vm.Head(
        {"part_detached": vm.HeadClass(np.zeros(512, np.float32), 0.0, 0.6)}, {}
    )
    scorer = FakeScorer(hot, head=head)
    tier = vm.ModelTier(scorer, vm.parse_config({"every_s": 1, "confirm": 1}))  # type: ignore[arg-type]
    assert tier.threshold("part_detached") == 0.6
    assert [f.kind for f in tier.update(_frame(90), CTX, 0.0)] == [
        "vision_model_part_detached"
    ]


def test_reset_print_drops_the_standing_finding_and_the_counters() -> None:
    tier, _ = _tier([{"spaghetti": 0.9}], every_s=30, confirm=1)
    assert tier.update(_frame(90), CTX, 0.0)
    tier.reset_print()
    assert tier.update(
        _frame(90), CTX, 1.0
    )  # scored again at once: the cadence restarted
    assert tier.runs == 2


def test_the_crop_is_what_the_model_sees() -> None:
    tier, scorer = _tier([{}], every_s=1, crop=[0, 0.5, 1, 1])
    tier.update(_np().zeros((100, 200, 3), _np().uint8), CTX, 0.0)
    assert scorer.seen_shapes == [(50, 200, 3)]


def test_report_and_close() -> None:
    tier, scorer = _tier([{"spaghetti": 0.4}], every_s=1)
    tier.update(_frame(90), CTX, 0.0)
    rep = tier.report()
    assert (
        rep["scores"]["spaghetti"] == 0.4
        and rep["device"] == "cpu"
        and rep["runs"] == 1
    )
    tier.close()
    assert scorer.closed


def test_visionmonitor_turns_the_held_finding_into_exactly_one_edge_triggered_event() -> (
    None
):
    """The monitor clears a Finding kind the moment a frame lacks it, so the tier must keep it
    alive between its 30 s runs: 20 frames at 10 s -> one event, not one per run."""
    pytest.importorskip("printer_ops.watch")
    tier, _ = _tier([{"spaghetti": 0.9}], every_s=30, confirm=2)
    t = [0.0]

    def score(jpeg: bytes) -> FrameScore:
        s = FrameScore(0.0, findings=tier.update(_frame(90), CTX, t[0]))
        t[0] += 10.0
        return s

    mon = VisionMonitor("u1", score)
    mon.set_job("printing", "part.gcode")
    events = [
        e
        for i in range(20)
        for e in mon.step(Frame(1000.0 + i * 10, JPEG + bytes([i]), None))[1]
    ]
    assert [(e.kind, e.severity) for e in events] == [
        ("vision_model_spaghetti", WARNING)
    ]
    assert events[0].data["class"] == "spaghetti"


# ---- prompts, preprocessing, the real onnxruntime CPU path -----------------------------------------


def test_shipped_prompt_embeddings_cover_every_prompt_and_are_unit_vectors() -> None:
    np = _np()
    import vision_model as vm

    table = vm.PromptTable.load()
    assert set(table.classes) == set(vm.PROMPTS)
    assert table.emb.shape == (sum(len(p) for p in vm.PROMPTS.values()), vm.EMBED_DIM)
    assert np.allclose(np.linalg.norm(table.emb, axis=1), 1.0, atol=1e-5)
    # a prompt's own embedding picks its own class (the text tower agrees with itself)
    own = table.emb[0]
    probs = table.probs(own)
    assert (
        max(probs, key=lambda c: probs[c]) == "spaghetti"
        and abs(sum(probs.values()) - 1) < 1e-6
    )


def test_preprocess_squashes_the_whole_frame_and_normalises() -> None:
    np = _np()
    _cv2()
    import vision_model as vm

    x = vm.preprocess(_frame(255, w=1920, h=1080))
    assert x.shape == (1, 3, 224, 224) and x.dtype == np.float32
    expect = (1.0 - np.array(vm.MEAN)) / np.array(vm.STD)
    assert np.allclose(x[0, :, 0, 0], expect, atol=1e-5)
    left_edge = np.zeros((36, 64, 3), np.uint8)
    left_edge[:, :8] = (
        255  # a bright strip at the left edge must survive (no centre crop)
    )
    assert vm.preprocess(left_edge)[0, 0, 100, 0] > 1.0


def test_tiny_model_on_the_cpu_embeds_deterministically() -> None:
    np = _np()
    _cv2()
    _ort()
    import vision_model as vm

    sc = vm.ModelScorer(TINY, device="cpu")
    assert sc.device == "cpu"
    a, b = sc.embed(_frame(200)), sc.embed(_frame(200))
    assert (
        a.shape == (512,)
        and np.allclose(a, b)
        and abs(float(np.linalg.norm(a)) - 1) < 1e-5
    )
    assert (
        float(a @ sc.embed(_frame(30))) < 0.99
    )  # different pictures, different embeddings
    probs = sc.score_bgr(_frame(200))
    assert set(probs) == set(vm.PROMPTS) and abs(sum(probs.values()) - 1) < 1e-5
    many = sc.embed_many([_frame(10), _frame(240)])
    assert many.shape == (2, 512)
    sc.close()
    sc.close()  # twice is fine
    with pytest.raises(RuntimeError, match="closed"):
        sc.embed(_frame(10))


def test_cuda_and_cpu_agree_when_the_cuda_provider_is_there() -> None:
    np = _np()
    _cv2()
    ort = _ort()
    import vision_model as vm

    if (
        "CUDAExecutionProvider" not in ort.get_available_providers()
        or vm.free_vram_mib() is None
    ):
        pytest.skip("no onnxruntime CUDA provider / NVIDIA driver")
    gpu = vm.ModelScorer(TINY, device="cuda")
    if gpu.device != "cuda":
        pytest.skip(
            "the CUDA provider is installed but cannot start (missing cuDNN/cuBLAS)"
        )
    cpu = vm.ModelScorer(TINY, device="cpu")
    img = _frame(180)
    assert np.allclose(gpu.embed(img), cpu.embed(img), atol=1e-3)
    gpu.close()
    cpu.close()


# ---- the head --------------------------------------------------------------------------------------


def _head_doc(**over: Any) -> dict[str, Any]:
    import vision_model as vm

    return {
        "version": 1,
        "model": vm.MODEL_TAG,
        "classes": {
            "part_detached": {"weights": [0.1] * 512, "bias": -0.5, "threshold": 0.7}
        },
        **over,
    }


def test_a_head_overrides_the_zero_shot_probability_of_its_class(
    tmp_path: Path,
) -> None:
    np = _np()
    _cv2()
    _ort()
    import vision_model as vm

    path = tmp_path / "head.json"
    path.write_text(json.dumps(_head_doc()))
    head = vm.Head.load(path)
    assert head.classes["part_detached"].threshold == 0.7
    sc = vm.ModelScorer(TINY, device="cpu", head=head)
    emb = sc.embed(_frame(200))
    probs = sc.probs(emb)
    assert probs["part_detached"] == pytest.approx(
        1 / (1 + np.exp(-(0.1 * float(emb.sum()) - 0.5))), abs=1e-5
    )
    assert (
        sc.probs(emb)["spaghetti"] == vm.PromptTable.load().probs(emb)["spaghetti"]
    )  # others stay zero-shot
    sc.close()


@pytest.mark.parametrize(
    "doc",
    [
        {"model": "some-other-model", "classes": {}},
        {"model": None, "classes": {}},
    ],
)
def test_a_head_fitted_on_another_model_is_refused(
    tmp_path: Path, doc: dict[str, Any]
) -> None:
    import vision_model as vm

    path = tmp_path / "head.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(ThreeDError, match="fitted on"):
        vm.Head.load(path)


def test_a_broken_head_file_is_a_structured_error(tmp_path: Path) -> None:
    import vision_model as vm

    path = tmp_path / "head.json"
    for text in (
        "{not json",
        json.dumps(_head_doc(classes={"cats": {"weights": [0.0] * 512, "bias": 0}})),
        json.dumps(_head_doc(classes={"spaghetti": {"weights": [0.0] * 3, "bias": 0}})),
    ):
        path.write_text(text)
        with pytest.raises(ThreeDError, match="head"):
            vm.Head.load(path)
    with pytest.raises(ThreeDError, match="cannot read"):
        vm.Head.load(tmp_path / "missing.json")


def test_logistic_fit_separates_two_clouds_and_auroc_is_exact() -> None:
    np = _np()
    import vision_fit as vf

    rng = np.random.RandomState(0)
    xp = rng.randn(60, 8) + 2.0
    xn = rng.randn(80, 8) - 2.0
    w, b = vf.fit_logistic(
        np.vstack([xp, xn]), np.r_[np.ones(60), np.zeros(80)], l2=1.0
    )
    assert vf.auroc(vf.sigmoid_scores(xp, w, b), vf.sigmoid_scores(xn, w, b)) == 1.0
    assert vf.auroc(np.array([1.0, 2.0]), np.array([1.0, 0.0])) == pytest.approx(0.875)
    assert [len(f) for f in vf.block_folds(10, 3)] == [4, 3, 3]
    cv = vf.cross_validate(xp, xn, l2=1.0, folds=4, fpr=0.05)
    assert cv["folds"] == 4 and cv["auroc"] == 1.0 and cv["tpr_at_threshold"] == 1.0
    assert vf.cross_validate(xp[:1], xn[:1], l2=1.0, folds=4, fpr=0.05) == {"folds": 0}


def _write_frames(folder: Path, levels: list[int]) -> Path:
    cv2 = _cv2()
    folder.mkdir()
    for i, level in enumerate(levels):
        cv2.imwrite(str(folder / f"{i:03d}.jpg"), _frame(level, 96, 54))
    return folder


def _fake_open_scorer(monkeypatch: pytest.MonkeyPatch) -> None:
    import vision_model as vm

    def open_tiny(mc: Any, *, download: bool, use_head: bool = True) -> Any:
        assert download is True
        head_file = vm.head_path(mc)
        head = vm.Head.load(head_file) if use_head and head_file.is_file() else None
        return vm.ModelScorer(TINY, device="cpu", head=head)

    monkeypatch.setattr(vm, "open_scorer", open_tiny)


def test_fit_head_writes_a_head_that_the_scorer_then_uses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bright vs dark frames stand in for 'detached' vs 'ok' (the tiny model embeds the mean
    colour): fit-head must learn it, write models/tier-b-head.json, keep earlier classes."""
    _cv2()
    _ort()
    import argparse

    import vision_fit as vf
    import vision_model as vm

    _fake_open_scorer(monkeypatch)
    pos = _write_frames(
        tmp_path / "pos", [200, 210, 220, 230, 240, 250, 205, 215, 225, 235]
    )
    neg = _write_frames(tmp_path / "neg", [10, 20, 30, 40, 50, 60, 15, 25, 35, 45])
    out = tmp_path / "head.json"
    out.write_text(
        json.dumps(
            _head_doc(classes={"empty_bed": {"weights": [0.0] * 512, "bias": 0.0}})
        )
    )
    ap = argparse.ArgumentParser()
    vf.add_fit_arguments(ap)
    args = ap.parse_args(
        [
            "--class",
            "part_detached",
            "--pos",
            str(pos),
            "--neg",
            str(neg),
            "--out",
            str(out),
            "--folds",
            "2",
            "--json",
        ]
    )
    assert vf.run_fit(args) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["frames"] == {"pos": 10, "neg": 10}
    assert summary["cross_validation"]["auroc"] == 1.0
    doc = json.loads(out.read_text())
    assert set(doc["classes"]) == {
        "empty_bed",
        "part_detached",
    }  # the other class was kept
    assert doc["classes"]["part_detached"]["fitted"]["frames"] == {"pos": 10, "neg": 10}
    head = vm.Head.load(out)
    sc = vm.ModelScorer(TINY, device="cpu", head=head)
    bright, dark = sc.score_bgr(_frame(220)), sc.score_bgr(_frame(30))
    assert bright["part_detached"] > 0.8 > 0.2 > dark["part_detached"]
    assert head.classes["part_detached"].threshold == summary["threshold"]
    sc.close()


def test_fit_head_refuses_unknown_class_missing_paths_and_tiny_sets(
    tmp_path: Path,
) -> None:
    import argparse

    import vision_fit as vf

    ap = argparse.ArgumentParser()
    vf.add_fit_arguments(ap)
    with pytest.raises(InvalidArgument, match="part_detached"):
        vf.run_fit(ap.parse_args(["--class", "cats", "--pos", "a", "--neg", "b"]))
    with pytest.raises(UsageError, match="missing path"):
        vf.run_fit(
            ap.parse_args(
                [
                    "--class",
                    "spaghetti",
                    "--pos",
                    str(tmp_path / "nope"),
                    "--neg",
                    str(tmp_path),
                ]
            )
        )


# ---- eval --model, build_tier, fetch ---------------------------------------------------------------


def test_eval_model_reports_per_threshold_numbers_and_every_class_mean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _cv2()
    _ort()
    import vision_model as vm

    _fake_open_scorer(monkeypatch)
    pos = _write_frames(tmp_path / "pos", [220, 230, 240])
    neg = _write_frames(tmp_path / "neg", [20, 30, 40, 50])
    m = vm.eval_sources(
        [(pos, "pos"), (neg, "neg")], vm.parse_config(None), cls="empty_bed"
    )
    assert (
        m["frames"] == {"pos": 3, "neg": 4}
        and m["device"] == "cpu"
        and m["class"] == "empty_bed"
    )
    assert set(m["mean"]["pos"]) == set(vm.PROMPTS)
    assert set(m["per_frame"]) == {str(t) for t in vm.EVAL_THRESHOLDS}
    assert len(m["worst_negatives"]) == 4
    vm.print_eval(m)
    out = capsys.readouterr().out
    assert "tier (b)" in out and "zero-shot prompts" in out and "s/frame" in out
    with pytest.raises(InvalidArgument, match="--model-class"):
        vm.eval_sources([(pos, "pos")], vm.parse_config(None), cls="cats")


def test_build_tier_is_off_unless_cached_or_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vision_model as vm

    monkeypatch.setattr(vm, "cached", lambda spec=vm.TIER_B: False)
    assert vm.build_tier(None) is None  # default: never starts a download by itself
    assert vm.build_tier({"enabled": False}) is None
    monkeypatch.setattr(vm, "cached", lambda spec=vm.TIER_B: True)
    assert vm.build_tier({"enabled": False}) is None
    assert vm.build_tier(None, flag=False) is None


def test_build_tier_builds_from_cached_weights_on_the_cpu(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _cv2()
    _ort()
    import vision_model as vm

    monkeypatch.setattr(vm, "cached", lambda spec=vm.TIER_B: True)
    monkeypatch.setattr(vm, "ensure_model", lambda spec, fetch=True: TINY)
    monkeypatch.setattr(vm, "head_path", lambda mc: tmp_path / "no-head.json")
    monkeypatch.setattr(vm, "available_providers", lambda: CPU_ONLY)
    monkeypatch.setenv("THREED_VISION_DEVICE", "auto")
    tier = vm.build_tier({"every_s": 45})
    assert (
        tier is not None and tier.scorer.device == "cpu" and tier.config.every_s == 45
    )
    tier.close()


def test_a_requested_tier_that_cannot_download_is_a_structured_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--model offline: MissingDependency naming the fetch command, never a raw URLError."""
    import urllib.error

    import vision_model as vm

    def offline(spec: Any, fetch: bool = True) -> Path:
        raise urllib.error.URLError("Name or service not known")

    monkeypatch.setattr(vm, "ensure_model", offline)
    with pytest.raises(MissingDependency) as err:
        vm.build_tier(None, flag=True)
    assert "fetch-model --tier b" in err.value.install


def test_cached_reflects_the_models_dir_without_downloading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vision_model as vm

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert vm.cached() is False
    models = tmp_path / "3d-cli" / "vision" / "models"
    models.mkdir(parents=True)
    (models / vm.TIER_B.name).write_bytes(b"not the weights")
    assert vm.cached() is False  # a file that fails its sha256 does not count
    assert not any(p.name.endswith(".part") for p in models.iterdir())


def test_the_pinned_spec_names_a_revision_and_a_permissive_licence() -> None:
    import vision_model as vm

    assert (
        "342fdf2f67aded64d138ff074745fb4a5d2bba5f" in vm.TIER_B.url
    )  # a commit, not `main`
    assert vm.TIER_B.licence.startswith("MIT") and len(vm.TIER_B.sha256) == 64
    assert vm.BASE_REVISION in "57c216476eefef5ab752ec549e440a49ae4ae5f3"


def test_guard_never_builds_tier_b() -> None:
    """The one-job PAUSE guard decides from tier (a) only: build_scorer's default is no tier (b)."""
    import inspect

    import vision_detect

    assert (
        inspect.signature(vision_detect.build_scorer).parameters["model"].default
        is False
    )


# ---- the CLI surface of vision_detect.main --------------------------------------------------------


def test_run_flags_are_tristate_so_the_config_decides_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    import vision_detect

    seen: list[Any] = []

    def fake_live(args: Any) -> int:
        seen.append(args.model)
        return 0

    monkeypatch.setattr(vision_detect, "run_live", fake_live)
    for argv in (["run", "u1"], ["run", "u1", "--model"], ["run", "u1", "--no-model"]):
        assert vision_detect.main(argv) == 0
    assert seen == [None, True, False]


def test_fetch_model_tier_b_never_touches_the_tier_a_weights(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import vision_detect
    import vision_model as vm

    def tier_a_must_not_run(*a: Any, **k: Any) -> Path:
        raise AssertionError("tier (a) weights requested")

    monkeypatch.setattr(vision_detect, "ensure_model", tier_a_must_not_run)
    monkeypatch.setattr(vm, "fetch", lambda spec=vm.TIER_B: Path("/cache/tier-b.onnx"))
    assert vision_detect.main(["fetch-model", "--tier", "b"]) == 0
    assert capsys.readouterr().out.strip() == "/cache/tier-b.onnx"
    with pytest.raises(SystemExit):
        vision_detect.main(["fetch-model", "--tier", "c"])


def test_fetch_model_all_fetches_both_tiers_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import vision_detect
    import vision_model as vm
    import vision_verify

    calls: list[str] = []

    def stub(name: str) -> Any:
        def fetch(*a: Any, **k: Any) -> Path:
            calls.append(name)
            return Path(f"/c/{name}")

        return fetch

    monkeypatch.setattr(vision_detect, "ensure_model", stub("obico"))
    monkeypatch.setattr(vision_verify, "ensure_clip", stub("clip"))
    monkeypatch.setattr(vm, "fetch", stub("b"))
    for _ in range(2):  # second run: same answer, ensure_model itself skips cached files
        assert vision_detect.main(["fetch-model"]) == 0
    assert calls == ["obico", "clip", "b"] * 2
    assert capsys.readouterr().out.split() == ["/c/obico", "/c/clip", "/c/b"] * 2


def test_frame_scorer_adds_tier_b_findings_and_forwards_new_prints() -> None:
    """The hook in FrameScorer: tier (b) Findings join the frame's findings, p/q are untouched,
    and reset_print reaches the tier."""
    np = _np()
    _cv2()
    import vision_detect
    from printer_ops.vision import Finding

    class Detector:
        def score_bgr(self, bgr: Any) -> FrameScore:
            return FrameScore(0.0, luma=100.0)

    class Tier:
        resets = 0
        seen: list[Any] = []

        def update(self, bgr: Any, ctx: Any, now: float) -> list[Finding]:
            self.seen.append((ctx, now))
            return [Finding("vision_model_spaghetti", WARNING, "x")]

        def reset_print(self) -> None:
            self.resets += 1

    tier = Tier()
    scorer = vision_detect.FrameScorer(Detector(), model=tier)  # type: ignore[arg-type]
    scorer.context, scorer.now = CTX, 123.0
    s = scorer.score_bgr(np.zeros((9, 16, 3), np.uint8))
    assert [f.kind for f in s.findings] == ["vision_model_spaghetti"] and s.p == 0.0 and s.q is None
    assert tier.seen == [(CTX, 123.0)]
    scorer.reset_print()
    assert tier.resets == 1
