"""parts.py — the shared part contract: roles, the print guardrail, 3MF tags and the part palette.

WHAT: every part of an assembly has a `role` (`printed | vitamin | reference | stand_in`) and a derived
`printable` flag. Bought or imaginary parts (PCBs, connectors, wires, screws, stand-ins for a real board) must
never reach a print; every part is drawn in its own stable colour. One module so `arrange`, `prep`, `export`,
`printer start/upload`, `publish`, render/usdz/3mf export and the assembly runner agree (umbrella #570, S0 #571).

ACCESSED VIA: the assembly/mates runner (S3), `3d arrange`/`prep`/`export`/`printer`/`models publish` (each
calls `assert_printable` / `filter_printable` at its print boundary, see the per-slice follow-ups in the PR),
and render/dimension/usdz/3mf export (`assign_styles`, `legend`).

INVARIANTS:
  - `printable` is never stored on its own: it is `role == "printed"`. A parts file that disagrees is rejected.
  - A `vitamin` or `reference` part must carry a `reference_source` (URL + licence): such models are taken
    from existing reference models (found with `3d models search` / `3d library`), never invented. A
    `stand_in` is a placeholder and may omit it.
  - `assert_printable` refuses with `NonPrintableParts` (exit 1) unless `include_non_printable=True`; the
    override is logged (stdlib `logging` logger `3d.parts`, WARNING) and returned in `PrintGuard.overridden`
    so the caller can put it in its report.
  - Colours depend only on the SET of part names + roles, never on input order or the run: the same assembly
    renders the same colours everywhere. Two parts never share a colour. Stand-ins are always the muted ghost
    style (low alpha, grey-tinted) so they cannot be mistaken for printed parts.
"""

from __future__ import annotations

import colorsys
import hashlib
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from errors import GateFailure, InvalidArgument

ROLES = ("printed", "vitamin", "reference", "stand_in")
REFERENCE_REQUIRED_ROLES = ("vitamin", "reference")
META_ROLE = "3d:role"
META_PRINTABLE = "3d:printable"
_LOG = logging.getLogger("3d.parts")
_GOLDEN = 0.618033988749895
_MIN_HUE_GAP = 0.02
# brightness steps from the brightest role look down to the 0.25 floor
_SHADE_STEPS = 40


class NonPrintableParts(GateFailure):
    """A non-printable part reached a print boundary without the explicit override."""

    def __init__(self, parts: Sequence[Part], *, command: str | None = None) -> None:
        names = ", ".join(f"{p.name} ({p.role})" for p in parts)
        super().__init__(
            f"refusing to print non-printable part(s): {names}",
            command=command,
            remediation=[
                "Remove them from the print set (vitamins/reference/stand-ins are bought or imaginary parts).",
                "Override only on purpose: pass --include-non-printable (the override is logged).",
            ],
        )
        self.parts = list(parts)


@dataclass(frozen=True)
class ReferenceSource:
    """Where the model of a non-printable part came from."""

    url: str
    license: str
    note: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"url": self.url, "license": self.license, "note": self.note}


@dataclass(frozen=True)
class Part:
    name: str
    role: str = "printed"
    reference_source: ReferenceSource | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise InvalidArgument(
                "part.name", "", ["a non-empty name"], command="parts"
            )
        if self.role not in ROLES:
            raise InvalidArgument("part.role", self.role, list(ROLES), command="parts")
        if self.role in REFERENCE_REQUIRED_ROLES and (
            self.reference_source is None
            or not self.reference_source.url
            or not self.reference_source.license
        ):
            raise InvalidArgument(
                f"part {self.name!r}.reference_source",
                "missing",
                ["{url, license}"],
                command="parts",
                extra=f"A {self.role} is taken from an existing reference model: record its source URL and "
                "licence (find one with `3d models search`).",
            )

    @property
    def printable(self) -> bool:
        return self.role == "printed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "printable": self.printable,
            "reference_source": self.reference_source.to_dict()
            if self.reference_source
            else None,
        }

    def to_metadata(self) -> dict[str, str]:
        """The tag a 3MF carries per object so arrange/retarget/export keep the role."""
        return {
            META_ROLE: self.role,
            META_PRINTABLE: "true" if self.printable else "false",
        }


def _source_from_dict(name: str, raw: Any) -> ReferenceSource | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise InvalidArgument(
            f"part {name!r}.reference_source",
            repr(raw),
            ["a mapping with url, license and note"],
            command="parts",
        )
    fields = {key: raw.get(key, "") for key in ("url", "license", "note")}
    bad = [key for key, value in fields.items() if not isinstance(value, str)]
    if bad:
        raise InvalidArgument(
            f"part {name!r}.reference_source.{bad[0]}",
            repr(fields[bad[0]]),
            ["a string"],
            command="parts",
        )
    return ReferenceSource(fields["url"], fields["license"], fields["note"])


def part_from_dict(raw: Mapping[str, Any]) -> Part:
    """Build a `Part` from a parts-file entry; a stored `printable` that contradicts `role` is rejected."""
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise InvalidArgument(
            "part.name", repr(name), ["a non-empty string"], command="parts"
        )
    part = Part(
        name,
        str(raw.get("role", "printed")),
        _source_from_dict(name, raw.get("reference_source")),
    )
    stored = raw.get("printable")
    if "printable" in raw and (
        not isinstance(stored, bool) or stored != part.printable
    ):
        raise InvalidArgument(
            f"part {part.name!r}.printable",
            str(stored),
            [str(part.printable).lower()],
            command="parts",
            extra="`printable` is a boolean derived from `role`; drop the field or fix the role.",
        )
    return part


def part_from_metadata(name: str, metadata: Mapping[str, str]) -> Part:
    """Rebuild the role of a 3MF object from its metadata; an untagged object is `printed`.

    A tag of `3d:printable=false` on an object without a role is read as `stand_in`, never silently as printed.
    """
    role = metadata.get(META_ROLE)
    tag = metadata.get(META_PRINTABLE)
    if role is None:
        role = "stand_in" if tag == "false" else "printed"
    elif tag is not None and tag != ("true" if role == "printed" else "false"):
        raise InvalidArgument(
            f"part {name!r}.{META_PRINTABLE}",
            tag,
            ["true" if role == "printed" else "false"],
            command="parts",
            extra=f"`{META_PRINTABLE}` contradicts `{META_ROLE}={role}`; fix or drop one of the two tags.",
        )
    if role in REFERENCE_REQUIRED_ROLES:
        # A 3MF carries only the tag; the source lives in the parts file. Keep the role without raising.
        return Part(
            name,
            role,
            ReferenceSource("unknown", "unknown", "role read from 3MF metadata"),
        )
    return Part(name, role)


@dataclass(frozen=True)
class PrintGuard:
    parts: tuple[Part, ...]  # what may go to the print (all of them under the override)
    overridden: tuple[Part, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "parts": [p.name for p in self.parts],
            "overridden": [p.to_dict() for p in self.overridden],
        }


def assert_printable(
    parts: Iterable[Part],
    *,
    include_non_printable: bool = False,
    command: str | None = None,
) -> PrintGuard:
    """Print-boundary guard: raise `NonPrintableParts` if any part is not printable.

    With `include_non_printable=True` nothing raises, every part passes and the non-printable ones are logged
    and returned in `PrintGuard.overridden`.
    """
    all_parts = tuple(parts)
    blocked = tuple(p for p in all_parts if not p.printable)
    if blocked and not include_non_printable:
        raise NonPrintableParts(blocked, command=command)
    if blocked:
        _LOG.warning(
            "non-printable parts included by override (%s): %s",
            command or "?",
            ", ".join(f"{p.name}:{p.role}" for p in blocked),
        )
    return PrintGuard(all_parts, blocked)


def filter_printable(parts: Iterable[Part]) -> tuple[list[Part], list[Part]]:
    """Drop mode for commands that build a set (`arrange`): `(printable, dropped)`; callers report `dropped`."""
    kept: list[Part] = []
    dropped: list[Part] = []
    for p in parts:
        (kept if p.printable else dropped).append(p)
    return kept, dropped


# --------------------------------------------------------------------------- palette
@dataclass(frozen=True)
class PartStyle:
    name: str
    role: str
    color: str  # "#RRGGBB"
    alpha: float  # 1.0 solid, <1 ghost
    style: str  # "solid" | "ghost"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "color": self.color,
            "alpha": self.alpha,
            "style": self.style,
        }


# role -> (saturation, value, alpha, style): printed bright, vitamins darker, reference pale, stand-ins ghost.
_ROLE_LOOK = {
    "printed": (0.65, 0.88, 1.0, "solid"),
    "vitamin": (0.60, 0.55, 1.0, "solid"),
    "reference": (0.25, 0.80, 1.0, "solid"),
    "stand_in": (0.12, 0.72, 0.35, "ghost"),
}


def _base_hue(name: str, role: str) -> float:
    digest = hashlib.sha256(f"{role}\0{name}".encode()).digest()
    return int.from_bytes(digest[:4], "big") / 2**32


def _hue_gap(a: float, b: float) -> float:
    d = abs(a - b) % 1.0
    return min(d, 1.0 - d)


def _hex(h: float, s: float, v: float) -> str:
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return f"#{round(r * 255):02X}{round(g * 255):02X}{round(b * 255):02X}"


def _unique_by_name(parts: Iterable[Part]) -> dict[str, Part]:
    """Parts keyed by name; one name declared with two different roles is ambiguous and refused."""
    by_name: dict[str, Part] = {}
    for p in parts:
        first = by_name.setdefault(p.name, p)
        if first.role != p.role:
            raise InvalidArgument(
                "part.name",
                p.name,
                ["one role per part name"],
                command="parts",
                extra=f"{p.name!r} is declared as both {first.role} and {p.role}.",
            )
    return by_name


def _place_hue(name: str, role: str, placed: Sequence[float], gap: float) -> float:
    hue = _base_hue(name, role)
    for _ in range(512):
        if all(_hue_gap(hue, h) >= gap for h in placed):
            break
        hue = (hue + _GOLDEN) % 1.0
    return hue


def _distinct_color(
    name: str, hue: float, sat: float, val: float, used: set[str]
) -> str:
    """The first unused shade of `hue`: a step of brightness keeps two parts apart. Bounded, then refused."""
    for _ in range(_SHADE_STEPS):
        color = _hex(hue, sat, val)
        if color not in used:
            return color
        val = max(0.25, val - 0.02)
    raise InvalidArgument(
        "parts",
        name,
        ["fewer parts"],
        command="parts",
        extra=f"no free colour is left for {name!r}: too many parts for a distinct palette.",
    )


def assign_styles(parts: Iterable[Part]) -> dict[str, PartStyle]:
    """A deterministic, pairwise distinct style per part name.

    The first-choice hue is a hash of (role, name); parts are placed in sorted order and a hue too close to an
    already placed one steps by the golden ratio, so the result depends on the set of parts only. The same
    name with two different roles raises `InvalidArgument`.
    """
    by_name = _unique_by_name(parts)
    placed: list[float] = []
    used: set[str] = set()
    gap = min(_MIN_HUE_GAP, 0.5 / max(len(by_name), 1))  # n parts need a gap below 1/n
    out: dict[str, PartStyle] = {}
    for name in sorted(by_name):
        part = by_name[name]
        sat, val, alpha, style = _ROLE_LOOK[part.role]
        hue = _place_hue(name, part.role, placed, gap)
        placed.append(hue)
        color = _distinct_color(name, hue, sat, val, used)
        used.add(color)
        out[name] = PartStyle(name, part.role, color, alpha, style)
    return out


def legend(parts: Iterable[Part]) -> list[dict[str, Any]]:
    """Legend rows (sorted by name) for text/JSON output: name, role, colour, style, printable, source."""
    by_name = _unique_by_name(parts)
    styles = assign_styles(by_name.values())
    rows = []
    for name in sorted(by_name):
        part = by_name[name]
        source = part.reference_source
        rows.append(
            {
                **styles[name].to_dict(),
                "printable": part.printable,
                "reference_source": source.to_dict() if source else None,
            }
        )
    return rows


def legend_text(parts: Iterable[Part]) -> str:
    """The legend as aligned text, one line per part."""
    rows = legend(parts)
    width = max((len(r["name"]) for r in rows), default=0)
    return "\n".join(
        f"{r['color']}  {r['name']:<{width}}  {r['role']}{' (ghost)' if r['style'] == 'ghost' else ''}"
        for r in rows
    )
