"""`3d doctor` vision rows: ORT-CUDA state from `threed_vision info` and the models inventory."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "packages" / "threed-vision" / "src"))

doctor = importlib.import_module("commands.doctor")
stage_json = importlib.import_module("cli.vision_env").stage_json


def _run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payloads: dict[str, Any],
) -> str:
    monkeypatch.setattr(doctor, "stage_json", lambda stage, args: payloads.get(stage))
    doctor.run([])
    return capsys.readouterr().out


def test_doctor_names_the_missing_cuda_library_when_ort_falls_back_to_cpu(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    info = {
        "onnxruntime": {
            "installed": True,
            "version": "1.23.2",
            "cuda_usable": False,
            "missing_cuda_libs": ["libcudnn.so.9", "libcublasLt.so.12"],
        }
    }
    out = _run(
        monkeypatch,
        capsys,
        {
            "info": info,
            "vision-models": {
                "models_dir": "/m",
                "models": {
                    "sam2-tiny": {"installed": False},
                    "u2net": {"installed": True},
                },
            },
        },
    )
    line = next(ln for ln in out.splitlines() if "onnxruntime CUDA" in ln)
    assert "WARN" in line and "libcudnn.so.9" in line and "libcublasLt.so.12" in line
    assert "3d vision-models pull sam2-tiny" in out


def test_doctor_passes_when_cuda_provider_loads_and_models_are_present(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    info = {
        "onnxruntime": {
            "installed": True,
            "version": "1.23.2",
            "cuda_usable": True,
            "missing_cuda_libs": [],
        }
    }
    out = _run(
        monkeypatch,
        capsys,
        {
            "info": info,
            "vision-models": {
                "models_dir": "/m",
                "models": {"u2net": {"installed": True}},
            },
        },
    )
    assert any("PASS" in ln and "onnxruntime CUDA" in ln for ln in out.splitlines())
    assert any("PASS" in ln and "vision models" in ln for ln in out.splitlines())


def test_doctor_without_a_vision_env_only_warns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _run(monkeypatch, capsys, {})
    assert any("WARN" in ln and "vision python env" in ln for ln in out.splitlines())
    assert not any("MISSING" in ln and "vision" in ln for ln in out.splitlines())


def test_vision_stage_json_is_none_without_an_interpreter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("THREED_VISION_PYTHON", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert stage_json("info", []) is None


def test_vision_stage_json_parses_one_json_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("THREED_VISION_PYTHON", sys.executable)
    info = stage_json("info", [])
    assert info is not None and "onnxruntime" in info
