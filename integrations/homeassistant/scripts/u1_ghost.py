"""Ghost of the rest of the print for the U1 camera proxy (3d-cli issue #261).

3d-cli's lib/vision_ghost renders the walls still to be printed through the bundled U1 camera
calibration (lib/data/vision/u1-camera.yaml). A render takes ~2 s on the ODROID-N2+, so it runs
on one worker thread once per (job, layer, frame size); every frame only blends a cached result.
While the next layer renders, the previous ghost stays up if it is at most MAX_LAG_MM below the
current layer (one or a few layers: under a pixel at this camera's scale). Toggled off, not
ready, failed, or for another job: frames pass through untouched.

The scripts are symlinks into the 3d-cli submodule, so the library is found next to them:
<3d-cli>/integrations/homeassistant/scripts/u1_ghost.py -> <3d-cli>/lib.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import math
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any

LIB = Path(__file__).resolve().parents[3] / "lib"
CAMERA_MODEL = "Snapmaker U1"
MAX_LAG_MM = 1.0


def default_enabled() -> bool:
    return os.environ.get("U1_GHOST", "on").strip().lower() not in (
        "0",
        "off",
        "false",
        "no",
    )


def _modules() -> tuple[Any, Any]:
    if str(LIB) not in sys.path:
        sys.path.insert(0, str(LIB))
    import vision_geometry
    import vision_ghost

    return vision_geometry, vision_ghost


class IsolatedShell:
    """Wraps a vision_ghost.ShellBuilder for the index pass: any failure inside it only drops
    the ghost (finish() -> None) and is reported, never breaking the G-code index."""

    def __init__(self, builder, report):
        self.builder = builder
        self.report = report

    def _fail(self, exc: Exception) -> None:
        self.builder = None
        self.report(f"{type(exc).__name__}: wall collection failed: {str(exc)[:160]}")

    def comment(self, text: str) -> None:
        if self.builder is not None:
            try:
                self.builder.comment(text)
            except Exception as exc:  # noqa: BLE001 - the ghost is optional
                self._fail(exc)

    def move(self, *args) -> None:
        if self.builder is not None:
            try:
                self.builder.move(*args)
            except Exception as exc:  # noqa: BLE001 - the ghost is optional
                self._fail(exc)

    def finish(self):
        if self.builder is None:
            return None
        try:
            return self.builder.finish()
        except Exception as exc:  # noqa: BLE001 - the ghost is optional
            self._fail(exc)
            return None


class GhostRenderer:
    def __init__(self, enabled: bool | None = None):
        self.lock = threading.Lock()
        self.enabled = default_enabled() if enabled is None else enabled
        # ((job, layer, shape), Ghost or None): the last finished render
        self.cache: tuple[tuple, Any] | None = None
        self.pending: tuple | None = None
        self.error: str | None = None
        self.render_seconds: float | None = None
        self._camera = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="u1-ghost")

    def _report(self, message: str) -> None:
        with self.lock:
            self.error = message

    def new_builder(self) -> IsolatedShell | None:
        """A wall collector for the G-code index pass, or None when the 3d-cli library is missing."""
        try:
            builder = _modules()[1].ShellBuilder()
        except Exception as exc:  # noqa: BLE001 - a missing library only disables the ghost
            self._report(
                f"{type(exc).__name__}: 3d-cli vision library unavailable: {str(exc)[:160]}"
            )
            return None
        return IsolatedShell(builder, self._report)

    def set_enabled(self, enabled: bool) -> None:
        with self.lock:
            self.enabled = bool(enabled)

    def state(self) -> dict:
        with self.lock:
            key, ghost = self.cache or (None, None)
            return {
                "enabled": self.enabled,
                "layer": key[1] if key else None,
                "layer_has_ghost": ghost is not None,
                "rendering_layer": self.pending[1] if self.pending else None,
                "render_seconds": self.render_seconds,
                "error": self.error,
            }

    def ghost_for(
        self, job: str | None, shell, layer: int | None, shape: tuple[int, int]
    ):
        """The ghost to draw for this job/layer/frame size, or None. Renders the exact layer in the
        background; meanwhile a render of the same job up to MAX_LAG_MM below is shown."""
        if job is None or shell is None or layer is None:
            return None
        key = (job, layer, tuple(shape))
        shown = None
        with self.lock:
            if not self.enabled:
                return None
            if self.cache is not None:
                (c_job, c_layer, c_shape), c_ghost = self.cache
                if (c_job, c_layer, c_shape) == key:
                    return c_ghost
                if c_job == job and c_shape == key[2]:
                    heights = getattr(shell, "layer_z", {})
                    lag = heights.get(layer, math.inf) - heights.get(c_layer, -math.inf)
                    if 0 <= lag <= MAX_LAG_MM:
                        shown = c_ghost
            if self.pending is None:
                self.pending = key
                self.executor.submit(self._render, key, shell)
        return shown

    def _render(self, key: tuple, shell) -> None:
        started = time.monotonic()
        ghost, error = None, None
        try:
            geometry, vision_ghost = _modules()
            if self._camera is None:
                self._camera = geometry.bundled_calibration(CAMERA_MODEL)
            if self._camera is None:
                raise LookupError("no bundled U1 camera calibration")
            ghost = vision_ghost.render(shell, self._camera, key[1], key[2])
        except Exception as exc:  # noqa: BLE001 - never break the camera stream
            error = f"{type(exc).__name__}: ghost render failed: {str(exc)[:160]}"
        with self.lock:
            self.cache, self.pending = (key, ghost), None
            self.error = error
            self.render_seconds = round(time.monotonic() - started, 2)
