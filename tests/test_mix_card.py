"""The tester's readout card (#416): layout, manifest id, PNG, manifest validation (stdlib only)."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from mix_tester_fixtures import build_tester
from PIL import Image

from slicing import mix_card
from slicing.mix_tester import validate_manifest

MID = "0123456789ab"


def test_layout_is_deterministic_and_fits_the_card() -> None:
    lay = mix_card.layout(9, 6.0, MID)
    assert lay == mix_card.layout(9, 6.0, MID)
    w, h = lay["card_mm"]
    assert (w, h) == (102.0, 136.0)
    boxes = (
        [c["face_box"] for c in lay["cells"]]
        + lay["white_boxes"]
        + [lay["qr"]["box_mm"]]
    )
    for x0, y0, x1, y1 in boxes:
        assert 0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h
    for c in lay["cells"]:
        f, p = c["face_box"], c["patch_box"]
        assert f[2] - f[0] == 20.0 and f[3] - f[1] == 6.0
        assert f[0] < p[0] < p[2] < f[2] and f[1] < p[1] < p[3] < f[3]
        assert p[0] - f[0] == mix_card.PATCH_INSET


def test_no_two_things_on_the_card_overlap() -> None:
    lay = mix_card.layout(11, 6.0, MID)
    boxes: list[tuple[str, list[float]]] = [("qr", lay["qr"]["box_mm"])]
    boxes += [(f"white{i}", b) for i, b in enumerate(lay["white_boxes"])]
    boxes += [
        (
            f"fid{f['id']}",
            [
                f["center_mm"][0] - 6,
                f["center_mm"][1] - 6,
                f["center_mm"][0] + 6,
                f["center_mm"][1] + 6,
            ],
        )
        for f in lay["fiducials"]
    ]
    o = mix_card.FRAME_OUT
    boxes += [
        (
            f"cell{c['number']}",
            [
                c["face_box"][0] - o,
                c["face_box"][1] - o,
                c["face_box"][2] + o,
                c["face_box"][3] + o,
            ],
        )
        for c in lay["cells"]
    ]
    for i, (na, a) in enumerate(boxes):
        for nb, b in boxes[i + 1 :]:
            apart = a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]
            assert apart, (na, nb, a, b)


@pytest.mark.parametrize("count", [1, 2, 3, 4, 9, 29])
def test_any_swatch_count_gets_a_card_with_its_cells(count: int) -> None:
    lay = mix_card.layout(count, 6.0, MID)
    assert [c["number"] for c in lay["cells"]] == list(range(1, count + 1))
    assert mix_card.validate_readout(lay, count, MID) == []


def test_the_shortest_swatch_still_gets_a_patch() -> None:
    """--height 3 is the tester's lower bound: its patch is half the face, not zero."""
    lay = mix_card.layout(3, 3.0, MID)
    p, f = lay["cells"][0]["patch_box"], lay["cells"][0]["face_box"]
    assert p[3] - p[1] == 1.5 and lay["patch_inset_mm"] == 0.75
    assert f[1] < p[1] < p[3] < f[3]
    assert mix_card.validate_readout(lay, 3, MID) == []


def test_layout_refuses_what_cannot_be_a_card() -> None:
    with pytest.raises(mix_card.MixCardError, match="outside 1..100"):
        mix_card.layout(3, 0.5, MID)
    with pytest.raises(mix_card.MixCardError, match="at least one"):
        mix_card.layout(0, 6.0, MID)


def test_a_tester_of_the_lowest_height_builds_its_card(tmp_path: Path) -> None:
    m = build_tester(tmp_path, height=3.0)
    assert validate_manifest(m) == [] and m["readout"]["patch_inset_mm"] == 0.75


def test_the_png_is_the_card_at_10_px_per_mm_and_prints_at_one_to_one(
    tmp_path: Path,
) -> None:
    lay = mix_card.layout(4, 6.0, MID)
    path = tmp_path / "card.png"
    path.write_bytes(mix_card.render(lay))
    assert mix_card.render(lay) == path.read_bytes()
    im = Image.open(path)
    assert im.size == (round(lay["card_mm"][0] * 10), round(lay["card_mm"][1] * 10))
    assert im.info["dpi"][0] == pytest.approx(254.0, abs=0.01)  # 10 px/mm
    px = im.convert("RGB")
    h_mm = lay["card_mm"][1]
    for b in lay["white_boxes"] + [
        c["face_box"] for c in lay["cells"]
    ]:  # paper and frames are blank
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        assert px.getpixel((round(cx * 10), round((h_mm - cy) * 10))) == (255, 255, 255)
    f = lay["fiducials"][0]
    cx, cy = f["center_mm"]
    assert px.getpixel((round((cx - 5.5) * 10), round((h_mm - cy) * 10))) == (
        0,
        0,
        0,
    )  # marker border


def test_the_manifest_id_is_its_content_and_changes_with_it() -> None:
    m = {
        "schema": "s",
        "target": "#808080",
        "candidates": [],
        "references": [],
        "files": {"a": "b"},
    }
    mid = mix_card.manifest_id(m)
    assert len(mid) == 12 and mid == mix_card.manifest_id(
        {**m, "files": {"x": "y"}, "id": "zzz", "readout": {}}
    )
    assert mid != mix_card.manifest_id({**m, "target": "#808081"})


def test_a_written_tester_manifest_is_valid_and_its_checks_have_teeth(
    tmp_path: Path,
) -> None:
    m = build_tester(tmp_path)
    assert validate_manifest(m) == []
    assert (
        m["files"]["card"] == "tester.card.png"
        and (tmp_path / "tester.card.png").is_file()
    )
    assert m["id"] == mix_card.manifest_id(m)
    for edit, fragment in (
        (lambda x: x.update(target="#101010"), "id is not the id"),
        (lambda x: x["readout"]["cells"].pop(), "readout.cells must list"),
        (lambda x: x["readout"]["qr"].update(payload="3dmix:other"), "readout.qr"),
        (lambda x: x["readout"].update(white_boxes=[]), "white_boxes"),
        (
            lambda x: x["readout"].update(fiducials=x["readout"]["fiducials"][:2]),
            "at least 3",
        ),
        (lambda x: x["readout"].update(schema="nope"), "readout.schema"),
        (
            lambda x: x["readout"]["white_boxes"].append([500.0, 500.0, 510.0, 510.0]),
            "white_boxes lie outside",
        ),
        (
            lambda x: x["readout"]["qr"].update(box_mm=[-5.0, 0.0, 20.0, 20.0]),
            "qr lies outside",
        ),
        (
            lambda x: x["readout"]["cells"][0].update(patch_box=[0.0, 0.0, 5.0, 5.0]),
            "patch must lie inside the face",
        ),
        (lambda x: x["readout"]["fiducials"][0].update(id=9), "id (0-3)"),
        (
            lambda x: x["readout"]["fiducials"][0].update(center_mm=[1.0, "x"]),
            "id (0-3)",
        ),
        (lambda x: x["readout"].update(card_mm=[True, 5]), "readout.card_mm"),
    ):
        bad = copy.deepcopy(m)
        edit(bad)
        assert any(fragment in e for e in validate_manifest(bad)), fragment
