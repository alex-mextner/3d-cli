from __future__ import annotations

import json
from pathlib import Path

from fit_fixtures import write_insert, write_plate

from .workflow_helper import REPO_ROOT, run_cli

FLEET = {
    "THREED_FLEET": str(REPO_ROOT / "fleet" / "printers.yaml")
}  # read-only registry, no printer is contacted


def test_fits_help_lists_every_subcommand_and_flag(tmp_path: Path) -> None:
    """A user reads `3d fits --help` to learn what the table can do."""
    result = run_cli(tmp_path, "fits", "--help")
    assert result.returncode == 0
    for word in (
        "list",
        "show",
        "resolve",
        "apply",
        "corrections",
        "--fit TYPE",
        "--printer NAME",
        "--material NAME",
        "--nozzle MM",
        "--layer MM",
        "--yes",
        "--json",
    ):
        assert word in result.stdout


def test_fits_list_prints_the_table_and_a_filtered_json(tmp_path: Path) -> None:
    """A designer looks up the five fit types, then asks which ones fit a hinge, as JSON for a script."""
    table = run_cli(tmp_path, "fits", "list")
    assert table.returncode == 0, table.stderr
    for fit in ("snap", "sliding", "tight", "press", "glue"):
        assert fit in table.stdout
    assert "alias loose = sliding + 0.15 = 0.35 mm" in table.stdout

    hinge = run_cli(tmp_path, "fits", "list", "--shape", "hinge", "--json")
    doc = json.loads(hinge.stdout)
    assert doc["schema"] == "3d.fits/1" and [f["name"] for f in doc["fit_types"]] == [
        "sliding"
    ]


def test_fits_resolve_k1se_petg_explains_every_number(tmp_path: Path) -> None:
    """Before modelling a snap latch for the K1 SE in PETG, the designer asks for the clearance and the chain behind it."""
    result = run_cli(
        tmp_path,
        "fits",
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
        "--json",
        env_extra=FLEET,
    )
    assert result.returncode == 0, result.stderr
    doc = json.loads(result.stdout)
    assert (
        doc["schema"] == "3d.fits/1"
        and doc["clearance_mm"] == 0.35
        and doc["tol_mm"] == 0.1
    )
    by_kind = {p["kind"]: p for p in doc["provenance"]}
    assert (
        by_kind["printer"]["model"] == "Creality K1 SE"
        and by_kind["material"]["name"] == "PETG"
    )
    assert by_kind["correction"]["status"] == "default, not calibrated"
    assert by_kind["material_correction"]["xy_scale"] == 1.004016

    text = run_cli(
        tmp_path,
        "fits",
        "resolve",
        "--fit",
        "snap",
        "--shape",
        "round",
        "--printer",
        "k1se",
        "--material",
        "Elegoo Rapid PLA+",
        env_extra=FLEET,
    )
    assert (
        "default, not calibrated" in text.stdout
        and "PLA (via word 'PLA+')" in text.stdout
    )


def test_fits_unknown_fit_or_shape_exits_2_listing_the_accepted_values(
    tmp_path: Path,
) -> None:
    """A typo in the fit name or the shape is told what the table accepts."""
    bad_fit = run_cli(tmp_path, "fits", "resolve", "--fit", "squishy")
    assert bad_fit.returncode == 2
    assert (
        "accepted: snap, sliding, tight, press, glue, free_running, tight_running, snug, normal, loose"
        in bad_fit.stderr
    )
    bad_shape = run_cli(tmp_path, "fits", "resolve", "--fit", "snap", "--shape", "cube")
    assert (
        bad_shape.returncode == 2
        and "accepted: planar, round, slot, pin, hinge" in bad_shape.stderr
    )
    bad_sub = run_cli(tmp_path, "fits", "frobnicate")
    assert (
        bad_sub.returncode == 2
        and "accepted: list, show, resolve, apply, corrections" in bad_sub.stderr
    )


def test_a_calibrated_by_hand_correction_changes_the_next_resolve_by_its_delta(
    tmp_path: Path,
) -> None:
    """After measuring a coupon by hand the designer records +0.07 mm for the K1 SE in PLA; the dry run shows the
    effect, --yes stores it, and `resolve` for that printer moves by exactly 0.07 while another printer does not."""
    scope = ["--printer", "k1se", "--material", "PLA", "--nozzle", "0.4"]
    dry = run_cli(tmp_path, "fits", "corrections", "add", *scope, "--delta", "0.07")
    assert (
        dry.returncode == 0
        and "would add manual row" in dry.stdout
        and "sliding/planar: +0.200 -> +0.270 mm" in dry.stdout
    )

    done = run_cli(
        tmp_path,
        "fits",
        "corrections",
        "add",
        *scope,
        "--delta",
        "0.07",
        "--yes",
        "--note",
        "coupon 2026-10-24",
    )
    assert done.returncode == 0 and done.stdout.startswith("added manual row")

    def clearance(printer: str) -> float:
        out = run_cli(
            tmp_path,
            "fits",
            "resolve",
            "--fit",
            "sliding",
            "--printer",
            printer,
            "--material",
            "PLA",
            "--nozzle",
            "0.4",
            "--json",
        )
        return float(json.loads(out.stdout)["clearance_mm"])

    assert clearance("k1se") == 0.27 and clearance("u1") == 0.2
    listing = json.loads(
        run_cli(
            tmp_path, "fits", "corrections", "list", "--origin", "manual", "--json"
        ).stdout
    )
    [row] = listing["corrections"]
    assert (
        row["printer"],
        row["material"],
        row["delta_mm"],
        row["status"],
        row["note"],
    ) == ("k1se", "PLA", 0.07, "manual override", "coupon 2026-10-24")

    gone = run_cli(tmp_path, "fits", "corrections", "remove", *scope, "--yes")
    assert gone.returncode == 0 and clearance("k1se") == 0.2


def test_fit_niche_and_fit_check_give_the_same_clearance_and_apply_recorrects_the_model(
    tmp_path: Path,
) -> None:
    """The designer generates an insert for the K1 SE in PETG, checks the printed pair with the same fit name and gets
    the same gap, then re-targets the insert to another printer without regenerating it."""
    niche = run_cli(
        tmp_path,
        "fit-niche",
        "--width",
        "20",
        "--depth",
        "10",
        "--height",
        "5",
        "--fit",
        "sliding",
        "--printer",
        "k1se",
        "--material",
        "PETG",
        "--nozzle",
        "0.4",
        "--json",
        "-o",
        "insert.scad",
        env_extra=FLEET,
    )
    assert niche.returncode == 0, niche.stderr
    summary = json.loads(niche.stdout)
    assert (
        summary["clearance"] == 0.25
        and summary["fit_resolution"]["schema"] == "3d.fits/1"
    )

    write_insert(tmp_path / "INSERT.stl")
    write_plate(tmp_path / "PLATE.stl")
    check = run_cli(
        tmp_path,
        "fit-check",
        "--insert",
        "INSERT.stl",
        "--socket",
        "PLATE.stl",
        "--fit",
        "sliding",
        "--printer",
        "k1se",
        "--material",
        "PETG",
        "--nozzle",
        "0.4",
        "--json",
        env_extra=FLEET,
    )
    assert check.returncode == 0, check.stdout + check.stderr
    fit = json.loads(check.stdout)["fits"][0]
    assert fit["gap_set_mm"] == summary["clearance"] and fit["gap_tol_mm"] == 0.1
    assert fit["fit"]["correction_status"] == "default, not calibrated"

    scad = tmp_path / "insert.scad"
    dry = run_cli(
        tmp_path,
        "fits",
        "apply",
        "insert.scad",
        "--printer",
        "k1se",
        "--material",
        "PETG",
        "--nozzle",
        "0.4",
    )
    assert dry.returncode == 0 and "(sliding/planar): 0.25 mm (unchanged)" in dry.stdout
    before = scad.read_text(encoding="utf-8")
    dry = run_cli(
        tmp_path, "fits", "apply", "insert.scad", "--printer", "u1", "--material", "PLA"
    )
    assert (
        "0.25 -> 0.2 mm" in dry.stdout
        and "dry run" in dry.stdout
        and scad.read_text(encoding="utf-8") == before
    )
    done = run_cli(
        tmp_path,
        "fits",
        "apply",
        "insert.scad",
        "--printer",
        "u1",
        "--material",
        "PLA",
        "--yes",
    )
    assert done.returncode == 0 and "clearance = 0.2;" in scad.read_text(
        encoding="utf-8"
    )
    assert "// @fit sliding shape=planar " in scad.read_text(encoding="utf-8")


def test_fit_check_spec_with_fit_types_and_a_file_without_markers(
    tmp_path: Path,
) -> None:
    """A project spec says `fit: sliding` for the whole assembly instead of 0.2 mm literals; a hand-written scad
    without markers is refused loudly instead of silently passing."""
    write_insert(tmp_path / "KEY.stl")
    write_plate(tmp_path / "BASE.stl")
    (tmp_path / "fits.yaml").write_text(
        "printer: k1se\nmaterial: PETG\nfit: sliding\nfits:\n  - {name: KEY, insert: KEY.stl, socket: BASE.stl}\n",
        encoding="utf-8",
    )
    result = run_cli(tmp_path, "fit-check", "--spec", "fits.yaml", env_extra=FLEET)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "from fit sliding/planar k1se PETG: default, not calibrated" in result.stdout

    (tmp_path / "plain.scad").write_text("clearance = 0.2;\n", encoding="utf-8")
    plain = run_cli(tmp_path, "fits", "apply", "plain.scad")
    assert plain.returncode == 2 and "declares no fit clearance" in plain.stderr
