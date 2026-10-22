"""`3d bank` / `3d recognise` end to end through bin/3d (threed-vision launchers, #467).

Plain-CI contract: help text, a `bank thumbs` of a hand-made photo bank, bad-input exit 2, and the exit-127 contract that names the
exact `3d vision-models pull` command when CLIP / DINOv2 are absent. Needs numpy + OpenCV + Pillow only (no trimesh, torch or weights).
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


def _photo_bank(tmp_path: Path) -> Path:
    """A hand-made bank of one photographed screwdriver: no trimesh or models needed to normalise it."""
    src = tmp_path / "photos"
    src.mkdir()
    img = np.full((120, 40, 3), (200, 30, 30), np.uint8)
    PIL_Image.fromarray(img).save(src / "driver.png")
    (src / "bank.json").write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "id": "screwdriver_1",
                        "category": "screwdriver",
                        "thumb": "driver.png",
                        "extents_mm": [180, 25, 25],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return src


def _segmented(tmp_path: Path) -> tuple[Path, Path]:
    photo = tmp_path / "photo.png"
    arr = np.full((90, 120, 3), 128, np.uint8)
    arr[20:60, 30:80] = (200, 40, 40)
    PIL_Image.fromarray(arr).save(photo)
    seg = tmp_path / "seg"
    (seg / "masks").mkdir(parents=True)
    mask = np.zeros((90, 120), np.uint8)
    mask[20:60, 30:80] = 255
    PIL_Image.fromarray(mask).save(seg / "masks" / "001.png")
    (seg / "instances.json").write_text(
        json.dumps(
            {
                "instances": [
                    {
                        "id": 1,
                        "bbox_xywh": [30, 20, 50, 40],
                        "area_px": 2000,
                        "mask": "masks/001.png",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return photo, seg / "instances.json"


def test_bank_help_documents_every_action_and_flag(tmp_path: Path) -> None:
    """A user opens `3d bank --help` to learn how to build, index and query a reference bank, and the texture warning."""
    result = run_cli(tmp_path, "bank", "--help")
    assert result.returncode == 0
    for word in (
        "thumbs",
        "index",
        "query",
        "--view",
        "--px",
        "--unit-scale",
        "--index",
        "--mask",
        "--instances",
        "--metric",
        "--topk",
        "textured",
        "TEXTURE",
    ):
        assert word in result.stdout


def test_recognise_help_documents_every_flag(tmp_path: Path) -> None:
    """A user opens `3d recognise --help` to find the vocabulary, bank and calibration flags and the output schema."""
    result = run_cli(tmp_path, "recognise", "--help")
    assert result.returncode == 0
    for word in (
        "--instances",
        "--categories",
        "--bank",
        "--topk",
        "--rerank-classes",
        "--metric",
        "--no-calibrate",
        "--pool-size",
        "--ids",
        "--device",
        "--json",
        "instances.json",
    ):
        assert word in result.stdout


def test_photo_bank_is_normalised_then_indexing_without_models_names_the_pull_command(
    tmp_path: Path,
) -> None:
    """A user turns their own photos into a bank, then tries to index it before pulling the CLIP/DINOv2 weights."""
    env = _env(tmp_path)
    thumbs = run_cli(
        tmp_path,
        "bank",
        "thumbs",
        str(_photo_bank(tmp_path) / "bank.json"),
        "-o",
        str(tmp_path / "bank"),
        "--json",
        env_extra=env,
    )
    assert thumbs.returncode == 0, thumbs.stderr
    doc = json.loads(thumbs.stdout)
    assert doc["textured"] is True
    assert doc["entries"][0]["extents_mm"] == [180, 25, 25]
    assert (tmp_path / "bank" / "driver.png").is_file()
    assert (
        json.loads((tmp_path / "bank" / "bank.json").read_text(encoding="utf-8"))[
            "entries"
        ][0]["id"]
        == "screwdriver_1"
    )
    index = run_cli(tmp_path, "bank", "index", str(tmp_path / "bank"), env_extra=env)
    assert index.returncode == 127, index.stderr
    assert "3d vision-models pull clip-b32" in index.stderr
    assert "Traceback" not in index.stderr
    assert not (tmp_path / "bank" / "index.npz").exists()


def test_recognise_without_models_exits_127(tmp_path: Path) -> None:
    """A user runs `3d recognise` on a segmented photo before pulling the CLIP weights and is told which command fixes it."""
    photo, inst = _segmented(tmp_path)
    result = run_cli(
        tmp_path,
        "recognise",
        str(photo),
        "--instances",
        str(inst),
        "--device",
        "cpu",
        env_extra=_env(tmp_path),
    )
    assert result.returncode == 127, result.stderr
    assert "3d vision-models pull clip-b32" in result.stderr
    assert "Traceback" not in result.stderr


def test_bad_inputs_are_exit_2_with_a_fix_hint(tmp_path: Path) -> None:
    """A user mistypes a path, a vocabulary or a view; each mistake is reported on stderr without a traceback."""
    env = _env(tmp_path)
    photo, inst = _segmented(tmp_path)
    cases = [
        (
            ("recognise", str(tmp_path / "nope.jpg"), "--instances", str(inst)),
            "image not found",
        ),
        (
            ("recognise", str(photo), "--instances", str(tmp_path / "none.json")),
            "3d segment",
        ),
        (
            ("recognise", str(photo), "--instances", str(inst), "--categories", "a,a"),
            "duplicate",
        ),
        (
            ("recognise", str(photo), "--instances", str(inst), "--ids", "3,a"),
            "comma-separated integers",
        ),
        (
            ("bank", "query", str(photo), "--index", str(tmp_path / "no-index")),
            "3d bank index",
        ),
        (
            ("bank", "thumbs", str(tmp_path / "no-meshes"), "-o", str(tmp_path / "o")),
            "bank source not found",
        ),
        (
            (
                "bank",
                "thumbs",
                str(tmp_path),
                "-o",
                str(tmp_path / "o"),
                "--view",
                "side",
            ),
            "invalid choice",
        ),
    ]
    for argv, needle in cases:
        result = run_cli(tmp_path, *argv, env_extra=env)
        assert result.returncode == 2, (argv, result.stderr)
        assert needle in result.stderr, argv
        assert "Traceback" not in result.stderr, argv
