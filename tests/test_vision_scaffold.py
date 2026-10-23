"""threed_vision scaffold contracts: package discovery, JSON helpers, exit codes, launcher resolution."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

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
    pytest.importorskip("cv2")  # mask_polygon needs OpenCV; the gate env of the fast hook has none
    m = np.zeros((20, 30), bool)
    m[5:10, 8:20] = True
    assert io.mask_bbox_xywh(m) == [8, 5, 12, 5]
    assert io.mask_iou(m, m) == 1.0
    assert io.mask_iou(m, ~m) == 0.0
    assert len(io.mask_polygon(m)) >= 4


def test_load_rgb_missing_is_bad_input(tmp_path: Path) -> None:
    pytest.importorskip("PIL")
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

    monkeypatch.setenv("THREED_VISION_PYTHON", sys.executable)
    assert vision_env.stage_argv("info", ["--json"], heavy=True) == [sys.executable, "-m", "threed_vision", "info", "--json"]


def test_heavy_stage_without_venv_raises_with_install_hint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from cli import vision_env
    from errors import MissingDependency

    monkeypatch.delenv("THREED_VISION_PYTHON", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    with pytest.raises(MissingDependency) as exc:
        vision_env.stage_argv("segment", [], heavy=True)
    assert "uv venv" in exc.value.install


def test_unknown_device_preference_is_rejected() -> None:
    from threed_vision import device

    with pytest.raises(errors.BadInput):
        device.resolve_device("gpu")
    assert device.resolve_device("cpu") == "cpu"


def test_library_path_extension_is_idempotent_and_never_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import site

    from threed_vision import device

    lib = tmp_path / "nvidia" / "cublas" / "lib"
    lib.mkdir(parents=True)
    monkeypatch.setattr(site, "getsitepackages", lambda: [str(tmp_path)])
    monkeypatch.setenv("LD_LIBRARY_PATH", f":{lib}")  # leading empty entry + a duplicate of what we add
    device._extend_library_path()
    device._extend_library_path()
    parts = os.environ["LD_LIBRARY_PATH"].split(":")
    assert parts == [str(lib)]


def test_free_vram_is_none_when_torch_cannot_reach_cuda(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    from threed_vision import device

    fake = types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: False))
    monkeypatch.setitem(sys.modules, "torch", fake)
    assert device.free_vram_mb() is None
    assert device.resolve_device("auto", 100) == "cpu"


def test_empty_labelmap_needs_shape_and_keeps_it() -> None:
    with pytest.raises(errors.BadInput):
        io.masks_to_labelmap([])
    assert io.masks_to_labelmap([], shape=(4, 6)).shape == (4, 6)


def test_missing_explicit_python_is_structured(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli import vision_env
    from errors import MissingDependency

    monkeypatch.setenv("THREED_VISION_PYTHON", "/nonexistent/python3.99")
    with pytest.raises(MissingDependency):
        vision_env.stage_argv("info", [], heavy=False)
