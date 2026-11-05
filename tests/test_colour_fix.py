"""Unit tests for lib/printer_ops/colour_fix.py: the CIEDE2000 slot colour tolerance and the rewrite
that aligns a G-code, its project 3MF, manifest and slice-warnings sidecar with the loaded colour
(issue https://github.com/alex-mextner/3d-cli/issues/728)."""

from __future__ import annotations

import hashlib
import json
import os
import zipfile
from pathlib import Path

import pytest
from colour_fix_fixtures import (
    LOADED,
    SLICED,
    THUMBNAILS,
    TOOLPATH,
    gcode_text,
    load_beige_petg,
    write_plate,
    write_project,
)
from printer_fakes import FakeMoonraker

from errors import InvalidArgument
from printer_ops import colour_fix, moonraker, preflight
from printer_ops.base import PrinterRefused, PrinterStatus
from slicing import slice_warnings as sw


def status(colour: str = LOADED) -> PrinterStatus:
    moon = FakeMoonraker()
    load_beige_petg(moon, colour=colour)
    return moonraker.parse_status(moon.status)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---- the distance ------------------------------------------------------------------------


def test_the_real_beige_pair_is_close_and_other_colours_are_far() -> None:
    assert colour_fix.colour_delta(SLICED, LOADED) == pytest.approx(6.4, abs=0.1)
    assert colour_fix.colour_delta("#FFFFFF", SLICED) == pytest.approx(16.7, abs=0.1)
    assert colour_fix.colour_delta("#FF0000", "#0000FF") == pytest.approx(52.9, abs=0.1)
    assert colour_fix.colour_delta(SLICED, SLICED) == 0.0
    assert colour_fix.colour_delta("nonsense", SLICED) is None


@pytest.mark.parametrize("bad", ["0", "-3", "abc", "nan"])
def test_delta_e_option_must_be_a_positive_number(bad: str) -> None:
    with pytest.raises(InvalidArgument):
        colour_fix.parse_delta_e(
            bad, "--colour-delta-e", 10.0, command="print-preflight"
        )
    assert colour_fix.parse_delta_e(None, "--colour-delta-e", 10.0, command="x") == 10.0
    assert colour_fix.parse_delta_e("7.5", "--colour-delta-e", 10.0, command="x") == 7.5


# ---- the slot check ----------------------------------------------------------------------


def slots_of(
    tmp_path: Path, text: str, st: PrinterStatus, **kw: float
) -> preflight.Check:
    g = tmp_path / "p.gcode"
    g.write_text(text, encoding="utf-8")
    (check,) = preflight.check_slots(preflight.read_gcode_meta(g), st, **kw)
    return check


def test_same_family_beige_passes_and_reports_the_distance(tmp_path: Path) -> None:
    check = slots_of(tmp_path, gcode_text(), status())
    assert check.status == "PASS"
    assert (
        "ΔE 6.4" in check.detail and SLICED in check.detail and "PETG" in check.detail
    )


def test_a_different_colour_fails_with_the_distance_and_the_fix(tmp_path: Path) -> None:
    check = slots_of(tmp_path, gcode_text("#FFFFFF"), status())
    assert check.status == "FAIL"
    assert "G-code #FFFFFF, loaded #FEE5A5 (ΔE 20.0 > 10)" in check.detail
    assert "--fix" in check.detail


def test_the_threshold_is_configurable(tmp_path: Path) -> None:
    assert (
        slots_of(tmp_path, gcode_text(), status(), colour_delta_e=5.0).status == "FAIL"
    )
    assert (
        slots_of(tmp_path, gcode_text("#FFFFFF"), status(), colour_delta_e=25.0).status
        == "PASS"
    )


def test_a_material_mismatch_stays_a_fail_even_for_the_same_colour(
    tmp_path: Path,
) -> None:
    check = slots_of(tmp_path, gcode_text(SLICED, "PLA"), status())
    assert (
        check.status == "FAIL" and "material: G-code PLA, loaded PETG" in check.detail
    )
    assert (
        slots_of(tmp_path, gcode_text(LOADED, "PLA"), status(colour=LOADED)).status
        == "FAIL"
    )


# ---- the plan ----------------------------------------------------------------------------


def test_plan_edits_the_used_tool_and_refuses_another_colour(tmp_path: Path) -> None:
    g = write_plate(tmp_path)
    edits, refusals = colour_fix.plan_colour_fix(preflight.read_gcode_meta(g), status())
    assert refusals == [] and [(e.tool, e.label, e.old, e.new) for e in edits] == [
        (3, "T4", SLICED, LOADED)
    ]
    assert edits[0].delta_e == pytest.approx(6.4, abs=0.1)

    edits, refusals = colour_fix.plan_colour_fix(
        preflight.read_gcode_meta(g), status(colour="#0000FF")
    )
    assert edits == [] and len(refusals) == 1
    assert "tool T3 (T4)" in refusals[0] and "another colour" in refusals[0]
    # the operator may widen the limit on purpose
    edits, _ = colour_fix.plan_colour_fix(
        preflight.read_gcode_meta(g), status(colour="#0000FF"), max_delta_e=100.0
    )
    assert [e.new for e in edits] == ["#0000FF"]


def test_plan_leaves_matching_and_empty_heads_alone(tmp_path: Path) -> None:
    g = write_plate(tmp_path)
    assert colour_fix.plan_colour_fix(
        preflight.read_gcode_meta(g), status(colour=SLICED)
    ) == ([], [])
    moon = FakeMoonraker()
    moon.status["print_task_config"]["filament_exist"][3] = False
    empty = moonraker.parse_status(moon.status)
    assert colour_fix.plan_colour_fix(preflight.read_gcode_meta(g), empty) == ([], [])


# ---- the rewrite -------------------------------------------------------------------------


def fix(
    g: Path, st: PrinterStatus | None = None, source: Path | None = None
) -> colour_fix.FixResult:
    result = colour_fix.fix_to_loaded(
        g, st or status(), source, command="print-preflight"
    )
    assert result is not None
    return result


def test_fix_rewrites_gcode_3mf_manifest_and_sidecar_consistently(
    tmp_path: Path,
) -> None:
    g = write_plate(tmp_path)
    project, manifest, side = (
        g.with_suffix(".3mf"),
        tmp_path / "plate.manifest.json",
        sw.sidecar_path(g),
    )
    old_g, old_p = sha(g), sha(project)
    g.chmod(0o640)
    os.utime(g, (1_700_000_000, 1_700_000_000))
    os.utime(project, (1_700_000_100, 1_700_000_100))
    model_before = zipfile.ZipFile(project).read("3D/3dmodel.model")

    result = fix(g)

    # G-code: only the colour entries of tool 3 changed, the toolpath bytes did not
    text = g.read_text(encoding="utf-8")
    assert text.startswith(
        "; HEADER_BLOCK_START\n; HEADER_BLOCK_END\n" + THUMBNAILS + TOOLPATH
    )
    assert f"; filament_colour = #000000;#808080;#1B5E20;{LOADED}\n" in text
    assert f"; extruder_colour = #000000;#808080;#1B5E20;{LOADED}\n" in text
    assert "; filament_type = PLA;PLA;PLA;PETG\n" in text
    assert (g.stat().st_mode & 0o777, g.stat().st_mtime_ns) == (
        0o640,
        1_700_000_000 * 10**9,
    )
    assert project.stat().st_mtime_ns == 1_700_000_100 * 10**9
    # the reported hashes are the real ones
    assert (result.gcode.old_sha256, result.gcode.new_sha256) == (
        old_g,
        sha(g),
    ) and old_g != sha(g)
    assert result.project is not None and (
        result.project.old_sha256,
        result.project.new_sha256,
    ) == (old_p, sha(project))
    # 3MF: colour lists + plate json updated, the other members untouched
    with zipfile.ZipFile(project) as z:
        cfg = json.loads(z.read("Metadata/project_settings.config"))
        assert cfg["filament_colour"][3] == cfg["extruder_colour"][3] == LOADED
        assert (
            cfg["filament_colour"][:3] == ["#000000", "#808080", "#1B5E20"]
            and cfg["layer_height"] == "0.2"
        )
        assert (
            z.read("Metadata/project_settings.config")
            .decode()
            .startswith('{\n    "filament_colour"')
        )
        assert (
            json.loads(z.read("Metadata/plate_1.json"))["filament_colors"][3] == LOADED
        )
        assert z.read("3D/3dmodel.model") == model_before and z.testzip() is None
    # manifest + slice-warnings sidecar follow
    assert [f["hex"] for f in json.loads(manifest.read_text())["filaments"]][
        3
    ] == LOADED
    data = json.loads(side.read_text(encoding="utf-8"))
    assert (
        data["gcode_sha256"] == sha(g)
        and data["post_processed"][-1]["step"] == "colour fix"
    )
    assert sw.evaluate(g)[0] == "PASS"
    assert result.manifest == manifest and result.sidecar == side
    lines = "\n".join(result.lines())
    assert (
        f"{SLICED} -> {LOADED} (ΔE 6.4)" in lines and old_g in lines and sha(g) in lines
    )
    # nothing is left behind and a second run finds nothing to do
    assert [
        p.name for p in tmp_path.iterdir() if p.name.startswith(".colour-fix")
    ] == []
    assert (
        colour_fix.fix_to_loaded(g, status(), None, command="print-preflight") is None
    )


def test_fix_uses_an_explicit_project_and_tolerates_a_non_project_3mf(
    tmp_path: Path,
) -> None:
    g = write_plate(tmp_path)
    other = tmp_path / "other.3mf"
    write_project(other)
    assert fix(g, source=other).project is not None
    assert (
        json.loads(zipfile.ZipFile(other).read("Metadata/project_settings.config"))[
            "filament_colour"
        ][3]
        == LOADED
    )
    assert (
        json.loads(
            zipfile.ZipFile(g.with_suffix(".3mf")).read(
                "Metadata/project_settings.config"
            )
        )["filament_colour"][3]
        == SLICED
    )  # the sibling was not the project asked for

    h = write_plate(tmp_path, "model")
    with zipfile.ZipFile(h.with_suffix(".3mf"), "w") as z:
        z.writestr(
            "3D/3dmodel.model", "<model/>"
        )  # a plain model, not a slicer project
    result = fix(h)
    assert result.project is None and "not a slicer project 3MF" in "\n".join(
        result.lines()
    )
    assert LOADED in h.read_text(encoding="utf-8")


def test_fix_without_a_project_rewrites_only_the_gcode(tmp_path: Path) -> None:
    g = tmp_path / "solo.gcode"
    g.write_text(gcode_text(), encoding="utf-8")
    result = fix(g)
    assert result.project is None and result.manifest is None and result.sidecar is None
    assert "no project 3MF found" in "\n".join(result.lines())


def test_fix_refuses_another_colour_and_writes_nothing(tmp_path: Path) -> None:
    g = write_plate(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(PrinterRefused, match="another colour"):
        colour_fix.fix_to_loaded(
            g, status(colour="#0000FF"), None, command="print-preflight"
        )
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_fix_without_a_colour_line_refuses_before_touching_the_3mf(
    tmp_path: Path,
) -> None:
    g = write_plate(tmp_path)
    g.write_text(
        TOOLPATH
        + "; filament used [mm] = 0.00, 0.00, 0.00, 30.50\n; filament_type = PLA;PLA;PLA;PETG\n"
    )
    project = g.with_suffix(".3mf")
    before = project.read_bytes()
    edits = [colour_fix.ColourEdit(3, "T4", SLICED, LOADED, 6.4)]
    with pytest.raises(PrinterRefused, match="no `; filament_colour`"):
        colour_fix.apply_colour_fix(g, project, edits)
    assert project.read_bytes() == before


def test_fix_refuses_a_printer_without_per_head_data(tmp_path: Path) -> None:
    g = write_plate(tmp_path)
    with pytest.raises(PrinterRefused, match="no per-head filament data"):
        colour_fix.fix_to_loaded(
            g, PrinterStatus("idle", "standby"), None, command="print-preflight"
        )


def test_fix_keeps_quotes_crlf_and_the_other_entries(tmp_path: Path) -> None:
    g = tmp_path / "crlf.gcode"
    g.write_bytes(
        b"G28\r\nT3\r\n"
        b'; filament_colour = "#000000";"#808080";"#1B5E20";"#E8D8B0FF"\r\n'
        b"; filament used [mm] = 0.00, 0.00, 0.00, 30.50\r\n"
        b"; filament_type = PLA;PLA;PLA;PETG\r\n"
    )
    fix(g)
    assert (
        b'; filament_colour = "#000000";"#808080";"#1B5E20";"#FEE5A5"\r\n'
        in g.read_bytes()
    )
    assert g.read_bytes().count(b"\r\n") == 5 and g.read_bytes().startswith(
        b"G28\r\nT3\r\n"
    )
