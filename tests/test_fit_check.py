"""Unit tests for lib/fit_check.py: printed-size lock and socket clearance of an insert."""
from __future__ import annotations

import json
from pathlib import Path

import fit_check
import pytest
from fit_fixtures import box, write_insert, write_manifold, write_plate

PRINTED = "20,10,5"


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    rc = fit_check.main(list(argv))
    out = capsys.readouterr()
    return rc, out.out, out.err


def _json(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict]:
    rc, out, _ = _run(capsys, *argv, "--json")
    return rc, json.loads(out)


def test_box_in_matching_pocket_passes_with_gap_over_depth(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ins, plate = write_insert(tmp_path / "ins.stl"), write_plate(tmp_path / "plate.stl")
    rc, rep = _json(capsys, "--insert", str(ins), "--socket", str(plate), "--gap", "0.2", "--printed-size", PRINTED)
    assert rc == 0
    fit = rep["fits"][0]
    assert rep["status"] == "PASS" and fit["status"] == "PASS" and fit["errors"] == []
    assert fit["insert_size_mm"] == pytest.approx([20, 10, 5], abs=1e-3)
    assert fit["printed_size_mm"] == [20, 10, 5]
    assert fit["gap_set_mm"] == 0.2
    assert fit["gap_mm"] == pytest.approx(0.2, abs=0.002) and fit["gap_max_mm"] == pytest.approx(0.2, abs=0.002)
    # layers every 0.25 mm from 0.05 up; the socket top itself is found by bisection
    assert fit["socket_depth_mm"] == pytest.approx(4.0, abs=0.01)
    assert fit["overlap_volume_mm3"] == pytest.approx(0, abs=1e-3)
    assert len(fit["layers"]) == 16 and fit["layers"][0]["z"] == 2.05


def test_narrow_pocket_fails_on_gap(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ins, plate = write_insert(tmp_path / "ins.stl"), write_plate(tmp_path / "plate.stl", pocket=(20.2, 10.4))
    rc, out, _ = _run(capsys, "--insert", str(ins), "--socket", str(plate), "--gap", "0.2", "--printed-size", PRINTED)
    assert rc == 1
    assert "FAIL socket gap off 0.2±0.03 mm at 16 of 16 layers: z 2.05: 0.1" in out
    assert "gap 0.1–0.1 mm" in out
    assert ">>> FIT-CHECK: FAIL (1 fit)" in out


def test_resized_insert_fails_on_printed_size(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ins = write_insert(tmp_path / "ins.stl", size=(20.1, 10.0, 5.0))
    plate = write_plate(tmp_path / "plate.stl")
    rc, rep = _json(capsys, "--insert", str(ins), "--socket", str(plate), "--gap", "0.2", "--printed-size", PRINTED)
    assert rc == 1
    errors = rep["fits"][0]["errors"]
    assert errors[0] == "size 20.1 x 10 x 5 mm differs from the printed 20 x 10 x 5 mm by more than ±0.02"


def test_overlap_names_the_z(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ins, plate = write_insert(tmp_path / "ins.stl"), write_plate(tmp_path / "plate.stl", pocket=(19.8, 10.4))
    rc, rep = _json(capsys, "--insert", str(ins), "--socket", str(plate), "--gap", "0.2")
    assert rc == 1
    fit = rep["fits"][0]
    assert fit["overlap_z_mm"][0] == 2.05 and len(fit["overlap_z_mm"]) == 16
    assert fit["errors"] == ["overlaps the socket at z 2.05, 2.30, 2.55, 2.80, 3.05, … (16 layers, up to 2 mm²)"]
    assert fit["gap_mm"] == 0


def test_lip_above_an_opening_is_still_checked(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """a rebuilt plate grows a lip over the pocket, past an opening in the wall: the insert would hit it"""
    ins, plate = write_insert(tmp_path / "ins.stl"), write_plate(tmp_path / "plate.stl", depth=2.0)
    write_manifold(tmp_path / "lip.stl", box(40.0, 30.0, 0.5, z0=5.0) - box(19.0, 10.4, 2.0, z0=4.5))
    rc, rep = _json(capsys, "--insert", str(ins), "--socket", str(plate), "--socket", str(tmp_path / "lip.stl"),
                    "--gap", "0.2")
    assert rc == 1
    fit = rep["fits"][0]
    assert [g["z"] for g in fit["layers"]][-2:] == [5.05, 5.3]
    assert fit["socket_depth_mm"] == pytest.approx(3.5, abs=0.01)
    assert fit["errors"] == ["overlaps the socket at z 5.05, 5.30 (2 layers, up to 10 mm²)"]


def test_loose_upper_pocket_past_an_opening_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """the socket has a second ring higher up, rebuilt too wide: every socket layer obeys the gap"""
    ins, plate = write_insert(tmp_path / "ins.stl"), write_plate(tmp_path / "plate.stl", depth=2.0)
    write_manifold(tmp_path / "ring.stl", box(40.0, 30.0, 0.5, z0=5.0) - box(21.0, 11.0, 2.0, z0=4.5))
    rc, rep = _json(capsys, "--insert", str(ins), "--socket", f"{plate},{tmp_path / 'ring.stl'}", "--gap", "0.2")
    assert rc == 1
    assert rep["fits"][0]["errors"] == ["socket gap off 0.2±0.03 mm at 2 of 10 layers: z 5.05: 0.5, z 5.30: 0.5"]


def test_shelf_thinner_than_the_step_is_caught_between_layers(tmp_path: Path,
                                                               capsys: pytest.CaptureFixture[str]) -> None:
    """a 0.1 mm shelf at z 4.10-4.20 falls between the layers at 4.05 and 4.30"""
    ins, plate = write_insert(tmp_path / "ins.stl"), write_plate(tmp_path / "plate.stl")
    write_manifold(tmp_path / "shelf.stl", box(40.0, 30.0, 0.1, z0=4.1) - box(19.0, 10.4, 1.0, z0=3.8))
    rc, rep = _json(capsys, "--insert", str(ins), "--socket", f"{plate},{tmp_path / 'shelf.stl'}", "--gap", "0.2")
    assert rc == 1
    fit = rep["fits"][0]
    assert fit["overlap_z_mm"] == [] and fit["overlap_volume_mm3"] == pytest.approx(1.0, abs=1e-3)
    assert fit["errors"] == [("overlaps the socket between the sampled layers, z 4.10–4.20 (1 mm³); "
                             "a smaller --step shows the layers")]


def test_min_depth_fails_a_shallow_socket(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ins, plate = write_insert(tmp_path / "ins.stl"), write_plate(tmp_path / "plate.stl", depth=2.0)
    base = ["--insert", str(ins), "--socket", str(plate), "--gap", "0.2"]
    assert _run(capsys, *base, "--min-depth", "1.9")[0] == 0
    rc, rep = _json(capsys, *base, "--min-depth", "3")
    assert rc == 1
    assert rep["fits"][0]["min_depth_mm"] == 3
    assert rep["fits"][0]["errors"] == ["socket only 2.00 mm deep, less than the required 3 mm"]


def test_insert_thinner_than_the_first_layer_is_still_sliced(tmp_path: Path,
                                                              capsys: pytest.CaptureFixture[str]) -> None:
    ins = write_insert(tmp_path / "ins.stl", size=(20.0, 10.0, 0.04))
    plate = write_plate(tmp_path / "plate.stl")
    rc, rep = _json(capsys, "--insert", str(ins), "--socket", str(plate), "--gap", "0.2")
    assert rc == 0, rep["fits"][0]["errors"]
    assert rep["fits"][0]["layers"] == [{"z": 2.02, "gap": 0.2}]


def test_tolerances_use_unrounded_measurements(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """20.0204 and a 0.2304 gap round to values inside the tolerance but are outside it"""
    ins = write_insert(tmp_path / "ins.stl", size=(20.0204, 10.0, 5.0))
    plate = write_plate(tmp_path / "plate.stl", pocket=(20.4, 10.4))
    rc, rep = _json(capsys, "--insert", str(ins), "--socket", str(plate), "--gap", "0.2", "--printed-size", PRINTED,
                    "--gap-tol", "0.1")
    assert rc == 1 and rep["fits"][0]["errors"][0].startswith("size 20.02 x 10 x 5 mm differs")
    wide = write_plate(tmp_path / "wide.stl", pocket=(20.4608, 10.4608))
    rc, rep = _json(capsys, "--insert", str(write_insert(tmp_path / "ok.stl")), "--socket", str(wide), "--gap", "0.2")
    assert rc == 1 and rep["fits"][0]["gap_mm"] == 0.23


def test_no_socket_around_the_insert_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ins, plate = write_insert(tmp_path / "ins.stl"), write_plate(tmp_path / "plate.stl", dx=100)
    rc, rep = _json(capsys, "--insert", str(ins), "--socket", str(plate), "--gap", "0.2")
    assert rc == 1
    fit = rep["fits"][0]
    assert fit["errors"][0].startswith("no socket around it")
    assert fit["gap_mm"] is None and fit["socket_depth_mm"] is None


def test_any_turn_accepts_a_turned_insert(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ins, plate = write_insert(tmp_path / "ins.stl"), write_plate(tmp_path / "plate.stl")
    base = ["--insert", str(ins), "--socket", str(plate), "--gap", "0.2", "--printed-size", "10,20,5"]
    assert _run(capsys, *base)[0] == 1
    assert _run(capsys, *base, "--any-turn")[0] == 0


def test_insert_and_socket_parts_are_unioned(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """an insert split into two colour parts and a socket split into two plates"""
    left = write_insert(tmp_path / "ins_l.stl", size=(10.0, 10.0, 5.0), dx=-5.0)
    right = write_insert(tmp_path / "ins_r.stl", size=(10.0, 10.0, 5.0), dx=5.0)
    write_plate(tmp_path / "plate.stl")
    write_manifold(tmp_path / "lid.stl", box(40.0, 30.0, 1.0, z0=6.0) - box(20.4, 10.4, 3.0, z0=5.5))
    rc, rep = _json(capsys, "--insert", f"{left},{right}", "--socket", str(tmp_path / "*.stl").replace("*", "[pl]*"),
                    "--gap", "0.2", "--printed-size", PRINTED)
    fit = rep["fits"][0]
    assert rc == 0, fit["errors"]
    assert fit["insert"] == "ins_l+ins_r" and fit["socket"] == "lid+plate"
    assert fit["insert_size_mm"] == pytest.approx([20, 10, 5], abs=1e-3)
    assert fit["socket_depth_mm"] > 4.0          # the lid deepens the socket to 5 mm


def test_spec_runs_many_fits_with_defaults(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "parts").mkdir()
    write_insert(tmp_path / "parts" / "A.stl")
    write_plate(tmp_path / "parts" / "BASE__a.stl")
    write_insert(tmp_path / "parts" / "B.stl", size=(20.1, 10.0, 5.0))
    spec = tmp_path / "fits.yaml"
    spec.write_text(
        "gap: 0.2\n"
        "fits:\n"
        "  - {name: A, insert: parts/A.stl, socket: 'parts/BASE__*.stl', printed_size: [20, 10, 5]}\n"
        "  - {name: B, insert: [parts/B.stl], socket: parts/BASE__a.stl, printed_size: '20x10x5', gap_tol: 0.1}\n",
        encoding="utf-8")
    rc, rep = _json(capsys, "--spec", str(spec))
    assert rc == 1 and rep["status"] == "FAIL"
    a, b = rep["fits"]
    assert (a["insert"], a["status"], a["socket"]) == ("A", "PASS", "BASE__a")
    assert b["status"] == "FAIL" and b["gap_tol_mm"] == 0.1 and len(b["errors"]) == 1   # size only, gap within 0.1


@pytest.mark.parametrize(("spec", "message"), [
    ("fits: []\n", "needs a non-empty `fits:` list"),
    ("gapp: 0.2\nfits: [{insert: a.stl, socket: a.stl, gap: 0.2}]\n", "unknown top-level key(s) gapp"),
    ("fits: [{insert: a.stl, socket: a.stl}]\n", "fit #1 is missing gap"),
    ("fits: [{insert: a.stl, socket: a.stl, gap: 0.2, size: 1}]\n", "unknown key(s) size"),
    ("fits: [{insert: nope.stl, socket: a.stl, gap: 0.2}]\n", "insert file not found: nope.stl"),
    ("fits: [{insert: a.stl, socket: a.stl, gap: -1}]\n", "gap must be a finite number > 0"),
    ("fits: [{insert: a.stl, socket: a.stl, gap: 0.2, printed_size: [1, 2]}]\n", "needs three numbers"),
    ("fits: [{insert: a.stl, socket: a.stl, gap: 0.2, any_turn: 'yes'}]\n", "any_turn must be true or false"),
    ("fits: [unclosed\n", "is not valid YAML/JSON"),
    ("fits: [{insert: a.stl, socket: a.stl, gap: true}]\n", "gap must be a number, got True"),
    ("step: false\nfits: [{insert: a.stl, socket: a.stl, gap: 0.2}]\n", "step must be a number, got False"),
    ("any_turn: true\nfits: [{insert: a.stl, socket: a.stl, gap: 0.2}]\n", "any_turn needs printed_size"),
])
def test_bad_spec_is_a_usage_error(tmp_path: Path, capsys: pytest.CaptureFixture[str], spec: str, message: str) -> None:
    write_insert(tmp_path / "a.stl")
    (tmp_path / "fits.yaml").write_text(spec, encoding="utf-8")
    rc, _, err = _run(capsys, "--spec", str(tmp_path / "fits.yaml"))
    assert rc == 2
    assert message in err


@pytest.mark.parametrize(("extra", "message"), [
    (["--gap", "0"], "gap must be a finite number > 0"),
    (["--gap", "0.2", "--step", "nan"], "step must be a finite number > 0"),
    (["--gap", "0.2", "--any-turn"], "--any-turn needs --printed-size"),
    (["--gap", "0.2", "--printed-size", "20,10"], "needs three numbers"),
    ([], "missing --gap"),
    (["--gap", "0.2", "--spec", "fits.yaml"], "drop --insert, --socket, --gap"),
])
def test_bad_options_are_usage_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: list[str],
                                      message: str) -> None:
    ins = write_insert(tmp_path / "ins.stl")
    rc, _, err = _run(capsys, "--insert", str(ins), "--socket", str(ins), *extra)
    assert rc == 2
    assert message in err


def test_open_mesh_is_a_load_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import trimesh
    ins = write_insert(tmp_path / "ins.stl")
    box = trimesh.creation.box((5, 5, 5))
    trimesh.Trimesh(box.vertices, box.faces[:-2], process=False).export(tmp_path / "open.stl")
    rc, _, err = _run(capsys, "--insert", str(ins), "--socket", str(tmp_path / "open.stl"), "--gap", "0.2")
    assert rc == 2
    assert "open.stl is not a closed manifold mesh" in err


def test_size_matches_turns() -> None:
    assert fit_check.size_matches((10.0, 20.0, 5.0), (20.0, 10.0, 5.0), 0.02, any_turn=True)
    assert not fit_check.size_matches((10.0, 20.0, 5.0), (20.0, 10.0, 5.0), 0.02, any_turn=False)
    assert fit_check.size_matches((20.02, 10.0, 5.0), (20.0, 10.0, 5.0), 0.02, any_turn=False)
    assert not fit_check.size_matches((20.03, 10.0, 5.0), (20.0, 10.0, 5.0), 0.02, any_turn=False)


def test_wrapper_reads_equals_form_path_flags() -> None:
    from commands.fit_check import _path_values
    assert _path_values(["--insert=a.stl,b.stl", "--socket", "s.stl", "--gap=0.2", "--spec"]) == {
        "--insert": ["a.stl,b.stl"], "--socket": ["s.stl"]}
