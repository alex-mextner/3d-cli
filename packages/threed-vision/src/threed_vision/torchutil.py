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


def is_oom(exc: BaseException) -> bool:
    """True for a CUDA out-of-memory failure (torch.cuda.OutOfMemoryError or a RuntimeError saying so)."""
    return (
        type(exc).__name__ == "OutOfMemoryError" or "out of memory" in str(exc).lower()
    )


def free_cuda() -> None:
    """Drop Python references first (caller), then return cached blocks to the driver."""
    gc.collect()
    try:
        import torch
    except ImportError:
        return
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def cuda_allocated_mb() -> int:
    """torch.cuda.memory_allocated of the current device in MiB (0 without CUDA)."""
    try:
        import torch
    except ImportError:
        return 0
    return (
        int(torch.cuda.memory_allocated() // (1 << 20))
        if torch.cuda.is_available()
        else 0
    )


def reset_peak() -> None:
    try:
        import torch
    except ImportError:
        return
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()


def peak_mb() -> int:
    try:
        import torch
    except ImportError:
        return 0
    return (
        int(torch.cuda.max_memory_allocated() // (1 << 20))
        if torch.cuda.is_available()
        else 0
    )
