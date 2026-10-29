"""`3d fit-check --open AXIS` (#553): the extraction axis is declared, not assumed to be Z."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import fit_check
import numpy as np
import pytest
import trimesh
from assembly.vec import rotation_taking
from fit_fixtures import FLOOR, box, write_insert, write_manifold, write_plate


def turn(src: Path, dst: Path, direction: tuple[float, float, float]) -> Path:
    """A copy of `src` turned so that +Z becomes `direction` (a rigid rotation, nothing else changes)."""
    rows = rotation_taking((0.0, 0.0, 1.0), direction)
    m = np.eye(4)
    m[:3, :3] = np.array(rows)
    mesh = trimesh.load(src, force="mesh")
    mesh.apply_transform(m)
    mesh.export(dst)
    return dst


def check(insert: Path, socket: Path, **kw: Any) -> dict[str, Any]:
    return fit_check.check_fit(
        fit_check.make_fit(insert=[str(insert)], socket=[str(socket)], gap=0.2, **kw)
    )


AXES = {
    "+Y": (0.0, 1.0, 0.0),
    "-X": (-1.0, 0.0, 0.0),
    "oblique": (0.0, -0.7071067811865476, 0.7071067811865476),
}
LABELS = {"+Y": "+Y", "-X": "-X", "oblique": "[0, -0.7071, 0.7071]"}


@pytest.mark.parametrize("name", AXES)
def test_the_same_pair_turned_onto_an_axis_gives_the_same_verdict(
    tmp_path: Path, name: str
) -> None:
    insert, plate = write_insert(tmp_path / "i.stl"), write_plate(tmp_path / "p.stl")
    ref = check(insert, plate)
    got = check(
        turn(insert, tmp_path / "ti.stl", AXES[name]),
        turn(plate, tmp_path / "tp.stl", AXES[name]),
        open=LABELS[name],
    )
    assert ref["ok"] and got["ok"], got["errors"]
    assert got["gap_mm"] == pytest.approx(ref["gap_mm"], abs=0.005)
    assert got["socket_depth_mm"] == pytest.approx(ref["socket_depth_mm"], abs=0.01)
    assert got["open_frame"]["direction"] == pytest.approx(list(AXES[name]), abs=1e-6)
    assert got["open"] == LABELS[name] and got["open_blocked_mm3"] is None


@pytest.mark.parametrize("name", AXES)
def test_a_roof_over_the_pocket_blocks_the_path_along_the_declared_axis_and_is_located_in_assembly_coordinates(
    tmp_path: Path, name: str
) -> None:
    insert = write_insert(tmp_path / "i.stl", size=(20.0, 10.0, 3.0))
    # a 24 x 14 roof (z 6..7) closes the pocket (z 2..6) except a 12 x 6 opening: the 20 x 10 insert cannot leave
    plate = write_manifold(
        tmp_path / "p.stl",
        box(40, 30, FLOOR + 4.0)
        - box(20.4, 10.4, 4.0, z0=FLOOR)
        + box(24, 14, 1.0, z0=FLOOR + 4.0)
        - box(12, 6, 3.0, z0=FLOOR + 3.5),
    )
    ref = check(insert, plate)
    assert (
        not ref["ok"]
        and ref["open_blocked_mm3"]
        and "not open above" in " ".join(ref["errors"])
    )
    t_insert, t_plate = (
        turn(insert, tmp_path / "ti.stl", AXES[name]),
        turn(plate, tmp_path / "tp.stl", AXES[name]),
    )
    got = check(t_insert, t_plate, open=LABELS[name])
    assert not got["ok"] and got["open_blocked_mm3"] == pytest.approx(
        ref["open_blocked_mm3"], rel=0.02
    )
    assert f"not open {LABELS[name]}" in " ".join(got["errors"])
    centre = np.array(got["open_blocked_at"])
    # the blocking roof sits at z ~ 5.5..6.5 over the pocket centre in the original frame: turned with the meshes
    expected = np.array(rotation_taking((0.0, 0.0, 1.0), AXES[name])) @ np.array(
        [0.0, 0.0, FLOOR + 4.5]
    )
    assert float(np.dot(centre, np.array(AXES[name]))) == pytest.approx(
        float(np.dot(expected, np.array(AXES[name]))), abs=0.6
    )


def test_existing_above_below_and_none_behave_as_before(tmp_path: Path) -> None:
    insert, plate = write_insert(tmp_path / "i.stl"), write_plate(tmp_path / "p.stl")
    for mode in ("above", "below", "none"):
        res = check(insert, plate, open=mode)
        assert res["open"] == mode and res["open_frame"] is None
    assert check(insert, plate, open="below")["open_blocked_mm3"] is not None, (
        "the floor is in the way down"
    )
    assert check(insert, plate, open="none")["ok"]
    assert check(insert, plate, open="+Z")["open"] == "above", "+Z is the old `above`"
    assert check(insert, plate, open="-Z")["open"] == "below"


@pytest.mark.parametrize("bad", ["0,0,0", "nan,0,1", "inf,0,0", "up", "1,2", "[0, 0]"])
def test_a_bad_vector_fails_before_any_mesh_is_read(tmp_path: Path, bad: str) -> None:
    with pytest.raises(fit_check.FitError, match="open must be one of"):
        fit_check.make_fit(
            insert=[str(tmp_path / "does-not-exist.stl")],
            socket=["nor-this.stl"],
            gap=0.2,
            open=bad,
        )


def test_the_inputs_are_never_modified_and_the_report_records_the_frame(
    tmp_path: Path,
) -> None:
    insert = turn(write_insert(tmp_path / "i.stl"), tmp_path / "ti.stl", AXES["+Y"])
    plate = turn(write_plate(tmp_path / "p.stl"), tmp_path / "tp.stl", AXES["+Y"])
    before = (insert.read_bytes(), plate.read_bytes())
    res = check(insert, plate, open="+Y")
    assert (insert.read_bytes(), plate.read_bytes()) == before
    assert res["open_frame"]["axis"] == "+Y" and res["open_frame"]["rotation"]
    assert json.dumps(res)  # JSON-ready


def test_a_vector_in_the_spec_file_is_accepted(tmp_path: Path) -> None:
    write_insert(tmp_path / "i.stl")
    write_plate(tmp_path / "p.stl")
    spec = tmp_path / "fits.yaml"
    spec.write_text(
        "fits:\n  - {insert: i.stl, socket: p.stl, gap: 0.2, open: [0, 0, 2]}\n",
        encoding="utf-8",
    )
    (fit,) = fit_check.load_spec(str(spec))
    assert fit.open == "above", "a vector along +Z is the plain `above`"
