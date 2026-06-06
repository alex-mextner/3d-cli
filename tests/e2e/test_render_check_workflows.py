from __future__ import annotations

from pathlib import Path

from .workflow_helper import png_size, require_working_openscad, run_cli, run_shell


def _asymmetric_model(path: Path) -> None:
    path.write_text(
        """
width = 28; // [10:50]
depth = 18; // [10:50]
height = 12; // [5:30]

difference() {
  union() {
    cube([width, depth, height]);
    translate([width - 7, depth - 4, height]) cube([7, 4, 9]);
  }
  translate([5, 4, 3]) cube([10, 8, height + 2]);
}
""".strip()
        + "\n",
        encoding="utf-8",
    )


def test_render_view_produces_a_named_png_a_user_can_inspect(tmp_path: Path) -> None:
    """A user renders an asymmetric model from a named camera and inspects the PNG artifact."""
    require_working_openscad()
    model = tmp_path / "bracket.scad"
    out = tmp_path / "left.png"
    _asymmetric_model(model)

    result = run_cli(tmp_path, "render", str(model), "--view", "left", "--size", "320x240", "-o", str(out))

    assert result.returncode == 0, result.stderr
    assert out.exists()
    assert out.stat().st_size > 1_000
    assert png_size(out) == (320, 240)


def test_render_multi_creates_a_preview_set_with_expected_view_names(tmp_path: Path) -> None:
    """A user creates a documentation preview folder and verifies every standard view exists."""
    require_working_openscad()
    model = tmp_path / "fixture.scad"
    previews = tmp_path / "previews"
    _asymmetric_model(model)

    result = run_cli(tmp_path, "render", str(model), "--multi", str(previews), "--size", "220x160")

    assert result.returncode == 0, result.stderr
    rendered = {path.name for path in previews.glob("*.png")}
    assert rendered == {
        "fixture_back.png",
        "fixture_front.png",
        "fixture_iso.png",
        "fixture_left.png",
        "fixture_right.png",
        "fixture_top.png",
    }
    assert all(png_size(path) == (220, 160) for path in previews.glob("*.png"))


def test_check_mesh_selector_reports_only_the_requested_gate(tmp_path: Path) -> None:
    """A user runs the fast mesh gate alone before spending time on heavier checks."""
    require_working_openscad()
    model = tmp_path / "simple.scad"
    _asymmetric_model(model)

    result = run_cli(tmp_path, "check", str(model), "--mesh")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "selected gates: manifold" in result.stdout
    assert "[MANIFOLD]" in result.stdout
    assert "[CONSISTENCY]" not in result.stdout
    assert "[PRINTABILITY]" not in result.stdout
    assert ">>> CHECK: PASS" in result.stdout


def test_rendered_view_can_feed_a_later_shell_report(tmp_path: Path) -> None:
    """A user chains render output into a shell-side artifact manifest."""
    require_working_openscad()
    model = tmp_path / "part.scad"
    _asymmetric_model(model)

    result = run_shell(
        "\n".join(
            [
                "set -eu",
                f'"$PYTHON" "$THREED" render "{model}" --view 3-4 --size 180x120 -o hero.png',
                '"$PYTHON" -c \'import pathlib; p=pathlib.Path("hero.png"); '
                'print(f"hero.png:{p.exists()}:{p.stat().st_size}")\' > manifest.txt',
            ]
        ),
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    manifest = (tmp_path / "manifest.txt").read_text(encoding="utf-8").strip()
    name, exists, size = manifest.split(":")
    assert (name, exists) == ("hero.png", "True")
    assert int(size) > 1_000
