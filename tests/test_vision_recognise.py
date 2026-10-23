"""threed_vision.recognise with fake encoders: calibration math, class vocabularies, bank re-ranking inside the proposed
classes, output schema (numpy + OpenCV only, CI-runnable; the real-weights smoke test is test_vision_recognise_models.py)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("cv2")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "threed-vision" / "src"))

from threed_vision import bank, recognise  # noqa: E402
from threed_vision.errors import BadInput  # noqa: E402
from threed_vision.recognise import Category, Recogniser  # noqa: E402
from vision_fakes import NAMES, FakeClip, FakeDino, patch_encoders, scene  # noqa: E402

# --- calibration math ---------------------------------------------------------------------------------------------


def test_calibrate_subtracts_the_per_class_mean_over_the_rows() -> None:
    sim = np.arange(30, dtype=float).reshape(10, 3) * np.array([1.0, 2.0, 3.0])
    cal, done = recognise.calibrate_scores(sim)
    assert done
    np.testing.assert_allclose(cal.mean(axis=0), 0.0, atol=1e-12)
    np.testing.assert_allclose(cal, sim - sim.mean(axis=0))


def test_calibrate_needs_enough_rows_unless_a_prior_is_given() -> None:
    sim = np.array([[3.0, 1.0], [2.0, 5.0]])
    raw, done = recognise.calibrate_scores(sim)
    assert not done and raw is sim
    cal, done = recognise.calibrate_scores(sim, prior=np.array([1.0, 2.0]))
    assert done
    np.testing.assert_allclose(cal, [[2.0, -1.0], [1.0, 3.0]])


def test_calibrate_rejects_a_prior_of_the_wrong_length() -> None:
    with pytest.raises(BadInput, match="one value per category"):
        recognise.calibrate_scores(np.zeros((9, 3)), prior=np.zeros(2))


def test_calibrate_uses_only_the_given_rows_for_the_mean() -> None:
    sim = np.vstack([np.full((8, 2), 10.0), np.full((4, 2), 50.0)])
    cal, done = recognise.calibrate_scores(sim, rows=np.arange(8))
    assert done
    np.testing.assert_allclose(cal[:8], 0.0)
    np.testing.assert_allclose(cal[8:], 40.0)
    _, done = recognise.calibrate_scores(sim, rows=np.arange(3))
    assert not done, "a pool of fewer than 8 rows is not a population"


def _masks_of_areas(
    areas: list[int], shape: tuple[int, int] = (100, 100)
) -> list[tuple[int, Any]]:
    items = []
    for k, a in enumerate(areas):
        m = np.zeros(shape, bool)
        m.flat[:a] = True
        items.append((k + 1, m))
    return items


def test_calibration_pool_is_the_largest_masks_above_the_area_floor() -> None:
    items = _masks_of_areas([900, 50, 800, 700, 5, 600, 500, 400, 300, 200, 100])
    rows = recognise.calibration_rows(
        items, size=8, min_area_frac=0.01
    )  # floor: 100 px of 10 000
    assert rows is not None and list(rows) == [0, 2, 3, 5, 6, 7, 8, 9], (
        "largest first, capped at size"
    )
    assert recognise.calibration_rows(items, size=4, min_area_frac=0.01) is None, (
        "a pool under 8 is no population"
    )
    rows = recognise.calibration_rows(items, size=60, min_area_frac=0.01)
    assert rows is not None and sorted(rows) == [0, 2, 3, 5, 6, 7, 8, 9, 10]
    assert recognise.calibration_rows(items, size=0, min_area_frac=0.01) is None, (
        "size 0 = every instance"
    )
    assert recognise.calibration_rows(items, size=60, min_area_frac=0.05) is None, (
        "too few big masks: use all rows"
    )
    assert recognise.calibration_rows([], size=60, min_area_frac=0.01) is None


def test_small_fragments_do_not_shift_the_class_means(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_encoders(monkeypatch, recognise, bias=2.0)
    rgb, instances = scene()
    red_patch = np.full((8, 8, 3), (220, 30, 30), np.uint8)
    fragments = []
    # 12 tiny red crops (64 px, below the area floor) would drag the 'red' class mean up if they took part
    for k in range(12):
        y, x = 10 + 10 * (k % 6), 440 + 10 * (k // 6)
        rgb[y : y + 8, x : x + 8] = red_patch
        m = np.zeros(rgb.shape[:2], bool)
        m[y : y + 8, x : x + 8] = True
        fragments.append({"id": 100 + k, "mask": m})
    rec = Recogniser("cpu")
    alone = rec.classify(rgb, instances, NAMES, topk=8)
    pooled = rec.classify(rgb, instances + fragments, NAMES, topk=8)
    everything = rec.classify(rgb, instances + fragments, NAMES, topk=8, pool_size=0)
    assert [r["text_topk"] for r in pooled[:8]] == [r["text_topk"] for r in alone]
    assert [r["text_topk"] for r in everything[:8]] != [r["text_topk"] for r in alone]
    assert len(pooled) == 20 and all(r["calibrated"] for r in pooled)


# --- classification with a biased encoder -------------------------------------------------------------------------


def _truth() -> list[str]:
    return list(NAMES)


def test_calibration_removes_the_class_bias_that_wins_raw_scores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_encoders(monkeypatch, recognise, bias=2.0)
    rgb, instances = scene()
    rec = Recogniser("cpu")
    raw = rec.classify(rgb, instances, NAMES, calibrate=False, topk=1)
    cal = rec.classify(rgb, instances, NAMES, calibrate=True, topk=1)
    raw_top = [r["text_topk"][0]["label"] for r in raw]
    cal_top = [r["text_topk"][0]["label"] for r in cal]
    assert raw_top.count("red") == 8, (
        "the bias pulls every crop to class 0 without calibration"
    )
    assert cal_top == _truth()
    assert not raw[0]["calibrated"] and cal[0]["calibrated"]


def test_record_schema_and_topk(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_encoders(monkeypatch, recognise)
    rgb, instances = scene()
    res = Recogniser("cpu").classify(rgb, instances, NAMES, topk=3)
    assert [r["id"] for r in res] == list(range(1, 9))
    first = res[0]
    assert set(first) == {"id", "text_topk", "bank_topk", "calibrated"}
    assert len(first["text_topk"]) == 3
    assert set(first["text_topk"][0]) == {"label", "score", "raw"}
    scores = [t["score"] for t in first["text_topk"]]
    assert scores == sorted(scores, reverse=True)
    assert first["bank_topk"] == []
    json.dumps(first)  # plain JSON types


def test_fewer_than_eight_masks_are_not_calibrated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_encoders(monkeypatch, recognise)
    rgb, instances = scene()
    res = Recogniser("cpu").classify(rgb, instances[:5], NAMES)
    assert all(not r["calibrated"] for r in res)
    assert all(r["text_topk"][0]["score"] == r["text_topk"][0]["raw"] for r in res)


def test_explicit_prior_calibrates_a_single_mask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_encoders(monkeypatch, recognise, bias=2.0)
    rgb, instances = scene()
    prior = np.zeros(len(NAMES))
    prior[0] = 90.0  # similarities are x100, like the scores
    res = Recogniser("cpu").classify(rgb, instances[3:4], NAMES, prior=prior, topk=1)
    assert res[0]["calibrated"] and res[0]["text_topk"][0]["label"] == "yellow"


def test_bare_masks_get_sequential_ids_and_bad_masks_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_encoders(monkeypatch, recognise)
    rgb, instances = scene()
    res = Recogniser("cpu").classify(
        rgb, [i["mask"] for i in instances[:2]], NAMES, topk=1
    )
    assert [r["id"] for r in res] == [1, 2]
    rec = Recogniser("cpu")
    with pytest.raises(BadInput, match="unique"):
        rec.classify(
            rgb,
            [
                {"id": 4, "mask": instances[0]["mask"]},
                {"id": 4, "mask": instances[1]["mask"]},
            ],
            NAMES,
        )
    with pytest.raises(BadInput, match="non-empty"):
        rec.classify(rgb, [np.zeros(rgb.shape[:2], bool)], NAMES)
    with pytest.raises(BadInput, match="need a 'mask'"):
        rec.classify(rgb, [{"id": 1}], NAMES)
    with pytest.raises(BadInput, match="HxWx3"):
        rec.classify(rgb[:, :, 0], instances, NAMES)
    assert rec.classify(rgb, [], NAMES) == []


def test_text_embeddings_are_cached_per_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clip, _ = patch_encoders(monkeypatch, recognise)
    rgb, instances = scene()
    rec = Recogniser("cpu")
    rec.classify(rgb, instances, NAMES)
    rec.classify(rgb, instances, NAMES)
    assert len(clip.text_calls) == 1
    rec.classify(rgb, instances, NAMES[:4])
    assert len(clip.text_calls) == 2


def test_unload_frees_only_the_encoders_the_recogniser_created() -> None:
    mine, theirs = FakeClip(), FakeDino()
    shared = Recogniser("cpu", clip=mine, dino=theirs)  # type: ignore[arg-type]
    mine.load()
    theirs.load()
    assert shared.loaded
    shared.unload()
    assert mine.loaded and theirs.loaded, "injected encoders stay with their owner"


def test_unload_frees_the_encoders_it_created(monkeypatch: pytest.MonkeyPatch) -> None:
    clip, dino = patch_encoders(monkeypatch, recognise, bank)
    rgb, instances = scene()
    rec = Recogniser("cpu")
    rec.classify(
        rgb, instances, NAMES, bank=_bank([("r", "red", 0)]), metric="clip+dino"
    )
    assert rec.loaded and clip.loaded and dino.loaded
    rec.unload()
    assert not clip.loaded and not dino.loaded and not rec.loaded


# --- category vocabularies ------------------------------------------------------------------------------------------


def test_default_vocabulary_is_the_spikes_22_categories() -> None:
    cats = recognise.parse_categories(None)
    assert (
        len(cats) == 22
        and cats[0].label == "screwdriver"
        and cats[-1].label == "power supply"
    )
    assert all(c.prompts == (c.label,) for c in cats)


def test_categories_accept_strings_objects_and_tool_class_tables() -> None:
    cats = recognise.parse_categories(
        [
            "hammer",
            {"id": "tape-roll", "prompts": ["tape", "duct tape"]},
            {
                "id": "hex-key",
                "name": {"ru": "Шестигранник", "en": "Hex key"},
                "synonyms": {"ru": ["имбусовый ключ"], "en": ["allen key", "Hex key"]},
            },
            {"id": "ruler"},
            Category("saw", ("hand saw",)),
        ]
    )
    assert [c.label for c in cats] == ["hammer", "tape-roll", "hex-key", "ruler", "saw"]
    assert cats[1].prompts == ("tape", "duct tape")
    assert cats[2].prompts == ("Hex key", "allen key"), (
        "en name + en synonyms, deduplicated, no Russian"
    )
    assert cats[3].prompts == ("ruler",)


@pytest.mark.parametrize(
    ("specs", "message"),
    [
        (["a", "a"], "duplicate"),
        ([{"prompts": ["x"]}], "without 'id'"),
        ([""], "empty category"),
        ([42], "string or an object"),
    ],
)
def test_bad_categories_are_bad_input(specs: list[Any], message: str) -> None:
    with pytest.raises(BadInput, match=message):
        recognise.parse_categories(specs)


def test_load_categories_from_comma_list_text_file_and_json(tmp_path: Path) -> None:
    assert [c.label for c in recognise.load_categories("screwdriver, pliers ,")] == [
        "screwdriver",
        "pliers",
    ]
    txt = tmp_path / "cats.txt"
    txt.write_text(
        "# comment\nscrewdriver\ntape-roll: tape, duct tape\n\n", encoding="utf-8"
    )
    loaded = recognise.load_categories(txt)
    assert [c.label for c in loaded] == ["screwdriver", "tape-roll"]
    assert loaded[1].prompts == ("tape-roll", "tape", "duct tape")
    lst = tmp_path / "list.json"
    lst.write_text(json.dumps(["a", {"id": "b", "prompts": ["bee"]}]), encoding="utf-8")
    assert [c.prompts for c in recognise.load_categories(lst)] == [("a",), ("bee",)]
    wrapped = tmp_path / "index.json"
    wrapped.write_text(
        json.dumps(
            {
                "toolClasses": [
                    {
                        "id": "drill",
                        "name": {"en": "Drill"},
                        "synonyms": {"en": ["cordless drill"]},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert recognise.load_categories(wrapped)[0].prompts == ("Drill", "cordless drill")
    assert [
        c.label for c in recognise.load_categories(None)
    ] == recognise.DEFAULT_CATEGORIES


def test_load_categories_errors(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2", encoding="utf-8")
    with pytest.raises(BadInput, match="not valid JSON"):
        recognise.load_categories(bad)
    obj = tmp_path / "obj.json"
    obj.write_text('{"x": 1}', encoding="utf-8")
    with pytest.raises(BadInput, match="expected a JSON list"):
        recognise.load_categories(obj)
    empty = tmp_path / "empty.txt"
    empty.write_text("# nothing\n", encoding="utf-8")
    with pytest.raises(BadInput, match="no categories"):
        recognise.load_categories(empty)
    with pytest.raises(BadInput, match="categories file not found"):
        recognise.load_categories(str(tmp_path / "missing.json"))
    with pytest.raises(BadInput, match="empty"):
        recognise.load_categories(" , ")


def test_every_prompt_of_a_category_feeds_its_text_embedding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clip, _ = patch_encoders(monkeypatch, recognise)
    rgb, instances = scene()
    cats = [
        {
            "id": "warm",
            "prompts": ["red", "yellow"],
        },  # synonyms of one class: the class embedding is their mean
        {"id": "cold", "prompts": ["blue"]},
    ]
    Recogniser("cpu").classify(rgb, instances[:1], cats, calibrate=False)
    prompts = clip.text_calls[0]
    assert len(prompts) == (2 + 1) * len(recognise.TEMPLATES)
    assert any("yellow" in p for p in prompts) and any("blue" in p for p in prompts)
    res = Recogniser("cpu").classify(
        rgb, [instances[3]], cats, calibrate=False, topk=2
    )  # the yellow rectangle
    assert res[0]["text_topk"][0]["label"] == "warm"


# --- bank retrieval inside the proposed classes ---------------------------------------------------------------------


def _bank(rows: list[tuple[str, str, int]]) -> bank.BankIndex:
    """(id, category, colour palette index) -> an index whose entries embed as that colour under the fake encoders."""
    n = len(rows)
    clip_e = np.zeros((n, len(NAMES)), np.float32)
    dino_e = np.zeros((n, len(NAMES) + 1), np.float32)
    for k, (_, _, colour) in enumerate(rows):
        clip_e[k, colour] = 1.0
        dino_e[k, colour] = 1.0
    entries = [
        {
            "id": i,
            "name": i,
            "category": c,
            "thumb": f"{i}.png",
            "textured": True,
            "extents_mm": [100.0, 20.0, 5.0],
        }
        for i, c, _ in rows
    ]
    return bank.BankIndex(
        entries, clip_e, dino_e, np.zeros((n, bank.SIL_SIZE, bank.SIL_SIZE), bool), {}
    )


def test_bank_search_is_restricted_to_the_proposed_classes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_encoders(monkeypatch, recognise, bank)
    rgb, instances = scene()
    # the red rectangle: text proposes red; the BEST bank match by appearance is a green-class entry that must not surface
    index = _bank(
        [
            ("red_a", "red", 0),
            ("red_b", "red", 3),  # a yellow-looking entry of the right class
            ("green_a", "green", 0),  # a perfect appearance match, but of another class
        ]
    )
    rec = Recogniser("cpu")
    res = rec.classify(
        rgb,
        instances,
        NAMES,
        bank=index,
        metric="clip",
        rerank_classes=1,
        topk=1,
        bank_topk=5,
    )
    first = res[0]
    assert first["text_topk"][0]["label"] == "red"
    assert first["bank_restricted_to"] == ["red"]
    ids = [m["bank_id"] for m in first["bank_topk"]]
    assert ids == ["red_a", "red_b"], (
        "ranked by appearance, but only entries of the proposed class"
    )
    assert "green_a" not in ids
    scores = [m["score"] for m in first["bank_topk"]]
    assert scores == sorted(scores, reverse=True)


def test_retrieval_never_changes_the_text_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_encoders(monkeypatch, recognise, bank)
    rgb, instances = scene()
    misleading = _bank(
        [("green_a", "green", 0), ("blue_a", "blue", 0)]
    )  # the red crop looks like these entries
    without = Recogniser("cpu").classify(rgb, instances, NAMES, topk=3)
    with_bank = Recogniser("cpu").classify(
        rgb, instances, NAMES, topk=3, bank=misleading, metric="clip", rerank_classes=1
    )
    assert [r["text_topk"] for r in without] == [r["text_topk"] for r in with_bank]
    assert with_bank[0]["bank_topk"] == [], (
        "a bank without the proposed class returns nothing instead of overriding the text"
    )
    assert with_bank[1]["bank_topk"][0]["bank_id"] == "green_a", (
        "the green crop does find the green entry"
    )


def test_rerank_classes_widens_the_search(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_encoders(monkeypatch, recognise, bank, bias=0.0)
    rgb, instances = scene()
    index = _bank([("red_a", "red", 0), ("green_a", "green", 0)])
    narrow = Recogniser("cpu").classify(
        rgb,
        instances[:1],
        NAMES,
        bank=index,
        metric="clip",
        rerank_classes=1,
        calibrate=False,
    )
    wide = Recogniser("cpu").classify(
        rgb,
        instances[:1],
        NAMES,
        bank=index,
        metric="clip",
        rerank_classes=8,
        calibrate=False,
    )
    assert [m["category"] for m in narrow[0]["bank_topk"]] == ["red"]
    assert {m["category"] for m in wide[0]["bank_topk"]} == {"red", "green"}
    assert len(wide[0]["bank_restricted_to"]) == 8
    with pytest.raises(BadInput, match="at least 1"):
        Recogniser("cpu").classify(rgb, instances[:1], NAMES, rerank_classes=0)


def test_silhouette_metric_reranks_by_shape_inside_the_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_encoders(monkeypatch, recognise, bank)
    rgb, instances = scene()
    mask = instances[0]["mask"]
    tall = np.zeros((bank.SIL_SIZE, bank.SIL_SIZE), bool)
    tall[5:90, 40:56] = True
    blob = np.zeros_like(tall)
    blob[30:60, 30:66] = True
    desc = bank.silhouette_descriptor(mask)
    index = _bank(
        [("wide_red", "red", 0), ("tall_red", "red", 0), ("self_red", "red", 0)]
    )
    index.sil[:] = [blob, tall, desc]
    res = Recogniser("cpu").classify(
        rgb, instances, NAMES, bank=index, metric="sil", rerank_classes=1, bank_topk=3
    )
    assert (
        res[0]["bank_topk"][0]["bank_id"] == "self_red"
        and res[0]["bank_topk"][0]["metric"] == "sil"
    )
