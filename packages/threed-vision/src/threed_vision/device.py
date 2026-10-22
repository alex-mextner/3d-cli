"""Device selection, VRAM budget and onnxruntime-gpu runtime setup."""
from __future__ import annotations

import contextlib
import glob
import os
import site
import subprocess
from typing import Any

Device = str  # "cuda" | "cpu"


def free_vram_mb() -> int | None:
    """Free VRAM of GPU 0 in MiB, or None when there is no usable CUDA device."""
    try:
        import torch

        if torch.cuda.is_available():
            return int(torch.cuda.mem_get_info()[0] // (1 << 20))
        return None
    except ImportError:
        pass
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip().splitlines()
        return int(out[0]) if out else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def resolve_device(preference: str = "auto", need_vram_mb: int = 0) -> Device:
    """'auto' -> cuda when a GPU has `need_vram_mb` free, else cpu. 'cuda' is strict (caller handles absence)."""
    if preference == "cpu":
        return "cpu"
    free = free_vram_mb()
    if preference == "cuda":
        return "cuda"
    return "cuda" if free is not None and free >= need_vram_mb else "cpu"


def setup_ort_cuda() -> bool:
    """Make onnxruntime-gpu (CUDA-12 build) find cuDNN/cuBLAS from the venv's nvidia-* wheels (torch ships them).

    Returns True when the CUDA execution provider is usable. Idempotent.
    """
    try:
        import onnxruntime as ort
    except ImportError:
        return False
    for sp in site.getsitepackages():
        libs = sorted(glob.glob(os.path.join(sp, "nvidia", "*", "lib")))
        if libs:
            os.environ["LD_LIBRARY_PATH"] = ":".join([*libs, os.environ.get("LD_LIBRARY_PATH", "")])
    if hasattr(ort, "preload_dlls"):
        with contextlib.suppress(Exception):  # best-effort; the provider probe below is authoritative
            ort.preload_dlls(cuda=True, cudnn=True, directory=None)
    return "CUDAExecutionProvider" in ort.get_available_providers()


def ort_providers(device: Device) -> list[str]:
    return ["CUDAExecutionProvider", "CPUExecutionProvider"] if device == "cuda" else ["CPUExecutionProvider"]


def ort_diagnose() -> dict[str, Any]:
    """What `3d doctor` reports: installed onnxruntime flavour and whether CUDA actually loads."""
    try:
        import onnxruntime as ort
    except ImportError:
        return {"installed": False}
    usable = setup_ort_cuda()
    return {"installed": True, "version": ort.__version__, "providers": ort.get_available_providers(), "cuda_usable": usable}
