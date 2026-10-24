"""mix_target.py — the colour to match, read from a region of a mockup photo or render (#415).

WHAT: `read_target()` takes an image, a box of pixels to match and (for a photo) a box on a white
  reference, and returns the target hex with what it came from. The pixels of the box are
  white-balanced, converted to CIELAB one by one and the target is the per-channel median in Lab
  (a median, so a shadow edge or a glint inside the box does not drag it). The balance is a
  per-channel gain in linear light that maps the median of the white box to `white_level`
  (default 235/255, as the trim of babushkin-dom gen/colour_match.py `photo_colours`, which this
  ports): it removes the colour cast and the exposure of the photo in one step. A render
  (Blender, a mockup tool) has no cast: `white=None` skips the balance.
WHY: epic #410 / issue #415, tg#22816: the target of a mix is usually a spot in a photo of the
  mockup, not a hex; picking pixels by hand gave wrong targets before `photo_samples.json`.
  `3d mix tester --target-photo` records `Reading.to_dict()` in the manifest, so the target can be
  traced to the image (sha256), the box and the balance.
INVARIANTS: numpy and Pillow are imported inside the functions (the registry imports every
  command module on every `3d` call); a box outside the image is an error, never clipped; the
  result depends only on the pixels (a box above `MAX_SAMPLES` pixels is read with a fixed
  stride); the Lab of a pixel is the one of printops.color (D65, IEC 61966-2-1).
"""

from __future__ import annotations

import hashlib
import io
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from printops.color import hex_from_linear

SCHEMA = "mix-target/1"
# the 8-bit value the white reference is mapped to (babushkin-dom trim)
DEFAULT_WHITE_LEVEL = 235
# a white box with a channel median below this is not a white reference
MIN_WHITE_CHANNEL = 64
# pixels read from a box; a larger box is read with a fixed stride
MAX_SAMPLES = 1_000_000

_D65 = (95.047, 100.0, 108.883)


class MixTargetError(ValueError):
    """The image, a box or the white reference cannot give a target."""


@dataclass(frozen=True)
class Box:
    """Pixel box [x0, x1) × [y0, y1) of the image as a viewer shows it (top-left origin)."""

    x0: int
    y0: int
    x1: int
    y1: int

    def as_list(self) -> list[int]:
        return [self.x0, self.y0, self.x1, self.y1]


def parse_box(text: str, flag: str) -> Box:
    """`x0,y0,x1,y1` → Box (integers, x0 < x1, y0 < y1, not negative)."""
    parts = [p.strip() for p in text.split(",")]
    try:
        v = [int(p) for p in parts]
    except ValueError:
        v = []
    if len(v) != 4:
        raise MixTargetError(
            f"{flag} {text!r} is not x0,y0,x1,y1 (four integer pixel coordinates)"
        )
    box = Box(v[0], v[1], v[2], v[3])
    if min(v) < 0 or box.x0 >= box.x1 or box.y0 >= box.y1:
        raise MixTargetError(
            f"{flag} {text!r}: need 0 <= x0 < x1 and 0 <= y0 < y1 (x1/y1 exclusive)"
        )
    return box


@dataclass(frozen=True)
class Reading:
    """The target and its provenance (`mix-target/1`)."""

    target: str
    lab: tuple[float, float, float]
    samples: int
    image: str
    sha256: str
    image_size: tuple[int, int]
    box: Box
    white_box: Box | None
    white_level: int | None
    white_rgb: tuple[int, int, int] | None
    gain: tuple[float, float, float] | None

    @property
    def kind(self) -> str:
        return "photo" if self.white_box is not None else "render"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "target": self.target,
            "lab": [round(v, 2) for v in self.lab],
            "kind": self.kind,
            "image": self.image,
            "sha256": self.sha256,
            "image_size": list(self.image_size),
            "box": self.box.as_list(),
            "samples": self.samples,
            "white_balance": self.white_box is not None,
            "white_box": self.white_box.as_list() if self.white_box else None,
            "white_level": self.white_level,
            "white_rgb": list(self.white_rgb) if self.white_rgb else None,
            "gain": [round(g, 4) for g in self.gain] if self.gain else None,
        }


SOURCE_KEYS = (
    "kind",
    "image",
    "sha256",
    "image_size",
    "box",
    "white_balance",
    "white_box",
    "white_level",
)


def source_of(record: dict[str, Any]) -> dict[str, Any]:
    """What a tester manifest records about its target, from a `mix-target/1` record: the image,
    boxes, balance and sha256 (the colour itself is the manifest's `target`)."""
    return {k: record[k] for k in SOURCE_KEYS}


def decode_srgb(c: Any) -> Any:
    """sRGB 0..1 → linear light (IEC 61966-2-1), numpy."""
    import numpy as np

    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def lab_of_linear(lin: Any) -> Any:
    """Linear RGB (n, 3) → CIELAB (n, 3), the maths of printops.color.linear_to_lab."""
    import numpy as np

    m = np.array(
        [
            [0.4124564, 0.3575761, 0.1804375],
            [0.2126729, 0.7151522, 0.0721750],
            [0.0193339, 0.1191920, 0.9503041],
        ]
    )
    xyz = (lin @ m.T) * 100 / np.array(_D65)
    d = 6 / 29
    f = np.where(xyz > d**3, np.cbrt(xyz), xyz / (3 * d * d) + 4 / 29)
    return np.stack(
        [116 * f[:, 1] - 16, 500 * (f[:, 0] - f[:, 1]), 200 * (f[:, 1] - f[:, 2])], 1
    )


def lab_to_hex(lab: tuple[float, float, float]) -> str:
    """CIELAB (D65) → "#RRGGBB", clipped to the sRGB gamut."""
    ell, a, b = lab
    fy = (ell + 16) / 116
    fx, fz = fy + a / 500, fy - b / 200
    d = 6 / 29

    def finv(t: float) -> float:
        return t**3 if t > d else 3 * d * d * (t - 4 / 29)

    x, y, z = (finv(t) * w / 100 for t, w in zip((fx, fy, fz), _D65, strict=True))
    rgb = (
        3.2404542 * x - 1.5371385 * y - 0.4985314 * z,
        -0.9692660 * x + 1.8760108 * y + 0.0415560 * z,
        0.0556434 * x - 0.2040259 * y + 1.0572252 * z,
    )
    return hex_from_linear(rgb)


def load_image(path: Path) -> tuple[Any, str]:
    """The image as an (h, w, 3) uint8 array, upright as a viewer shows it (EXIF orientation),
    and the sha256 of the very bytes decoded (so the record cannot name another file state)."""
    import numpy as np
    from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError

    try:
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        with Image.open(io.BytesIO(data)) as im:
            upright = ImageOps.exif_transpose(im)
            profile = upright.info.get("icc_profile")
            if upright.mode.startswith("I;16"):
                # a 16-bit grey frame (a camera still): Pillow's RGB conversion would clip it to white
                grey = np.asarray(upright, dtype=np.float64) / 257.0
                rgb8 = np.clip(np.rint(grey), 0, 255).astype(np.uint8)
                return np.repeat(rgb8[:, :, None], 3, axis=2), digest
            rgb = upright.convert("RGB")
            if profile:
                # a phone saves Display P3, a camera Adobe RGB: the pixels are not sRGB until converted
                try:
                    converted = ImageCms.profileToProfile(
                        rgb,
                        ImageCms.ImageCmsProfile(io.BytesIO(profile)),
                        ImageCms.createProfile("sRGB"),
                        outputMode="RGB",
                    )
                    if converted is not None:
                        rgb = converted
                except (ImageCms.PyCMSError, OSError) as e:
                    raise MixTargetError(
                        f"{path} carries a colour profile that cannot be converted to sRGB: {e}"
                    ) from e
            return np.asarray(rgb), digest
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as e:
        raise MixTargetError(f"{path} cannot be read as an image: {e}") from e


def _pixels(img: Any, box: Box, flag: str) -> Any:
    h, w = img.shape[:2]
    if box.x1 > w or box.y1 > h:
        raise MixTargetError(
            f"{flag} {','.join(map(str, box.as_list()))} reaches outside the {w}x{h} image"
        )
    px = img[box.y0 : box.y1, box.x0 : box.x1].reshape(-1, 3)
    if len(px) > MAX_SAMPLES:
        px = px[:: math.ceil(len(px) / MAX_SAMPLES)]
    return px


def white_gain(
    white_px: Any, white_level: int = DEFAULT_WHITE_LEVEL, flag: str = "--white"
) -> tuple[Any, tuple[int, int, int]]:
    """The linear-light gain per channel that maps the median of `white_px` (n, 3) to `white_level`,
    and that median as 8-bit RGB. A median with a channel under MIN_WHITE_CHANNEL is not white."""
    import numpy as np

    med = np.median(white_px, axis=0)
    if med.min() < MIN_WHITE_CHANNEL:
        raise MixTargetError(
            f"{flag} box has median RGB {tuple(int(v) for v in med)}: a white reference "
            f"needs every channel at {MIN_WHITE_CHANNEL} or more"
        )
    rgb8 = [int(v) for v in np.rint(med)]
    gain = decode_srgb(np.full(3, white_level / 255.0)) / decode_srgb(med / 255.0)
    return gain, (rgb8[0], rgb8[1], rgb8[2])


def lab_pixels(px: Any, gain: Any | None = None) -> Any:
    """CIELAB (n, 3) of the 8-bit RGB pixels `px` (n, 3), after the linear-light `gain` (None: none)."""
    import numpy as np

    lin = decode_srgb(px.astype(np.float64) / 255.0)
    if gain is not None:
        lin = np.clip(lin * gain, 0.0, 1.0)
    return lab_of_linear(lin)


def median_lab(px: Any, gain: Any | None = None) -> tuple[float, float, float]:
    """Median in CIELAB of the 8-bit RGB pixels `px` (n, 3), after the linear-light `gain` (None: none)."""
    import numpy as np

    m = np.median(lab_pixels(px, gain), axis=0)
    return float(m[0]), float(m[1]), float(m[2])


def read_target(
    image: Path,
    box: Box,
    white: Box | None,
    white_level: int = DEFAULT_WHITE_LEVEL,
) -> Reading:
    """The target colour of `box`, balanced on the white reference `white` (None: a render)."""
    if not 1 <= white_level <= 255:
        raise MixTargetError(f"white level {white_level} must be 1..255")
    img, sha256 = load_image(image)
    px = _pixels(img, box, "--box")
    white_rgb: tuple[int, int, int] | None = None
    gain: tuple[float, float, float] | None = None
    gain_vec = None
    if white is not None:
        gain_vec, white_rgb = white_gain(_pixels(img, white, "--white"), white_level)
        gain = (float(gain_vec[0]), float(gain_vec[1]), float(gain_vec[2]))
    lab = median_lab(px, gain_vec)
    return Reading(
        target=lab_to_hex(lab),
        lab=lab,
        samples=len(px),
        image=str(image.resolve()),
        sha256=sha256,
        image_size=(int(img.shape[1]), int(img.shape[0])),
        box=box,
        white_box=white,
        white_level=white_level if white is not None else None,
        white_rgb=white_rgb,
        gain=gain,
    )


def _is_box(b: Any) -> bool:
    return (
        isinstance(b, list)
        and len(b) == 4
        and all(isinstance(v, int) and not isinstance(v, bool) for v in b)
        and 0 <= b[0] < b[2]
        and 0 <= b[1] < b[3]
    )


def validate_source(src: Any) -> list[str]:
    """Problems with the `target_source` of a tester manifest ([] = valid)."""
    if not isinstance(src, dict):
        return ["target_source is not an object"]
    errs = [f"target_source.{k} is missing" for k in SOURCE_KEYS if k not in src]
    if errs:
        return errs
    sha = src["sha256"]
    if not (isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha)):
        errs.append("target_source.sha256 is not a sha256")
    if not isinstance(src["image"], str):
        errs.append("target_source.image is not a path")
    if src["kind"] not in ("photo", "render"):
        errs.append("target_source.kind must be photo or render")
    size = src["image_size"]
    if not (
        isinstance(size, list)
        and len(size) == 2
        and all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in size)
    ):
        errs.append("target_source.image_size must be [width, height]")
    if not _is_box(src["box"]):
        errs.append("target_source.box must be [x0, y0, x1, y1]")
    white, level = src["white_box"], src["white_level"]
    if level is not None and not (isinstance(level, int) and 1 <= level <= 255):
        errs.append("target_source.white_level must be 1..255 or null")
    if white is not None and not _is_box(white):
        errs.append("target_source.white_box must be [x0, y0, x1, y1] or null")
    balanced = white is not None
    if src["white_balance"] is not balanced or (level is not None) is not balanced:
        errs.append(
            "target_source.white_balance and white_level must be set exactly when white_box is"
        )
    if (src["kind"] == "photo") is not balanced:
        errs.append("target_source.kind is photo exactly when white_box is set")
    return errs
