"""E2E: a user ranks mixes for a target colour and writes a numbered tester plate (#413)."""

from __future__ import annotations

import json
from pathlib import Path

from mix_tester_fixtures import SLOTS, write_base_project
from slicing.mix_tester import validate_manifest

from .workflow_helper import run_cli


def test_recipes_text_and_json_name_the_same_candidates(tmp_path: Path) -> None:
    """A user ranks mixes for a grey from three spools; the text table and --json list the same candidates."""
    args = ("mix", "recipes", "--target", "#808080", "--slots", SLOTS, "--count", "4")
    text = run_cli(tmp_path, *args)
    as_json = run_cli(tmp_path, *args, "--json")
    assert text.returncode == 0, text.stderr
    payload = json.loads(as_json.stdout)
    assert len(payload["candidates"]) == 4
    assert payload["candidates"][0]["role"] == "best"
    for c in payload["candidates"]:
        assert f"{c['pattern']:<8} {c['hex']}" in text.stdout


def test_tester_writes_project_manifest_and_sheet_from_a_base_project(
    tmp_path: Path,
) -> None:
    """A user turns a target colour into a tester plate: project, manifest and swatch sheet, then lints it."""
    write_base_project(tmp_path / "base.3mf")
    args = (
        "mix", "tester", "--target", "#808080", "--slots", SLOTS,
        "--base-3mf", "base.3mf", "-o", "out/grey", "--count", "4",
    )  # fmt: skip
    text = run_cli(tmp_path, *args)
    assert text.returncode == 0, text.stdout + text.stderr
    as_json = run_cli(tmp_path, *args, "--json")
    assert as_json.returncode == 0, as_json.stderr
    summary = json.loads(as_json.stdout)

    for name in ("grey.3mf", "grey.mix.json", "grey.png"):
        assert (tmp_path / "out" / name).is_file()
    manifest = json.loads((tmp_path / "out" / "grey.mix.json").read_text())
    assert validate_manifest(manifest) == []
    assert manifest["settings_source"] == "base.3mf"
    # the text is the JSON summary, line by line
    assert text.stdout.splitlines()[0] == (
        f"mix tester for #808080: {len(summary['candidates'])} mixes + "
        f"{len(summary['references'])} references, labels on T{summary['label_slot']}"
    )
    for c in summary["candidates"]:
        assert (
            f"{c['number']:>2} {c['role']:<8} {c['pattern']:<8} {c['hex']}  "
            f"ΔE {c['delta_e']:.2f}  filament {c['filament']}"
        ) in text.stdout
    for r in summary["references"]:
        assert f"{r['number']:>2} T{r['slot']} pure  {r['hex']}" in text.stdout
    for path in summary["files"].values():
        assert f": {path}" in text.stdout
    assert summary["label_slot"] == 1  # black is the darkest

    check = run_cli(tmp_path, "mix", "check", "out/grey.3mf")
    assert check.returncode == 0, check.stdout + check.stderr
    assert "mix check grey.3mf: PASS" in check.stdout


def test_tester_usage_errors_say_what_to_pass(tmp_path: Path) -> None:
    """A user who forgets -o, passes both slot sources or a bad colour is told exactly what to pass."""
    no_out = run_cli(tmp_path, "mix", "tester", "--target", "#808080", "--slots", SLOTS)
    both = run_cli(
        tmp_path,
        "mix",
        "recipes",
        "--target",
        "#808080",
        "--slots",
        SLOTS,
        "--printer",
        "u1",
    )
    bad = run_cli(tmp_path, "mix", "recipes", "--target", "#808080", "--slots", "1=red")
    assert [r.returncode for r in (no_out, both, bad)] == [2, 2, 2]
    assert "-o OUT/NAME is required" in no_out.stderr
    assert "exactly one of --slots and --printer" in both.stderr
    assert "is not N=#RRGGBB" in bad.stderr
