"""threed_vision scaffold contracts: package discovery, JSON helpers, exit codes, launcher resolution."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("PIL")

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "packages" / "threed-vision" / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(ROOT / "lib"))

from threed_vision import cli, errors, io  # noqa: E402


def test_discovery_finds_info_command() -> None:
    cmds = cli.discover()
    assert "info" in cmds
    assert all(hasattr(m, "run") and hasattr(m, "add_arguments") and m.SUMMARY for m in cmds.values())


def test_to_jsonable_handles_numpy_and_nonfinite() -> None:
    out = io.to_jsonable({"a": np.float32(1.5), "b": np.arange(3), "c": float("nan"), "d": (np.int64(2),)})
    assert json.loads(json.dumps(out)) == {"a": 1.5, "b": [0, 1, 2], "c": None, "d": [2]}


def test_mask_helpers_roundtrip() -> None:
    m = np.zeros((20, 30), bool)
    m[5:10, 8:20] = True
    assert io.mask_bbox_xywh(m) == [8, 5, 12, 5]
    assert io.mask_iou(m, m) == 1.0
    assert io.mask_iou(m, ~m) == 0.0
    assert len(io.mask_polygon(m)) >= 4


def test_load_rgb_missing_is_bad_input(tmp_path: Path) -> None:
    with pytest.raises(errors.BadInput):
        io.load_rgb(tmp_path / "nope.png")


def test_exit_codes_are_stable() -> None:
    assert (errors.BadInput.exit_code, errors.LowConfidence.exit_code, errors.MissingDependency.exit_code) == (2, 3, 127)


def test_module_entrypoint_runs_info() -> None:
    env = dict(os.environ, PYTHONPATH=str(SRC))
    r = subprocess.run([sys.executable, "-m", "threed_vision", "info"], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["version"]


def test_stage_argv_prefers_explicit_python(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli import vision_env

    monkeypatch.setenv("THREED_VISION_PYTHON", "/opt/vpy")
    assert vision_env.stage_argv("info", ["--json"], heavy=True) == ["/opt/vpy", "-m", "threed_vision", "info", "--json"]


def test_heavy_stage_without_venv_raises_with_install_hint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from cli import vision_env
    from errors import MissingDependency

    monkeypatch.delenv("THREED_VISION_PYTHON", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    with pytest.raises(MissingDependency) as exc:
        vision_env.stage_argv("segment", [], heavy=True)
    assert "uv venv" in exc.value.install
