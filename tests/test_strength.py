"""`3d strength`: request validation, verdict rules, beam and FEA results, orientation, projects."""

from __future__ import annotations

import json
import math
import pathlib

import pytest
from errors import InputNotFound, InvalidArgument, UsageError
from structural_fixtures import bar, tapered_bar


def _stl(tmp_path: pathlib.Path, name: str, mesh) -> pathlib.Path:  # type: ignore[no-untyped-def]
    path = tmp_path / name
    mesh.export(path)
    return path


def _bar_stl(tmp_path: pathlib.Path) -> pathlib.Path:
    return _stl(tmp_path, "bar.stl", bar(100.0, 10.0, 10.0))


def _analyze(argv: list[str]) -> dict:  # type: ignore[type-arg]
    from strength import parse_args
    from structural import run

    return run.tidy(run.analyze(parse_args(argv)))  # type: ignore[no-any-return]


# ---- request validation ------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["nan", "inf", "-5", "0", "abc"])
def test_load_must_be_a_positive_finite_number(tmp_path, bad) -> None:  # type: ignore[no-untyped-def]
    from strength import parse_args

    with pytest.raises(InvalidArgument) as exc:
        parse_args([str(_bar_stl(tmp_path)), "--material", "PLA", "--load", bad])
    assert exc.value.exit_code == 2


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (["--material", "PLA", "--load", "5"], UsageError),
        (["{part}", "--material", "PLA"], UsageError),
        (
            ["{part}", "--material", "PLA", "--load", "5", "--fixture", "arch"],
            InvalidArgument,
        ),
        (
            ["{part}", "--material", "PLA", "--load", "5", "--axis", "W"],
            InvalidArgument,
        ),
        (
            ["{part}", "--material", "PLA", "--load", "5", "--method", "magic"],
            InvalidArgument,
        ),
        (
            ["{part}", "--material", "PLA", "--load", "5", "--contact", "glue"],
            InvalidArgument,
        ),
        (
            ["{part}", "--material", "PLA", "--load", "5", "--crop", "1,2,3"],
            InvalidArgument,
        ),
        (["{part}", "--material", "PLA", "--load", "5", "--bogus"], UsageError),
        (["{part}", "extra.stl", "--material", "PLA", "--load", "5"], UsageError),
        (["--hook", "t=1,L=7,w=6", "--material", "PLA"], UsageError),
        (["--hook", "t=1,L=7,w=6,y=nan", "--material", "PLA"], InvalidArgument),
        (["--hook", "t=1,L=7,w=6,y=0.2,bogus=1", "--material", "PLA"], InvalidArgument),
    ],
)
def test_invalid_requests_exit_2(tmp_path, argv, error) -> None:  # type: ignore[no-untyped-def]
    from strength import parse_args

    part = str(_bar_stl(tmp_path))
    with pytest.raises(error) as exc:
        parse_args([part if a == "{part}" else a for a in argv])
    assert exc.value.exit_code == 2


def test_missing_file_and_unsupported_extension(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from strength import parse_args

    with pytest.raises(InputNotFound):
        parse_args([str(tmp_path / "nope.stl"), "--material", "PLA", "--load", "5"])
    obj = tmp_path / "part.obj"
    obj.write_text("v 0 0 0\n", encoding="utf-8")
    with pytest.raises(InvalidArgument, match="accepted"):
        parse_args([str(obj), "--material", "PLA", "--load", "5"])


def test_verdict_thresholds_and_exit_codes() -> None:
    from strength import exit_code_for, verdict_for

    assert verdict_for(2.0, 2.0) == "PASS"
    assert verdict_for(1.99, 2.0) == "WEAK"
    assert verdict_for(1.0, 2.0) == "WEAK"
    assert verdict_for(0.99, 2.0) == "FAIL"
    assert verdict_for(math.inf, 2.0) == "PASS"
    assert [exit_code_for(v) for v in ("PASS", "WEAK", "FAIL")] == [0, 1, 1]


def test_command_help_and_no_args(capsys) -> None:  # type: ignore[no-untyped-def]
    from commands import strength as cmd

    assert cmd.run(["--help"]) == 0
    out = capsys.readouterr().out
    assert "3d strength" in out and "ANALYTICAL_SCREEN" in out and "FEA_LINEAR" in out
    assert cmd.run([]) == 1
    assert "3d strength" in capsys.readouterr().out


def test_command_help_after_positional_returns_success(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    from commands import strength as cmd

    assert cmd.run([str(_bar_stl(tmp_path)), "--help"]) == 0
    assert "3d strength" in capsys.readouterr().out


# ---- beam path: closed-form fixtures within 1 % ------------------------------------------------
def test_beam_cantilever_on_a_mesh_matches_the_closed_form(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result = _analyze(
        [
            str(_bar_stl(tmp_path)),
            "--material",
            "PLA",
            "--load",
            "10",
            "--method",
            "beam",
        ]
    )
    assert result["method"] == "beam" and result["label"] == "ANALYTICAL_SCREEN"
    assert result["schema"] == "3d.strength/2"
    assert result["verdict"] in ("PASS", "WEAK", "FAIL")  # never NOT_EVALUATED
    case = result["cases"][0]
    assert case["max_displacement_mm"] == pytest.approx(
        10.0 * 100.0**3 / (3 * 3300.0 * (10.0 * 10.0**3 / 12)), rel=0.01
    )
    assert case["max_von_mises_mpa"] == pytest.approx(6.0, rel=0.01)
    assert result["mesh"] is None  # the beam path makes no FEA claim
    assert any("Euler-Bernoulli" in a for a in result["assumptions"])


def test_beam_tapered_cantilever_from_the_mesh_matches_the_taper_formula(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    from structural import beams

    part = _stl(tmp_path, "taper.stl", tapered_bar(60.0, 10.0, 8.0, 4.0))
    result = _analyze(
        [str(part), "--material", "PLA", "--load", "6", "--method", "beam"]
    )
    expected = beams.tapered_thickness_tip_deflection(6.0, 60.0, 3300.0, 10.0, 8.0, 0.5)
    assert result["cases"][0]["max_displacement_mm"] == pytest.approx(
        expected, rel=0.01
    )


def test_beam_three_point_bend_matches_the_closed_form(tmp_path) -> None:  # type: ignore[no-untyped-def]
    part = _stl(tmp_path, "bend.stl", bar(80.0, 10.0, 8.0))
    result = _analyze(
        [
            str(part),
            "--material",
            "PLA",
            "--load",
            "20",
            "--fixture",
            "simple",
            "--method",
            "beam",
        ]
    )
    expected = 20.0 * 80.0**3 / (48 * 3300.0 * (10.0 * 8.0**3 / 12))
    case = result["cases"][0]
    assert case["max_displacement_mm"] == pytest.approx(expected, rel=0.01)
    assert case["max_von_mises_mpa"] == pytest.approx(
        20.0 * 80.0 / 4 * 4.0 / (10.0 * 8.0**3 / 12), rel=0.01
    )


def test_beam_compression_reports_stress_and_buckling(tmp_path) -> None:  # type: ignore[no-untyped-def]
    part = _stl(tmp_path, "column.stl", bar(60.0, 6.0, 6.0))
    result = _analyze(
        [
            str(part),
            "--material",
            "PLA",
            "--load",
            "30",
            "--fixture",
            "compression",
            "--axis",
            "X",
            "--method",
            "beam",
        ]
    )
    case = result["cases"][0]
    assert case["max_von_mises_mpa"] == pytest.approx(30.0 / 36.0, rel=0.01)
    assert set(case["safety_factors"]) == {"compressive stress", "euler buckling"}


def test_beam_rejects_assemblies_and_project_loads(tmp_path) -> None:  # type: ignore[no-untyped-def]
    project = _two_part_project(tmp_path)
    with pytest.raises(UsageError, match="assembly"):
        _analyze([str(project), "--material", "PLA", "--method", "beam"])


# ---- anisotropy by print orientation -----------------------------------------------------------
def test_orientation_changes_the_allowable_and_is_recorded(tmp_path) -> None:  # type: ignore[no-untyped-def]
    part = _bar_stl(tmp_path)
    flat = _analyze(
        [
            str(part),
            "--material",
            "PLA",
            "--load",
            "10",
            "--method",
            "beam",
            "--orientation",
            "z-up",
        ]
    )
    standing = _analyze(
        [
            str(part),
            "--material",
            "PLA",
            "--load",
            "10",
            "--method",
            "beam",
            "--orientation",
            "x-up",
        ]
    )
    assert flat["cases"][0]["allowable_mpa"] == pytest.approx(45.0)
    assert standing["cases"][0]["allowable_mpa"] == pytest.approx(45.0 * 0.45)
    assert standing["safety_factor"] == pytest.approx(
        flat["safety_factor"] * 0.45, rel=0.02
    )
    body_flat, body_standing = (
        flat["input"]["bodies"][0],
        standing["input"]["bodies"][0],
    )
    assert body_flat["orientation"]["build_axis"] == [0.0, 0.0, 1.0]
    assert body_standing["orientation"]["build_axis"] == [1.0, 0.0, 0.0]
    assert "x-up" in body_standing["orientation"]["source"]


def test_orientation_from_a_3mf_build_transform(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import zipfile

    model = (
        '<?xml version="1.0"?><model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        '<resources/><build><item objectid="1" transform="1 0 0 0 0 1 0 -1 0 0 0 0"/></build></model>'
    )
    path = tmp_path / "project.3mf"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", model)
    from structural import orientation

    got = orientation.from_3mf(path)
    assert got.build_axis == pytest.approx((0.0, 1.0, 0.0))  # a 90 deg turn about X: the model's +Y points up
    assert "3mf:project.3mf" in got.source
    spec = orientation.resolve(str(path), extents=(1.0, 1.0, 1.0))
    assert spec.build_axis == got.build_axis
    assert orientation.resolve("flat", extents=(30.0, 4.0, 10.0)).build_axis == (
        0.0,
        1.0,
        0.0,
    )
    assert orientation.resolve(
        None, extents=(1, 1, 1), part_orientation="x-up"
    ).source.startswith("3d.yaml part orientation")
    with pytest.raises(InvalidArgument):
        orientation.resolve("sideways", extents=(1.0, 1.0, 1.0))


# ---- snap hook ---------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("material", "arm_axis", "y", "verdict"),
    [
        ("PLA", "X", "0.2", "PASS"),  # arm in the layer plane: 0.61 % vs 2 %
        ("PLA", "Z", "0.2", "WEAK"),  # arm across layers: 0.61 % vs 0.9 %
        ("PLA", "Z", "0.5", "FAIL"),  # 1.53 % vs 0.9 %
        ("PETG", "Z", "0.5", "PASS"),  # 1.53 % vs 3.15 %
    ],
)
def test_snap_hook_verdict_against_published_strain_limits(
    material, arm_axis, y, verdict
) -> None:  # type: ignore[no-untyped-def]
    result = _analyze(
        [
            "--hook",
            f"t=1.0,L=7,w=6,y={y}",
            "--material",
            material,
            "--arm-axis",
            arm_axis,
            "--orientation",
            "z-up",
        ]
    )
    assert result["verdict"] == verdict
    assert (
        result["label"] == "ANALYTICAL_SCREEN"
        and result["governing"] == "permissible strain"
    )
    case = result["cases"][0]
    assert case["root_strain"] == pytest.approx(1.5 * float(y) / 49.0)
    assert case["deflection_force_n"] > 0.0


def test_snap_hook_repeated_use_derates_the_limit() -> None:
    once = _analyze(
        ["--hook", "t=1.0,L=7,w=6,y=0.3", "--material", "PETG", "--arm-axis", "X"]
    )
    often = _analyze(
        [
            "--hook",
            "t=1.0,L=7,w=6,y=0.3",
            "--material",
            "PETG",
            "--arm-axis",
            "X",
            "--repeated-use",
        ]
    )
    assert often["cases"][0]["permissible_strain"] == pytest.approx(
        0.6 * once["cases"][0]["permissible_strain"]
    )


# ---- FEA on projects ---------------------------------------------------------------------------
def _two_part_project(tmp_path: pathlib.Path) -> pathlib.Path:
    """A base bar and a stiffening plate on top of it, glued, fixed at x = 0, loaded at the tip."""
    base = bar(60.0, 10.0, 6.0)
    plate = bar(60.0, 10.0, 3.0)
    plate.apply_translation([0.0, 0.0, 4.5])
    base.export(tmp_path / "base.stl")
    plate.export(tmp_path / "plate.stl")
    (tmp_path / "3d.yaml").write_text(
        "project: {name: bracket, material: PLA}\n"
        "parts:\n"
        "  base: {file: base.stl}\n"
        "  plate: {file: plate.stl, material: PETG}\n"
        "anchors:\n"
        "  wall: {pos: [0, 0, 0], area: 400}\n"
        "  tip: {pos: [60, 0, 0], area: 200}\n"
        "loads:\n"
        "  clamp: {anchor: wall, kind: fixed}\n"
        "  push: {anchor: tip, vector: [0, 0, -4]}\n"
        "mates:\n"
        "  - {a: base, b: plate, fit_type: glue}\n",
        encoding="utf-8",
    )
    return tmp_path / "3d.yaml"


def test_assembled_project_uses_loads_mates_and_per_part_materials(tmp_path) -> None:  # type: ignore[no-untyped-def]
    pytest.importorskip("skfem")
    pytest.importorskip("pytetwild")
    project = _two_part_project(tmp_path)
    png = tmp_path / "stress.png"
    result = _analyze([str(project), "--viz", str(png), "--max-elements", "3000"])
    assert result["method"] == "fea" and result["label"] == "FEA_LINEAR"
    assert result["verdict"] in ("PASS", "WEAK", "FAIL")
    assert [c["name"] for c in result["cases"]] == ["push"]
    assert {b["material"] for b in result["input"]["bodies"]} == {"PLA", "PETG"}
    assert (
        result["contacts"][0]["kind"] == "tied"
        and result["contacts"][0]["origin"] == "3d.yaml mate"
    )
    case = result["cases"][0]
    assert case["hotspots"] and case["max_von_mises_mpa"] > 0.0
    assert set(case["safety_factors"]) >= {"in_plane", "across_layers"}
    assert {"in_plane_mpa", "across_layers_mpa"} <= set(
        result["materials"][0]["allowables"]
    )
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert result["artifacts"]["stress_png"] == str(png)
    assert any("tied" in a or "bonded" in a for a in result["assumptions"])
    assert result["unverified"]


def test_assembled_stress_differs_from_the_parts_analysed_separately(tmp_path) -> None:  # type: ignore[no-untyped-def]
    pytest.importorskip("skfem")
    pytest.importorskip("pytetwild")
    project = _two_part_project(tmp_path)
    assembled = _analyze([str(project), "--max-elements", "3000"])
    alone = _analyze(
        [
            str(tmp_path / "base.stl"),
            "--loads",
            str(project),
            "--material",
            "PLA",
            "--max-elements",
            "3000",
        ]
    )
    stress_assembled = max(
        b["max_von_mises_mpa"]
        for b in assembled["cases"][0]["per_body"]
        if b["name"] == "base"
    )
    stress_alone = alone["cases"][0]["max_von_mises_mpa"]
    assert stress_assembled < 0.75 * stress_alone
    assert (
        assembled["cases"][0]["max_displacement_mm"]
        < 0.6 * alone["cases"][0]["max_displacement_mm"]
    )


def test_fea_fixture_run_and_orientation_changes_the_safety_factor(tmp_path) -> None:  # type: ignore[no-untyped-def]
    pytest.importorskip("skfem")
    pytest.importorskip("pytetwild")
    part = _bar_stl(tmp_path)
    common = [
        str(part),
        "--material",
        "PLA",
        "--load",
        "12",
        "--method",
        "fea",
        "--max-elements",
        "2500",
    ]
    flat = _analyze([*common, "--orientation", "z-up"])
    standing = _analyze([*common, "--orientation", "x-up"])
    assert flat["mesh"]["order"] == 2 and flat["mesh"]["total_tets"] > 500
    assert flat["cases"][0]["max_displacement_mm"] == pytest.approx(
        12 * 100.0**3 / (3 * 3300.0 * (10.0 * 10.0**3 / 12)), rel=0.03
    )
    assert standing["safety_factor"] < 0.6 * flat["safety_factor"]
    assert (
        standing["cases"][0]["max_displacement_mm"]
        > flat["cases"][0]["max_displacement_mm"]
    )
    assert standing["cases"][0]["governing"] == "across_layers"


def test_displacement_load_is_judged_on_strain_not_stress(tmp_path) -> None:  # type: ignore[no-untyped-def]
    pytest.importorskip("skfem")
    pytest.importorskip("pytetwild")
    arm = bar(7.0, 6.0, 1.0)
    arm.export(tmp_path / "arm.stl")
    (tmp_path / "3d.yaml").write_text(
        "project: {name: hook, material: PETG}\n"
        "parts:\n  arm: {file: arm.stl}\n"
        "anchors:\n  root: {pos: [0, 0, 0], area: 12}\n  tip: {pos: [7, 0, 0], area: 12}\n"
        "loads:\n"
        "  clamp: {anchor: root, kind: fixed}\n"
        "  deflect: {anchor: tip, kind: displacement, vector: [0, 0, -0.3], radius_mm: 3.5}\n",
        encoding="utf-8",
    )
    result = _analyze(
        [str(tmp_path / "3d.yaml"), "--mesh-size", "0.6", "--max-elements", "6000"]
    )
    case = result["cases"][0]
    assert (
        case["control"] == "displacement" and case["governing"] == "permissible strain"
    )
    assert case["safety_factor"] == pytest.approx(
        case["safety_factors"]["permissible strain"]
    )
    assert case["max_principal_strain"] > 0.5 * (1.5 * 1.0 * 0.3 / 49.0)


_DEFAULT_LOADS = "loads:\n  clamp: {anchor: wall, kind: fixed}\n  push: {anchor: tip, vector: [0, 0, -4]}\n"


@pytest.mark.parametrize(
    ("project_extra", "loads", "message"),
    [
        (", units: in", _DEFAULT_LOADS, "mm"),
        ("", _DEFAULT_LOADS + "mates:\n  - {a: base, b: ghost}\n", "ghost"),
        ("", "loads:\n  push: {anchor: nowhere, vector: [0, 0, -4]}\n", "nowhere"),
        ("", "loads:\n  clamp: {anchor: wall, kind: fixed}\n  push: {anchor: tip, vector: [0, 0, .nan]}\n", "finite"),
        ("", "loads:\n  push: {anchor: tip, vector: [0, 0, -4]}\n", "support"),
        ("", "loads:\n  clamp: {anchor: wall, kind: fixed}\n  hit: {anchor: tip, kind: thump, vector: [0, 0, 1]}\n", "thump"),
        ("", "loads:\n  clamp: {anchor: wall, kind: fixed}\n", "no load"),
    ],
)
def test_unsupported_project_inputs_exit_2(tmp_path, project_extra, loads, message) -> None:  # type: ignore[no-untyped-def]
    pytest.importorskip("skfem")
    pytest.importorskip("pytetwild")
    from errors import ThreeDError

    bar(40.0, 10.0, 6.0).export(tmp_path / "base.stl")
    (tmp_path / "3d.yaml").write_text(
        f"project: {{name: p, material: PLA{project_extra}}}\n"
        "parts:\n  base: {file: base.stl}\n"
        "anchors:\n  wall: {pos: [0, 0, 0], area: 60}\n  tip: {pos: [40, 0, 0], area: 60}\n" + loads,
        encoding="utf-8",
    )
    with pytest.raises(ThreeDError, match=message) as exc:
        _analyze([str(tmp_path / "3d.yaml"), "--max-elements", "1500"])
    assert exc.value.exit_code == 2


def test_dry_run_plans_without_solving(tmp_path) -> None:  # type: ignore[no-untyped-def]
    pytest.importorskip("skfem")
    pytest.importorskip("pytetwild")
    project = _two_part_project(tmp_path)
    result = _analyze([str(project), "--dry-run"])
    assert result["status"] == "PLAN" and result["verdict"] is None
    assert result["cases"][0]["verdict"] == "PLAN"
    assert json.dumps(result)


def test_dry_run_plans_the_beam_and_hook_paths_without_solving(tmp_path) -> None:  # type: ignore[no-untyped-def]
    beam = _analyze(
        [str(_bar_stl(tmp_path)), "--material", "PLA", "--load", "10", "--method", "beam", "--dry-run"]
    )
    hook = _analyze(["--hook", "t=1,L=7,w=6,y=.6", "--material", "PLA", "--dry-run"])
    for result in (beam, hook):
        assert result["status"] == "PLAN"
        assert result["verdict"] is None
        assert result["safety_factor"] is None


def test_compression_buckling_uses_the_fixed_free_restraint(tmp_path) -> None:  # type: ignore[no-untyped-def]
    part = _stl(tmp_path, "slim.stl", bar(100.0, 2.0, 2.0))
    result = _analyze(
        [str(part), "--material", "PLA", "--load", "1.5", "--fixture", "compression",
         "--axis", "X", "--method", "beam"]
    )
    assert result["cases"][0]["safety_factors"]["euler buckling"] < 1.0
