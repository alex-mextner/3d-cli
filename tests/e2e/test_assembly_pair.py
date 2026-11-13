"""Readable e2e stories for `3d assembly pair`: does a plug screw into a rim, on the model and as printed?

Every story runs the real `bin/3d` on synthetic threads (tests/pair_fixtures.py): the rim's bore is the plug's thread plus a
0.5 mm radial gap at the aligned phase. No printer, slicer or network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pair_fixtures as PF
import pytest

from .workflow_helper import run_cli

FEATURE = [
    "--a-side",
    "inner",
    "--b-side",
    "outer",
    "--pitch",
    "3",
    "--axis-point",
    "0,0,0",
]


@pytest.fixture(scope="module")
def parts(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    tmp = tmp_path_factory.mktemp("pair")
    bore, boss = PF.thread_pair(tmp, gap=0.5, amplitude=0.8, pitch=3.0)
    tight_dir = tmp / "tight"
    tight_dir.mkdir()
    tight_bore, tight_boss = PF.thread_pair(
        tight_dir, gap=0.05, amplitude=0.8, pitch=3.0
    )
    gcode = PF.write_wall_gcode(
        tmp / "plate.gcode",
        [
            ("Rim", PF.load(bore), (110.0, 90.0)),
            ("Plug", PF.load(boss), (60.0, 40.0)),
        ],
    )
    return {
        "bore": bore,
        "boss": boss,
        "tight_bore": tight_bore,
        "tight_boss": tight_boss,
        "gcode": gcode,
    }


def pair(tmp_path: Path, *args: str) -> Any:
    return run_cli(tmp_path, "assembly", "pair", *args, timeout=300)


def as_json(res: Any) -> dict[str, Any]:
    assert res.stdout.strip(), res.stderr
    doc: dict[str, Any] = json.loads(res.stdout)
    assert doc["schema"] == "3d.assembly/1" and doc["kind"] == "pair"
    return doc


def help_entry(lines: list[str], flag: str) -> list[str]:
    """The help lines of one options entry (or the `pair` subcommand): its header plus the indented continuation."""
    prefix = "  pair " if flag == "pair" else "  --"
    start = next(
        (i for i, ln in enumerate(lines) if ln.startswith(prefix) and flag in ln.split(" Example")[0]),
        None,
    )
    assert start is not None, flag
    block = [lines[start]]
    for ln in lines[start + 1 :]:
        if not ln.startswith("      ") or not ln.strip():  # the next flag / subcommand / blank line ends the entry
            break
        block.append(ln)
    return block


def test_help_documents_pair_and_every_flag_with_an_example(tmp_path: Path) -> None:
    res = run_cli(tmp_path, "assembly", "--help")
    assert res.returncode == 0
    lines = res.stdout.splitlines()
    for flag in (
        "pair", "--scale-a", "--scale-b", "--pose-a", "--pose-b", "--axis", "--axis-point", "--a-side", "--b-side", "--a-z",
        "--b-z", "--a-r", "--b-r", "--pitch", "--hand", "--feature-name", "--features", "--min-radial-gap", "--fit", "--shape",
        "--nozzle", "--layer", "--min-clearance", "--zone", "--max-overlap-mm3", "--seat-tol", "--screw-to-best", "--gcode-a",
        "--gcode-b", "--object-a", "--object-b", "--print-a", "--print-b", "--skip-below", "--max-faces",
    ):  # fmt: skip
        assert any("Example: 3d assembly pair" in ln for ln in help_entry(lines, flag)), flag


def test_a_rim_and_plug_that_mate_pass_with_the_radial_gap_of_the_thread(
    tmp_path: Path, parts: dict[str, Path]
) -> None:
    res = pair(
        tmp_path,
        str(parts["bore"]),
        str(parts["boss"]),
        *FEATURE,
        "--min-radial-gap",
        "0.2",
        "--json",
    )
    assert res.returncode == 0, res.stderr
    doc = as_json(res)
    assert doc["ok"] and doc["interpenetration"]["volume_mm3"] == 0.0
    cl = doc["features"][0]["model"]["clearance"]
    assert cl["best_phase_min_gap_mm"] == pytest.approx(0.5, abs=0.03)
    assert cl["free_play"]["value"] == pytest.approx(0.5 / 0.8 * 3.0, abs=0.15)
    text = pair(tmp_path, str(parts["bore"]), str(parts["boss"]), *FEATURE).stdout
    assert (
        "feature feature (thread, pitch 3.000 mm): PASS" in text
        and "right-handed" in text
    )
    assert "interpenetration: PASS" in text


def test_a_plug_that_is_too_tight_exits_1_and_says_which_gap(
    tmp_path: Path, parts: dict[str, Path]
) -> None:
    res = pair(
        tmp_path,
        str(parts["tight_bore"]),
        str(parts["tight_boss"]),
        *FEATURE,
        "--min-radial-gap",
        "0.2",
    )
    assert res.returncode == 1
    assert (
        "best-phase radial gap" in res.stdout
        and "FAILED: feature:feature" in res.stdout
    )


def test_a_plug_posed_in_the_wrong_phase_collides_until_screwed_to_best(
    tmp_path: Path, parts: dict[str, Path]
) -> None:
    args = [
        str(parts["bore"]),
        str(parts["boss"]),
        *FEATURE,
        "--pose-b",
        "{rotate: [0, 0, 180]}",
    ]
    blocked = pair(tmp_path, *args)
    assert blocked.returncode == 1
    assert "interpenetration: FAIL" in blocked.stdout and "move B" in blocked.stdout
    screwed = pair(tmp_path, *args, "--screw-to-best", "--json")
    assert screwed.returncode == 0, screwed.stdout + screwed.stderr
    assert as_json(screwed)["screw_to_best"]["applied_to_b"][
        "rotate_deg"
    ] == pytest.approx(-180.0, abs=3.0)


def test_negative_window_bounds_are_accepted_as_separate_values(
    tmp_path: Path, parts: dict[str, Path]
) -> None:
    res = pair(
        tmp_path, str(parts["bore"]), str(parts["boss"]), *FEATURE, "--pose-b", "{translate: [0, 0, -3]}",
        "--a-z", "-3:6", "--b-z", "-3:6", "--json",
    )  # fmt: skip  (one pitch down: the same thread phase, so it still screws)
    assert res.returncode == 0, res.stderr
    assert as_json(res)["features"][0]["windows"]["a"]["z"] == [-3.0, 6.0]


def test_the_printed_walls_are_read_from_the_gcode_by_object_name(
    tmp_path: Path, parts: dict[str, Path]
) -> None:
    res = pair(
        tmp_path, str(parts["bore"]), str(parts["boss"]), *FEATURE, "--gcode-a", str(parts["gcode"]), "--object-a", "Rim",
        "--gcode-b", str(parts["gcode"]), "--object-b", "plug", "--json",
    )  # fmt: skip
    assert res.returncode == 0, res.stderr
    doc = as_json(res)
    assert doc["printed"]["a"]["placement"]["shift_applied_mm"] == pytest.approx(
        [110.0, 90.0], abs=0.02
    )
    assert doc["printed"]["b"]["placement"]["shift_applied_mm"] == pytest.approx(
        [60.0, 40.0], abs=0.02
    )
    assert doc["features"][0]["as_printed"]["clearance"][
        "best_phase_min_gap_mm"
    ] == pytest.approx(0.5, abs=0.04)
    text = pair(
        tmp_path,
        str(parts["bore"]),
        str(parts["boss"]),
        *FEATURE,
        "--gcode-a",
        str(parts["gcode"]),
        "--object-a",
        "Rim",
    ).stdout
    assert (
        "as printed A:" in text
        and "wall vs model: max" in text
        and "[as printed (A, rest model)]" in text
    )


def test_a_wrong_object_name_exits_2_and_lists_the_objects_in_the_gcode(
    tmp_path: Path, parts: dict[str, Path]
) -> None:
    res = pair(
        tmp_path,
        str(parts["bore"]),
        str(parts["boss"]),
        *FEATURE,
        "--gcode-a",
        str(parts["gcode"]),
        "--object-a",
        "Nope",
    )
    assert res.returncode == 2
    assert "Rim_id_0_copy_0, Plug_id_0_copy_0" in res.stderr and "--object-a" in res.stderr


def test_features_come_from_a_file_and_usage_errors_exit_2(
    tmp_path: Path, parts: dict[str, Path]
) -> None:
    feats = tmp_path / "features.yaml"
    feats.write_text(
        "features:\n  - {name: rim_thread, pitch: 3, a: {side: inner}, b: {side: outer}, min_gap_mm: 0.2}\n"
    )
    ok = pair(
        tmp_path,
        str(parts["bore"]),
        str(parts["boss"]),
        "--features",
        str(feats),
        "--axis-point",
        "0,0,0",
        "--json",
    )
    assert ok.returncode == 0, ok.stderr
    assert as_json(ok)["features"][0]["name"] == "rim_thread"
    for argv, needle in (
        ([str(parts["bore"])], "needs two parts"),
        ([str(parts["bore"]), str(parts["boss"]), "--a-side", "inner"], "--b-side"),
        (
            [
                str(parts["bore"]),
                str(parts["boss"]),
                "--features",
                str(feats),
                "--pitch",
                "3",
            ],
            "cannot be combined",
        ),
        (
            [str(parts["bore"]), str(parts["boss"]), *FEATURE, "--a-z", "9:1"],
            "LO <= HI",
        ),
        ([str(parts["bore"]), str(tmp_path / "missing.stl")], "missing.stl"),
        (
            [str(parts["bore"]), str(parts["boss"]), "--pose-b", "{rotate: [1, 2"],
            "--pose-b",
        ),
    ):
        res = pair(tmp_path, *argv)
        assert res.returncode == 2, (argv, res.stdout, res.stderr)
        assert needle in res.stderr, (argv, res.stderr)
