"""The prep flow end to end on small synthetic models: what it decides, what it caches, when it stops."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import trimesh

from errors import InvalidArgument
from prep.pipeline import STEPS, Pipeline, PrepConfig, PrepOutcome, StepRecord, load_mesh_npz

FAST_SKIP = frozenset({"project", "slice", "gates"})
NO_PROOF = FAST_SKIP | {"proof"}


def mushroom(path: Path) -> Path:
    """A 10 mm stem under a 40 mm square cap: the cap's underside is a flat 15 mm overhang on every side."""
    stem = trimesh.creation.box((10, 10, 20))
    stem.apply_translation([0, 0, 10])
    cap = trimesh.creation.box((40, 40, 6))
    cap.apply_translation([0, 0, 23])
    solid = trimesh.boolean.union([stem, cap], engine="manifold")
    solid.export(path)
    return path


def cube_with_defects(path: Path) -> Path:
    """A 20 mm cube with one face deleted (a hole) and a tiny floating debris tetrahedron."""
    cube = trimesh.creation.box((20, 20, 20))
    faces = cube.faces[1:]  # a deleted triangle leaves a hole and two open edges
    debris = trimesh.creation.icosphere(subdivisions=0, radius=0.4)
    debris.apply_translation([30, 0, 0])
    verts = np.vstack([cube.vertices, debris.vertices])
    all_faces = np.vstack([faces, debris.faces + len(cube.vertices)])
    trimesh.Trimesh(verts, all_faces, process=False).export(path)
    return path


def load_mesh_npz_from_stl(path: Path) -> trimesh.Trimesh:
    loaded = trimesh.load(str(path), force="mesh")
    assert isinstance(loaded, trimesh.Trimesh)
    return loaded


def config(model: Path, out: Path, **kw: Any) -> PrepConfig:
    base: dict[str, Any] = {"orient": "keep", "skip": NO_PROOF, "pitch": 0.5}
    base.update(kw)
    return PrepConfig(model=model, out=out, **base)


def rec(out: PrepOutcome, name: str) -> StepRecord:
    record = out.record(name)
    assert record is not None, name
    return record


def run(cfg: PrepConfig) -> PrepOutcome:
    return Pipeline(cfg, log=lambda _m: None).run()


@pytest.fixture(scope="module")
def mushroom_stl(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return mushroom(tmp_path_factory.mktemp("models") / "mushroom.stl")


def test_overhanging_model_is_adapted_and_the_proof_pack_is_written(
    mushroom_stl: Path, tmp_path: Path
) -> None:
    out = run(config(mushroom_stl, tmp_path / "out", skip=FAST_SKIP))
    adapt = out.record("adapt")
    assert adapt is not None and adapt.verdict == "PASS"
    ops = adapt.result["ops"]
    assert (
        ops and ops[0]["added_volume_mm3"] > 100
    )  # wedges under the 40 mm cap were added
    after = out.record("overhang")
    assert (
        after is not None
        and after.verdict == "PASS"
        and after.result["island_count"] == 0
    )
    final = load_mesh_npz(tmp_path / "out" / "meshes" / "adapt.npz")
    original = load_mesh_npz_from_stl(mushroom_stl)
    assert final.is_watertight and final.volume > original.volume
    assert (tmp_path / "out" / "proof-report.md").is_file()
    assert list((tmp_path / "out" / "proof").glob("*.png"))
    data = json.loads((tmp_path / "out" / "prep.json").read_text())
    assert [s["name"] for s in data["steps"]] == list(STEPS)


def test_without_adaptation_the_same_model_is_not_ok(
    mushroom_stl: Path, tmp_path: Path
) -> None:
    out = run(config(mushroom_stl, tmp_path / "out", adapt=False))
    after = out.record("overhang")
    assert (
        after is not None and after.verdict != "PASS"
    )  # the 15 mm cap overhang is still there
    assert rec(out, "adapt").result == {"ops": []}


def test_second_run_is_all_cache_hits_and_a_downstream_change_keeps_the_upstream(
    mushroom_stl: Path, tmp_path: Path
) -> None:
    work = tmp_path / "out"
    run(config(mushroom_stl, work))
    again = run(config(mushroom_stl, work))
    ran = [r for r in again.records if r.verdict != "SKIP"]
    assert ran and all(r.cached for r in ran), [r.name for r in ran if not r.cached]

    changed = run(
        config(mushroom_stl, work, hmax=0.28)
    )  # layer band: only `layers` and what consumes it
    cached = {r.name for r in changed.records if r.cached}
    assert {"diagnose", "repair", "orient", "adapt", "overhang", "stability"} <= cached
    assert "layers" not in cached


def test_force_and_from_recompute(mushroom_stl: Path, tmp_path: Path) -> None:
    work = tmp_path / "out"
    run(config(mushroom_stl, work))
    forced = run(config(mushroom_stl, work, force=True))
    assert not any(r.cached for r in forced.records)
    run(config(mushroom_stl, work))
    redo = run(config(mushroom_stl, work, from_step="adapt"))
    cached = {r.name for r in redo.records if r.cached}
    assert (
        {"diagnose", "repair", "orient"} <= cached
        and "adapt" not in cached
        and "overhang" not in cached
    )


def test_defective_model_is_repaired_before_anything_else(tmp_path: Path) -> None:
    model = cube_with_defects(tmp_path / "bad.stl")
    out = run(config(model, tmp_path / "out"))
    diag, repair = out.record("diagnose"), out.record("repair")
    assert (
        diag is not None
        and diag.verdict == "FAIL"
        and diag.result["watertight"] is False
    )
    assert repair is not None and repair.verdict in ("PASS", "WARN")
    assert repair.result["after"]["watertight"] is True
    final = load_mesh_npz(tmp_path / "out" / "meshes" / "repair.npz")
    assert (
        final.is_watertight and abs(final.volume - 8000) < 1.0
    )  # the cube, without the debris


def test_failed_repair_stops_the_flow(tmp_path: Path) -> None:
    sheet = trimesh.creation.box(
        (20, 20, 0.001)
    )  # two huge faces and nothing between them: not a solid
    sheet.faces = sheet.faces[:2]
    model = tmp_path / "sheet.stl"
    trimesh.Trimesh(sheet.vertices, sheet.faces, process=False).export(model)
    out = run(config(model, tmp_path / "out"))
    repair = out.record("repair")
    assert repair is not None and repair.verdict == "FAIL"
    later = [out.record(n) for n in ("orient", "adapt", "overhang", "stability")]
    assert all(
        r is not None and r.verdict == "SKIP" and "blocked" in r.summary for r in later
    )
    assert out.verdict == "FAIL"


def test_only_runs_the_named_steps_and_what_they_need(
    mushroom_stl: Path, tmp_path: Path
) -> None:
    out = run(
        config(
            mushroom_stl, tmp_path / "out", skip=frozenset(), only=frozenset({"orient"})
        )
    )
    verdicts = {r.name: r.verdict for r in out.records}
    assert verdicts["repair"] != "SKIP" and verdicts["orient"] != "SKIP"
    assert all(
        verdicts[n] == "SKIP" for n in ("adapt", "overhang", "stability", "proof")
    )


def test_missing_tools_are_a_skip_with_the_reason_and_are_never_cached(
    mushroom_stl: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("ORCA", raising=False)
    monkeypatch.delenv("SLICER", raising=False)
    cfg = config(mushroom_stl, tmp_path / "out", skip=frozenset({"proof"}))
    out = run(cfg)
    project = out.record("project")
    assert project is not None
    if (
        project.verdict == "PASS"
    ):  # a machine with a system-wide Orca install: nothing to assert about the gap
        pytest.skip("an Orca install is reachable through the system PATH")
    assert project.verdict == "SKIP" and "not found" in project.summary
    fit, colors = out.record("fit"), out.record("colors")
    assert fit is not None and fit.verdict == "SKIP"
    assert colors is not None and colors.verdict == "SKIP"
    again = run(cfg)
    assert not rec(again, "project").cached


def test_configuration_errors_name_the_accepted_values(
    mushroom_stl: Path, tmp_path: Path
) -> None:
    with pytest.raises(InvalidArgument) as exc:
        Pipeline(config(mushroom_stl, tmp_path / "o", printer="x1"))
    assert "u1" in str(exc.value)
    with pytest.raises(InvalidArgument):
        Pipeline(config(mushroom_stl, tmp_path / "o", orient="sideways"))
    with pytest.raises(InvalidArgument):
        Pipeline(config(mushroom_stl, tmp_path / "o", only=frozenset({"nope"})))
    with pytest.raises(InvalidArgument):
        Pipeline(config(mushroom_stl, tmp_path / "o", angle_deg=95.0))


def test_a_structured_error_in_one_step_is_that_steps_fail_and_the_flow_goes_on(mushroom_stl: Path, tmp_path: Path) -> None:
    bad = tmp_path / "colors.yaml"
    bad.write_text("filaments: [{slot: 1, key: K, name: Black, hex: '#000000'}]\ndefault: {key: K}\nregions: [{name: x, landmark: nowhere, assign: {key: K}}]\n")
    out = run(config(mushroom_stl, tmp_path / "out", colors=bad))
    colors = rec(out, "colors")
    assert colors.verdict == "FAIL" and "nowhere" in colors.summary
    assert rec(out, "stability").verdict != "SKIP"  # steps that do not need the colours still ran
    assert out.verdict == "FAIL"


def test_diagnosed_defects_that_repair_fixed_are_a_warn_in_the_proof_report(tmp_path: Path) -> None:
    model = cube_with_defects(tmp_path / "bad.stl")
    run(config(model, tmp_path / "out", skip=FAST_SKIP))
    report = (tmp_path / "out" / "proof-report.md").read_text()
    assert "repaired by the next step" in report
    assert "**Overall: FAIL**" not in report
