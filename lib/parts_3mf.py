"""parts_3mf.py — read the part-role tags of a 3MF project and guard print boundaries with them (stdlib only).

WHAT: a 3MF written by `3d assembly position` (or any tool following lib/parts.py) tags every object in
  `Metadata/model_settings.config` with `3d:role` / `3d:printable` (`parts.Part.to_metadata`). This module reads those
  tags back (`tags`, `parts_of`) and gives the print boundaries one call to refuse non-printable parts:
  `guard(path, command=..., include_non_printable=...)` raises `parts.NonPrintableParts` (exit 1) unless the explicit
  `--include-non-printable` override is passed, which is logged (`3d.parts` WARNING) and returned.

WHY: vitamins, reference models and stand-ins must never reach a print by mistake; the tag has to survive
  arrange / retarget / prep / export round trips and each of them has to look at it. One reader, so they all agree.

ACCESSED VIA: `3d arrange`, `3d retarget`, `3d prep`, `3d export` (3mf), `3d assembly position|check`.
"""

from __future__ import annotations

import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from parts import (
    META_PRINTABLE,
    META_ROLE,
    Part,
    PrintGuard,
    assert_printable,
    part_from_metadata,
)

MODEL_SETTINGS = "Metadata/model_settings.config"


@dataclass(frozen=True)
class ObjectTag:
    """One 3MF object's id, name and its `3d:*` metadata."""

    object_id: int
    name: str
    metadata: Mapping[str, str] = field(default_factory=dict)


def tags(path: str | Path) -> list[ObjectTag]:
    """Every object of a project 3MF with its `<metadata>` rows; a plain 3MF (no model_settings) yields []."""
    try:
        with zipfile.ZipFile(path) as zf:
            if MODEL_SETTINGS not in zf.namelist():
                return []
            return tags_in(zf.read(MODEL_SETTINGS))
    except zipfile.BadZipFile:
        return []


def tags_in(model_settings: str | bytes) -> list[ObjectTag]:
    """The `ObjectTag`s of a `Metadata/model_settings.config` document (text or bytes); unparsable yields []."""
    try:
        root = ET.fromstring(model_settings)
    except ET.ParseError:
        return []
    out: list[ObjectTag] = []
    for obj in root.iter("object"):
        meta = {m.get("key", ""): m.get("value", "") for m in obj.findall("metadata")}
        try:
            oid = int(obj.get("id", "0"))
        except ValueError:
            continue
        out.append(ObjectTag(oid, meta.get("name", f"object{oid}"), meta))
    return out


def parts_of(path: str | Path) -> list[Part]:
    """The `Part` (role + derived printable) of every object; an untagged object is `printed`."""
    return [part_from_metadata(t.name, t.metadata) for t in tags(path)]


def has_tags(path: str | Path) -> bool:
    return any(
        META_ROLE in t.metadata or META_PRINTABLE in t.metadata for t in tags(path)
    )


def guard(
    path: str | Path,
    *,
    command: str,
    include_non_printable: bool = False,
    reason: str = "",
) -> PrintGuard:
    """Refuse (`NonPrintableParts`) a 3MF that holds a non-printable object, unless the override is given."""
    result = assert_printable(
        parts_of(path), include_non_printable=include_non_printable, command=command
    )
    if result.overridden and reason:
        import logging

        logging.getLogger("3d.parts").warning(
            "override reason (%s): %s", command, reason
        )
    return result


def metadata_by_name(path: str | Path) -> dict[str, dict[str, str]]:
    """object name -> its `3d:*` rows only (what arrange/retarget must carry over to their output)."""
    out: dict[str, dict[str, str]] = {}
    for t in tags(path):
        keep = {k: v for k, v in t.metadata.items() if k.startswith("3d:")}
        if keep:
            out[t.name] = keep
    return out


def summary(path: str | Path) -> list[dict[str, Any]]:
    return [
        {
            "object": t.object_id,
            "name": t.name,
            **{k: v for k, v in t.metadata.items() if k.startswith("3d:")},
        }
        for t in tags(path)
    ]
