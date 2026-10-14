"""gcode_labels.py — the slicer's own labels in a G-code: which object, layer and feature a line belongs to.

WHAT: one reader for every gate that attributes G-code to objects (print-preflight's head sequence,
  overhangs, visible faces, print area), for both object-label dialects:
    Klipper     EXCLUDE_OBJECT_DEFINE / EXCLUDE_OBJECT_START NAME=... / EXCLUDE_OBJECT_END (Orca,
                Snapmaker Orca, Creality Print); once a file uses them, the comment labels the same
                slicers also write are ignored, so the EXCLUDE_OBJECT name wins;
    Bambu       `; start printing object, unique label id: N` + `M624 <mask>` ... `; stop printing
                object, unique label id: N` + `M625` (Bambu Studio; the firmware's skip-object
                block). The block is named by the `; printing object NAME id:I copy C` comment
                written just before its start; a bare M624 mask (comments stripped) is bit k of
                the header's `; model label id: N0,N1,...` list and is named `object N`. The
                inner `; stop printing object NAME` comment does NOT end the block: Bambu changes
                layer inside it.
    Marlin      M486 S<n> (S-1 = between objects) -> `object n`;
    PrusaSlicer `; printing object NAME` / `; stop printing object` comments.
  Layer changes: `;LAYER_CHANGE` (Orca, PrusaSlicer) and `; CHANGE_LAYER` (Bambu). Feature tags:
  `;TYPE:X` and `; FEATURE: X` (Bambu); line widths `;WIDTH:` and `; LINE_WIDTH:` (Bambu).

WHY: issue https://github.com/alex-mextner/3d-cli/issues/184 — gates that knew only EXCLUDE_OBJECT
  read a Bambu Studio A1 mini plate as unlabelled or attributed no extrusion to any object, and
  print-preflight FAILed plates that were fine. Each gate had its own label parsing; now there is
  one. Stdlib only: head_sequence is imported without numpy.
"""

from __future__ import annotations

import base64
import binascii
import re

LAYER_MARKERS = (";LAYER_CHANGE", "; CHANGE_LAYER")

_PRINTING = "; printing object "
_STOP = "; stop printing object"
_BAMBU_START = "; start printing object, unique label id:"
_BAMBU_STOP = "; stop printing object, unique label id:"
_BAMBU_IDS = "; model label id:"
_COMMENT_LABELS = (_PRINTING, _STOP, _BAMBU_START, _BAMBU_IDS)
_NAME = re.compile(r"NAME=(\"[^\"]*\"|'[^']*'|\S+)", re.IGNORECASE)
_M486_S = re.compile(r"\bS\s*([-+]?\d+)", re.IGNORECASE)
# `<name>_id_0_copy_0` (EXCLUDE_OBJECT, spaces as `_`) and `<name> id:0 copy 0` (comments)
_DECORATION = re.compile(r"(?:_id_\d+_copy_\d+| id:\d+ copy \d+)$")


def undecorated(label: str) -> str:
    """The project object name inside a G-code object label (the slicer's id/copy suffix cut)."""
    return _DECORATION.sub("", label)


def feature(line: str) -> str | None:
    """`;TYPE:Support` / `; FEATURE: Support` -> 'Support'; None for any other line."""
    if line.startswith(";TYPE:"):
        return line[6:].strip()
    if line.startswith("; FEATURE:"):
        return line[10:].strip()
    return None


def line_width(line: str) -> str | None:
    """`;WIDTH:0.45` / `; WIDTH:0.45` / `; LINE_WIDTH: 0.45` -> '0.45'; None for any other line."""
    if line.startswith((";WIDTH:", "; WIDTH:", "; LINE_WIDTH:")):
        return line.split(":", 1)[1].strip()
    return None


class ObjectLabels:
    """Feed every G-code line in order (leading whitespace stripped); `current` is the object
    printing now (None between objects), `labeled` whether any object label was seen so far,
    `defined` the EXCLUDE_OBJECT_DEFINE names."""

    __slots__ = (
        "_bambu_ids",
        "_bambu_names",
        "_klipper",
        "_pending",
        "current",
        "defined",
        "labeled",
    )

    def __init__(self) -> None:
        self.current: str | None = None
        self.labeled = False
        # EXCLUDE_OBJECT seen: the comment labels the same slicer writes are duplicates
        self._klipper = False
        self.defined: list[str] = []
        self._pending: str | None = None  # the last `; printing object NAME`
        self._bambu_ids: list[int] = []
        self._bambu_names: dict[int, str] = {}

    @property
    def bambu(self) -> bool:
        """The labels so far are Bambu Studio's (header id list or a `start printing object` block)."""
        return bool(self._bambu_ids or self._bambu_names)

    def feed(self, line: str) -> bool:
        """True when `line` is an object label (and `current` may have changed)."""
        c0 = line[:1]
        if c0 == ";":
            return line.startswith(_COMMENT_LABELS) and self._comment(line)
        if c0 in ("E", "e"):
            return self._exclude(line)
        if c0 in ("M", "m"):
            return self._mcode(line)
        return False

    def _start(self, name: str) -> bool:
        self.current, self.labeled = name, True
        return True

    def _stop(self) -> bool:
        self.current, self.labeled = None, True
        return True

    def _bambu_start(self, label_id: int) -> bool:
        name = self._pending or self._bambu_names.get(label_id) or f"object {label_id}"
        self._bambu_names[label_id] = name
        self._pending = None
        return self._start(name)

    def _comment(self, line: str) -> bool:
        if line.startswith(_BAMBU_START):
            try:
                label_id = int(line[len(_BAMBU_START) :].strip())
            except ValueError:
                return False
            return self._bambu_start(label_id)
        if line.startswith(_BAMBU_STOP):
            return self._stop()
        if line.startswith(_BAMBU_IDS):
            ids = line[len(_BAMBU_IDS) :].split(",")
            self._bambu_ids = [int(i) for i in ids if i.strip().isdigit()]
            # the header lists the objects: everything before the first block is not one
            self.labeled = self.labeled or bool(self._bambu_ids)
            return False
        if line.startswith(_PRINTING):
            self._pending = line[len(_PRINTING) :].strip()
            if self._klipper or self.bambu:
                return False
            return self._start(self._pending)
        # `; stop printing object [NAME]`: ends a PrusaSlicer-style label only; inside a Bambu
        # block it comes before the layer change that still belongs to the object
        if self._klipper or self.bambu:
            return False
        return self._stop()

    def _exclude(self, line: str) -> bool:
        head = line[:21].upper()
        if not head.startswith("EXCLUDE_OBJECT"):
            return False
        self._klipper = self.labeled = True
        if head.startswith("EXCLUDE_OBJECT_DEFINE"):
            m = _NAME.search(line)
            if m:
                self.defined.append(m.group(1).strip("\"'"))
            return True
        if head.startswith("EXCLUDE_OBJECT_START"):
            m = _NAME.search(line)
            return self._start(m.group(1).strip("\"'") if m else "?")
        if head.startswith("EXCLUDE_OBJECT_END"):
            return self._stop()
        return True  # EXCLUDE_OBJECT (cancel): no change to what prints now

    def _mcode(self, line: str) -> bool:
        code = line.split(";", 1)[0].split()
        word = code[0].upper() if code else ""
        if word == "M486":
            self.labeled = True
            m = _M486_S.search(" ".join(code[1:]))
            if m is None:
                return True  # M486 T<count> / A<name>: what prints now is unchanged
            n = int(m.group(1))
            return self._stop() if n < 0 else self._start(f"object {n}")
        if word == "M624":
            if (
                self.current is not None
            ):  # already opened by its `; start printing object` comment
                return True
            label_id = self._mask_label(code[1] if len(code) > 1 else "")
            return self._bambu_start(label_id) if label_id is not None else False
        if word == "M625":
            return self._stop()
        return False

    def _mask_label(self, mask: str) -> int | None:
        """`M624 BAAAAAAAAAA=` -> the label id of the mask's single bit (index into the header's
        `; model label id:` list); None for a mask that names no single object."""
        try:
            bits = int.from_bytes(base64.b64decode(mask, validate=True), "little")
        except (binascii.Error, ValueError):
            return None
        if bits <= 0 or bits & (bits - 1):
            return None
        k = bits.bit_length() - 1
        return self._bambu_ids[k] if k < len(self._bambu_ids) else k
