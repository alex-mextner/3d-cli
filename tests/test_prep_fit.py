"""Fit table: items-table schema, friction LP against closed forms, lean-stand and pocket verdicts."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest
import trimesh

from errors import InputNotFound, InvalidArgument
from prep.contracts import StepResult
from prep.voxel import voxelize
from prep.fit import (
    Case,
    FitReport,
    HolderSpec,
    Item,
    ItemTable,
    check_fit,
    _collides,
    _profile,
    _rect_mask,
    _top_collision,
    load_items,
    min_friction,
)

SEATS = (-20.0, -19.0, -18.0, -17.0, -16.0)


def _box(
    lo: tuple[float, float, float], hi: tuple[float, float, float]
) -> trimesh.Trimesh:
    box = trimesh.creation.box(extents=np.array(hi) - np.array(lo))
    box.apply_translation((np.array(hi) + np.array(lo)) / 2.0)
    return box


def _stand() -> trimesh.Trimesh:
    """Ledge at z = 13, a 83 mm back block whose front-top edge the slab leans on, a 33 mm front lip at y -32..-28."""
    parts = [
        _box((-40, -60, 0), (40, -5, 13)),
        _box((-40, -5, 0), (40, 30, 83)),
        _box((-40, -32, 0), (40, -28, 33)),
    ]
    return trimesh.boolean.union(parts, engine="manifold")


def _mirrored(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    out = mesh.copy()
    out.vertices = out.vertices * np.array([1.0, -1.0, 1.0])
    out.invert()  # a mirror flips the winding; keep the outward normals right
    return out


def _table(*items: Item) -> ItemTable:
    return ItemTable(items=items, cases=(Case("none", 0, 0, 0), Case("bulky", 2, 4, 6)))


def _spec(**kw: object) -> HolderSpec:
    return HolderSpec(seat_y0_mm=SEATS, ledge_z_mm=13.0, **kw)  # type: ignore[arg-type]


def _scan_min_friction(
    phi_deg: float, height: float, thick: float, s_top: float
) -> float:
    """Independent brute-force answer for one top contact and no lip: scan how the weight splits along the back face."""
    phi = math.radians(phi_deg)
    rc_y = (height / 2) * math.sin(phi) - (thick / 2) * math.cos(phi)
    nt = rc_y / s_top
    a = math.sin(phi) - nt  # floor force component along the face normal (weight = 1)
    m = np.linspace(0.0, 10.0, 2_000_001)  # floor force component along the face
    nb = m * math.cos(phi) + a * math.sin(phi)
    fb = m * math.sin(phi) - a * math.cos(phi)
    with np.errstate(divide="ignore", invalid="ignore"):
        mu = np.maximum(
            np.where(nb > 0, np.abs(fb) / nb, np.inf), np.abs(math.cos(phi) - m) / nt
        )
    return float(mu.min())


# ---------------------------------------------------------------- friction LP


@pytest.mark.parametrize("phi", [12.0, 20.0, 30.0, 40.0])
def test_min_friction_of_a_slab_resting_at_its_centre_line_is_tan_of_the_lean(
    phi: float,
) -> None:
    height, thick = 100.0, 2.0
    # contact where the line through the weight meets the back face: the floor then pushes along the slab
    s_top = height / 2 - (thick / 2) / math.tan(math.radians(phi))
    mu = min_friction(phi, height, thick, [s_top], 0.2)
    assert mu == pytest.approx(math.tan(math.radians(phi)), abs=2e-3)


@pytest.mark.parametrize(("phi", "s_top"), [(15.0, 41.0), (25.0, 90.0), (35.0, 20.0)])
def test_min_friction_matches_a_brute_force_scan(phi: float, s_top: float) -> None:
    assert min_friction(phi, 150.0, 8.0, [s_top], 0.19) == pytest.approx(
        _scan_min_friction(phi, 150.0, 8.0, s_top), abs=2e-3
    )


def test_front_lip_never_needs_more_friction_and_here_needs_less() -> None:
    free = min_friction(20.0, 150.0, 8.0, [60.0], 0.19)
    lipped = min_friction(20.0, 150.0, 8.0, [60.0], 0.19, lip=True)
    assert lipped < free


def test_min_friction_reports_infinity_when_no_friction_can_hold() -> None:
    assert (
        min_friction(3.0, 150.0, 8.0, [149.0], 0.19) == math.inf
    )  # a near-vertical slab pushed by a top contact


# ---------------------------------------------------------------- items table


def _write_table(tmp_path: Path, **overrides: object) -> Path:
    body: dict[str, object] = {
        "cases": {
            "none": {"d_side_mm": 0, "d_height_mm": 0, "d_thick_mm": 0},
            "typical": {"d_side_mm": 1.5, "d_height_mm": 3.0, "d_thick_mm": 1.5},
        },
        "phones": [
            {
                "id": "a",
                "name": "Phone A",
                "height_mm": 150.0,
                "width_mm": 70.0,
                "thick_mm": 8.0,
                "mass_g": 180.0,
            }
        ],
        "non_goals": [
            {
                "id": "pad",
                "name": "Pad",
                "height_mm": 200.0,
                "width_mm": 130.0,
                "thick_mm": 6.0,
            }
        ],
    }
    body.update(overrides)
    body = {
        k: v for k, v in body.items() if v is not None
    }  # an override of None drops the key
    path = tmp_path / "table.json"
    path.write_text(json.dumps(body))
    return path


def test_load_items_reads_the_phones_schema_and_flags_non_goals(tmp_path: Path) -> None:
    table = load_items(_write_table(tmp_path))
    assert [(i.id, i.non_goal) for i in table.items] == [("a", False), ("pad", True)]
    assert table.items[0].mass_g == 180.0 and table.items[1].mass_g is None
    assert table.items[1].name == "Pad (non-goal)"
    assert [c.name for c in table.cases] == ["none", "typical"]
    assert table.cases[1].d_thick_mm == 1.5


def test_load_items_accepts_the_generic_items_alias(tmp_path: Path) -> None:
    listed = [{"id": "a", "height_mm": 150.0, "width_mm": 70.0, "thick_mm": 8.0}]
    table = load_items(_write_table(tmp_path, items=listed, phones=None))
    assert [i.id for i in table.items] == ["a", "pad"]


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "items": [{"id": "x", "height_mm": 1, "width_mm": 1, "thick_mm": 1}]
        },  # both 'items' and 'phones'
        {"phones": []},
        {"cases": {}},
        {
            "phones": [
                {"id": "b", "height_mm": 150.0, "width_mm": "wide", "thick_mm": 8.0}
            ]
        },
        {"phones": [{"id": "b", "height_mm": -1.0, "width_mm": 70.0, "thick_mm": 8.0}]},
    ],
)
def test_load_items_rejects_a_bad_schema(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    with pytest.raises(InvalidArgument):
        load_items(_write_table(tmp_path, **overrides))


def test_load_items_rejects_missing_file_and_bad_json(tmp_path: Path) -> None:
    with pytest.raises(InputNotFound):
        load_items(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(InvalidArgument):
        load_items(bad)


# ---------------------------------------------------------------- lean mode


THIN = Item("thin", "thin slab", 150.0, 60.0, 6.0, 190.0)
THICK = Item("thick", "thick slab", 150.0, 60.0, 14.0, 190.0)


def test_thin_item_rests_on_the_ledge_and_its_friction_matches_the_independent_scan() -> (
    None
):
    report = check_fit(_stand(), _table(THIN), _spec())
    row = next(r for r in report.rows if r.case == "none")
    assert row.verdict == "PASS" and row.front_stop is False
    assert row.z0_mm == pytest.approx(13.0)
    assert row.y0_mm == -16.0  # set against the back-most seat
    # the slab leans on the top edge of the back block at (-5, 83): its back face passes through that edge
    assert row.phi_deg == pytest.approx(
        math.degrees(math.atan2(-5 - row.y0_mm, 83 - 13)), abs=1.0
    )
    assert row.contact_s_mm is not None and len(row.contact_s_mm) == 1
    assert row.mu_req == pytest.approx(
        _scan_min_friction(row.phi_deg or 0.0, 150.0, 6.0, row.contact_s_mm[0]),
        abs=0.01,
    )
    assert row.mu_window is not None and row.mu_req is not None
    assert row.mu_window[0] <= row.mu_req <= row.mu_window[1]


def test_item_thicker_than_the_gap_fails_while_the_thin_one_passes() -> None:
    report = check_fit(_stand(), _table(THIN, THICK), _spec())
    assert report.matrix() == {
        "thin": {"none": "PASS", "bulky": "PASS"},
        "thick": {"none": "FAIL", "bulky": "FAIL"},
    }
    assert report.verdict == "FAIL"
    assert (
        "no resting pose on the ledge"
        in next(r for r in report.rows if r.item_id == "thick").reason
    )
    assert report.counts() == {"PASS": 2, "RISK": 0, "FAIL": 2}


def test_removing_the_lip_lets_the_thick_item_rest() -> None:
    no_lip = trimesh.boolean.union(
        [_box((-40, -60, 0), (40, -5, 13)), _box((-40, -5, 0), (40, 30, 83))],
        engine="manifold",
    )
    row = check_fit(no_lip, _table(THICK), _spec()).rows[0]
    assert row.verdict == "PASS" and row.z0_mm == pytest.approx(
        13.0
    )  # the lip was what blocked it


def test_thresholds_move_the_verdict_between_pass_risk_and_fail() -> None:
    mu = check_fit(_stand(), _table(THIN), _spec()).rows[0].mu_req
    assert mu is not None and 0.05 < mu < 0.25
    risky = check_fit(_stand(), _table(THIN), _spec(pass_mu=mu / 2, risk_mu=0.4)).rows[
        0
    ]
    assert risky.verdict == "RISK" and "friction" in risky.reason
    failing = check_fit(
        _stand(), _table(THIN), _spec(pass_mu=0.0, risk_mu=mu / 2)
    ).rows[0]
    assert (
        failing.verdict == "FAIL"
        and "no pose on the ledge needs less than mu" in failing.reason
    )
    # the lean the slab settles at must be inside the accepted range
    assert (
        check_fit(_stand(), _table(THIN), _spec(lean_range_deg=(12.0, 36.0)))
        .rows[0]
        .reason.startswith("lean")
    )


def test_mirrored_holder_with_front_sign_plus_one_gives_the_same_poses() -> None:
    plain = check_fit(_stand(), _table(THIN), _spec()).rows[0]
    mirrored = check_fit(
        _mirrored(_stand()),
        _table(THIN),
        HolderSpec(seat_y0_mm=tuple(-s for s in SEATS), front_sign=1, ledge_z_mm=13.0),
    ).rows[0]
    assert mirrored.verdict == plain.verdict == "PASS"
    assert mirrored.phi_deg == plain.phi_deg and mirrored.mu_req == plain.mu_req
    assert mirrored.y0_mm == -(plain.y0_mm or 0.0)


def test_rest_lean_does_not_depend_on_the_scan_step() -> None:
    coarse = check_fit(_stand(), _table(THIN), _spec(scan_step_deg=3.0)).rows[0]
    fine = check_fit(_stand(), _table(THIN), _spec(scan_step_deg=0.5)).rows[0]
    assert coarse.phi_deg is not None and fine.phi_deg is not None
    assert coarse.phi_deg == pytest.approx(
        fine.phi_deg, abs=0.3
    )  # bisection finds the touching lean, not the grid one
    assert coarse.mu_req == pytest.approx(fine.mu_req, abs=0.02)


def test_no_ledge_means_no_resting_pose() -> None:
    flat = _box(
        (-40, -60, 0), (40, 30, 13)
    )  # a plain block: the slab has nothing to lean on
    row = check_fit(flat, _table(THIN), _spec()).rows[0]
    assert row.verdict == "FAIL" and "touches nothing above it" in row.reason


def test_non_goal_items_and_optional_cases_do_not_decide_the_verdict() -> None:
    goal = THIN
    non_goal = Item("big", "big (non-goal)", 150.0, 60.0, 14.0, 190.0, non_goal=True)
    report = check_fit(_stand(), _table(goal, non_goal), _spec())
    assert report.matrix()["big"] == {"none": "FAIL", "bulky": "FAIL"}
    assert report.verdict == "PASS"
    # an item that holds bare but not in the bulky case: FAIL when every case is required, WARN otherwise
    mid = Item("mid", "mid slab", 150.0, 60.0, 9.0, 190.0)
    full = check_fit(_stand(), _table(mid), _spec())
    assert full.matrix()["mid"] == {"none": "PASS", "bulky": "FAIL"}
    assert full.verdict == "FAIL"
    assert (
        check_fit(_stand(), _table(mid), _spec(required_cases=("none",))).verdict
        == "WARN"
    )


def test_report_is_a_strict_json_step_result() -> None:
    report = check_fit(_stand(), _table(THIN), _spec())
    assert isinstance(report, StepResult) and isinstance(report, FitReport)
    data = report.to_dict()
    json.dumps(data, allow_nan=False)
    assert data["matrix"] == {"thin": {"none": "PASS", "bulky": "PASS"}}
    assert data["spec"]["lean_range_deg"] == [8.0, 36.0]
    assert "1 items x 2 cases (lean)" in report.summary()


def test_check_is_deterministic() -> None:
    a = check_fit(_stand(), _table(THIN, THICK), _spec()).to_dict()
    b = check_fit(_stand(), _table(THIN, THICK), _spec()).to_dict()
    assert a == b


# ---------------------------------------------------------------- pocket mode

POCKET = (-20.0, -15.0, 10.0, 20.0, 15.0, 40.0)


def _holder(rib: bool = False) -> trimesh.Trimesh:
    body = trimesh.boolean.difference(
        [_box((-30, -20, 0), (30, 20, 40)), _box((-20, -15, 10), (20, 15, 50))],
        engine="manifold",
    )
    if rib:
        body = trimesh.boolean.union(
            [body, _box((-2, -15, 10), (2, 15, 40))], engine="manifold"
        )
    return body


def _pocket_spec(**kw: object) -> HolderSpec:
    return HolderSpec(mode="pocket", pocket=POCKET, **kw)  # type: ignore[arg-type]


def test_pocket_pass_risk_and_fail_depend_on_the_item_size() -> None:
    table = ItemTable(
        items=(
            Item("fits", "fits", 25.0, 30.0, 10.0),
            Item("tight", "tight", 25.0, 39.8, 10.0),
            Item("wide", "wide", 25.0, 41.0, 10.0),
            Item("tall", "tall", 31.0, 30.0, 10.0),
        ),
        cases=(Case("none", 0, 0, 0),),
    )
    report = check_fit(_holder(), table, _pocket_spec())
    assert {i: r["none"] for i, r in report.matrix().items()} == {
        "fits": "PASS",
        "tight": "RISK",
        "wide": "FAIL",
        "tall": "FAIL",
    }
    fits = report.rows[0]
    assert fits.slack_mm == pytest.approx(5.0)  # the tightest axis is Z: 30 - 25
    assert fits.position_mm is not None and fits.position_mm[2] == pytest.approx(
        10.0
    )  # rests on the pocket floor
    assert fits.clearance_mm is not None and fits.clearance_mm > 4.0
    assert (
        "too large for the pocket along X" in report.rows[2].reason
        and "along Z" in report.rows[3].reason
    )
    assert report.verdict == "FAIL"


def test_pocket_case_margins_grow_the_item() -> None:
    table = ItemTable(
        items=(Item("p", "p", 25.0, 36.0, 10.0),),
        cases=(Case("none", 0, 0, 0), Case("bulky", 2.5, 5.0, 3.0)),
    )
    matrix = check_fit(_holder(), table, _pocket_spec()).matrix()
    assert matrix == {"p": {"none": "PASS", "bulky": "FAIL"}}  # 36 + 2 x 2.5 = 41 > 40


def test_pocket_material_inside_the_box_blocks_wide_items_but_not_narrow_ones() -> None:
    table = ItemTable(
        items=(
            Item("wide", "wide", 25.0, 30.0, 10.0),
            Item("narrow", "narrow", 25.0, 15.0, 10.0),
        ),
        cases=(Case("none", 0, 0, 0),),
    )
    report = check_fit(_holder(rib=True), table, _pocket_spec())
    assert report.matrix() == {"wide": {"none": "FAIL"}, "narrow": {"none": "PASS"}}
    assert "blocks every position" in report.rows[0].reason
    narrow_x = report.rows[1].position_mm[0]  # type: ignore[index]
    assert (
        narrow_x + 15.0 <= -2.0 + 1e-6 or narrow_x >= 2.0 - 1e-6
    )  # beside the rib, not through it
    # negative control: the same wide item fits the holder without the rib
    assert (
        check_fit(_holder(), table, _pocket_spec()).matrix()["wide"]["none"] == "PASS"
    )


def test_pocket_item_axes_choose_which_model_axis_the_width_runs_along() -> None:
    table = ItemTable(
        items=(Item("p", "p", 25.0, 34.0, 10.0),), cases=(Case("none", 0, 0, 0),)
    )
    assert (
        check_fit(_holder(), table, _pocket_spec()).matrix()["p"]["none"] == "PASS"
    )  # 34 along X (40)
    assert (
        check_fit(_holder(), table, _pocket_spec(item_axes=(1, 0, 2))).matrix()["p"][
            "none"
        ]
        == "FAIL"
    )  # 34 along Y (30)


def test_invalid_specs_are_rejected() -> None:
    with pytest.raises(InvalidArgument):
        HolderSpec(mode="pocket")
    with pytest.raises(InvalidArgument):
        HolderSpec(mode="pocket", pocket=POCKET, item_axes=(0, 0, 2))
    with pytest.raises(InvalidArgument):
        HolderSpec(lean_range_deg=(30.0, 10.0))
    with pytest.raises(InvalidArgument):
        HolderSpec(pass_mu=0.5, risk_mu=0.2)
    with pytest.raises(InvalidArgument):
        check_fit(_holder(), _table(THIN), _pocket_spec(), pitch=0.0)


# ---------------------------------------------------------------- the fast rest solver against brute force


@pytest.mark.parametrize(
    ("phi_deg", "height", "thick"),
    [(14.0, 147.6, 7.8), (33.3, 80.0, 12.0), (5.0, 30.0, 3.0)],
)
@pytest.mark.parametrize("h", [0.5, 0.25])
def test_rect_mask_covers_exactly_the_cells_whose_centres_are_inside_the_slab(
    phi_deg: float, height: float, thick: float, h: float
) -> None:
    phi = math.radians(phi_deg)
    mask = _rect_mask(phi, height, thick, h)
    u, n = (
        np.array([math.sin(phi), math.cos(phi)]),
        np.array([-math.cos(phi), math.sin(phi)]),
    )
    covered: set[tuple[int, int]] = set()
    for row, lo, hi in zip(mask.row_off, mask.z_lo, mask.z_hi, strict=True):
        covered.update((int(row), int(k)) for k in range(int(lo), int(hi) + 1))
    expected: set[tuple[int, int]] = set()
    for row in range(-int(thick / h) - 3, int(height / h) + 3):
        for k in range(-3, int((height + thick) / h) + 3):
            centre = np.array([(row + 0.5) * h, (k + 0.5) * h])
            if 0.0 <= centre @ u <= height and 0.0 <= centre @ n <= thick:
                expected.add((row, k))
    assert covered == expected


def test_top_collision_and_collides_match_a_cell_by_cell_scan() -> None:
    grid = voxelize(_stand(), 0.5)
    zpad, ncol, ny = (
        400,
        400 + int(math.ceil((grid.origin[2] + grid.shape[2] * 0.5) / 0.5)) + 400,
        280,
    )
    prof = _profile(grid, -30.0, 30.0, -70.0, ny, zpad, ncol, False)
    for phi_deg in (12.0, 20.0, 31.0):
        mask = _rect_mask(math.radians(phi_deg), 150.0, 8.0, 0.5)
        for y0 in (-30.0, -20.0, -10.0, 0.0):
            iy = prof.seat_row(y0)
            rows = iy + mask.row_off
            colliding = [
                kb
                for kb in range(0, 200)
                if any(
                    prof.occ[r, zpad + kb + z_lo : zpad + kb + z_hi + 1].any()
                    for r, z_lo, z_hi in zip(rows, mask.z_lo, mask.z_hi, strict=True)
                    if 0 <= r < ny
                )
            ]
            assert _top_collision(prof, iy, mask) == max(colliding)
            for kb in (
                max(colliding) - 3,
                max(colliding),
                max(colliding) + 1,
                max(colliding) + 7,
            ):
                assert _collides(prof, iy, kb, mask) == (kb in colliding)


def test_seats_beyond_the_rest_seat_are_not_part_of_the_friction_window() -> None:
    row = check_fit(_stand(), _table(THIN), _spec()).rows[0]
    assert row.mu_window is not None and row.mu_req is not None
    assert row.mu_window[0] <= row.mu_req <= row.mu_window[1]
    assert (
        row.y0_mm == -16.0
    )  # the back-most seat already holds, so only that seat was solved
    assert row.mu_window == [row.mu_req, row.mu_req]
