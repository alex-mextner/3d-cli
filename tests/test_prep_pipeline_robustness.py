"""Pipeline failure modes found by the post-merge review of #437: paths, errored steps, stale artifacts, cache keys."""

from __future__ import annotations

from pathlib import Path

import pytest
import trimesh

from test_prep_pipeline import FAST_SKIP, NO_PROOF, config, mushroom, rec, run

BAD_COLOURS = (
    "filaments: [{slot: 1, key: K, name: Black, hex: '#000000'}]\n"
    "default: {key: K}\n"
    "regions: [{name: x, landmark: nowhere, assign: {key: K}}]\n"
)


@pytest.fixture(scope="module")
def mushroom_stl(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return mushroom(tmp_path_factory.mktemp("models") / "mushroom.stl")


def test_a_relative_out_directory_runs_end_to_end(
    mushroom_stl: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # `3d prep cat.stl` defaults to the relative `cat-prep`
    out = run(config(mushroom_stl, Path("rel-out")))
    assert rec(out, "orient").verdict != "FAIL"
    assert (tmp_path / "rel-out" / "prep.json").is_file()
    assert (tmp_path / "rel-out" / "meshes" / "repair.npz").is_file()


def test_proof_report_is_written_when_a_step_errored(
    mushroom_stl: Path, tmp_path: Path
) -> None:
    bad = tmp_path / "colors.yaml"
    bad.write_text(BAD_COLOURS)
    out = run(config(mushroom_stl, tmp_path / "out", colors=bad, skip=FAST_SKIP))
    assert rec(out, "colors").verdict == "FAIL"
    assert rec(out, "proof").verdict == "PASS"
    assert "nowhere" in (tmp_path / "out" / "proof-report.md").read_text()


def test_an_unknown_stability_option_is_that_steps_fail_not_a_crash(
    mushroom_stl: Path, tmp_path: Path
) -> None:
    cfg = config(
        mushroom_stl, tmp_path / "out", stability={"sf_fial": 1.0}, skip=FAST_SKIP
    )
    out = run(cfg)
    stability = rec(out, "stability")
    assert stability.verdict == "FAIL" and "sf_fial" in stability.summary
    assert rec(out, "proof").verdict == "PASS"  # the report is still written


def test_a_gate_set_to_none_in_the_spec_switches_it_off_and_a_typo_is_named(
    mushroom_stl: Path, tmp_path: Path
) -> None:
    from errors import InvalidArgument
    from prep.steps import gate_spec

    assert gate_spec(config(mushroom_stl, tmp_path, gate={"walls": None})).walls is None
    assert (
        gate_spec(config(mushroom_stl, tmp_path, gate={"supports": True})).supports
        is True
    )
    with pytest.raises(InvalidArgument) as exc:
        gate_spec(config(mushroom_stl, tmp_path, gate={"wals": 3}))
    assert "wals" in str(exc.value)


def test_gcode_left_by_an_earlier_run_is_not_judged(
    mushroom_stl: Path, tmp_path: Path
) -> None:
    work = tmp_path / "out"
    work.mkdir()
    (work / "mushroom.gcode").write_text("; stale\n")
    out = run(
        config(mushroom_stl, work, slice=False, skip=frozenset({"project", "proof"}))
    )
    assert rec(out, "gates").verdict == "SKIP"


def test_skipping_repair_still_keys_the_meshes_on_the_model(tmp_path: Path) -> None:
    model = tmp_path / "m.stl"
    trimesh.creation.box((20, 20, 20)).export(model)
    cfg = config(model, tmp_path / "out", skip=NO_PROOF | {"repair"})
    run(cfg)
    trimesh.creation.box((30, 30, 30)).export(
        model
    )  # another model, same out dir and flags
    assert not rec(run(cfg), "orient").cached


def test_colour_spec_filaments_missing_from_the_printer_slots_are_padded_with_stubs() -> (
    None
):
    from prep.contracts import Filament
    from prep.pipeline import complete_filaments

    two = [Filament(2, "W", "White", "#ffffff"), Filament(1, "K", "Black", "#000000")]
    full = complete_filaments(two, 4)
    assert [f.slot for f in full] == [1, 2, 3, 4]
    assert [f.key for f in full[:2]] == ["K", "W"] and {f.key for f in full[2:]} == {""}
