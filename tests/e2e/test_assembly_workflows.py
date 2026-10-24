"""Readable e2e stories for `3d assembly` (umbrella #570, S3 #574): a user checks a case, sweeps a drawer, positions plates.

Every story runs the real `bin/3d` on synthetic STL fixtures (tests/assembly_fixtures.py); no printer, slicer or network.
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any

import pytest
from assembly_fixtures import (
    board_in_case,
    box,
    drawer,
    peg_pocket,
    snap_tab,
    write_spec,
    write_stl,
)
from fit_fixtures import write_insert, write_plate
from retarget_fixtures import Box, make_project, write_presets

from .workflow_helper import REPO_ROOT, run_cli

ENV = {
    "THREED_FLEET": str(REPO_ROOT / "fleet" / "printers.yaml"),
    "THREED_PROFILES_DIR": "/nonexistent-profiles",
}


def cli(tmp_path: Path, *args: str) -> Any:
    return run_cli(tmp_path, "assembly", *args, env_extra=ENV, timeout=300)


def as_json(res: Any) -> dict[str, Any]:
    assert res.stdout.strip(), res.stderr
    doc: dict[str, Any] = json.loads(res.stdout)
    assert doc["schema"] == "3d.assembly/1"
    return doc


def test_help_lists_every_subcommand_and_flag(tmp_path: Path) -> None:
    """A user reads `3d assembly --help` and finds each subcommand and flag with an example."""
    res = run_cli(tmp_path, "assembly", "--help")
    assert res.returncode == 0
    for word in (
        "check", "sweep", "order", "ergonomics", "explode", "bom", "position", "show", "--mate", "--printer", "--material",
        "--viz", "--direction", "--step", "--distance", "--ignore", "--base", "--finger", "--nail", "--tool", "--reach",
        "--usdz", "--3mf", "--bed", "--base-project", "--include-non-printable", "--reason", "--yes", "--json", "--mem-limit-mb",
    ):  # fmt: skip
        assert word in res.stdout, word
    assert run_cli(tmp_path, "assembly").returncode == 1


def test_usage_errors_exit_2_with_the_accepted_values(tmp_path: Path) -> None:
    bad_sub = run_cli(tmp_path, "assembly", "weld", "x.yaml")
    assert bad_sub.returncode == 2 and "check, sweep, order" in bad_sub.stderr
    assert run_cli(tmp_path, "assembly", "check").returncode == 2
    doc = peg_pocket(tmp_path)
    doc["mates"][0]["insertion"]["axis"] = [0, 0, 0]
    spec = write_spec(tmp_path, doc)
    res = cli(tmp_path, "check", str(spec))
    assert (
        res.returncode == 2
        and "mates[0].insertion.axis" in res.stderr
        and "+X" in res.stderr
    )
    assert cli(tmp_path, "check", str(tmp_path / "missing.yaml")).returncode == 2


def test_a_clean_assembly_exits_0_and_a_blocking_boss_exits_1_with_pair_depth_and_location(
    tmp_path: Path,
) -> None:
    """The main deliverable: see the intersections that block assembly."""
    clean = write_spec(tmp_path, peg_pocket(tmp_path), "clean.yaml")
    ok = cli(tmp_path, "check", str(clean), "--json")
    assert ok.returncode == 0, ok.stderr
    doc = as_json(ok)
    assert (
        doc["ok"]
        and doc["summary"]["blocking"] == 0
        and doc["mates"][0]["fit"]["provenance"]
    )

    bad = write_spec(tmp_path, peg_pocket(tmp_path, boss=True), "boss.yaml")
    res = cli(tmp_path, "check", str(bad))
    assert res.returncode == 1
    assert (
        "BLOCKING intersections" in res.stdout
        and "socket x peg" in res.stdout
        or "peg x socket" in res.stdout
    )
    assert re.search(r"depth 1\.7\d\d mm", res.stdout) and "at (4.15" in res.stdout
    assert (
        "fix: open the clearance" in res.stdout
        and ">>> ASSEMBLY CHECK: FAIL" in res.stdout
    )
    js = as_json(cli(tmp_path, "check", str(bad), "--json"))
    assert not js["ok"] and js["blocking"][0]["center"][2] == pytest.approx(
        10.0, abs=0.1
    )


def test_the_check_writes_section_pngs_with_a_legend_and_distinct_part_colours(
    tmp_path: Path,
) -> None:
    from PIL import Image

    bad = write_spec(tmp_path, board_in_case(tmp_path, board_dx=1.0))
    res = cli(tmp_path, "check", str(bad), "--viz", str(tmp_path / "viz"), "--json")
    assert res.returncode == 1
    doc = as_json(res)
    assert doc["images"] and all(Path(p).is_file() for p in doc["images"])
    overview = Image.open(tmp_path / "viz" / "overview.png")
    assert overview.size[0] >= 800
    colours = [row["color"] for row in doc["legend"]]
    assert len(set(colours)) == len(colours) == 2, "no two parts share a colour"
    again = as_json(cli(tmp_path, "check", str(bad), "--json"))
    assert again["legend"] == doc["legend"], "the palette is stable across runs"


def test_mole_pattern_designed_ribs_are_allowed_and_the_board_one_millimetre_off_is_blocking(
    tmp_path: Path,
) -> None:
    good = as_json(
        cli(
            tmp_path,
            "check",
            str(write_spec(tmp_path, board_in_case(tmp_path), "good.yaml")),
            "--json",
        )
    )
    assert (
        good["ok"]
        and good["summary"]["allowed"] == 2
        and good["summary"]["blocking"] == 0
    )
    broken = cli(
        tmp_path,
        "check",
        str(write_spec(tmp_path, board_in_case(tmp_path, board_dx=1.0), "broken.yaml")),
        "--json",
    )
    assert broken.returncode == 1 and as_json(broken)["summary"]["blocking"] >= 1


def test_the_verdict_changes_with_printer_and_material_through_the_fit_table(
    tmp_path: Path,
) -> None:
    doc = peg_pocket(tmp_path)
    doc["mates"][0]["fit"] = "snap"
    spec = write_spec(tmp_path, doc)
    env = {**ENV, "THREED_FITS_CORRECTIONS": str(tmp_path / "corr.yaml")}
    add = run_cli(
        tmp_path,
        "fits",
        "corrections",
        "add",
        "--printer",
        "k1se",
        "--material",
        "PETG",
        "--delta",
        "0.25",
        "--yes",
        env_extra=env,
    )
    assert add.returncode == 0, add.stderr
    petg = run_cli(
        tmp_path,
        "assembly",
        "check",
        str(spec),
        "--printer",
        "k1se",
        "--material",
        "PETG",
        "--json",
        env_extra=env,
    )
    pla = run_cli(
        tmp_path,
        "assembly",
        "check",
        str(spec),
        "--printer",
        "k1se",
        "--material",
        "PLA",
        "--json",
        env_extra=env,
    )
    assert petg.returncode == 1 and pla.returncode == 0
    assert as_json(petg)["mates"][0]["fit"]["clearance_mm"] == pytest.approx(0.55)
    assert as_json(pla)["mates"][0]["fit"]["clearance_mm"] == pytest.approx(0.30)


@pytest.mark.parametrize("axis", ["+Y", "-X", "oblique"])
def test_sweep_reports_the_first_blocking_distance_on_any_axis(
    tmp_path: Path, axis: str
) -> None:
    free = write_spec(tmp_path, drawer(tmp_path, axis=axis), "free.yaml")
    assert (
        cli(
            tmp_path, "sweep", str(free), "--mate", "slide", "--direction", "remove"
        ).returncode
        == 0
    )
    lip = write_spec(tmp_path, drawer(tmp_path, lip=True, axis=axis), "lip.yaml")
    res = cli(
        tmp_path,
        "sweep",
        str(lip),
        "--mate",
        "slide",
        "--direction",
        "remove",
        "--json",
    )
    assert res.returncode == 1
    doc = as_json(res)
    assert (
        doc["first_blocking"]["travelled"] == pytest.approx(2.0, abs=0.03)
        and doc["direction"] == "remove"
    )
    both = cli(tmp_path, "sweep", str(lip), "--mate", "slide", "--json")
    assert as_json(both)["kind"] == "sweeps" and len(as_json(both)["sweeps"]) == 2
    assert cli(tmp_path, "sweep", str(lip)).returncode == 2, "sweep needs --mate"


def test_a_snap_hook_is_swept_against_its_flex_limit(tmp_path: Path) -> None:
    ok = cli(
        tmp_path,
        "sweep",
        str(write_spec(tmp_path, snap_tab(tmp_path), "ok.yaml")),
        "--mate",
        "cap_snap",
        "--direction",
        "remove",
        "--step",
        "0.05",
        "--json",
    )
    assert ok.returncode == 0, ok.stdout
    hook = as_json(ok)["snap"]["hooks"][0]
    assert (
        hook["implied_deflection_mm"] == pytest.approx(0.2, abs=0.01)
        and hook["flex_limit_mm"] == 0.3
    )
    low = write_spec(tmp_path, snap_tab(tmp_path, flex_limit=0.1), "low.yaml")
    res = cli(
        tmp_path,
        "sweep",
        str(low),
        "--mate",
        "cap_snap",
        "--direction",
        "insert",
        "--step",
        "0.05",
    )
    assert res.returncode == 1 and "flex limit" in res.stdout


def test_order_prints_the_assembly_sequence_and_exits_1_when_there_is_none(
    tmp_path: Path,
) -> None:
    doc = peg_pocket(tmp_path)
    write_stl(tmp_path / "cover.stl", box((-8, -8, 20.5), (8, 8, 22.5)))
    doc["parts"]["cover"] = {"mesh": "cover.stl"}
    res = cli(
        tmp_path,
        "order",
        str(write_spec(tmp_path, doc)),
        "--base",
        "socket",
        "--step",
        "0.5",
    )
    assert res.returncode == 0, res.stdout
    assert (
        "assembly order:" in res.stdout
        and "place socket" in res.stdout
        and ">>> ASSEMBLY ORDER: FEASIBLE" in res.stdout
    )
    write_stl(
        tmp_path / "shell.stl",
        box((-15, -15, 0), (15, 15, 20)) - box((-5.3, -5.3, 5), (5.3, 5.3, 19)),
    )
    write_stl(tmp_path / "inner.stl", box((-5, -5, 5), (5, 5, 15)))
    sealed = write_spec(
        tmp_path,
        {"parts": {"shell": {"mesh": "shell.stl"}, "inner": {"mesh": "inner.stl"}}},
        "sealed.yaml",
    )
    stuck = cli(tmp_path, "order", str(sealed), "--base", "shell", "--step", "0.5")
    assert (
        stuck.returncode == 1
        and "INFEASIBLE" in stuck.stdout
        and "blocked" in stuck.stdout
    )


def test_ergonomics_flags_a_buried_hook_and_passes_one_a_nail_reaches(
    tmp_path: Path,
) -> None:
    buried = cli(
        tmp_path,
        "ergonomics",
        str(write_spec(tmp_path, snap_tab(tmp_path, covered=True), "buried.yaml")),
    )
    assert buried.returncode == 1 and "FAIL  cap_snap.h1.access" in buried.stdout
    open_ = cli(
        tmp_path,
        "ergonomics",
        str(write_spec(tmp_path, snap_tab(tmp_path, covered=False), "open.yaml")),
        "--json",
    )
    assert open_.returncode == 0, open_.stdout
    rows = {c["id"]: c for c in as_json(open_)["checks"]}
    assert (
        rows["cap_snap.h1.access"]["status"] == "warn"
        and rows["cap_snap.h1.deflection"]["status"] == "pass"
    )
    narrow = cli(
        tmp_path,
        "ergonomics",
        str(write_spec(tmp_path, snap_tab(tmp_path, covered=False), "open.yaml")),
        "--finger",
        "6",
        "--json",
    )
    assert {c["id"]: c for c in as_json(narrow)["checks"]}["cap_snap.h1.access"][
        "status"
    ] == "pass"


def test_explode_bom_and_show_label_stand_ins_and_colour_every_part(
    tmp_path: Path,
) -> None:
    spec = write_spec(tmp_path, board_in_case(tmp_path))
    png = tmp_path / "out" / "exploded.png"
    res = cli(tmp_path, "explode", str(spec), "--viz", str(png), "--json")
    assert res.returncode == 0 and png.is_file() and len(as_json(res)["legend"]) == 2
    bom = as_json(cli(tmp_path, "bom", str(spec), "--json"))
    board = next(p for p in bom["parts"] if p["part"] == "board")
    assert (
        board["role"] == "stand_in"
        and board["label"].startswith("stand-in")
        and not board["printable"]
    )
    text = cli(tmp_path, "show", str(spec)).stdout
    assert "board" in text and "ghost" in text


def test_position_is_a_dry_run_until_yes_and_keeps_stand_ins_off_the_plates(
    tmp_path: Path,
) -> None:
    spec = write_spec(tmp_path, board_in_case(tmp_path))
    out = tmp_path / "out" / "plates.3mf"
    dry = cli(tmp_path, "position", str(spec), "--3mf", str(out))
    assert dry.returncode == 0 and not out.exists() and "DRY RUN" in dry.stdout
    assert "NOT on any plate: board (stand_in" in dry.stdout
    done = cli(tmp_path, "position", str(spec), "--3mf", str(out), "--yes", "--json")
    assert done.returncode == 0, done.stderr
    doc = as_json(done)
    assert (
        doc["written"]
        and doc["verification"]["non_printable_on_plates"] == []
        and doc["verification"]["objects"] == 1
    )
    with zipfile.ZipFile(out) as zf:
        assert "Metadata/3d_assembly.json" in zf.namelist()
        assert (
            'key="3d:printable" value="true"'
            in zf.read("Metadata/model_settings.config").decode()
        )
    forced = cli(
        tmp_path,
        "position",
        str(spec),
        "--3mf",
        str(tmp_path / "out" / "all.3mf"),
        "--include-non-printable",
        "--reason",
        "fit test",
        "--yes",
        "--json",
    )
    assert forced.returncode == 0 and [
        o["name"] for o in as_json(forced)["layout"]["override"]
    ] == ["board"]
    assert cli(tmp_path, "position", str(spec)).returncode == 2, "position needs --3mf"
    assert (
        cli(tmp_path, "position", str(out), "--3mf", str(tmp_path / "x.3mf")).returncode
        == 2
    ), "a 3MF is already positioned"


def test_positioned_3mf_goes_through_retarget_and_arrange_and_the_mates_survive(
    tmp_path: Path,
) -> None:
    """position -> retarget -> check: the printed file is verified, the role tag and the mates survive."""
    base = make_project(tmp_path / "base.3mf", [Box("part", (30, 20, 10))])
    presets = write_presets(tmp_path / "presets")
    spec = write_spec(tmp_path, board_in_case(tmp_path))
    out = tmp_path / "out" / "plates.3mf"
    res = cli(
        tmp_path, "position", str(spec), "--3mf", str(out), "--base-project", str(base), "--printer", "testk1",
        "--preset-dir", str(presets), "--yes", "--json",
    )  # fmt: skip
    assert res.returncode == 0, res.stdout + res.stderr
    with zipfile.ZipFile(out) as zf:
        settings = json.loads(zf.read("Metadata/project_settings.config"))
        embedded = json.loads(zf.read("Metadata/3d_assembly.json"))
        tags = zf.read("Metadata/model_settings.config").decode()
    assert settings["printable_area"] == ["0x0", "220x0", "220x220", "0x220"], (
        "retargeted to the K1-like printer"
    )
    assert [m["name"] for m in embedded["spec"]["mates"]] == [
        "board_ribs"
    ] and 'value="printed"' in tags
    check = cli(tmp_path, "check", str(out), "--json")
    assert check.returncode == 0, check.stdout
    rep = as_json(check)
    assert (
        rep["summary"]["allowed"] == 2
        and not rep["drift"]
        and rep["source_3mf"].endswith("plates.3mf")
    )
    plain = cli(
        tmp_path,
        "position",
        str(spec),
        "--3mf",
        str(tmp_path / "out" / "geo.3mf"),
        "--yes",
    )
    assert plain.returncode == 0
    retarget = run_cli(
        tmp_path,
        "retarget",
        str(tmp_path / "out" / "geo.3mf"),
        "--to",
        "testk1",
        "--preset-dir",
        str(presets),
        env_extra=ENV,
    )
    assert retarget.returncode == 2 and "not a slicer project" in retarget.stderr, (
        "without --base-project there are no slicer settings to retarget"
    )
    no_base = cli(
        tmp_path,
        "position",
        str(spec),
        "--3mf",
        str(tmp_path / "p.3mf"),
        "--printer",
        "testk1",
        "--yes",
    )
    assert no_base.returncode == 2 and "--base-project" in no_base.stderr


def test_arrange_leaves_stand_ins_off_the_plates_and_retarget_refuse_them(
    tmp_path: Path,
) -> None:
    """The gates, not a note in AGENTS.md: a tagged non-printable object stops at arrange / retarget."""
    spec = write_spec(tmp_path, board_in_case(tmp_path))
    all_parts = tmp_path / "out" / "all.3mf"
    assert (
        cli(
            tmp_path,
            "position",
            str(spec),
            "--3mf",
            str(all_parts),
            "--include-non-printable",
            "--yes",
        ).returncode
        == 0
    )

    arranged = run_cli(
        tmp_path,
        "arrange",
        str(all_parts),
        "-o",
        str(tmp_path / "arr.3mf"),
        "--bed",
        "220",
        env_extra=ENV,
        timeout=300,
    )
    if arranged.returncode != 0 and "lxml" in arranged.stderr:
        pytest.skip(
            "arrange re-reads its output with trimesh, whose 3MF loader needs lxml (not installed here)"
        )
    assert arranged.returncode == 0, arranged.stderr
    assert "NOT placed (non-printable part, role tag): board" in arranged.stdout
    with zipfile.ZipFile(tmp_path / "arr.3mf") as zf:
        names = re.findall(
            r'key="name" value="([^"]+)"',
            zf.read("Metadata/model_settings.config").decode(),
        )
    assert names == ["case"]

    base = make_project(tmp_path / "base.3mf", [Box("part", (30, 20, 10))])
    presets = write_presets(tmp_path / "presets")
    project = tmp_path / "out" / "project.3mf"
    assert (
        cli(
            tmp_path,
            "position",
            str(spec),
            "--3mf",
            str(project),
            "--base-project",
            str(base),
            "--include-non-printable",
            "--yes",
        ).returncode
        == 0
    )
    refused = run_cli(
        tmp_path,
        "retarget",
        str(project),
        "--to",
        "testk1",
        "--preset-dir",
        str(presets),
        env_extra=ENV,
    )
    assert (
        refused.returncode == 1
        and "refusing to print non-printable part(s): board (stand_in)"
        in refused.stderr
    )
    allowed = run_cli(
        tmp_path, "retarget", str(project), "--to", "testk1", "--preset-dir", str(presets), "--include-non-printable",
        "-o", str(tmp_path / "out" / "forced.3mf"), env_extra=ENV,
    )  # fmt: skip
    assert allowed.returncode == 0, allowed.stderr



def test_a_horizontal_drawer_is_checked_with_fit_check_open_plus_y(
    tmp_path: Path,
) -> None:
    """#553: the declared extraction axis works through the real CLI; `above` is not assumed."""
    import numpy as np
    import trimesh
    from assembly.vec import rotation_taking

    _insert, _plate = write_insert(tmp_path / "i.stl"), write_plate(tmp_path / "p.stl")
    m = np.eye(4)
    m[:3, :3] = np.array(rotation_taking((0.0, 0.0, 1.0), (0.0, 1.0, 0.0)))
    for name in ("i", "p"):
        mesh = trimesh.load(tmp_path / f"{name}.stl", force="mesh")
        mesh.apply_transform(m)
        mesh.export(tmp_path / f"{name}_y.stl")
    res = run_cli(
        tmp_path,
        "fit-check",
        "--insert",
        "i_y.stl",
        "--socket",
        "p_y.stl",
        "--gap",
        "0.2",
        "--open",
        "+Y",
        "--json",
        env_extra=ENV,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    fit = json.loads(res.stdout)["fits"][0]
    assert fit["open"] == "+Y" and fit["open_frame"]["direction"] == pytest.approx(
        [0.0, 1.0, 0.0]
    )
    bad = run_cli(
        tmp_path,
        "fit-check",
        "--insert",
        "i_y.stl",
        "--socket",
        "p_y.stl",
        "--gap",
        "0.2",
        "--open",
        "0,0,0",
        env_extra=ENV,
    )
    assert bad.returncode == 2 and "open must be one of" in bad.stderr
