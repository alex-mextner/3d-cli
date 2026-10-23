"""`3d printer vision` tier (b) flags end to end through bin/3d: `fit-head`, `fetch-model --tier`,
`--model/--no-model` and `eval --model`. The surface checks run offline: every case fails or
prints help before any weights would be downloaded (no printer, no network, no model)."""
from __future__ import annotations

from pathlib import Path

import pytest

from .workflow_helper import require_cli_python_deps, run_cli

pytest.importorskip("numpy")
pytest.importorskip("cv2")


def _need_deps(tmp_path: Path) -> None:
    require_cli_python_deps(
        tmp_path,
        ["numpy", "opencv-python-headless", "onnxruntime", "pyyaml", "scipy"],
        ["numpy", "cv2", "onnxruntime", "yaml", "scipy"],
    )


def test_fit_head_help_names_the_class_and_the_label_folders(tmp_path: Path) -> None:
    """The owner reads `vision fit-head --help` before labelling frames: it names the class and the positive/negative folders."""
    _need_deps(tmp_path)
    r = run_cli(tmp_path, "printer", "vision", "fit-head", "--help")
    assert r.returncode == 0, r.stderr
    for flag in ("--class", "--pos", "--neg", "--fpr", "--folds", "--out"):
        assert flag in r.stdout


def test_fit_head_with_an_unknown_class_lists_the_accepted_ones(tmp_path: Path) -> None:
    """`fit-head --class cats` is refused with the list of classes the model knows."""
    _need_deps(tmp_path)
    r = run_cli(tmp_path, "printer", "vision", "fit-head", "--class", "cats", "--pos", "a", "--neg", "b")
    assert r.returncode == 2
    assert "part_detached" in r.stderr and "spaghetti" in r.stderr and "empty_bed" in r.stderr


def test_fit_head_with_a_missing_folder_is_a_usage_error_before_any_download(tmp_path: Path) -> None:
    """`fit-head` with a folder that does not exist fails as a usage error before any weights are downloaded."""
    _need_deps(tmp_path)
    r = run_cli(
        tmp_path, "printer", "vision", "fit-head", "--class", "spaghetti",
        "--pos", str(tmp_path / "nope"), "--neg", str(tmp_path),
    )
    assert r.returncode == 2
    assert "missing path" in r.stderr and "nope" in r.stderr


def test_fetch_model_tier_only_accepts_a_b_all(tmp_path: Path) -> None:
    """`fetch-model --tier z` is refused and the accepted tiers are listed."""
    _need_deps(tmp_path)
    r = run_cli(tmp_path, "printer", "vision", "fetch-model", "--tier", "z")
    assert r.returncode == 2
    assert "'a', 'b', 'all'" in r.stderr


def test_run_and_eval_document_the_model_flags(tmp_path: Path) -> None:
    """`vision run --help` and `vision eval --help` document the tier (b) model flags."""
    _need_deps(tmp_path)
    run = run_cli(tmp_path, "printer", "vision", "run", "--help")
    assert run.returncode == 0
    assert "--model" in run.stdout and "--no-model" in run.stdout
    ev = run_cli(tmp_path, "printer", "vision", "eval", "--help")
    assert ev.returncode == 0
    assert "--model" in ev.stdout and "--model-class" in ev.stdout
