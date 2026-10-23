"""Small torch/CUDA helpers shared by the model-backed stages (torch is imported lazily inside each function)."""

from __future__ import annotations

import gc

from .errors import MissingDependency


def require_torch() -> None:
    try:
        import torch  # noqa: F401
    except ImportError as exc:
        raise MissingDependency(
            "torch is not installed (needed for SAM2 / OWLv2)",
            "uv pip install torch==2.11.0 torchvision==0.26.0 --index-url https://download.pytorch.org/whl/cu128 "
            "--extra-index-url https://pypi.org/simple --index-strategy unsafe-best-match && "
            "uv pip install -e 'packages/threed-vision[gpu,seg]'",
        ) from exc


def require_cuda_if_requested(device: str) -> None:
    """An explicit `cuda` request without a usable CUDA torch is a missing dependency (exit 127), not a traceback."""
    if device != "cuda":
        return
    import torch

    if not torch.cuda.is_available():
        raise MissingDependency(
            "device 'cuda' was requested but torch cannot see a CUDA GPU",
            "use --device auto|cpu, or install the CUDA build of torch (cu128 index) and check `3d doctor`",
        )


def is_oom(exc: BaseException) -> bool:
    """True for a CUDA out-of-memory failure (torch.cuda.OutOfMemoryError or a RuntimeError saying so)."""
    return (
        type(exc).__name__ == "OutOfMemoryError" or "out of memory" in str(exc).lower()
    )


def free_cuda() -> None:
    """Drop Python references first (caller), then return cached blocks to the driver.

    Only when this process already initialised CUDA: a CPU-only run (`--device cpu`, the GPU is busy or absent) must never
    be the one that creates a CUDA context, so there is nothing to free and nothing is touched.
    """
    gc.collect()
    try:
        import torch
    except ImportError:
        return
    if torch.cuda.is_initialized():
        torch.cuda.empty_cache()


def cuda_allocated_mb() -> int:
    """torch.cuda.memory_allocated of the current device in MiB (0 when CUDA was never initialised)."""
    try:
        import torch
    except ImportError:
        return 0
    return (
        int(torch.cuda.memory_allocated() // (1 << 20))
        if torch.cuda.is_initialized()
        else 0
    )


def reset_peak(device: str) -> None:
    """Reset torch's peak-allocation counter; a no-op unless `device` is "cuda"."""
    if device != "cuda":
        return
    try:
        import torch
    except ImportError:
        return
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()


def peak_mb(device: str) -> int:
    """Peak torch CUDA allocation in MiB since `reset_peak`; 0 unless `device` is "cuda"."""
    if device != "cuda":
        return 0
    try:
        import torch
    except ImportError:
        return 0
    return (
        int(torch.cuda.max_memory_allocated() // (1 << 20))
        if torch.cuda.is_available()
        else 0
    )
