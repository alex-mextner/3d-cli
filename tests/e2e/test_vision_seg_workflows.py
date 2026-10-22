"""`3d segment` / `3d detect` / `3d vision-models` end to end through bin/3d (threed-vision launchers, #465).

These run in plain CI (numpy + OpenCV + Pillow, no torch / weights): help text, the models registry, and the
exit-127 contract that names the exact `3d vision-models pull` command when a model is absent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
PIL_Image = pytest.importorskip("PIL.Image")

from .workflow_helper import run_cli  # noqa: E402


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "THREED_VISION_PYTHON": sys.executable,
        "THREED_VISION_MODELS_DIR": str(tmp_path / "models"),
        "THREED_VISION_IMPORT_DIRS": str(tmp_path / "no-legacy"),
    }


def _photo(tmp_path: Path) -> Path:
    arr = np.full((90, 120, 3), 128, np.uint8)
    arr[20:60, 30:80] = (200, 40, 40)
    p = tmp_path / "photo.png"
    PIL_Image.fromarray(arr).save(p)
    return p


def test_segment_help_documents_every_flag_and_artefact(tmp_path: Path) -> None:
    """A user runs `3d segment --help` before a first photo and finds every flag and every artefact the command writes."""
    result = run_cli(tmp_path, "segment", "--help")
    assert result.returncode == 0
    for word in (
        "instances.json",
        "labels.png",
        "masks/NNN.png",
        "overlay.jpg",
        "--method",
        "sam2-dense",
        "u2net",
        "--fill",
        "--queries",
        "--no-reject-bg",
        "--points-per-side",
        "--max-side",
        "--min-area",
        "--max-area-frac",
        "--device",
        "--json",
    ):
        assert word in result.stdout


def test_detect_and_models_help(tmp_path: Path) -> None:
    """A user reads `3d detect --help` and `3d vision-models --help` to learn the flags, the subcommands and the model names to pull."""
    detect = run_cli(tmp_path, "detect", "--help")
    assert detect.returncode == 0
    for word in (
        "--queries",
        "--backend",
        "--threshold",
        "--nms",
        "--max-area-frac",
        "--masks",
        "--device",
    ):
        assert word in detect.stdout
    models = run_cli(tmp_path, "vision-models", "--help")
    assert models.returncode == 0
    for word in (
        "status",
        "pull",
        "--verify",
        "sam2-tiny",
        "owlv2-base",
        "THREED_VISION_MODELS_DIR",
    ):
        assert word in models.stdout


def test_models_status_json_lists_every_model_as_missing_in_an_empty_cache(
    tmp_path: Path,
) -> None:
    """A fresh machine runs `3d vision-models status --json` and sees every model missing with the exact pull command to run."""
    result = run_cli(
        tmp_path, "vision-models", "status", "--json", env_extra=_env(tmp_path)
    )
    assert result.returncode == 0, result.stderr
    doc = json.loads(result.stdout)
    assert doc["models_dir"] == str(tmp_path / "models")
    assert set(doc["models"]) == {
        "sam2-tiny",
        "owlv2-base",
        "clip-b32",
        "dinov2-s",
        "u2net",
    }
    assert all(not m["installed"] for m in doc["models"].values())
    assert doc["models"]["sam2-tiny"]["pull"].startswith(
        "3d vision-models pull sam2-tiny"
    )
    human = run_cli(tmp_path, "vision-models", "status", env_extra=_env(tmp_path))
    assert "MISSING" in human.stdout and "3d vision-models pull" in human.stdout


def test_models_pull_unknown_name_is_bad_input(tmp_path: Path) -> None:
    """A user mistypes a model name in `3d vision-models pull` and gets a clear message listing the valid names."""
    result = run_cli(
        tmp_path, "vision-models", "pull", "nope", env_extra=_env(tmp_path)
    )
    assert result.returncode == 2
    assert (
        "unknown vision model 'nope'" in result.stderr and "sam2-tiny" in result.stderr
    )


@pytest.mark.parametrize(
    ("argv", "model"),
    [
        (["segment", "{img}", "-o", "{out}"], "sam2-tiny"),
        (
            [
                "segment",
                "{img}",
                "-o",
                "{out}",
                "--method",
                "sam2-dense",
                "--device",
                "cpu",
            ],
            "sam2-tiny",
        ),
        (
            ["segment", "{img}", "-o", "{out}", "--method", "u2net", "--device", "cpu"],
            "u2net",
        ),
        (
            ["detect", "{img}", "--queries", "a hand tool", "--device", "cpu"],
            "owlv2-base",
        ),
    ],
)
def test_missing_model_exits_127_naming_the_pull_command(
    tmp_path: Path, argv: list[str], model: str
) -> None:
    """A user runs `3d segment` or `3d detect` before pulling weights and is told the exact `3d vision-models pull <name>` to run, with no traceback and no output files."""
    img = _photo(tmp_path)
    args = [a.format(img=img, out=tmp_path / "out") for a in argv]
    result = run_cli(tmp_path, *args, env_extra=_env(tmp_path))
    assert result.returncode == 127, result.stderr
    assert f"3d vision-models pull {model}" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "out" / "instances.json").exists()


def test_bad_inputs_are_exit_2(tmp_path: Path) -> None:
    """A user passes a missing photo or empty queries and gets a specific bad-input message."""
    missing = run_cli(
        tmp_path,
        "segment",
        str(tmp_path / "nope.jpg"),
        "-o",
        str(tmp_path / "o"),
        env_extra=_env(tmp_path),
    )
    assert missing.returncode == 2
    assert "image not found" in missing.stderr
    empty_q = run_cli(
        tmp_path,
        "detect",
        str(_photo(tmp_path)),
        "--queries",
        " , ",
        env_extra=_env(tmp_path),
    )
    assert empty_q.returncode == 2
    assert "--queries is empty" in empty_q.stderr
