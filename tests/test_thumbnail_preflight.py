"""Layers (c) and (d): what `3d print-preflight` says about a missing thumbnail and where `3d printer`
adds it (the end-to-end runs are in tests/e2e/test_thumbnail_layers_workflows.py)."""
from __future__ import annotations

from pathlib import Path

from thumbnail_fixtures import bare

from printer_ops.preflight import FAIL, PASS, check_thumbnails
from slicing import thumbnails

LIB = Path(__file__).resolve().parents[1] / "lib"


def test_preflight_names_the_exact_fix_command_for_the_original_file(
    tmp_path: Path,
) -> None:
    original = bare(tmp_path / "my part.gcode")
    snapshot = tmp_path / "snapshot.gcode"
    snapshot.write_bytes(original.read_bytes())
    check = check_thumbnails(snapshot, original)
    assert check.status == FAIL
    assert (
        f"3d gcode-thumbnails add '{original}'" in check.detail
        and "--fix" in check.detail
    )
    assert "48x48/PNG" in check.detail

    thumbnails.finalize_gcode(snapshot)
    assert check_thumbnails(snapshot, original).status == PASS


def test_printer_upload_and_start_call_the_in_place_fix_before_the_snapshot() -> None:
    source = (LIB / "commands" / "printer.py").read_text(encoding="utf-8")
    assert source.count("_ensure_thumbnails(original)") == 2  # _cmd_upload and _cmd_start
