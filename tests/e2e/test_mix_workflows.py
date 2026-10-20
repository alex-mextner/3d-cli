"""E2E: a user lints a mixed-colour project before slicing and preflights its G-code (#411)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from head_sequence_fixtures import gcode as head_gcode
from head_sequence_fixtures import project_3mf
from mix_fixtures import orca_project, tile

from .workflow_helper import run_cli


def test_mix_check_fails_a_flat_mixed_tile_and_passes_it_standing(
    tmp_path: Path,
) -> None:
    """The PTA tile lies flat in mix 431: the lint names it; stood up, the same project passes."""
    orca_project(tmp_path / "flat.3mf", [tile("PTA", 5), tile("REF", 1)])
    orca_project(tmp_path / "standing.3mf", [tile("PTA", 5, standing=True)])

    flat = run_cli(tmp_path, "mix", "check", "flat.3mf")
    flat_json = run_cli(tmp_path, "mix", "check", "flat.3mf", "--json")
    standing = run_cli(tmp_path, "mix", "check", "standing.3mf")

    assert flat.returncode == 1, flat.stdout + flat.stderr
    assert "mix check flat.3mf: FAIL" in flat.stdout
    assert (
        "FAIL PTA / PTA: filament 5 (431) — visible face horizontal: top 400 mm²"
        in flat.stdout
    )
    assert "REF" not in flat.stdout  # a physical filament is not a mix
    payload = json.loads(flat_json.stdout)
    assert flat_json.returncode == 1
    assert payload["status"] == "FAIL" and payload["failures"] == 1
    assert [
        (p["object"], p["recipe"], p["top_mm2"], p["side_mm2"])
        for p in payload["parts"]
    ] == [("PTA", "431", 400.0, 240.0)]
    assert standing.returncode == 0, standing.stdout + standing.stderr
    assert "mix check standing.3mf: PASS" in standing.stdout


def test_mix_check_rejects_a_file_that_is_not_a_project(tmp_path: Path) -> None:
    """A user points mix check at a file that is not a project 3MF and gets a clear usage error."""
    (tmp_path / "part.3mf").write_bytes(b"solid not a zip")
    result = run_cli(tmp_path, "mix", "check", "part.3mf")
    assert result.returncode == 2
    assert "not a readable Orca-family project 3MF" in result.stderr


def test_print_preflight_shows_the_mix_face_line_from_the_sibling_project(
    tmp_path: Path,
) -> None:
    """plate.3mf next to plate.gcode: the flat mixed tile FAILs preflight; standing it PASSes;
    without a project 3MF the line is a SKIP that says what to pass."""
    layers = [{"PTA": int("431"[i % 3])} for i in range(6)]
    orca_project(tmp_path / "plate.3mf", [tile("PTA", 5)])
    head_gcode(tmp_path / "plate.gcode", layers)
    good = tmp_path / "good"
    good.mkdir()
    orca_project(good / "plate.3mf", [tile("PTA", 5, standing=True)])
    head_gcode(good / "plate.gcode", layers)
    bare = tmp_path / "bare"
    bare.mkdir()
    head_gcode(bare / "plate.gcode", layers)

    bad = run_cli(tmp_path, "print-preflight", "plate.gcode")
    ok = run_cli(tmp_path, "print-preflight", "good/plate.gcode", "--json")
    none = run_cli(tmp_path, "print-preflight", "bare/plate.gcode")

    assert bad.returncode == 1
    assert (
        "FAIL  mix face    1 of 1 mixed part(s) show their colour on a horizontal face "
        "(plate.3mf next to the G-code)" in bad.stdout
    )
    assert "FAIL PTA / PTA: filament 5 (431)" in bad.stdout
    checks = {c["name"]: c for c in json.loads(ok.stdout)["checks"]}
    assert checks["mix face"]["status"] == "PASS", checks["mix face"]
    assert checks["head seq"]["status"] == "PASS", checks["head seq"]
    assert "SKIP  mix face    no project 3MF" in none.stdout


def test_mix_pattern_rejects_gwg_and_accepts_an_isolated_minor_layer(
    tmp_path: Path,
) -> None:
    """GWG repeats as GWGGWG — a double-thick G band; GW (and GWG once a base colour may double) pass."""
    bad = run_cli(tmp_path, "mix", "pattern", "GWG")
    good = run_cli(tmp_path, "mix", "pattern", "GW")
    relaxed = run_cli(tmp_path, "mix", "pattern", "GWG", "--max-run-mm", "0.24")
    thick = run_cli(tmp_path, "mix", "pattern", "GW", "--layer", "0.32")
    as_json = run_cli(tmp_path, "mix", "pattern", "GWG", "--json")

    assert bad.returncode == 1, bad.stdout + bad.stderr
    assert "mix pattern GWG: FAIL" in bad.stdout
    assert "run-too-thick: G forms a 0.240 mm band" in bad.stdout
    assert good.returncode == 0 and "mix pattern GW: PASS" in good.stdout
    assert relaxed.returncode == 0, relaxed.stdout + relaxed.stderr
    assert thick.returncode == 1 and "layer-too-thick" in thick.stdout
    payload = json.loads(as_json.stdout)
    assert payload["ok"] is False
    assert [v["rule"] for v in payload["violations"]] == ["run-too-thick"]


def test_mix_pattern_takes_variable_layer_heights(tmp_path: Path) -> None:
    """A user checks a pattern that will print at variable layer heights: one 0.2 mm layer fails it."""
    ok = run_cli(tmp_path, "mix", "pattern", "WGWB", "--heights", "0.12,0.08,0.12,0.1")
    bad = run_cli(tmp_path, "mix", "pattern", "WGWB", "--heights", "0.12,0.08,0.2,0.1")
    wrong = run_cli(tmp_path, "mix", "pattern", "WG", "--heights", "0.12")
    assert [ok.returncode, bad.returncode, wrong.returncode] == [0, 1, 2], ok.stdout + ok.stderr
    assert "mix pattern WGWB: PASS" in ok.stdout
    assert "0.200 mm at layer 2" in bad.stdout
    assert "lists 1 layers for a pattern of 2" in wrong.stderr


def test_mix_recipes_never_offer_a_pattern_the_rules_reject(tmp_path: Path) -> None:
    """A user ranks black+white mixes for a grey: the double-black cycle 141 is not offered."""
    out = run_cli(
        tmp_path,
        "mix",
        "recipes",
        "--target",
        "#808080",
        "--slots",
        "1=#000000,4=#FFFFFF",
        "--json",
    )
    assert out.returncode == 0, out.stderr
    patterns = [c["pattern"] for c in json.loads(out.stdout)["candidates"]]
    assert "141" not in patterns and "14" in patterns
    thick = run_cli(
        tmp_path,
        "mix",
        "recipes",
        "--target",
        "#808080",
        "--slots",
        "1=#000000,4=#FFFFFF",
        "--layer",
        "0.2",
    )
    assert thick.returncode == 2 and "0.12 mm cap" in thick.stderr


def _sliced(directory: Path, heads: list[int], heights: list[float]) -> Path:
    """NAME.3mf (object A in mixed filament 5 = heads 1/4) next to a sliced NAME.gcode."""
    directory.mkdir()
    project_3mf(directory / "plate.3mf", {"A": 5})
    return head_gcode(
        directory / "plate.gcode", [{"A": h} for h in heads], heights=heights
    )


def test_mix_gcode_fails_consecutive_minor_layers_and_thick_layers_and_passes_the_good_print(
    tmp_path: Path,
) -> None:
    """A user gates four sliced plates: good and variable-height pass; doubled-colour and thick-layer fail."""
    thin = [0.2] + [0.12] * 7
    _sliced(tmp_path / "good", [1, 4] * 4, thin)
    _sliced(tmp_path / "double", [1, 4, 4, 1, 4, 1, 4, 1], thin)
    _sliced(tmp_path / "thick", [1, 4] * 4, [0.2] + [0.32] * 7)
    _sliced(tmp_path / "var", [1, 4] * 4, [0.2, 0.08, 0.1, 0.12, 0.08, 0.12, 0.1, 0.08])

    def gate(name: str, *extra: str):
        return run_cli(
            tmp_path,
            "mix",
            "gcode",
            f"{name}/plate.gcode",
            "--project",
            f"{name}/plate.3mf",
            *extra,
        )

    good, double, thick, var = gate("good"), gate("double"), gate("thick"), gate("var")
    assert good.returncode == 0, good.stdout + good.stderr
    assert "mix gcode plate.gcode: PASS" in good.stdout
    assert (
        double.returncode == 1
        and "layers 1-2" in double.stdout
        and "0.240 mm band" in double.stdout
    )
    assert thick.returncode == 1 and "thicker than the 0.12 mm cap" in thick.stdout
    # variable layer heights 0.08-0.12 mm: every stripe within the cap, measured from the real Z
    assert var.returncode == 0, var.stdout + var.stderr
    payload = json.loads(gate("var", "--json").stdout)
    (row,) = payload["results"]
    assert row["report"]["max_layer_mm"] <= 0.12 + 1e-6 and row["report"]["ok"] is True
    assert json.loads(gate("double", "--json").stdout)["status"] == "FAIL"
    # a configured cap that the print respects
    assert gate("thick", "--layer-cap", "0.32").returncode == 0


def test_print_preflight_has_a_mix_layers_line(tmp_path: Path) -> None:
    """A user preflights two sliced plates: the mix layers line passes the good one and fails the doubled one."""
    thin = [0.2] + [0.12] * 7
    _sliced(tmp_path / "good", [1, 4] * 4, thin)
    _sliced(tmp_path / "double", [1, 4, 4, 1, 4, 1, 4, 1], thin)
    good = run_cli(tmp_path, "print-preflight", "good/plate.gcode", "--json")
    bad = run_cli(tmp_path, "print-preflight", "double/plate.gcode")
    checks = {c["name"]: c for c in json.loads(good.stdout)["checks"]}
    assert checks["mix layers"]["status"] == "PASS", checks["mix layers"]
    assert bad.returncode == 1
    assert "mix layers" in bad.stdout and "layers 1-2" in bad.stdout


def test_mix_check_and_profile_cap_the_layers_over_a_mixed_part(tmp_path: Path) -> None:
    """A standing mixed tile at the process layer height 0.2 mm FAILs the layer cap; `mix profile`
    writes a capped copy that passes and leaves the input alone."""
    orca_project(
        tmp_path / "thick.3mf",
        [tile("PTA", 5, standing=True)],
        settings={"layer_height": "0.2"},
    )
    before = (tmp_path / "thick.3mf").read_bytes()

    bad = run_cli(tmp_path, "mix", "check", "thick.3mf")
    made = run_cli(tmp_path, "mix", "profile", "thick.3mf", "-o", "capped.3mf")
    good = run_cli(tmp_path, "mix", "check", "capped.3mf", "--json")

    assert bad.returncode == 1, bad.stdout + bad.stderr
    assert (
        "FAIL layers PTA / PTA" in bad.stdout
        and "thicker than the 0.12 mm cap" in bad.stdout
    )
    assert made.returncode == 0, made.stdout + made.stderr
    assert "mixed layers capped at 0.12 mm" in made.stdout
    assert (tmp_path / "thick.3mf").read_bytes() == before
    assert good.returncode == 0, good.stdout + good.stderr
    layers = {f["scope"]: f for f in json.loads(good.stdout)["layers"]["findings"]}[
        "layers"
    ]
    assert layers["status"] == "PASS" and "its layer height profile" in layers["detail"]
    with zipfile.ZipFile(tmp_path / "capped.3mf") as zf:
        assert (
            zf.read("Metadata/layer_heights_profile.txt")
            .decode()
            .startswith("object_id=1|")
        )


def test_mix_profile_refuses_what_orca_would_refuse_and_never_overwrites(
    tmp_path: Path,
) -> None:
    orca_project(
        tmp_path / "two.3mf",
        [tile("PTA", 5, standing=True), tile("REF", 1, standing=True)],
        settings={"enable_prime_tower": "1"},
    )
    two = run_cli(tmp_path, "mix", "profile", "two.3mf", "-o", "out.3mf")
    same = run_cli(tmp_path, "mix", "profile", "two.3mf", "-o", "two.3mf")
    assert two.returncode == 2 and "ONE multi-part object" in two.stderr
    assert same.returncode == 2 and "never overwritten" in same.stderr
    assert not (tmp_path / "out.3mf").exists()
