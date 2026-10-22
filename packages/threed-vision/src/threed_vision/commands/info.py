"""`info`: versions, device and onnxruntime runtime state (what `3d doctor` shows for the vision stack)."""
from __future__ import annotations

import argparse
import json

from .. import __version__
from ..device import free_vram_mb, ort_diagnose

NAME = "info"
SUMMARY = "print threed_vision version, GPU free VRAM and onnxruntime CUDA state as JSON"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="accepted for symmetry; output is always JSON")


def run(args: argparse.Namespace) -> int:
    del args
    print(json.dumps({"version": __version__, "free_vram_mb": free_vram_mb(), "onnxruntime": ort_diagnose()}))
    return 0
