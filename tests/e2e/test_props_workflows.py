from __future__ import annotations

import json
import zipfile
from pathlib import Path

from props_fixtures import box, write_orca_3mf, write_stl

from .workflow_helper import run_cli, run_shell


def test_props_adds_ribs_as_a_part_of_the_wall_and_stops_an_unstable_one(tmp_path: Path) -> None:
    """A user about to print a two-colour wall panel standing adds snap-off props to its back:
    the output 3MF keeps the props as a third part of the same object. A tall thin slab with
    short ribs is too tall for its base: the gate fails with the ratio and writes nothing."""
    write_orca_3mf(tmp_path / "wall.3mf",
                   [("wall", [("panel", box(0, 0, 0, 120, 4, 60), 2), ("sign", box(30, 4, 20, 90, 5, 30), 3)])])

    ok = run_cli(tmp_path, "props", "wall.3mf", "--back", "+y", "-o", "wall_props.3mf")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "ribs     3 at x = 10, 60, 110 mm" in ok.stdout
    assert ">>> PROPS: PASS; wrote wall_props.3mf" in ok.stdout
    with zipfile.ZipFile(tmp_path / "wall_props.3mf") as zf:
        model = zf.read("3D/3dmodel.model").decode()
        cfg = zf.read("Metadata/model_settings.config").decode()
    assert model.count("<item ") == 1 and model.count("<component ") == 3
    assert 'value="wall props"' in cfg and cfg.count("<part ") == 3

    write_stl(tmp_path / "post.stl", box(0, 0, 0, 20, 2, 80))
    bad = run_cli(tmp_path, "props", "post.stl", "--back", "-y", "--height", "0.2", "-o", "post_props.stl")
    assert bad.returncode == 1
    assert ">>> PROPS: FAIL height/base ratio 5.56 > 3" in bad.stdout
    assert not (tmp_path / "post_props.stl").exists()


def test_props_json_report_feeds_a_preflight_script(tmp_path: Path) -> None:
    """A pre-print script saves the stability report as JSON and greps the verdict line."""
    write_stl(tmp_path / "wall.stl", box(0, 0, 0, 120, 4, 60))

    result = run_shell(
        "\n".join([
            '"$PYTHON" "$THREED" props wall.stl --back +y --json > report.json',
            '"$PYTHON" "$THREED" props wall.stl --back +y | grep ">>> PROPS" > verdict.txt',
        ]),
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["status"] == "PASS" and report["stations"] == 3 and report["output"] is None
    assert report["ratio"] < report["max_ratio"]
    assert (tmp_path / "verdict.txt").read_text().strip() == ">>> PROPS: PASS"
