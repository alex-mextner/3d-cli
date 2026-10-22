"""Mix recipe model (#116) and the mix tester plate (#413): ranking, files, manifest."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from printops.color import hex_from_linear, linear_rgb, linear_to_lab, srgb_to_lab
from printops.mix_key import canonical
from slicing.head_sequence import read_object_extruders, read_recipes
from slicing.mix_faces import check
from slicing.mix_model import Slot, mix, pick, rank
from slicing.mix_tester import (
    LoadedSlot,
    MixTesterError,
    build,
    digit_rects,
    layer_height,
    read_base,
    validate_manifest,
)

BW = [Slot(1, "#000000"), Slot(4, "#FFFFFF")]
U1 = [
    LoadedSlot(1, "#F6C700", "Snapdragon", 11, 11),
    LoadedSlot(3, "#E8732A", "orange", 13, 13),
    LoadedSlot(4, "#B0284F", "bordeaux", 12, 12),
]


def base_project(path: Path) -> Path:
    """A minimal U1 project as Snapmaker Orca saves it: 4 physical filaments, a prime tower."""
    cfg = {
        "filament_colour": ["#FFFFFF"] * 4,
        "layer_height": "0.12",
        "enable_prime_tower": "1",
        "prime_tower_width": "35",
        "prime_tower_brim_width": "3",
        "wipe_tower_x": ["165"],
        "wipe_tower_y": ["250"],
        "different_settings_to_system": ["", "", "", "", "", ""],
    }
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Metadata/project_settings.config", json.dumps(cfg))
        zf.writestr(
            "3D/3dmodel.model",
            '<model><metadata name="Application">Snapmaker_Orca-01.10.01.50</metadata></model>',
        )
    return path


def test_linear_helpers_round_trip_and_agree_with_srgb_to_lab() -> None:
    for h in ("#000000", "#FFFFFF", "#B5654F", "#1B5E20", "#F6C700"):
        assert hex_from_linear(linear_rgb(h)) == h
        assert linear_to_lab(linear_rgb(h)) == pytest.approx(srgb_to_lab(h))
    assert hex_from_linear((1.5, -0.2, 0.5)) == "#FF00BC"  # clipped, then encoded


def test_stripes_mix_in_linear_light() -> None:
    assert mix(["#000000", "#FFFFFF"], [1, 1]) == "#BCBCBC"
    assert mix(["#000000", "#FFFFFF"], [2, 1]) == "#9C9C9C"


def test_black_and_white_grey_ranks_a_short_two_filament_cycle_first() -> None:
    """#808080 is 22 % white in linear light: of the cycles up to 3 layers the 2:1 one is
    closest (the 1:1 cycle shows #BCBCBC, ΔE ~20); it is a short black/white cycle."""
    best = rank("#808080", BW, max_cycle=3)[0]
    assert best.pattern == "141" and best.weights == (2, 1)
    assert best.delta_e < 10


def test_max_cycle_bounds_every_candidate_and_ranking_is_deterministic() -> None:
    slots = [Slot(s.slot, s.hex) for s in U1]
    first = rank("#B5654F", slots, max_cycle=3)
    again = rank("#B5654F", slots, max_cycle=3)
    assert all(c.cycle <= 3 for c in first)
    assert [(c.pattern, c.hex, c.delta_e) for c in first] == [
        (c.pattern, c.hex, c.delta_e) for c in again
    ]
    assert len({c.weights for c in first}) == len(first)
    chosen = pick(first, 5)
    assert chosen[0].role == "best" and len({c.weights for c in chosen}) == len(chosen)


def test_seven_segment_eight_has_seven_strokes_that_do_not_overlap() -> None:
    rects = digit_rects("8")
    assert len(rects) == 7
    for i, a in enumerate(rects):
        for b in rects[i + 1 :]:
            overlap_x = min(a[2], b[2]) - max(a[0], b[0])
            overlap_y = min(a[3], b[3]) - max(a[1], b[1])
            assert overlap_x <= 1e-9 or overlap_y <= 1e-9
    assert len(digit_rects("10")) == 2 + 6


def _tester(tmp_path: Path) -> tuple[dict[str, Any], Path]:
    slots = [Slot(s.slot, s.hex) for s in U1]
    mixes = pick([c for c in rank("#B5654F", slots) if c.cycle > 1], 4)
    settings, app = read_base(base_project(tmp_path / "base.3mf"))
    manifest = build(
        target="#B5654F",
        slots=U1,
        candidates=mixes,
        out=tmp_path / "out" / "terracotta",
        layer=0.12,
        height=6.0,
        base_settings=settings,
        app=app,
        source="base.3mf",
    )
    return manifest, tmp_path / "out" / "terracotta.3mf"


def test_tester_project_has_one_mixed_row_per_candidate_and_passes_mix_check(
    tmp_path: Path,
) -> None:
    manifest, path = _tester(tmp_path)
    cands, refs = manifest["candidates"], manifest["references"]
    with zipfile.ZipFile(path) as zf:
        patterns, raft = read_recipes(zf)
        objects = read_object_extruders(zf)
        cfg = json.loads(zf.read("Metadata/project_settings.config"))
        model = zf.read("3D/3dmodel.model").decode()
    # virtual filaments follow the 4 physical ones, one manual pattern per candidate
    assert patterns == {5 + i: c["pattern"] for i, c in enumerate(cands)}
    assert raft == 0
    assert cfg["mixed_filament_definitions"].split(";")[0].startswith("1,2,")
    assert cfg["filament_colour"] == ["#F6C700", "#FFFFFF", "#E8732A", "#B0284F"]
    assert "Snapmaker_Orca-01.10.01.50" in model
    for c in cands:
        assert objects[f"{c['number']} swatch"] == [c["filament"]]
    for r in refs:
        assert objects[f"{r['number']} swatch"] == [r["slot"]]
    darkest = 4  # bordeaux B0284F has the lowest L*
    assert manifest["label_slot"] == darkest
    assert {
        e for name, ex in objects.items() if name.endswith("label") for e in ex
    } == {darkest}
    assert [r["slot"] for r in refs] == [1, 3, 4]
    report = check(str(path))
    assert report.status == "PASS", report.to_dict()
    # the prime tower clears every swatch and label
    tx, ty = float(cfg["wipe_tower_x"][0]), float(cfg["wipe_tower_y"][0])
    for e in cands + refs:
        for x0, y0, x1, y1 in (e["swatch_box"], e["label_box"]):
            assert x1 < tx - 3 or x0 > tx + 38 or y1 < ty - 3 or y0 > ty + 38


def test_tester_manifest_validates_and_carries_the_colour_calibration_keys(
    tmp_path: Path,
) -> None:
    manifest, path = _tester(tmp_path)
    on_disk = json.loads(path.with_name("terracotta.mix.json").read_text())
    assert on_disk == manifest
    assert validate_manifest(manifest) == []
    fid = {s.slot: s.filament_id for s in U1}
    for c in manifest["candidates"]:
        key = c["calibration"]["mix"]
        assert c["calibration"]["kind"] == "mix_color"
        assert key == canonical(
            "h0.12:" + ".".join(f"f{fid[int(d)]}" for d in c["pattern"])
        )
    assert [r["calibration"] for r in manifest["references"]] == [
        {"kind": "filament_color", "filament_id": 11},
        {"kind": "filament_color", "filament_id": 13},
        {"kind": "filament_color", "filament_id": 12},
    ]
    png = path.with_name("terracotta.png").read_bytes()
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert validate_manifest({**manifest, "schema": "x"}) == [
        "schema must be printops.mix_tester/1"
    ]


def test_tester_refuses_a_pure_candidate_and_a_slot_the_project_lacks(
    tmp_path: Path,
) -> None:
    settings, app = read_base(base_project(tmp_path / "base.3mf"))
    pure = [c for c in rank("#000000", BW) if c.cycle == 1]
    kw: dict[str, Any] = {
        "target": "#808080",
        "out": tmp_path / "x",
        "layer": 0.12,
        "height": 6.0,
        "base_settings": settings,
        "app": app,
        "source": "t",
    }
    with pytest.raises(MixTesterError, match="layer mixes"):
        build(slots=[LoadedSlot(1, "#000000")], candidates=pure[:1], **kw)
    mixes = [
        c
        for c in rank("#808080", [Slot(1, "#000000"), Slot(6, "#FFFFFF")])
        if c.cycle > 1
    ]
    with pytest.raises(MixTesterError, match="slot 6"):
        build(
            slots=[LoadedSlot(1, "#000000"), LoadedSlot(6, "#FFFFFF")],
            candidates=mixes[:2],
            **kw,
        )


@pytest.mark.parametrize("raw", ["nan", "0", "-0.2", "inf", "abc"])
def test_layer_height_rejects_non_positive_and_non_finite_values(raw: str) -> None:
    with pytest.raises(MixTesterError, match="layer_height"):
        layer_height({"layer_height": raw})


def test_layer_height_reads_a_plain_value() -> None:
    assert layer_height({"layer_height": ["0.2"]}) == pytest.approx(0.2)
