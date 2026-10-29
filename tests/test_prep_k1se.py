"""`3d prep --printer k1se`: flat presets and the placed STL out, the slicer called at the process boundary, real
feedrates judged. The slicer is a fake executable (`k1se_fixtures.fake_slicer`), so Orca is not needed."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import trimesh
from k1se_fixtures import (
    ELEGOO,
    MACHINE,
    PROCESS,
    fake_slicer,
    isolate_presets,
    k1se_gcode,
)

from errors import InvalidArgument
from prep import cli as prep_cli
from prep.pipeline import Pipeline, PrepConfig, PrepOutcome, StepRecord

SKIP = frozenset(
    {"repair", "fit", "stability", "proof"}
)  # repair needs the meshfix build


@pytest.fixture
def slicer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Presets isolated, the heavy lock treated as held, `$ORCA` the fake slicer; returns its argv log."""
    isolate_presets(monkeypatch, tmp_path)
    monkeypatch.setenv("THREED_HEAVY_HELD", "1")
    log = tmp_path / "slicer-argv.jsonl"
    monkeypatch.setenv("FAKE_SLICER_LOG", str(log))
    monkeypatch.setenv("ORCA", str(fake_slicer(tmp_path, k1se_gcode())))
    return log


def box(path: Path, size: tuple[float, float, float] = (40.0, 30.0, 0.6)) -> Path:
    trimesh.creation.box(size).export(path)
    return path


def config(model: Path, out: Path, **kw: Any) -> PrepConfig:
    base: dict[str, Any] = {
        "printer": "k1se",
        "orient": "keep",
        "adapt": False,
        "skip": SKIP,
        "pitch": 0.5,
    }
    base.update(kw)
    return PrepConfig(model=model, out=out, **base)


def run(cfg: PrepConfig) -> PrepOutcome:
    return Pipeline(cfg, log=lambda _m: None).run()


def rec(out: PrepOutcome, name: str) -> StepRecord:
    r = out.record(name)
    assert r is not None, name
    return r


SPEEDS = {
    "outer_wall_speed": "250",
    "inner_wall_speed": "500",
    "sparse_infill_speed": "550",
    "travel_speed": "600",
}


def test_the_k1se_flow_writes_presets_slices_and_judges_the_real_feedrates(
    tmp_path: Path, slicer: Path
) -> None:
    out = run(
        config(
            box(tmp_path / "part.stl"),
            tmp_path / "out",
            filament_preset=ELEGOO,
            walls=3,
            set_process=dict(SPEEDS),
            set_filament={"filament_max_volumetric_speed": "30"},
        )
    )
    project, sliced, gates = rec(out, "project"), rec(out, "slice"), rec(out, "gates")
    assert (project.verdict, sliced.verdict) == ("PASS", "PASS")
    assert (
        rec(out, "layers").verdict == "SKIP"
        and "fixed layer height" in rec(out, "layers").summary
    )

    names = project.result["preset_names"]
    assert names == {"machine": MACHINE, "process": PROCESS, "filament": ELEGOO}
    process = json.loads(
        (tmp_path / "out" / project.result["profiles"]["process"]).read_text()
    )
    assert process["wall_loops"] == "3" and process["outer_wall_speed"] == "250"
    assert process["enable_support"] == "0" and process["brim_type"] == "no_brim"
    filament = json.loads(
        (tmp_path / "out" / project.result["profiles"]["filament"]).read_text()
    )
    assert filament["filament_max_volumetric_speed"] == ["30"] and filament[
        "nozzle_temperature"
    ] == ["235"]

    # the STL is placed: bed centre, on the bed (the box was centred on the origin)
    placed = trimesh.load(str(tmp_path / "out" / project.result["stl"]), force="mesh")
    assert placed.bounds[0].tolist() == pytest.approx([90.0, 95.0, 0.0], abs=1e-3)
    assert placed.bounds[1].tolist() == pytest.approx([130.0, 125.0, 0.6], abs=1e-3)

    # the slicer is called with the three flat files, nothing arranged or reoriented
    argv = json.loads(slicer.read_text().splitlines()[-1])
    settings = argv[argv.index("--load-settings") + 1].split(";")
    assert [Path(p).name for p in settings] == ["machine.json", "process.json"]
    assert Path(argv[argv.index("--load-filaments") + 1]).name == "filament.json"
    assert (
        argv[argv.index("--arrange") + 1] == "0"
        and argv[argv.index("--orient") + 1] == "0"
    )
    assert (tmp_path / "out" / "part.gcode").is_file()

    feed = next(g for g in gates.result["gates"] if g["name"] == "feedrates")
    assert (
        feed["verdict"] == "PASS"
        and "Outer wall 250" in feed["detail"]
        and "travel 600" in feed["detail"]
    )
    assert gates.result["spec"]["speed_limits"]["Outer wall"] == 250.0
    assert gates.result["stats"]["time"] == "32m 51s" and gates.result["stats"][
        "filament_g"
    ] == [13.56]
    assert "32m 51s" in gates.summary and "13.56 g" in gates.summary
    assert out.verdict != "FAIL", out.table()


def test_a_slice_faster_than_the_process_asked_for_fails_the_gates(
    tmp_path: Path, slicer: Path
) -> None:
    out = run(
        config(
            box(tmp_path / "part.stl"),
            tmp_path / "out",
            walls=3,
            set_process={**SPEEDS, "outer_wall_speed": "200"},
        )
    )
    gates = rec(out, "gates")
    assert gates.verdict == "FAIL" and out.verdict == "FAIL"
    assert (
        "Outer wall 250 > 200"
        in next(g for g in gates.result["gates"] if g["name"] == "feedrates")["detail"]
    )


def test_supports_auto_reaches_the_preset_and_the_gate_allows_them(
    tmp_path: Path, slicer: Path
) -> None:
    out = run(
        config(box(tmp_path / "part.stl"), tmp_path / "out", supports="auto", walls=3)
    )
    process = json.loads(
        (
            tmp_path / "out" / rec(out, "project").result["profiles"]["process"]
        ).read_text()
    )
    assert (
        process["enable_support"] == "1" and process["support_threshold_angle"] == "45"
    )
    support_gate = next(
        g for g in rec(out, "gates").result["gates"] if g["name"] == "supports"
    )
    assert (
        support_gate["verdict"] == "PASS"
        and "allowed by spec" in support_gate["detail"]
    )


def test_the_gates_expect_the_walls_the_generated_process_has_after_a_set(
    tmp_path: Path, slicer: Path
) -> None:
    out = run(
        config(
            box(tmp_path / "part.stl"),
            tmp_path / "out",
            walls=4,
            set_process={"wall_loops": "3"},
        )
    )
    project = rec(out, "project").result
    assert project["walls"] == 3
    assert project["infill"] == 0.15
    assert project["supports_enabled"] is False


def test_a_list_for_wall_loops_fails_the_project_step_instead_of_crashing(
    tmp_path: Path, slicer: Path
) -> None:
    overlay = tmp_path / "list.json"
    overlay.write_text(json.dumps({"process": {"wall_loops": [3]}}))
    out = run(config(box(tmp_path / "part.stl"), tmp_path / "out", overlay=overlay))
    project = rec(out, "project")
    assert project.verdict == "FAIL"
    assert "wall_loops" in project.summary


def test_a_set_on_the_3mf_project_moves_the_gate_expectations_too() -> None:
    from prep.steps import _set_gate_fields

    assert _set_gate_fields({"wall_loops": "3", "sparse_infill_density": "20%"}) == {
        "walls": 3,
        "infill": 0.2,
    }
    assert _set_gate_fields({"travel_speed": "600"}) == {}
    assert _set_gate_fields({"wall_loops": "three"}) == {}


def test_an_overlay_file_sets_speeds_acceleration_and_the_volumetric_cap(
    tmp_path: Path, slicer: Path
) -> None:
    overlay = tmp_path / "speeds.json"
    overlay.write_text(
        json.dumps(
            {
                "process": {"outer_wall_speed": 250, "inner_wall_acceleration": 12000},
                "filament": {"filament_max_volumetric_speed": 30},
                "machine": {"machine_max_acceleration_extruding": 12000},
            }
        )
    )
    out = run(
        config(box(tmp_path / "part.stl"), tmp_path / "out", overlay=overlay, walls=3)
    )
    project = rec(out, "project")
    files = {
        k: json.loads((tmp_path / "out" / v).read_text())
        for k, v in project.result["profiles"].items()
    }
    assert (
        files["process"]["outer_wall_speed"] == "250"
        and files["process"]["inner_wall_acceleration"] == "12000"
    )
    assert files["machine"]["machine_max_acceleration_extruding"] == ["12000", "12000"]
    assert files["filament"]["filament_max_volumetric_speed"] == ["30"]
    assert project.result["renamed"] and project.result["preset_names"][
        "process"
    ].endswith("prep overrides")


def test_changing_a_setting_recomputes_from_the_project_step_only(
    tmp_path: Path, slicer: Path
) -> None:
    model = box(tmp_path / "part.stl")
    run(config(model, tmp_path / "out", walls=3))
    again = run(config(model, tmp_path / "out", walls=3))
    ran = [r for r in again.records if r.verdict != "SKIP"]
    assert ran and all(r.cached for r in ran)
    changed = run(
        config(
            model, tmp_path / "out", walls=3, set_process={"outer_wall_speed": "250"}
        )
    )
    assert rec(changed, "orient").cached and rec(changed, "adapt").cached
    assert not rec(changed, "project").cached and not rec(changed, "slice").cached


def test_a_misspelt_setting_is_caught_by_the_gates_because_the_slicer_does_not_echo_it(
    tmp_path: Path, slicer: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "FAKE_SLICER_DROP", "outer_wal_speed"
    )  # Orca ignores a key it does not know
    out = run(
        config(
            box(tmp_path / "part.stl"),
            tmp_path / "out",
            walls=3,
            set_process={"outer_wal_speed": "250"},
        )
    )
    gates = rec(out, "gates")
    overrides = next(g for g in gates.result["gates"] if g["name"] == "overrides")
    assert gates.verdict == "FAIL" and overrides["verdict"] == "FAIL"
    assert (
        "outer_wal_speed" in overrides["detail"] and "misspelt" in overrides["detail"]
    )


def test_a_value_the_slicer_replaced_is_caught(tmp_path: Path, slicer: Path) -> None:
    out = run(config(box(tmp_path / "part.stl"), tmp_path / "out", walls=3))
    gcode = tmp_path / "out" / "part.gcode"
    gcode.write_text(gcode.read_text().replace("; wall_loops = 3", "; wall_loops = 2"))
    from prep import gcode_gates

    spec = gcode_gates.GateSpec(
        walls=None,
        infill=None,
        expect_settings=rec(out, "project").result["requested_settings"],
    )
    g = gcode_gates.check_gcode(gcode, spec).gate("overrides")
    assert g.verdict == "FAIL" and "wall_loops = 2 (asked 3)" in g.detail


def test_a_part_that_does_not_fit_the_k1se_bed_fails_with_the_bed_size(
    tmp_path: Path, slicer: Path
) -> None:
    out = run(config(box(tmp_path / "big.stl", (230.0, 30.0, 5.0)), tmp_path / "out"))
    project = rec(out, "project")
    assert (
        project.verdict == "FAIL"
        and "220" in project.summary
        and "250" in project.summary
    )


def test_no_slice_stops_after_the_presets(tmp_path: Path, slicer: Path) -> None:
    out = run(config(box(tmp_path / "part.stl"), tmp_path / "out", slice=False))
    assert rec(out, "project").verdict == "PASS" and rec(out, "slice").verdict == "SKIP"
    assert not slicer.exists()


def test_colours_and_the_u1_only_options_are_refused(tmp_path: Path) -> None:
    model = box(tmp_path / "part.stl")
    colors = tmp_path / "colors.yaml"
    colors.write_text("filaments: []\n")
    with pytest.raises(InvalidArgument, match="colors"):
        config(model, tmp_path / "o", colors=colors).validate()
    with pytest.raises(InvalidArgument, match="supports"):
        config(model, tmp_path / "o", printer="u1", supports="auto").validate()
    with pytest.raises(InvalidArgument, match="fset"):
        config(model, tmp_path / "o", printer="u1", set_filament={"a": "1"}).validate()
    with pytest.raises(InvalidArgument, match="supports"):
        config(model, tmp_path / "o", supports="tree").validate()
    with pytest.raises(InvalidArgument, match="printer"):
        config(model, tmp_path / "o", printer="k2").validate()


# ---------------------------------------------------------------- command line -> config


def make(argv: list[str]) -> PrepConfig:
    ns, extra = prep_cli.build_parser().parse_known_args(argv)
    assert not extra
    return prep_cli.make_config(ns)


def test_flags_become_the_config(tmp_path: Path) -> None:
    model = box(tmp_path / "part.stl")
    cfg = make(
        [
            str(model),
            "--printer",
            "k1se",
            "--supports",
            "auto",
            "--set",
            "outer_wall_speed=250",
            "--set",
            "travel_speed=600",
            "--fset",
            "pressure_advance=0.04",
            "--max-volumetric",
            "30",
            "--filament",
            ELEGOO,
        ]  # fmt: skip
    )
    assert (
        cfg.printer == "k1se"
        and cfg.supports == "auto"
        and cfg.filament_preset == ELEGOO
    )
    assert cfg.set_process == {"outer_wall_speed": "250", "travel_speed": "600"}
    assert cfg.set_filament == {
        "pressure_advance": "0.04",
        "filament_max_volumetric_speed": "30",
    }
    assert cfg.process_name is None  # the printer's own fleet process


def test_u1_keeps_its_default_process_and_k1se_has_none_of_its_own_set(
    tmp_path: Path,
) -> None:
    model = box(tmp_path / "part.stl")
    assert (
        make([str(model)]).process_name == "0.20mm Standard @Snapmaker U1 (0.4 nozzle)"
    )
    assert make([str(model), "--process", "x"]).process_name == "x"


def test_a_spec_file_carries_supports_set_fset_and_a_relative_overlay(
    tmp_path: Path,
) -> None:
    (tmp_path / "o.json").write_text("{}")
    spec = tmp_path / "prep.yaml"
    spec.write_text(
        "printer: k1se\nprocess:\n  supports: auto\n  overlay: o.json\n  set: {outer_wall_speed: 250}\n"
        "  fset: {pressure_advance: 0.04}\n"
    )
    model = box(tmp_path / "part.stl")
    cfg = make([str(model), "--spec", str(spec), "--set", "travel_speed=600"])
    assert (
        cfg.printer == "k1se"
        and cfg.supports == "auto"
        and cfg.overlay == (tmp_path / "o.json").resolve()
    )
    assert cfg.set_process == {"outer_wall_speed": "250", "travel_speed": "600"}
    assert cfg.set_filament == {"pressure_advance": "0.04"}
    assert (
        make([str(model), "--spec", str(spec), "--supports", "none"]).supports == "none"
    )  # a flag wins


def test_a_spec_set_normalises_booleans_like_an_overlay_and_rejects_lists(
    tmp_path: Path,
) -> None:
    model = box(tmp_path / "part.stl")
    spec = tmp_path / "prep.yaml"
    spec.write_text(
        "process:\n  set: {enable_support: true, brim_width: 0, wall_loops: 3.0}\n"
    )
    cfg = make([str(model), "--spec", str(spec)])
    assert cfg.set_process == {
        "enable_support": "1",
        "brim_width": "0",
        "wall_loops": "3",
    }
    spec.write_text(
        "process:\n  set: {machine_max_acceleration_extruding: [8000, 9000]}\n"
    )
    with pytest.raises(InvalidArgument):
        make([str(model), "--spec", str(spec)])


def test_a_set_without_equals_is_an_invalid_argument(tmp_path: Path) -> None:
    with pytest.raises(InvalidArgument):
        make([str(box(tmp_path / "p.stl")), "--set", "outer_wall_speed"])
