"""Staging cleanup must not reverse an already committed conversion outcome."""
from __future__ import annotations
from contextlib import contextmanager
from pathlib import Path
import tempfile
from typing import Iterator

@contextmanager
def workspace(parent: Path, prefix: str, warnings: list[str]) -> Iterator[Path]:
    directory = tempfile.TemporaryDirectory(prefix=prefix, dir=parent)
    try:
        yield Path(directory.name)
    finally:
        try:
            directory.cleanup()
        except OSError as exc:
            warnings.append(f"Temporary cleanup failed at {directory.name}: {exc}")
