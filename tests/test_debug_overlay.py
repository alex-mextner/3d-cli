from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import pytest

from errors import InvalidArgument, UsageError

import debug_overlay

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_THREED = os.path.join(_REPO, "bin", "3d")


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as config_home:
        app_config = os.path.join(config_home, "3d-cli")
        os.makedirs(app_config, exist_ok=True)
        open(os.path.join(app_config, ".bootstrapped"), "w", encoding="utf-8").close()
        env = dict(os.environ)
        env["REPO_ROOT"] = _REPO
        env["XDG_CONFIG_HOME"] = config_home
        return subprocess.run(
            [sys.executable, _THREED, *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )


def test_overlay_plan_defaults_to_all_artifacts(tmp_path: object) -> None:
    render = os.path.join(str(tmp_path), "render.png")
    reference = os.path.join(str(tmp_path), "reference.jpg")
    plan = debug_overlay.build_plan(render, reference, out_dir="")

    assert plan.out_dir == str(tmp_path)
    assert [a.kind for a in plan.artifacts] == ["difference", "ghost", "edge"]
    assert [os.path.basename(a.path) for a in plan.artifacts] == [
        "overlay.png",
        "ghost.png",
        "edge_overlay.png",
    ]


def test_overlay_plan_can_select_single_mode(tmp_path: object) -> None:
    plan = debug_overlay.build_plan(
        "render.png",
        "reference.png",
        out_dir=str(tmp_path),
        modes=("edge",),
    )

    assert len(plan.artifacts) == 1
    assert plan.artifacts[0].kind == "edge"
    assert plan.artifacts[0].advice.startswith("Use when silhouettes disagree")


def test_overlay_plan_rejects_unknown_mode() -> None:
    with pytest.raises(InvalidArgument) as err:
        debug_overlay.build_plan("render.png", "reference.png", modes=("heat",))

    assert err.value.flag == "--mode"
    assert "difference" in err.value.accepted


def test_overlay_advice_buckets_are_actionable() -> None:
    perfect = debug_overlay.summarize_advice("0", frame_pixels=100)
    small = debug_overlay.summarize_advice("5", frame_pixels=100)
    large = debug_overlay.summarize_advice("80", frame_pixels=100)

    assert perfect.bucket == "identical"
    assert small.bucket == "minor"
    assert large.bucket == "major"
    assert "camera" in large.next_steps[0].lower()


def test_overlay_advice_does_not_assume_selected_artifacts() -> None:
    small = debug_overlay.summarize_advice("5", frame_pixels=100)
    large = debug_overlay.summarize_advice("80", frame_pixels=100)

    combined = " ".join((*small.next_steps, *large.next_steps))
    assert "ghost.png" not in combined
    assert "overlay.png" not in combined
    assert "edge_overlay.png" not in combined


def test_overlay_advice_rejects_bad_ae() -> None:
    with pytest.raises(UsageError):
        debug_overlay.summarize_advice("not-a-number", frame_pixels=100)


def test_overlay_plan_json_is_machine_readable(tmp_path: object) -> None:
    plan = debug_overlay.build_plan("render.png", "reference.png", out_dir=str(tmp_path))
    payload = json.loads(debug_overlay.plan_to_json(plan))

    assert payload["render"] == "render.png"
    assert payload["reference"] == "reference.png"
    assert payload["artifacts"][0]["kind"] == "difference"


def test_overlay_help_and_no_args_contract() -> None:
    help_result = _run(["overlay", "--help"])
    assert help_result.returncode == 0
    assert "--mode" in help_result.stdout

    no_args = _run(["overlay"])
    assert no_args.returncode == 1
    assert "3d overlay" in no_args.stdout


def test_overlay_advice_only_json_needs_no_imagemagick(tmp_path: object) -> None:
    render = os.path.join(str(tmp_path), "render.png")
    reference = os.path.join(str(tmp_path), "reference.png")
    open(render, "wb").close()
    open(reference, "wb").close()

    result = _run(["overlay", render, reference, "--advice-only", "--json", "--mode", "edge"])

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert [artifact["kind"] for artifact in payload["artifacts"]] == ["edge"]
    assert payload["out_dir"] == str(tmp_path)
    assert result.stderr == ""


def test_overlay_missing_option_value_is_structured() -> None:
    result = _run(["overlay", "render.png", "reference.png", "--mode"])

    assert result.returncode == 2
    assert "option --mode needs a value" in result.stderr
    assert "Traceback" not in result.stderr
