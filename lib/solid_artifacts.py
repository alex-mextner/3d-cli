"""Staging cleanup must not reverse an already committed conversion outcome."""

from __future__ import annotations
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import tempfile
from errors import ThreeDError


@contextmanager
def workspace(parent: Path, prefix: str, warnings: list[str]) -> Iterator[Path]:
    try:
        directory = tempfile.TemporaryDirectory(prefix=prefix, dir=parent)
    except OSError as exc:
        raise ThreeDError(
            f"could not create a staging directory beside the destination: {exc}",
            command="solid",
        ) from exc
    try:
        yield Path(directory.name)
    finally:
        try:
            directory.cleanup()
        except OSError as exc:
            warnings.append(f"Temporary cleanup failed at {directory.name}: {exc}")
