from __future__ import annotations

import json
from pathlib import Path

from .workflow_helper import png_size, require_working_openscad, run_cli


def test_fit_niche_emits_a_parametric_insert_a_user_can_inspect(tmp_path: Path) -> None:
    """A user describes a rectangular cavity and gets a tunable insert .scad plus a summary."""
    out = tmp_path / "insert.scad"

    result = run_cli(
        tmp_path, "fit-niche", "--width", "20", "--depth", "16", "--height", "12", "-o", str(out)
    )

    assert result.returncode == 0, result.stderr
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "module insert()" in text
    assert "insert_width = cavity_width - 2 * clearance;" in text
    assert "insert: width=19.6mm" in result.stdout


def test_fit_niche_json_reports_resolved_clearance_and_insert_size(tmp_path: Path) -> None:
    """A user pipes the resolved spec as JSON to confirm the clearance convention and dims."""
    out = tmp_path / "plug.scad"

    result = run_cli(
        tmp_path, "fit-niche", "--shape", "round", "--diameter", "20",
        "--height", "14", "--fit", "snug", "--json", "-o", str(out),
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["clearance"] == 0.10
    assert payload["clearance_convention"] == "radial (uniform gap)"
    assert payload["insert"]["diameter"] == 19.8


def test_fit_niche_insert_renders_and_passes_the_check_gate(tmp_path: Path) -> None:
    """A user generates a plain insert, renders a preview, and confirms it clears `3d check`."""
    require_working_openscad()
    out = tmp_path / "insert.scad"
    run_cli(tmp_path, "fit-niche", "--width", "24", "--depth", "20", "--height", "14", "-o", str(out))

    png = tmp_path / "preview.png"
    render = run_cli(tmp_path, "render", str(out), "--view", "3-4", "--size", "320x240", "-o", str(png))
    assert render.returncode == 0, render.stderr
    assert png_size(png) == (320, 240)

    check = run_cli(tmp_path, "check", str(out))
    assert check.returncode == 0, check.stdout + check.stderr
    assert ">>> CHECK: PASS" in check.stdout


def test_fit_niche_seated_section_shows_the_mating_gap(tmp_path: Path) -> None:
    """A user renders the seated-in-cavity section proof and gets a non-trivial cut PNG."""
    require_working_openscad()
    out = tmp_path / "insert.scad"
    # An exaggerated clearance so the gap is unmistakable in the demo cut.
    run_cli(
        tmp_path, "fit-niche", "--width", "30", "--depth", "24", "--height", "16",
        "--clearance", "1.2", "-o", str(out),
    )

    proof = tmp_path / "proof.png"
    result = run_cli(
        tmp_path, "render", str(out), "--section", "--plane", "XY", "--keep", "neg",
        "-D", "show_cavity=true", "-o", str(proof), "--size", "400x400",
    )

    assert result.returncode == 0, result.stderr
    assert proof.exists()
    # A cut that actually shows the insert-in-cavity is not an empty frame.
    assert proof.stat().st_size > 5_000
    assert png_size(proof) == (400, 400)


def test_fit_niche_check_flag_generates_and_gates_in_one_command(tmp_path: Path) -> None:
    """A user generates and gate-checks a plug in a single `--check` invocation."""
    require_working_openscad()
    out = tmp_path / "insert.scad"

    result = run_cli(
        tmp_path, "fit-niche", "--width", "24", "--depth", "20", "--height", "14",
        "--check", "-o", str(out),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert out.exists()
    # The one command both wrote the summary AND ran the acceptance gate to PASS.
    assert "fit-niche: wrote" in result.stdout
    assert ">>> CHECK: PASS" in result.stdout


def test_fit_niche_render_flag_writes_a_preview_png(tmp_path: Path) -> None:
    """A user asks fit-niche itself to render a preview alongside the .scad."""
    require_working_openscad()
    out = tmp_path / "insert.scad"

    result = run_cli(
        tmp_path, "fit-niche", "--shape", "round", "--diameter", "20", "--height", "14",
        "--lead-in", "--render", "-o", str(out),
    )

    assert result.returncode == 0, result.stderr
    preview = tmp_path / "insert.png"
    assert preview.exists()
    assert preview.stat().st_size > 1_000
    assert "preview:" in result.stdout


def test_fit_niche_rejects_clearance_that_eats_the_cavity(tmp_path: Path) -> None:
    """A user asks for an impossible clearance and gets a structured usage error, not a traceback."""
    result = run_cli(
        tmp_path, "fit-niche", "--width", "1", "--depth", "1", "--height", "8", "--clearance", "0.8"
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    assert "too large" in result.stderr
