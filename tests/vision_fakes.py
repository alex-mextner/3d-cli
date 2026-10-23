"""Deterministic stand-ins for the CLIP / DINOv2 ONNX encoders (numpy only) for the recognise / bank tests.

Colour is the whole "appearance": a crop is embedded by the palette colour nearest to its mean non-padding colour, a text prompt
by the palette colour word it contains. `bias` adds a constant pull towards palette entry 0 (CLIP's "battery" effect that the
per-class mean calibration is there to remove).
"""

from __future__ import annotations

from typing import Any

import numpy as np

from threed_vision.clip import PAD_RGB

PALETTE: dict[str, tuple[int, int, int]] = {
    "red": (220, 30, 30),
    "green": (30, 200, 30),
    "blue": (30, 30, 220),
    "yellow": (230, 220, 30),
    "cyan": (30, 220, 220),
    "magenta": (220, 30, 220),
    "white": (245, 245, 245),
    "black": (10, 10, 10),
}
NAMES = list(PALETTE)
DIM = len(NAMES)


def object_colour(crop: np.ndarray) -> np.ndarray:
    """Mean colour of the crop pixels that are neither the CLIP padding grey nor the bank thumbnail background."""
    px = crop.reshape(-1, 3).astype(np.float64)
    keep = ~(
        (px == np.array(PAD_RGB)).all(1) | (px == np.array((235, 235, 235))).all(1)
    )
    return np.asarray((px[keep] if keep.any() else px).mean(axis=0))


def colour_index(crop: np.ndarray) -> int:
    c = object_colour(crop)
    return int(np.argmin([np.linalg.norm(c - np.array(v)) for v in PALETTE.values()]))


def _unit(v: np.ndarray) -> np.ndarray:
    return np.asarray(v / max(float(np.linalg.norm(v)), 1e-12), np.float32)


class FakeClip:
    """`ClipEncoder` stand-in: images -> one-hot colour (+ bias), texts -> one-hot of the colour word in the prompt."""

    model_name = "clip-b32"
    vram_estimate_mb = 1

    def __init__(self, bias: float = 0.0) -> None:
        self.bias = bias
        self.loaded = False
        self.device = "cpu"
        self.text_calls: list[list[str]] = []
        self.image_calls = 0

    def load(self) -> None:
        self.loaded = True

    def unload(self) -> None:
        self.loaded = False

    def images(self, crops: list[np.ndarray]) -> np.ndarray:
        self.loaded = True
        self.image_calls += 1
        out = np.zeros((len(crops), DIM), np.float32)
        for i, crop in enumerate(crops):
            out[i, colour_index(crop)] = 1.0
            out[i, 0] += self.bias
            out[i] = _unit(out[i])
        return out

    def texts(self, prompts: list[str]) -> np.ndarray:
        self.loaded = True
        self.text_calls.append(list(prompts))
        out = np.zeros((len(prompts), DIM), np.float32)
        for i, prompt in enumerate(prompts):
            words = prompt.lower().replace(".", " ").split()
            hit = [k for k, name in enumerate(NAMES) if name in words]
            out[i, hit[0] if hit else 0] = 1.0
        return out


class FakeDino:
    """`DinoEncoder` stand-in: colour one-hot plus the crop's tallness, so an unrotated wide crop differs from an upright one."""

    model_name = "dinov2-s"
    vram_estimate_mb = 1

    def __init__(self) -> None:
        self.loaded = False
        self.device = "cpu"

    def load(self) -> None:
        self.loaded = True

    def unload(self) -> None:
        self.loaded = False

    def images(self, crops: list[np.ndarray]) -> np.ndarray:
        self.loaded = True
        out = np.zeros((len(crops), DIM + 1), np.float32)
        for i, crop in enumerate(crops):
            out[i, colour_index(crop)] = 1.0
            h, w = crop.shape[:2]
            out[i, DIM] = 0.3 * h / (h + w)
            out[i] = _unit(out[i])
        return out


def patch_encoders(
    monkeypatch: Any, *modules: Any, bias: float = 0.0
) -> tuple[FakeClip, FakeDino]:
    """Replace `ClipEncoder` / `DinoEncoder` in the given modules with factories returning ONE shared fake instance each."""
    clip, dino = FakeClip(bias), FakeDino()
    for mod in modules:
        monkeypatch.setattr(mod, "ClipEncoder", lambda device="auto", _c=clip: _c)
        monkeypatch.setattr(mod, "DinoEncoder", lambda device="auto", _d=dino: _d)
    return clip, dino


def scene(
    width: int = 480, height: int = 240
) -> tuple[np.ndarray, list[dict[str, Any]]]:
    """A grey canvas with one coloured rectangle per palette entry (two rows of 4) and their instance dicts (ids 1..8)."""
    rgb = np.full((height, width, 3), 128, np.uint8)
    instances = []
    cw, ch = width // 4, height // 2
    for k, rgb_value in enumerate(PALETTE.values()):
        x0, y0 = (k % 4) * cw + 20, (k // 4) * ch + 20
        x1, y1 = x0 + cw - 40, y0 + ch - 40
        rgb[y0:y1, x0:x1] = rgb_value
        mask = np.zeros((height, width), bool)
        mask[y0:y1, x0:x1] = True
        instances.append({"id": k + 1, "mask": mask})
    return rgb, instances
