"""Tests for `3d slice-check` — headless 3MF open/plate-count/slice verification.

These run fully offline: a FAKE slicer binary (a tiny python script) stands in for the
real Orca CLI, and synthetic 3MF zips stand in for project/bare-mesh files. No real
slicer, no GUI, no network.
"""
from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from errors import GateFailure, InputNotFound, MissingDependency, UsageError


# ---------------------------------------------------------------------------
# Fixtures: synthetic 3MF files + a fake slicer binary.
# ---------------------------------------------------------------------------
def _bare_mesh_3mf(path: Path) -> Path:
    """A 3MF with only geometry (no plate metadata) — one implicit plate."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("_rels/.rels", "<Relationships/>")
        zf.writestr("3D/3dmodel.model", "<model/>")
    return path


def _project_3mf(path: Path, n_plates: int) -> Path:
    """An Orca project 3MF with n plate thumbnails (+ a no_light decoy per plate)."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
        zf.writestr("Metadata/project_settings.config", "{}")
        zf.writestr("Metadata/slice_info.config", "<config/>")
        for i in range(1, n_plates + 1):
            zf.writestr(f"Metadata/plate_{i}.png", b"\x89PNG")
            # no_light decoys must NOT be counted:
            zf.writestr(f"Metadata/plate_no_light_{i}.png", b"\x89PNG")
    return path


def _unsliced_project_3mf(path: Path, n_plates: int) -> Path:
    """An UNSLICED Orca project: plate list lives only in model_settings.config (no PNGs)."""
    plates = "".join(
        f'<plate><metadata key="plater_id" value="{i}"/></plate>'
        for i in range(1, n_plates + 1)
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
        zf.writestr("Metadata/project_settings.config", "{}")
        zf.writestr("Metadata/model_settings.config", f"<config>{plates}</config>")
    return path


def _fake_slicer(tmp_path: Path, *, plates: int = 1, info_ok: bool = True, slice_rc: int = 0) -> Path:
    """A fake Orca CLI: handles --info and --slice, writes plate_N.gcode to --outputdir."""
    slicer = tmp_path / "fake_orca.py"
    info_block = (
        "print('size_x = 10.0'); print('number_of_parts =  1'); print('manifold = yes')"
        if info_ok
        else "print('error: cannot parse')"
    )
    slicer.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "args = sys.argv[1:]\n"
        f"PLATES = {plates}\n"
        f"SLICE_RC = {slice_rc}\n"
        "if '--info' in args:\n"
        f"    {info_block}\n"
        "    raise SystemExit(0)\n"
        "if '--slice' in args:\n"
        "    od = args[args.index('--outputdir') + 1]\n"
        "    os.makedirs(od, exist_ok=True)\n"
        "    if SLICE_RC == 0:\n"
        "        for i in range(1, PLATES + 1):\n"
        "            open(os.path.join(od, f'plate_{i}.gcode'), 'w').write('; gcode\\n')\n"
        "    print('run, Finished' if SLICE_RC == 0 else 'run found error, exit')\n"
        "    raise SystemExit(SLICE_RC)\n"
        "raise SystemExit(2)\n"
    )
    slicer.chmod(0o755)
    return slicer


# ---------------------------------------------------------------------------
# Plate-count detection (pure zip introspection, no slicer).
# ---------------------------------------------------------------------------
def test_detect_plate_count_bare_mesh_is_one(tmp_path: Path) -> None:
    import commands.slice_check as sc

    f = _bare_mesh_3mf(tmp_path / "bare.3mf")
    assert sc.detect_plate_count(str(f)) == 1


def test_detect_plate_count_project_counts_plate_pngs(tmp_path: Path) -> None:
    import commands.slice_check as sc

    f = _project_3mf(tmp_path / "proj.3mf", n_plates=4)
    # 4 plates despite 4 no_light decoys present.
    assert sc.detect_plate_count(str(f)) == 4


def test_detect_plate_count_unsliced_project_uses_model_settings(tmp_path: Path) -> None:
    import commands.slice_check as sc

    # An unsliced project has no plate_N.png — the count comes from model_settings.config.
    f = _unsliced_project_3mf(tmp_path / "unsliced.3mf", n_plates=3)
    assert sc.detect_plate_count(str(f)) == 3


def test_detect_plate_count_non_3mf_is_one(tmp_path: Path) -> None:
    import commands.slice_check as sc

    stl = tmp_path / "part.stl"
    stl.write_text("solid x\nendsolid x\n")
    assert sc.detect_plate_count(str(stl)) == 1


def test_detect_plate_count_corrupt_zip_is_one(tmp_path: Path) -> None:
    import commands.slice_check as sc

    f = tmp_path / "broken.3mf"
    f.write_text("not a zip")
    assert sc.detect_plate_count(str(f)) == 1


# ---------------------------------------------------------------------------
# Argument parsing.
# ---------------------------------------------------------------------------
def test_help_explains_checks(capsys: pytest.CaptureFixture[str]) -> None:
    import commands.slice_check as sc

    assert sc.run(["--help"]) == 0
    out = capsys.readouterr().out
    assert "--no-slice" in out
    assert "--plates" in out
    assert "PLATES" in out and "SLICE" in out and "OPEN" in out
    assert "--slice 0" in out


def test_no_args_prints_usage_and_fails() -> None:
    import commands.slice_check as sc

    assert sc.run([]) == 1


def test_option_before_model_is_usage_error() -> None:
    import commands.slice_check as sc

    with pytest.raises(UsageError):
        sc.run(["--no-slice", "model.3mf"])


def test_unknown_option_is_usage_error(tmp_path: Path) -> None:
    import commands.slice_check as sc

    f = _bare_mesh_3mf(tmp_path / "m.3mf")
    with pytest.raises(UsageError):
        sc.run([str(f), "--bogus"])


def test_missing_input_raises_input_not_found(tmp_path: Path) -> None:
    import commands.slice_check as sc

    with pytest.raises(InputNotFound):
        sc.run([str(tmp_path / "nope.3mf")])


def test_no_slicer_found_raises_missing_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import commands.slice_check as sc

    f = _bare_mesh_3mf(tmp_path / "m.3mf")
    monkeypatch.delenv("SLICER", raising=False)
    monkeypatch.setattr(sc, "_find_info_binary", lambda _forced: None)
    with pytest.raises(MissingDependency) as got:
        sc.run([str(f)])
    assert got.value.exit_code == 127


# ---------------------------------------------------------------------------
# End-to-end with a fake slicer binary.
# ---------------------------------------------------------------------------
def test_no_slice_reports_open_and_plate_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import commands.slice_check as sc

    f = _project_3mf(tmp_path / "proj.3mf", n_plates=3)
    monkeypatch.setenv("SLICER", str(_fake_slicer(tmp_path)))

    assert sc.run([str(f), "--no-slice"]) == 0
    out = capsys.readouterr().out
    assert "OPEN:   PASS" in out
    assert "PLATES: 3" in out
    assert "SLICE:  skipped" in out
    assert "STATUS: PASS" in out


def test_full_slice_passes_when_gcode_produced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import commands.slice_check as sc

    f = _project_3mf(tmp_path / "proj.3mf", n_plates=2)
    monkeypatch.setenv("SLICER", str(_fake_slicer(tmp_path, plates=2)))

    assert sc.run([str(f)]) == 0
    out = capsys.readouterr().out
    assert "SLICE:  PASS  2 plate g-code produced" in out
    assert "STATUS: PASS - opens OK, 2 plate(s)" in out


def test_slice_fails_when_slicer_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import commands.slice_check as sc

    f = _project_3mf(tmp_path / "proj.3mf", n_plates=1)
    monkeypatch.setenv("SLICER", str(_fake_slicer(tmp_path, slice_rc=1)))

    with pytest.raises(GateFailure):
        sc.run([str(f)])
    out = capsys.readouterr().out
    assert "SLICE:  FAIL" in out
    assert "STATUS: FAIL" in out
    assert "slicer log (tail)" in out


def test_open_fail_is_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import commands.slice_check as sc

    f = _bare_mesh_3mf(tmp_path / "m.3mf")
    monkeypatch.setenv("SLICER", str(_fake_slicer(tmp_path, info_ok=False)))

    with pytest.raises(GateFailure):
        sc.run([str(f), "--no-slice"])
    out = capsys.readouterr().out
    assert "OPEN:   FAIL" in out


def test_plates_assertion_passes_and_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import commands.slice_check as sc

    f = _project_3mf(tmp_path / "proj.3mf", n_plates=4)
    monkeypatch.setenv("SLICER", str(_fake_slicer(tmp_path)))

    assert sc.run([str(f), "--no-slice", "--plates", "4"]) == 0
    assert "PLATES: PASS  4" in capsys.readouterr().out

    with pytest.raises(GateFailure):
        sc.run([str(f), "--no-slice", "--plates", "2"])
    assert "PLATES: FAIL  4 (expected 2)" in capsys.readouterr().out


def test_slice_step_avoids_snapmaker_fork(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import commands.slice_check as sc

    opts = sc._Opts()
    opts.slicer = None
    snap = "/Applications/Snapmaker Orca.app/Contents/MacOS/Snapmaker_Orca"
    orca = str(tmp_path / "OrcaSlicer")
    Path(orca).write_text("#!/bin/sh\n")
    os.chmod(orca, 0o755)
    monkeypatch.setattr(sc, "_SLICE_BINARY_BUNDLES", (orca, snap))
    # Given the Snapmaker fork as the info binary, the slice step must pick the real Orca.
    assert sc._slice_step_binary(opts, snap) == orca


def test_forced_slicer_is_used_for_both_steps(tmp_path: Path) -> None:
    import commands.slice_check as sc

    opts = sc._Opts()
    opts.slicer = "/forced/path"
    assert sc._slice_step_binary(opts, "/forced/path") == "/forced/path"
