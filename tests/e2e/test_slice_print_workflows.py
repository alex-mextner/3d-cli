from __future__ import annotations

import json
from pathlib import Path

from .workflow_helper import CUBE, run_cli, run_shell


def test_print_dry_run_exports_a_user_reviewable_job_plan(tmp_path: Path) -> None:
    """A user reviews printer, material, copies, and steps before touching hardware."""
    result = run_cli(
        tmp_path,
        "print",
        str(CUBE),
        "--printer",
        "Prusa MK4",
        "--material",
        "PLA",
        "--copies",
        "3",
        "--dry-run",
        "--job-name",
        "cube-batch",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["mode"] == "dry-run"
    assert payload["input_path"] == str(CUBE)
    assert payload["input_format"] == "scad"
    assert payload["printer"]["name"] == "Prusa MK4"
    assert payload["job"] == {"copies": 3, "material": "PLA", "name": "cube-batch", "start": False}
    assert payload["steps"] == [
        "validate input",
        "slice model",
        "upload job",
    ]


def test_slice_list_profiles_reports_profiles_readably(tmp_path: Path) -> None:
    """A user can ask what slicer profiles are visible before attempting a slice.

    The visible-profile set depends on the host (a slicer app installed under
    /Applications ships hundreds of bundled .json profiles, which `_profile_roots`
    scans and the env-isolated HOME cannot hide). So assert the invariants that hold
    in BOTH the empty and the populated case — exit 0, the readable header, the Bambu
    A1 shortcut hint, and no traceback — plus the empty-only guidance line only when
    no profiles were discovered."""
    result = run_cli(tmp_path, "slice", "--list-profiles")

    assert result.returncode == 0, result.stderr
    assert "3d slice profiles" in result.stdout
    assert "Profiles are slicer config files" in result.stdout
    assert "--printer bambu-a1 --material pla|petg" in result.stdout
    assert "Traceback" not in result.stdout + result.stderr
    if "No slicer profile files found" in result.stdout:
        assert "Put exported .ini/.json files in ./profiles/" in result.stdout


def test_slice_rejects_unusable_profile_before_invoking_a_slicer(tmp_path: Path) -> None:
    """A user gets profile export guidance without needing a slicer to be installed."""
    bad_profile = tmp_path / "profile.txt"
    bad_profile.write_text("not a slicer profile\n", encoding="utf-8")

    result = run_cli(tmp_path, "slice", str(CUBE), "--profile", str(bad_profile), "--dry-run")

    assert result.returncode == 2
    assert "got --profile=" in result.stderr
    assert ".ini profile/config exported from PrusaSlicer" in result.stderr
    assert "Open OrcaSlicer/Bambu Studio/PrusaSlicer" in result.stderr


def test_shell_chain_creates_print_plan_then_extracts_slice_readiness_note(tmp_path: Path) -> None:
    """A user chains print planning and slicer profile discovery into a text handoff."""
    result = run_shell(
        "\n".join(
            [
                "set -eu",
                '"$PYTHON" "$THREED" print "$CUBE" --printer "Prusa MK4" --dry-run --job-name handoff > print.json',
                '"$PYTHON" "$THREED" slice --list-profiles > profiles.txt',
                '"$PYTHON" -c \'import json, pathlib; '
                'plan=json.loads(pathlib.Path("print.json").read_text()); '
                'profiles=pathlib.Path("profiles.txt").read_text().splitlines()[0]; '
                'print(plan["printer"]["name"] + "|" + plan["job"]["name"] + "|" + profiles)\' '
                "> handoff.txt",
            ]
        ),
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "handoff.txt").read_text(encoding="utf-8") == (
        "Prusa MK4|handoff|3d slice profiles\n"
    )
