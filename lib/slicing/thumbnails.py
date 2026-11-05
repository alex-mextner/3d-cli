"""slicing/thumbnails.py — the layers that make a thumbnail impossible to miss in a sliced G-code.

WHAT: the shared glue between the slicing wrappers / the printer commands and lib/gcode_thumbnails.py.
  Layer (a) `spec_for` / `with_project_spec` / `with_machine_spec` / `PRUSA_ARGS`: ask the slicer for
  48x48, 96x96 and 300x300 PNG in its own settings, so a slicer that writes thumbnails (Orca's GUI,
  PrusaSlicer, Bambu Studio) does, and the config block of every other G-code names the sizes the
  checks require.
  Layer (b) `finalize_slice` / `finalize_gcode` / `finalize_plate_images`: after the slice, draw the
  missing sizes from the toolpaths into the G-code (and the missing Metadata/plate_N.png into a 3MF
  project). Idempotent: complete output stays byte-identical.
  Layer (c) `fix_in_place`: what `3d printer upload|start` call before sending a G-code.

WHY: the Orca-family CLI exports G-code with a null thumbnail callback, so a headless slice never has
  a preview (docs/commands/gcode-thumbnails.md). Every path that produces a G-code goes through
  one of these functions; tests/test_thumbnail_guard.py fails when a module that runs a slicer
  does not.

INVARIANTS: stdlib at import (numpy/Pillow come in with gcode_thumbnails, lazily). A failure raises
  GateFailure naming the exact `3d gcode-thumbnails add` command: a slice whose preview cannot be
  written is not reported as done.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
import zipfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from errors import GateFailure

if TYPE_CHECKING:
    from gcode_thumbnails import Image, ThumbCheck

PLATE_SIDE = 512  # Metadata/plate_N.png, as the Orca GUI writes it
PLATE_SMALL_SIDE = 128  # Metadata/plate_N_small.png
RENDER_SIDE = (
    1024  # one toolpath render feeds every size (2x supersampled up to 512 px)
)
SETTINGS_MEMBER = "Metadata/project_settings.config"
# PrusaSlicer takes every config option as a CLI flag; it writes the blocks itself when given sizes.
PRUSA_ARGS = ["--thumbnails", "48x48,96x96,300x300", "--thumbnails-format", "PNG"]
_THUMB_REL = (
    "http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail"
)
_PLATE_GCODE = re.compile(r"plate_(\d+)\.gcode$", re.I)


def fix_command(path: str | Path) -> str:
    """The command that repairs a G-code without its thumbnails (what every message prints)."""
    return f"3d gcode-thumbnails add {shlex.quote(str(path))}"


# ------------------------------------------------------------ (a) ask the slicer for them


def spec_for(cfg: Mapping[str, object]) -> str | list[str]:
    """The `thumbnails` value of settings `cfg` (a project's config or a machine preset) extended by DEFAULT_SIZES, in the
    shape the preset uses: Orca presets hold either `"48x48/PNG, 300x300/PNG"` or a list of sizes."""
    from gcode_thumbnails import ThumbnailError, merge_spec

    value = cfg.get("thumbnails")
    fmt = cfg.get("thumbnails_format")
    existing = value if isinstance(value, (str, list)) else None
    try:
        merged = merge_spec(existing, fmt.upper() if isinstance(fmt, str) and fmt else "PNG")
    except ThumbnailError:  # a size the slicer itself would reject: ask for the defaults only
        merged = merge_spec(None)
    return merged.split(", ") if isinstance(value, list) else merged


def _rewrite_zip(
    src: Path,
    dst: Path,
    edit: Callable[[str, bytes], bytes | None],
    extra: Mapping[str, bytes] | None = None,
) -> None:
    """Copy zip `src` to `dst` (atomically; `dst` may be `src`) member by member with the zip metadata kept;
    `edit(name, data)` returns the new bytes of a member or None to keep it. `extra` members are appended.
    Mode and mtime of `src` carry over: a project rewritten for its thumbnails must not look newer than the
    G-code sliced from it (the staleness gate)."""
    st = src.stat()
    with tempfile.NamedTemporaryFile(
        dir=dst.parent, prefix=f".{dst.name}.", suffix=".partial", delete=False
    ) as h:
        tmp = Path(h.name)
    try:
        with (
            zipfile.ZipFile(src) as zi,
            zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zo,
        ):
            for item in zi.infolist():
                data = zi.read(item.filename)
                new = edit(item.filename, data)
                zo.writestr(item, data if new is None else new)
            for name, data in (extra or {}).items():
                zo.writestr(name, data)
        os.chmod(tmp, st.st_mode & 0o7777)
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)


def with_project_spec(src: Path, dst: Path) -> None:
    """Copy of 3MF project `src` whose `thumbnails` setting also asks for DEFAULT_SIZES. Everything else
    keeps its bytes. A zip without Metadata/project_settings.config is copied as it is."""

    def edit(name: str, data: bytes) -> bytes | None:
        if name != SETTINGS_MEMBER:
            return None
        cfg = json.loads(data)
        cfg["thumbnails"] = spec_for(cfg)
        return json.dumps(cfg, indent=4, ensure_ascii=False).encode()

    _rewrite_zip(src, dst, edit)


def thumbnail_cfg(project: Path) -> dict[str, str | list[str]]:
    """`set_cfg` entry that makes 3MF `project` ask for DEFAULT_SIZES on top of its own `thumbnails`;
    {} for a zip without Metadata/project_settings.config (nothing to override)."""
    with zipfile.ZipFile(project) as z:
        if SETTINGS_MEMBER not in z.namelist():
            return {}
        cfg = json.loads(z.read(SETTINGS_MEMBER))
    return {"thumbnails": spec_for(cfg)}


def with_machine_spec(src: Path, dst: Path) -> None:
    """Copy of a flat Orca machine preset (JSON) that also asks for DEFAULT_SIZES."""
    cfg = json.loads(src.read_text(encoding="utf-8"))
    cfg["thumbnails"] = spec_for(cfg)
    dst.write_text(json.dumps(cfg, indent=1), encoding="utf-8")


# ------------------------------------------------------------ (b) draw what the slicer did not


def _fail(path: Path | str, why: str) -> GateFailure:
    return GateFailure(
        f"{path}: thumbnails could not be added ({why})",
        command="gcode-thumbnails",
        remediation=[
            f"Add them by hand: {fix_command(path)}",
            "numpy and Pillow are core dependencies: `uv sync` repairs a broken install.",
        ],
    )


def placeholder(side: int) -> Image:
    """A flat grey tile with a darker rim: the picture of a G-code that extrudes nothing visible, so that
    even a macro-only file carries every thumbnail its checks require."""
    import numpy as np

    img = np.zeros((side, side, 4), np.uint8)
    margin, rim = side // 8, max(side // 32, 1)
    img[margin : side - margin, margin : side - margin] = (70, 70, 70, 255)
    img[margin + rim : side - margin - rim, margin + rim : side - margin - rim] = (130, 130, 130, 255)
    return img


class Picture:
    """One lazy toolpath render shared by the G-code blocks and the plate images of the same slice."""

    def __init__(self, gcode: Path) -> None:
        self.gcode = gcode
        self._image: Image | None = None

    def __call__(self, _path: str = "") -> Image:
        if self._image is None:
            from gcode_thumbnails import ThumbnailError, render_gcode

            try:
                self._image = render_gcode(str(self.gcode), size=RENDER_SIDE)
            except ThumbnailError:  # nothing visible to draw (console macros, travel-only): a neutral tile
                self._image = placeholder(RENDER_SIDE)
        return self._image


def finalize_gcode(
    gcode: Path, picture: Callable[[str], Image] | None = None
) -> ThumbCheck:
    """Layer (b): every required size is in `gcode` afterwards (added in place when missing), else GateFailure.
    A complete G-code is not touched."""
    import gcode_thumbnails as gt

    try:
        result = gt.ensure(str(gcode), source=picture or Picture(gcode))
    except (gt.ThumbnailError, OSError, ImportError) as exc:
        raise _fail(gcode, str(exc)) from exc
    if not result.ok:
        raise _fail(gcode, result.reason)
    return result


def plate_members(plate: int) -> tuple[str, str]:
    return f"Metadata/plate_{plate}.png", f"Metadata/plate_{plate}_small.png"


def missing_plate_images(three_mf: Path, plate: int = 1) -> list[str]:
    """The Metadata/plate_N PNGs `three_mf` lacks (a member that is not a PNG counts as missing)."""
    with zipfile.ZipFile(three_mf) as z:
        names = set(z.namelist())
        return [
            m
            for m in plate_members(plate)
            if m not in names or not z.read(m).startswith(b"\x89PNG\r\n\x1a\n")
        ]


def finalize_plate_images(
    three_mf: Path, plate: int, picture: Callable[[str], Image]
) -> list[str]:
    """Add the missing Metadata/plate_N.png / plate_N_small.png (drawn from `picture`) to `three_mf` in place and
    return the members written; a 3MF that has both is left byte-identical. Plate 1 also gets the package
    thumbnail relationship when the package names none."""
    missing = missing_plate_images(three_mf, plate)
    if not missing:
        return []
    from gcode_thumbnails import encode_png, fit

    pic = picture("")
    side = {
        plate_members(plate)[0]: PLATE_SIDE,
        plate_members(plate)[1]: PLATE_SMALL_SIDE,
    }
    images = {m: encode_png(fit(pic, side[m], side[m])) for m in missing}

    def edit(name: str, data: bytes) -> bytes | None:
        if name in images:  # a corrupt member is replaced, not duplicated
            return images.pop(name)
        if name == "[Content_Types].xml" and b'Extension="png"' not in data:
            return data.replace(
                b"</Types>",
                b' <Default Extension="png" ContentType="image/png"/>\n</Types>',
            )
        if name == "_rels/.rels" and plate == 1 and _THUMB_REL.encode() not in data:
            rel = f'<Relationship Target="/{plate_members(1)[0]}" Id="rel-thumbnail" Type="{_THUMB_REL}"/>'
            return data.replace(b"</Relationships>", rel.encode() + b"</Relationships>")
        return None

    with zipfile.ZipFile(three_mf) as z:
        present = set(z.namelist())
    new_members = {m: images[m] for m in missing if m not in present}
    _rewrite_zip(three_mf, three_mf, edit, extra=new_members)
    return missing


def plates_of(three_mf: Path) -> list[int]:
    """Plate numbers a 3MF carries a G-code for (`Metadata/plate_N.gcode`); [1] when it has none."""
    with zipfile.ZipFile(three_mf) as z:
        found = sorted({int(m.group(1)) for n in z.namelist() if (m := _PLATE_GCODE.search(n))})
    return found or [1]


def check_plate_images(three_mf: Path) -> ThumbCheck:
    """`3d gcode-thumbnails check X.3mf`: every plate has plate_N.png and plate_N_small.png (a 3MF is not a
    printer input, so a missing one is reported like a missing thumbnail but never blocks a print)."""
    from gcode_thumbnails import Thumb, ThumbCheck

    big, small = Thumb(PLATE_SIDE, PLATE_SIDE, "PNG"), Thumb(PLATE_SMALL_SIDE, PLATE_SMALL_SIDE, "PNG")
    required: list[Thumb] = []
    missing: list[Thumb] = []
    for plate in plates_of(three_mf):
        lacking = missing_plate_images(three_mf, plate)
        for member, size in zip(plate_members(plate), (big, small), strict=True):
            required.append(size)
            if member in lacking:
                missing.append(size)
    ok = not missing
    reason = "every plate has its images" if ok else "missing " + ", ".join(
        m for p in plates_of(three_mf) for m in missing_plate_images(three_mf, p))
    return ThumbCheck(str(three_mf), ok, required, [t for t in required if t not in missing], missing, reason)


def finalize_embedded(three_mf: Path, picture: Callable[[str], Image] | None = None) -> ThumbCheck:
    """Add the missing plate images to a `.gcode.3mf`/sliced project, drawn from `picture` or from each plate's
    embedded `Metadata/plate_N.gcode`. A 3MF without an embedded G-code needs `picture`."""
    try:
        for plate in plates_of(three_mf):
            if not missing_plate_images(three_mf, plate):
                continue
            if picture is not None:
                finalize_plate_images(three_mf, plate, picture)
                continue
            with tempfile.TemporaryDirectory(prefix="3d-plate-") as tmp, zipfile.ZipFile(three_mf) as z:
                member = f"Metadata/plate_{plate}.gcode"
                if member not in z.namelist():
                    raise ValueError(f"{three_mf} has no {member} to draw the image from; pass --image PNG")
                gcode = Path(tmp) / f"plate_{plate}.gcode"
                gcode.write_bytes(z.read(member))
                finalize_plate_images(three_mf, plate, Picture(gcode))
    except (OSError, ImportError, zipfile.BadZipFile, ValueError) as exc:
        raise _fail(three_mf, str(exc)) from exc
    return check_plate_images(three_mf)


def finalize_slice(
    gcode: Path, three_mf: Path | None = None, plate: int | None = None
) -> ThumbCheck:
    """Layer (b) for one slice: the G-code, and the project/gcode.3mf next to it when given, from one render.
    `plate` defaults to the number in `plate_N.gcode`, else 1."""
    picture = Picture(gcode)
    result = finalize_gcode(gcode, picture)
    if three_mf is not None and three_mf.is_file():
        match = _PLATE_GCODE.search(gcode.name)
        number = plate if plate is not None else int(match.group(1)) if match else 1
        try:
            finalize_plate_images(three_mf, number, picture)
        except (OSError, ImportError, zipfile.BadZipFile, ValueError) as exc:
            raise _fail(three_mf, str(exc)) from exc
    return result


def finalize_workdir(workdir: Path, three_mf: Path | None = None) -> list[Path]:
    """Layer (b) over a slicer output directory: every non-empty `*.gcode` in it. Returns the G-codes."""
    gcodes = [g for g in sorted(workdir.glob("*.gcode")) if g.stat().st_size > 0]
    for g in gcodes:
        finalize_slice(
            g, three_mf if len(gcodes) == 1 or _PLATE_GCODE.search(g.name) else None
        )
    return gcodes


# ------------------------------------------------------------ (c) before a printer gets the file


def fix_in_place(gcode: Path) -> ThumbCheck | None:
    """`3d printer upload|start`: add what `gcode` lacks, in place (atomically, mode/mtime kept, the slice-warnings
    record follows the new hash). None when the G-code was already complete. The remote name is
    content-addressed, so the fixed file gets its own name and the next upload/start refer to the same bytes."""
    from gcode_thumbnails import check

    if check(str(gcode)).ok:
        return None
    return finalize_gcode(gcode)
