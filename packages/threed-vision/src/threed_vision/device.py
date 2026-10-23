"""Device selection, VRAM budget and onnxruntime-gpu runtime setup."""
from __future__ import annotations

import contextlib
import ctypes
import glob
import os
import site
import subprocess
from typing import Any

from .errors import BadInput

Device = str  # "cuda" | "cpu"


def free_vram_mb() -> int | None:
    """Free VRAM of GPU 0 in MiB, or None when there is no usable CUDA device.

    When torch is importable its answer is authoritative (a CPU-only torch, an exclusive-process GPU held elsewhere
    or broken nvidia libs mean "no usable device" for the torch stages: None). nvidia-smi is only consulted when
    torch is not installed at all (ORT-only environments).
    """
    try:
        import torch
    except ImportError:
        pass
    except OSError:  # torch present but its CUDA libs fail to load
        return None
    else:
        try:
            return int(torch.cuda.mem_get_info()[0] // (1 << 20)) if torch.cuda.is_available() else None
        except RuntimeError:
            return None
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
    if preference not in ("auto", "cuda", "cpu"):
        raise BadInput(f"unknown device {preference!r}", "use auto, cuda or cpu")
    if preference == "cpu":
        return "cpu"
    if preference == "cuda":
        return "cuda"
    free = free_vram_mb()
    return "cuda" if free is not None and free >= need_vram_mb else "cpu"


# Shared libraries the CUDA-12 build of onnxruntime-gpu (1.23.x) dlopen()s for its CUDA provider.
ORT_CUDA12_LIBS = ("libcudart.so.12", "libcublas.so.12", "libcublasLt.so.12", "libcufft.so.11", "libcudnn.so.9")


def ort_missing_cuda_libs() -> list[str]:
    """Names of the CUDA-12 runtime libraries onnxruntime-gpu needs that cannot be loaded (empty = usable).

    `ort.get_available_providers()` lists CUDAExecutionProvider even when these are missing, and the session
    then silently falls back to CPU, so a loadability probe is the only honest check.
    """
    return [lib for lib in ORT_CUDA12_LIBS if not _loadable(lib)]


def _loadable(lib: str) -> bool:
    try:
        ctypes.CDLL(lib)
    except OSError:
        return False
    return True


def _extend_library_path() -> None:
    """Prepend the venv's nvidia-*/lib dirs to LD_LIBRARY_PATH once (idempotent; never leaves an empty entry,
    which the loader treats as the current directory). Only child processes see it: glibc reads the variable at
    process start, so this process relies on `ort.preload_dlls` / already-loaded libs (torch loads its own)."""
    wanted = [d for sp in site.getsitepackages() for d in sorted(glob.glob(os.path.join(sp, "nvidia", "*", "lib")))]
    current = [p for p in os.environ.get("LD_LIBRARY_PATH", "").split(":") if p]
    merged = [*(d for d in wanted if d not in current), *current]
    if merged:
        os.environ["LD_LIBRARY_PATH"] = ":".join(merged)


def setup_ort_cuda() -> bool:
    """Make onnxruntime-gpu (CUDA-12 build) find cuDNN/cuBLAS from the venv's nvidia-* wheels (torch cu128 ships them).

    Returns True only when the CUDA provider is listed AND its CUDA-12 libraries load. Idempotent.
    """
    try:
        import onnxruntime as ort
    except ImportError:
        return False
    _extend_library_path()
    if hasattr(ort, "preload_dlls"):
        with contextlib.suppress(Exception):  # best-effort; the loadability probe below is authoritative
            ort.preload_dlls(cuda=True, cudnn=True, directory=None)
    return "CUDAExecutionProvider" in ort.get_available_providers() and not ort_missing_cuda_libs()


def ort_providers(device: Device) -> list[str]:
    return ["CUDAExecutionProvider", "CPUExecutionProvider"] if device == "cuda" else ["CPUExecutionProvider"]


def ort_diagnose() -> dict[str, Any]:
    """What `3d doctor` reports: installed onnxruntime flavour and whether CUDA actually loads."""
    try:
        import onnxruntime as ort
    except ImportError:
        return {"installed": False}
    usable = setup_ort_cuda()
    providers = ort.get_available_providers()
    return {
        "installed": True,
        "version": ort.__version__,
        "providers": providers,
        # the CPU-only `onnxruntime` wheel lists no CUDAExecutionProvider at all: no CUDA lib is "missing", the wrong wheel is installed
        "cpu_only_wheel": "CUDAExecutionProvider" not in providers,
        "cuda_usable": usable,
        "missing_cuda_libs": [] if usable else ort_missing_cuda_libs(),
    }
