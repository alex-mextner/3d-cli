"""niche_fit.py — parametric OpenSCAD emitter for cavity/niche inserts (plugs).

WHAT: given a described cavity (rectangular pocket or round bore, plus optional
retention/entry features) this module resolves FDM mating clearances and emits a
parametric `.scad` for a printable insert that seats into that cavity.

HOW IT IS REACHED: `lib/commands/fit_niche.py` (the `3d fit-niche` command) lazy-imports
this module inside `run()`, builds a `NicheSpec` from flags or a `--spec` JSON file, and
writes `emit_scad(spec)` to disk. The emitted `.scad` is then rendered/checked through the
normal `bin/3d render` / `bin/3d check` path.

CLEARANCE CONVENTION (the load-bearing invariant this module exists to get right):
  * rect cavity  -> clearance is applied PER MATING FACE. Each of the 2 X walls and 2 Y
    walls gets `clearance` mm of gap, so the insert footprint shrinks by `2*clearance`
    in width AND depth. Total diagonal slop is `2*clearance` per axis.
  * round cavity -> clearance is RADIAL (a uniform gap all around the bore), so the
    insert diameter shrinks by `2*clearance`.
  * height (Z) is NOT clearanced by default — the insert seats flush to the cavity floor.

The emitted `.scad` also carries a `show_cavity` demo toggle: with `-D show_cavity=true`
it additionally renders the surrounding cavity block (a solid with the nominal pocket cut
out) so a `3d render --section` visibly shows the insert seated inside with the clearance
gap between the mating walls. That demo block is NOT part of the printable insert
(`show_cavity` defaults to false, so a plain export/check sees only `insert()`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from errors import InvalidArgument, UsageError

# ---------------------------------------------------------------------------
# Fit presets. `--clearance` overrides any of these. Values are millimetres of
# gap PER MATING FACE (rect) / RADIAL (round) and reflect typical 0.4mm-nozzle
# FDM tolerances: snug needs sanding/press, normal slides by hand, loose is a
# drop-in with visible play.
# ---------------------------------------------------------------------------
FIT_CLEARANCES: dict[str, float] = {"snug": 0.10, "normal": 0.20, "loose": 0.35}
DEFAULT_FIT = "normal"
DEFAULT_CLEARANCE = FIT_CLEARANCES[DEFAULT_FIT]

SHAPES = ("rect", "round")

# Default feature geometry (all millimetres). Chosen to be printable on a 0.4mm nozzle.
LEAD_IN_SIZE = 1.0        # 45-degree entry chamfer run at the top rim
GROOVE_WIDTH = 1.5        # channel extent along Z
GROOVE_DEPTH = 0.8        # channel radial depth
TAB_LENGTH = 4.0          # snap ridge length (tangential)
TAB_PROTRUSION = 0.4      # snap ridge stand-off past the mating face

INSERT_COLOR = "SteelBlue"
CAVITY_COLOR = "Silver"
DEMO_WALL = 3.0           # wall thickness of the demo cavity block


@dataclass(frozen=True)
class NicheSpec:
    """A resolved cavity + insert specification, ready to emit as `.scad`.

    Dimensions are the CAVITY (the hole to fit into); the insert is derived from them
    via `clearance`. `width`/`depth` are used for rect, `diameter` for round.
    """

    shape: str
    height: float
    clearance: float
    fit: str
    width: float = 0.0
    depth: float = 0.0
    diameter: float = 0.0
    lead_in: bool = False
    groove: bool = False
    snap_tab: bool = False

    # ---- derived insert geometry -------------------------------------------
    @property
    def insert_width(self) -> float:
        return self.width - 2 * self.clearance

    @property
    def insert_depth(self) -> float:
        return self.depth - 2 * self.clearance

    @property
    def insert_diameter(self) -> float:
        return self.diameter - 2 * self.clearance

    def summary(self) -> dict[str, Any]:
        """Machine-readable resolved spec (for `--json` and reporting)."""
        data: dict[str, Any] = {
            "shape": self.shape,
            "height": self.height,
            "clearance": self.clearance,
            "fit": self.fit,
            "clearance_convention": (
                "per mating face" if self.shape == "rect" else "radial (uniform gap)"
            ),
            "features": {
                "lead_in": self.lead_in,
                "groove": self.groove,
                "snap_tab": self.snap_tab,
            },
        }
        if self.shape == "rect":
            data["cavity"] = {"width": self.width, "depth": self.depth, "height": self.height}
            data["insert"] = {
                "width": round(self.insert_width, 4),
                "depth": round(self.insert_depth, 4),
                "height": self.height,
            }
        else:
            data["cavity"] = {"diameter": self.diameter, "height": self.height}
            data["insert"] = {
                "diameter": round(self.insert_diameter, 4),
                "height": self.height,
            }
        return data


# ---------------------------------------------------------------------------
# Resolution + validation
# ---------------------------------------------------------------------------
def resolve_clearance(fit: str | None, override: float | None) -> tuple[float, str]:
    """Resolve the effective clearance and its label from `--fit`/`--clearance`.

    An explicit `--clearance` always wins; the label becomes ``custom`` unless a `--fit`
    was also named. With no override the fit preset supplies the value. A named `--fit` is
    validated against the closed enum whether or not a clearance override is also given.
    """
    if fit is not None and fit not in FIT_CLEARANCES:
        raise InvalidArgument(
            "--fit", fit, list(FIT_CLEARANCES),
            command="fit-niche",
            extra="Or pass an explicit --clearance MM.",
        )
    if override is not None:
        if override < 0:
            raise InvalidArgument(
                "--clearance", str(override), ["a value >= 0"],
                command="fit-niche",
                extra="Clearance is millimetres of gap per mating face; it cannot be negative.",
            )
        return override, (fit or "custom")
    label = fit or DEFAULT_FIT
    return FIT_CLEARANCES[label], label


def _require_positive(name: str, value: float) -> None:
    if value <= 0:
        raise InvalidArgument(
            name, str(value), ["a value > 0"],
            command="fit-niche",
            extra="Cavity dimensions are millimetres and must be greater than zero.",
        )


def _smallest_insert_span(spec: NicheSpec) -> tuple[float, str]:
    """Return the smallest cross-sectional insert dimension and its human label."""
    if spec.shape == "rect":
        return min(spec.insert_width, spec.insert_depth), "width/depth"
    return spec.insert_diameter, "diameter"


def _validate_insert_positive(spec: NicheSpec) -> None:
    """Fail loudly when the clearance eats the whole cavity (insert <= 0)."""
    smallest, face = _smallest_insert_span(spec)
    if smallest <= 0:
        raise UsageError(
            f"clearance {spec.clearance} mm is too large for this cavity: "
            f"the insert {face} would be {smallest:g} mm (<= 0)",
            command="fit-niche",
            remediation=[
                "Increase the cavity size or lower --clearance / use --fit snug.",
            ],
        )


def _validate_features(spec: NicheSpec) -> None:
    """Reject feature requests on a cavity too small to carry them without degenerate scad.

    The groove carves `groove_depth` off every mating face, so the insert cross-section
    must exceed `2*groove_depth` or the emitted inner square/cylinder would go negative.
    (Lead-in and snap features clamp their own geometry in the `.scad`, so they are safe on
    any positive insert.)
    """
    if not spec.groove:
        return
    smallest, face = _smallest_insert_span(spec)
    floor = 2 * GROOVE_DEPTH
    if smallest <= floor:
        raise UsageError(
            f"--groove needs an insert {face} greater than {floor:g} mm "
            f"(a {GROOVE_DEPTH:g} mm channel on each face); this insert is {smallest:g} mm",
            command="fit-niche",
            remediation=[
                "Use a larger cavity, drop --groove, or lower the clearance.",
            ],
        )


def make_spec(
    *,
    shape: str,
    height: float,
    clearance: float | None = None,
    fit: str | None = None,
    width: float = 0.0,
    depth: float = 0.0,
    diameter: float = 0.0,
    lead_in: bool = False,
    groove: bool = False,
    snap_tab: bool = False,
) -> NicheSpec:
    """Validate inputs and build a resolved `NicheSpec` (the single construction path)."""
    if shape not in SHAPES:
        raise InvalidArgument("--shape", shape, list(SHAPES), command="fit-niche")
    _require_positive("--height", height)
    if shape == "rect":
        _require_positive("--width", width)
        _require_positive("--depth", depth)
    else:
        _require_positive("--diameter", diameter)
    resolved, label = resolve_clearance(fit, clearance)
    spec = NicheSpec(
        shape=shape,
        height=height,
        clearance=resolved,
        fit=label,
        width=width,
        depth=depth,
        diameter=diameter,
        lead_in=lead_in,
        groove=groove,
        snap_tab=snap_tab,
    )
    _validate_insert_positive(spec)
    _validate_features(spec)
    return spec


_ALLOWED_JSON_KEYS = {
    "shape", "height", "clearance", "fit", "width", "depth", "diameter",
    "lead_in", "groove", "snap_tab",
}


def spec_from_json(data: dict[str, Any]) -> NicheSpec:
    """Build a `NicheSpec` from a parsed `--spec` JSON object (same field names as flags)."""
    if not isinstance(data, dict):
        raise UsageError(
            "spec file must contain a JSON object",
            command="fit-niche",
            remediation=['Example: {"shape": "rect", "width": 20, "depth": 16, "height": 12}'],
        )
    unknown = set(data) - _ALLOWED_JSON_KEYS
    if unknown:
        raise UsageError(
            f"unknown spec field(s): {', '.join(sorted(unknown))}",
            command="fit-niche",
            remediation=[f"Accepted fields: {', '.join(sorted(_ALLOWED_JSON_KEYS))}"],
        )
    shape = str(data.get("shape", "rect"))
    return make_spec(
        shape=shape,
        height=_as_float(data, "height"),
        clearance=_as_opt_float(data, "clearance"),
        fit=(str(data["fit"]) if "fit" in data else None),
        width=_as_float(data, "width", default=0.0),
        depth=_as_float(data, "depth", default=0.0),
        diameter=_as_float(data, "diameter", default=0.0),
        lead_in=bool(data.get("lead_in", False)),
        groove=bool(data.get("groove", False)),
        snap_tab=bool(data.get("snap_tab", False)),
    )


def _as_float(data: dict[str, Any], key: str, *, default: float | None = None) -> float:
    if key not in data:
        if default is not None:
            return default
        raise UsageError(f"spec is missing required field '{key}'", command="fit-niche")
    try:
        return float(data[key])
    except (TypeError, ValueError):
        raise InvalidArgument(key, str(data[key]), ["a number"], command="fit-niche") from None


def _as_opt_float(data: dict[str, Any], key: str) -> float | None:
    if key not in data:
        return None
    return _as_float(data, key)


# ---------------------------------------------------------------------------
# SCAD emission
#
# Everything is centred at the origin in XY with the insert base at z=0, so the
# insert seats onto the cavity floor. The emitted modules branch on the `shape`
# constant at OpenSCAD runtime, so `-D 'shape="round"'` flips the whole part without
# re-generating the file. Features compose as: lead-in is built INTO the body
# (a positive truncated cap), the groove is a subtractive perimeter ring, and the
# snap tab is an additive ridge on the +X mating face.
# ---------------------------------------------------------------------------
def _fmt(value: float) -> str:
    """Format a float as a compact OpenSCAD literal (no trailing zeros)."""
    return f"{value:g}"


def _shape_dims(spec: NicheSpec) -> tuple[float, float, float]:
    """Cavity (width, depth, diameter) for the header, with a sensible positive fallback
    for the shape's inactive dimensions so a runtime `-D shape=...` flip stays valid."""
    if spec.shape == "rect":
        diameter = spec.diameter or min(spec.width, spec.depth)
        return spec.width, spec.depth, diameter
    side = spec.diameter
    return (spec.width or side), (spec.depth or side), spec.diameter


def _emit_header(spec: NicheSpec) -> str:
    b = lambda flag: "true" if flag else "false"  # noqa: E731
    width, depth, diameter = _shape_dims(spec)
    return "\n".join(
        [
            "// Generated by `3d fit-niche` — parametric cavity insert / plug.",
            "// Clearance convention: rect = per mating face; round = radial (gap all around).",
            "// Edit the constants below, or override any of them with `-D name=value`.",
            "// BOSL2 is available (OPENSCADPATH auto-set) but this file uses only stdlib",
            "// OpenSCAD primitives so it renders with no external library.",
            "",
            "/* [Cavity] */",
            f'shape = "{spec.shape}";            // rect | round',
            f"cavity_width = {_fmt(width)};       // mm, X opening (rect)",
            f"cavity_depth = {_fmt(depth)};       // mm, Y opening (rect)",
            f"cavity_diameter = {_fmt(diameter)};    // mm, bore (round)",
            f"cavity_height = {_fmt(spec.height)};      // mm, Z depth of the cavity",
            "",
            "/* [Fit] */",
            f"clearance = {_fmt(spec.clearance)};        // mm gap per mating face (rect) / radial (round)",
            "",
            "/* [Features] */",
            f"lead_in = {b(spec.lead_in)};          // 45-degree entry chamfer at the top rim",
            f"lead_in_size = {_fmt(LEAD_IN_SIZE)};      // mm chamfer run",
            f"groove = {b(spec.groove)};           // retention channel around the insert",
            f"groove_width = {_fmt(GROOVE_WIDTH)};      // mm channel extent along Z",
            f"groove_depth = {_fmt(GROOVE_DEPTH)};      // mm channel radial depth",
            # Derived at OpenSCAD runtime so a -D cavity_height override moves the band too.
            "groove_z = max(groove_width / 2 + 0.5, "
            "min(cavity_height * 0.6, cavity_height - groove_width / 2 - 0.5));"
            "  // mm channel centre height",
            f"snap_tab = {b(spec.snap_tab)};         // retention ridge on the +X face",
            f"tab_length = {_fmt(TAB_LENGTH)};        // mm ridge length",
            f"tab_protrusion = {_fmt(TAB_PROTRUSION)};    // mm ridge stand-off past the face",
            "",
            "/* [Demo] */",
            "show_cavity = false;      // also render the surrounding cavity block (section proof only)",
            f"demo_wall = {_fmt(DEMO_WALL)};          // mm wall of the demo cavity block",
            "",
            "$fn = 96;",
            "",
            "// ---- derived insert geometry ----",
            "insert_width = cavity_width - 2 * clearance;",
            "insert_depth = cavity_depth - 2 * clearance;",
            "insert_diameter = cavity_diameter - 2 * clearance;",
            "insert_height = cavity_height;  // full seat: no Z clearance by default",
        ]
    )


_BODY_MODULES = """
module rect_body(w, d, h) {
    if (lead_in) {
        lead = min(lead_in_size, h / 2, w / 2 - 0.2, d / 2 - 0.2);
        union() {
            linear_extrude(h - lead) square([w, d], center = true);
            translate([0, 0, h - lead]) hull() {
                linear_extrude(0.01) square([w, d], center = true);
                translate([0, 0, lead])
                    linear_extrude(0.01) square([w - 2 * lead, d - 2 * lead], center = true);
            }
        }
    } else {
        linear_extrude(h) square([w, d], center = true);
    }
}

module round_body(dia, h) {
    if (lead_in) {
        lead = min(lead_in_size, h / 2, dia / 2 - 0.2);
        union() {
            cylinder(h = h - lead, d = dia);
            translate([0, 0, h - lead])
                cylinder(h = lead, d1 = dia, d2 = max(dia - 2 * lead, 0.2));
        }
    } else {
        cylinder(h = h, d = dia);
    }
}

module insert_body() {
    if (shape == "rect") rect_body(insert_width, insert_depth, insert_height);
    else round_body(insert_diameter, insert_height);
}

module groove_cut() {
    translate([0, 0, groove_z - groove_width / 2]) {
        if (shape == "rect") {
            difference() {
                linear_extrude(groove_width)
                    square([insert_width + 4, insert_depth + 4], center = true);
                translate([0, 0, -1]) linear_extrude(groove_width + 2)
                    square([insert_width - 2 * groove_depth, insert_depth - 2 * groove_depth],
                           center = true);
            }
        } else {
            difference() {
                cylinder(h = groove_width, d = insert_diameter + 4);
                translate([0, 0, -1]) cylinder(h = groove_width + 2,
                    d = insert_diameter - 2 * groove_depth);
            }
        }
    }
}

module snap_bump() {
    face_x = (shape == "rect") ? insert_width / 2 : insert_diameter / 2;
    translate([face_x, 0, insert_height / 2]) rotate([90, 0, 0])
        cylinder(h = tab_length, r = tab_protrusion, center = true);
}

module insert() {
    union() {
        difference() {
            insert_body();
            if (groove) groove_cut();
        }
        if (snap_tab) snap_bump();
    }
}

module cavity_block() {
    if (shape == "rect") {
        difference() {
            translate([0, 0, -demo_wall]) linear_extrude(cavity_height + demo_wall)
                square([cavity_width + 2 * demo_wall, cavity_depth + 2 * demo_wall], center = true);
            translate([0, 0, -0.01]) linear_extrude(cavity_height + 0.02)
                square([cavity_width, cavity_depth], center = true);
        }
    } else {
        difference() {
            translate([0, 0, -demo_wall])
                cylinder(h = cavity_height + demo_wall, d = cavity_diameter + 2 * demo_wall);
            translate([0, 0, -0.01]) cylinder(h = cavity_height + 0.02, d = cavity_diameter);
        }
    }
}
"""


def _emit_toplevel() -> str:
    return "\n".join(
        [
            f'color("{INSERT_COLOR}") insert();',
            f'if (show_cavity) color("{CAVITY_COLOR}") cavity_block();',
        ]
    )


def emit_scad(spec: NicheSpec) -> str:
    """Return the full parametric `.scad` text for `spec`."""
    return "\n".join([_emit_header(spec), _BODY_MODULES, _emit_toplevel(), ""])
