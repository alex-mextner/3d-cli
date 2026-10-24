"""`3d fits`: list / show / resolve / apply / corrections through the command module."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import fits
from commands import fits as cmd
from errors import InputNotFound, InvalidArgument, UsageError

REPO = Path(__file__).resolve().parents[1]
FLEET = REPO / "fleet" / "printers.yaml"


@pytest.fixture(autouse=True)
def _hermetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "THREED_FITS_CORRECTIONS", str(tmp_path / "fits-corrections.yaml")
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)


def run_json(capsys: pytest.CaptureFixture[str], *argv: str) -> dict[str, Any]:
    assert cmd.run([*argv, "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema"] == "3d.fits/1"
    assert isinstance(doc, dict)
    return doc


def run_text(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    assert cmd.run(list(argv)) == 0
    return capsys.readouterr().out


# --------------------------------------------------------------------------- help / dispatch
def test_no_args_prints_usage_and_returns_1(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        cmd.run([]) == 1
        and "3d fits <list|show|resolve|apply|corrections>" in capsys.readouterr().out
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["list", "--help"],
        ["corrections", "add", "-h"],
        ["corrections", "--help"],
    ],
)
def test_help_is_exit_0_everywhere(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    assert cmd.run(argv) == 0 and "Subcommands:" in capsys.readouterr().out


def test_unknown_subcommand_lists_the_accepted_ones() -> None:
    with pytest.raises(
        InvalidArgument, match="accepted: list, show, resolve, apply, corrections"
    ):
        cmd.run(["frobnicate"])


def test_a_bad_flag_is_a_usage_error_not_an_argparse_exit() -> None:
    with pytest.raises(UsageError, match="unrecognized arguments"):
        cmd.run(["list", "--bogus"])
    with pytest.raises(UsageError, match="invalid float value"):
        cmd.run(["resolve", "--fit", "snap", "--nozzle", "wide"])


def test_every_flag_of_the_usage_text_is_documented_with_an_example() -> None:
    for flag in (
        "--fit",
        "--shape",
        "--printer",
        "--material",
        "--nozzle",
        "--layer",
        "--json",
        "--yes",
        "--delta",
    ):
        line = next(
            ln for ln in cmd.USAGE.splitlines() if ln.lstrip().startswith(flag + " ")
        )
        assert line
    assert cmd.USAGE.count("Example:") >= 12


# --------------------------------------------------------------------------- list / show
def test_list_json_has_every_fit_type_with_its_numbers_and_the_aliases(
    capsys: pytest.CaptureFixture[str],
) -> None:
    doc = run_json(capsys, "list")
    by_name = {ft["name"]: ft for ft in doc["fit_types"]}
    assert list(by_name) == ["snap", "sliding", "tight", "press", "glue"]
    assert (by_name["snap"]["clearance_mm_per_face"], by_name["snap"]["tol_mm"]) == (
        0.3,
        0.1,
    )
    assert by_name["press"]["interference_mm"] == 0.1
    loose = next(a for a in doc["aliases"] if a["name"] == "loose")
    assert (loose["fit_type"], loose["extra_mm"], loose["clearance_mm"]) == (
        "sliding",
        0.15,
        0.35,
    )
    assert doc["shapes"] == ["planar", "round", "slot", "pin", "hinge"]


def test_list_filters_by_fit_alias_and_shape(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert [
        f["name"] for f in run_json(capsys, "list", "--fit", "snap")["fit_types"]
    ] == ["snap"]
    via_alias = run_json(capsys, "list", "--fit", "snug")
    assert [f["name"] for f in via_alias["fit_types"]] == ["tight"]
    assert [a["name"] for a in via_alias["aliases"]] == ["snug"]
    assert [
        f["name"] for f in run_json(capsys, "list", "--shape", "hinge")["fit_types"]
    ] == ["sliding"]


def test_list_text_is_a_table(capsys: pytest.CaptureFixture[str]) -> None:
    out = run_text(capsys, "list", "--shape", "round")
    assert out.splitlines()[0].split() == [
        "FIT",
        "CLEARANCE",
        "INTERF.",
        "TOL",
        "SHAPES",
        "USE",
    ]
    assert "press" in out and "alias loose = sliding + 0.15 = 0.35 mm" in out


@pytest.mark.parametrize(
    ("argv", "accepted"),
    [
        (
            ["list", "--fit", "squishy"],
            "snap, sliding, tight, press, glue, free_running",
        ),
        (["list", "--shape", "cube"], "planar, round, slot, pin, hinge"),
        (["show", "squishy"], "snap, sliding, tight, press, glue"),
        (["show", "snap", "--shape", "cube"], "planar, round, slot, pin, hinge"),
    ],
)
def test_unknown_fit_or_shape_names_the_accepted_values(
    argv: list[str], accepted: str
) -> None:
    with pytest.raises(InvalidArgument, match=accepted) as err:
        cmd.run(argv)
    assert err.value.exit_code == 2


def test_show_lists_the_clearance_of_every_shape_for_a_printer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    doc = run_json(capsys, "show", "snap", "--printer", "k1se", "--material", "PETG")
    assert doc["fit_type"] == "snap" and [s["shape"] for s in doc["shapes"]] == [
        "planar",
        "round",
        "slot",
        "pin",
        "hinge",
    ]
    assert {s["clearance_mm"] for s in doc["shapes"]} == {0.35}
    text = run_text(capsys, "show", "snap", "--shape", "pin")
    assert "outside applies_to" in text and "features: snap_hook" in text


def test_show_needs_a_fit_type() -> None:
    with pytest.raises(UsageError, match="needs a fit type"):
        cmd.run(["show"])


# --------------------------------------------------------------------------- resolve
def test_resolve_json_always_has_a_provenance_chain(
    capsys: pytest.CaptureFixture[str],
) -> None:
    doc = run_json(
        capsys,
        "resolve",
        "--fit",
        "snap",
        "--shape",
        "round",
        "--printer",
        "k1se",
        "--material",
        "petg",
        "--nozzle",
        "0.4",
        "--layer",
        "0.2",
    )
    assert doc["command"] == "resolve" and (doc["fit_type"], doc["shape"]) == (
        "snap",
        "round",
    )
    assert (
        doc["clearance_mm"] == 0.35
        and doc["tol_mm"] == 0.1
        and doc["interference_mm"] == 0.0
    )
    kinds = [p["kind"] for p in doc["provenance"]]
    assert kinds[:2] == ["fit_type", "correction"] and {
        "printer",
        "material",
        "material_correction",
    } <= set(kinds)
    corr = next(p for p in doc["provenance"] if p["kind"] == "correction")
    assert corr["status"] == "default, not calibrated" and corr["fallback"] is True


def test_resolve_without_printer_or_material_still_explains_itself(
    capsys: pytest.CaptureFixture[str],
) -> None:
    doc = run_json(capsys, "resolve", "--fit", "press")
    assert (
        doc["shape"] == "planar" and doc["clearance_mm"] == -0.1 and doc["provenance"]
    )
    assert any(
        p.get("message", "").startswith("no material correction")
        for p in doc["provenance"]
    )
    assert doc["fallback"] is False and doc["xy_scale"] is None


def test_k1se_with_rapid_pla_plus_is_known_and_says_default_not_calibrated(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("THREED_FLEET", str(FLEET))
    doc = run_json(
        capsys,
        "resolve",
        "--fit",
        "sliding",
        "--shape",
        "round",
        "--printer",
        "k1se",
        "--material",
        "Elegoo Rapid PLA+",
        "--nozzle",
        "0.4",
    )
    printer = next(p for p in doc["provenance"] if p["kind"] == "printer")
    material = next(p for p in doc["provenance"] if p["kind"] == "material")
    assert (printer["key"], printer["model"], printer["known"]) == (
        "k1se",
        "Creality K1 SE",
        True,
    )
    assert (material["name"], material["known"], material["via"]) == (
        "PLA",
        True,
        "word 'PLA+'",
    )
    corr = next(p for p in doc["provenance"] if p["kind"] == "correction")
    assert corr["status"] == "default, not calibrated" and corr["source"] == "default"
    assert (
        doc["clearance_mm"] == 0.2 and doc["xy_scale"] == 1.003009
    )  # no invented calibration number


def test_unknown_printer_falls_back_with_provenance_not_an_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    doc = run_json(
        capsys,
        "resolve",
        "--fit",
        "sliding",
        "--printer",
        "mystery-printer",
        "--material",
        "unobtainium",
    )
    assert doc["clearance_mm"] == 0.2 and doc["fallback"] is True
    printer = next(p for p in doc["provenance"] if p["kind"] == "printer")
    assert (
        printer["known"] is False
        and "not in the fleet or printer registries" in printer["message"]
    )
    assert any(
        p["kind"] == "material_correction" and "unobtainium" in p["message"]
        for p in doc["provenance"]
    )


def test_resolve_text_shows_the_numbers_and_the_chain(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = run_text(
        capsys, "resolve", "--fit", "loose", "--shape", "round", "--material", "PETG"
    )
    assert (
        "fit sliding (as loose) / round" in out
        and "clearance     +0.400 mm per face" in out
    )
    assert "provenance:" in out and "alias        loose = sliding + 0.15 mm" in out


def test_resolve_needs_a_fit() -> None:
    with pytest.raises(UsageError, match="needs --fit TYPE"):
        cmd.run(["resolve"])


# --------------------------------------------------------------------------- corrections
def test_corrections_add_is_a_dry_run_until_yes(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    store = tmp_path / "fits-corrections.yaml"
    out = run_text(
        capsys,
        "corrections",
        "add",
        "--printer",
        "k1se",
        "--material",
        "PETG",
        "--nozzle",
        "0.4",
        "--delta",
        "0.07",
    )
    assert (
        "would add manual row" in out
        and "sliding/planar: +0.250 -> +0.270 mm" in out
        and not store.exists()
    )
    out = run_text(
        capsys,
        "corrections",
        "add",
        "--printer",
        "k1se",
        "--material",
        "PETG",
        "--nozzle",
        "0.4",
        "--delta",
        "0.07",
        "--yes",
    )
    assert out.startswith("added manual row") and store.is_file()


def test_a_correction_row_for_printer_material_nozzle_changes_resolve_by_exactly_delta_and_no_other_printer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = run_json(capsys, "resolve", "--fit", "sliding", "--shape", "round")[
        "clearance_mm"
    ]
    for who, delta in (("k1se", 0.13), ("u1", -0.04)):
        assert (
            cmd.run(
                [
                    "corrections",
                    "add",
                    "--printer",
                    who,
                    "--material",
                    "PLA",
                    "--nozzle",
                    "0.4",
                    "--delta",
                    str(delta),
                    "--yes",
                ]
            )
            == 0
        )
        capsys.readouterr()
    k1 = run_json(
        capsys,
        "resolve",
        "--fit",
        "sliding",
        "--shape",
        "round",
        "--printer",
        "k1se",
        "--material",
        "PLA",
        "--nozzle",
        "0.4",
    )
    u1 = run_json(
        capsys,
        "resolve",
        "--fit",
        "sliding",
        "--shape",
        "round",
        "--printer",
        "u1",
        "--material",
        "PLA",
        "--nozzle",
        "0.4",
    )
    other = run_json(
        capsys,
        "resolve",
        "--fit",
        "sliding",
        "--shape",
        "round",
        "--printer",
        "p1s",
        "--material",
        "PLA",
        "--nozzle",
        "0.4",
    )
    wrong_nozzle = run_json(
        capsys,
        "resolve",
        "--fit",
        "sliding",
        "--shape",
        "round",
        "--printer",
        "k1se",
        "--material",
        "PLA",
        "--nozzle",
        "0.6",
    )
    assert k1["clearance_mm"] == round(base + 0.13, 4) and u1["clearance_mm"] == round(
        base - 0.04, 4
    )
    assert other["clearance_mm"] == base and wrong_nozzle["clearance_mm"] == base
    row = next(p for p in k1["provenance"] if p["kind"] == "correction")
    assert (row["source"], row["status"], row["measured_at"] is not None) == (
        "manual",
        "manual override",
        True,
    )


def test_adding_the_same_scope_twice_replaces_and_says_so(
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = ["corrections", "add", "--printer", "k1se", "--material", "PETG", "--yes"]
    run_text(capsys, *args, "--delta", "0.05")
    out = run_text(capsys, *args, "--delta", "0.09")
    assert "(replaces +0.05 mm)" in out
    rows = run_json(capsys, "corrections", "list", "--origin", "manual")["corrections"]
    assert [(r["delta_mm"], r["origin"]) for r in rows] == [(0.09, "manual")]


def test_corrections_list_shows_table_manual_and_sources(
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_text(
        capsys,
        "corrections",
        "add",
        "--printer",
        "k1se",
        "--material",
        "PETG",
        "--delta",
        "0.05",
        "--yes",
        "--note",
        "coupon by hand",
    )
    doc = run_json(capsys, "corrections", "list")
    origins = [r["origin"] for r in doc["corrections"]]
    assert (
        origins[0] == "table" and origins[-1] == "manual" and "manual" in doc["sources"]
    )
    manual = doc["corrections"][-1]
    assert (
        manual["printer"],
        manual["material"],
        manual["note"],
        manual["status"],
    ) == ("k1se", "PETG", "coupon by hand", "manual override")
    assert "ORIGIN" in run_text(capsys, "corrections", "list")
    assert (
        run_json(capsys, "corrections", "list", "--origin", "table")["corrections"][0][
            "printer"
        ]
        == "*"
    )
    with pytest.raises(InvalidArgument, match="accepted: table, manual"):
        cmd.run(["corrections", "list", "--origin", "calibration"])


def test_corrections_remove_is_a_dry_run_until_yes_and_needs_the_exact_scope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    scope = ["--printer", "k1se", "--material", "PETG", "--nozzle", "0.4"]
    run_text(capsys, "corrections", "add", *scope, "--delta", "0.05", "--yes")
    with pytest.raises(UsageError, match="no manual correction row with that scope"):
        cmd.run(
            [
                "corrections",
                "remove",
                "--printer",
                "k1se",
                "--material",
                "PETG",
                "--yes",
            ]
        )
    assert "would remove" in run_text(capsys, "corrections", "remove", *scope)
    assert (
        len(
            run_json(capsys, "corrections", "list", "--origin", "manual")["corrections"]
        )
        == 1
    )
    assert run_text(capsys, "corrections", "remove", *scope, "--yes").startswith(
        "removed manual row"
    )
    assert (
        run_json(capsys, "corrections", "list", "--origin", "manual")["corrections"]
        == []
    )


def test_corrections_json_for_add_carries_the_preview_and_the_applied_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    dry = run_json(
        capsys,
        "corrections",
        "add",
        "--printer",
        "k1se",
        "--material",
        "PLA",
        "--delta",
        "0.1",
        "--fit",
        "snap",
        "--shape",
        "round",
    )
    assert dry["applied"] is False and dry["preview"] == [
        {"fit_type": "snap", "shape": "round", "before_mm": 0.3, "after_mm": 0.4}
    ]
    done = run_json(
        capsys,
        "corrections",
        "add",
        "--printer",
        "k1se",
        "--material",
        "PLA",
        "--delta",
        "0.1",
        "--fit",
        "snap",
        "--shape",
        "round",
        "--yes",
    )
    assert (
        done["applied"] is True
        and done["row"]["source"] == "manual"
        and done["previous"] is None
    )


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (
            ["corrections", "add", "--material", "PLA", "--delta", "0.1"],
            "needs --printer",
        ),
        (
            ["corrections", "add", "--printer", "k1se", "--material", "PLA"],
            "needs --delta",
        ),
        (["corrections", "remove", "--printer", "k1se"], "needs --material"),
    ],
)
def test_corrections_need_an_explicit_scope(argv: list[str], message: str) -> None:
    with pytest.raises(UsageError, match=message):
        cmd.run(argv)


def test_corrections_validate_names_and_numbers() -> None:
    base = [
        "corrections",
        "add",
        "--printer",
        "k1se",
        "--material",
        "PLA",
        "--delta",
        "0.1",
    ]
    with pytest.raises(InvalidArgument, match="accepted: planar"):
        cmd.run([*base, "--shape", "cube"])
    with pytest.raises(
        InvalidArgument, match="accepted: snap, sliding, tight, press, glue$"
    ):
        cmd.run([*base, "--fit", "loose"])  # rows match canonical types, not aliases
    with pytest.raises(UsageError, match="nozzle_mm"):
        cmd.run([*base, "--nozzle", "0.01"])
    with pytest.raises(InvalidArgument, match="accepted: list, add, remove"):
        cmd.run(["corrections", "purge"])


def test_a_hand_broken_store_stops_every_resolution_with_exit_2(tmp_path: Path) -> None:
    (tmp_path / "fits-corrections.yaml").write_text(
        "schema: 3d.fits/1\ncorrections: nope\n", encoding="utf-8"
    )
    with pytest.raises(UsageError, match="corrections: expected a list"):
        cmd.run(["resolve", "--fit", "snap"])


# --------------------------------------------------------------------------- apply
def test_apply_emits_a_snippet_or_json_and_never_writes(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out = run_text(
        capsys,
        "apply",
        "--fit",
        "sliding",
        "--shape",
        "round",
        "--printer",
        "k1se",
        "--material",
        "PETG",
        "--var",
        "pin_clearance",
    )
    assert (
        "pin_clearance = 0.25;  // @fit sliding shape=round" in out
        and "pin_clearance_tol = 0.1;" in out
    )
    doc = run_json(capsys, "apply", "--fit", "snap", "--shape", "round")
    assert (
        doc["mode"] == "snippet"
        and doc["clearance_mm"] == 0.3
        and doc["snippet"].startswith("// 3d fits apply")
    )
    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_apply_file_corrects_marked_clearances_dry_run_then_yes(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    scad = tmp_path / "insert.scad"
    scad.write_text(
        "clearance = 0.2;  // @fit sliding shape=planar mm\nwall = 2;\n",
        encoding="utf-8",
    )
    before = scad.read_text(encoding="utf-8")
    out = run_text(
        capsys, "apply", str(scad), "--printer", "k1se", "--material", "PETG"
    )
    assert (
        f"{scad}:1: clearance (sliding/planar): 0.2 -> 0.25 mm" in out
        and "dry run" in out
    )
    assert scad.read_text(encoding="utf-8") == before
    out = run_text(
        capsys, "apply", str(scad), "--printer", "k1se", "--material", "PETG", "--yes"
    )
    assert "1 clearance(s) rewritten" in out
    assert (
        scad.read_text(encoding="utf-8")
        == "clearance = 0.25;  // @fit sliding shape=planar mm\nwall = 2;\n"
    )
    doc = run_json(
        capsys, "apply", str(scad), "--printer", "k1se", "--material", "PETG", "--yes"
    )
    assert doc["mode"] == "correct" and doc["files"][0]["written"] is False
    assert doc["files"][0]["changes"][0]["changed"] is False


def test_apply_file_keeps_the_file_mode(tmp_path: Path) -> None:
    scad = tmp_path / "insert.scad"
    scad.write_text("c = 0.5;  // @fit snap\n", encoding="utf-8")
    scad.chmod(0o640)
    assert cmd.run(["apply", str(scad), "--yes"]) == 0
    assert scad.stat().st_mode & 0o777 == 0o640 and scad.read_text(
        encoding="utf-8"
    ).startswith("c = 0.3;")


def test_apply_file_errors(tmp_path: Path) -> None:
    plain = tmp_path / "plain.scad"
    plain.write_text("clearance = 0.2;\n", encoding="utf-8")
    with pytest.raises(UsageError, match="declares no fit clearance"):
        cmd.run(["apply", str(plain)])
    with pytest.raises(InputNotFound):
        cmd.run(["apply", str(tmp_path / "nope.scad")])
    with pytest.raises(UsageError, match="not both"):
        cmd.run(["apply", str(plain), "--fit", "snap"])
    with pytest.raises(UsageError, match="needs --fit TYPE"):
        cmd.run(["apply"])


# --------------------------------------------------------------------------- one table
def test_there_is_exactly_one_clearance_table_in_the_code_base() -> None:
    hits = [
        str(p.relative_to(REPO))
        for p in (REPO / "lib").rglob("*")
        if p.suffix in (".py", ".yaml")
        and "FIT_CLEARANCES" in p.read_text(encoding="utf-8")
    ]
    assert hits == []
    assert set(fits.load_table().fit_types) == {
        "snap",
        "sliding",
        "tight",
        "press",
        "glue",
    }
