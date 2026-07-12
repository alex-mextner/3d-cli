# ─────────────────────────────────────────────────────────────────────────────
# ai/blockout.py — parametric BLOCKOUT generator (the missing image→3D generator).
#
# WHAT / WHY
#   The image→3D loop was stuck because everything downstream (fit-camera, match)
#   assumed the right .scad already existed. fit-camera only fits a CAMERA to a FIXED
#   model; match_loop only nudges numeric constants of an EXISTING assembly. Neither
#   can invent structure. This module is the generator match_loop can tune INTO
#   existence: a small template FAMILY whose whole shape is a handful of named,
#   top-level OpenSCAD constants.
#
#   The constants are emitted as single-line `name = number;` at the TOP of the file so
#   match_loop.derive_tunables (regex `^name = number;`) picks them up verbatim, and
#   fit-camera/spatial_fit_metrics can score the silhouette of any instance. Stage 1 is
#   the coarse mass (podium + colonnade + pediment); richer substructure is future work.
#
# HOW IT'S REACHED
#   `lib/img3d_loop.py` (the closed-loop tool) calls render_scad()/write_scad() to
#   materialize a candidate, then fit-camera + the monotonic refine tune the continuous
#   dimensions while a VLM veto pins the discrete structural count (n_columns).
#
# INVARIANTS
#   - STDLIB-ONLY: emits text; imports nothing heavy. Safe to import from a command
#     module (tests/test_imports.py contract) even though today only the tool uses it.
#   - Every tunable is a top-level `name = number;` line (no `{}` nesting) so the
#     match-loop numeric-scalar extractor sees it. Geometry lives below, in modules.
#   - The FRONT silhouette (low elevation, facing -Y) encodes span/base_height/
#     column_height/pediment_height/column_radius and the column COUNT (comb teeth),
#     which is what makes boundary-fit recovery + the semantic veto meaningful.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping


@dataclass(frozen=True)
class BlockoutParams:
    """One instance of the temple/colonnade template.

    Continuous fields are recoverable from the boundary silhouette; `n_columns` is the
    discrete structural feature the semantic veto is responsible for (a VLM reads it
    from the reference, the boundary optimizer is poor at integer counts).
    """

    n_columns: int = 6
    column_radius: float = 2.2
    column_height: float = 30.0
    span: float = 60.0
    base_height: float = 8.0
    pediment_height: float = 14.0
    depth: float = 18.0
    entablature_height: float = 4.0
    dome_radius: float = 0.0

    def to_dict(self) -> dict[str, float]:
        # n_columns stays an int in the serialized form (it is a discrete count); int is
        # a valid float value in the mapping, and JSON then emits `5`, not `5.0`.
        return {
            "n_columns": self.n_columns,
            "column_radius": self.column_radius,
            "column_height": self.column_height,
            "span": self.span,
            "base_height": self.base_height,
            "pediment_height": self.pediment_height,
            "depth": self.depth,
            "entablature_height": self.entablature_height,
            "dome_radius": self.dome_radius,
        }


# The continuous dimensions the monotonic refine is allowed to tune (n_columns is set
# by the semantic veto, not the boundary optimizer; depth/entablature are structural
# constants held fixed for the coarse stage).
CONTINUOUS_TUNABLES: tuple[str, ...] = (
    "column_radius",
    "column_height",
    "span",
    "base_height",
    "pediment_height",
)


def default_params() -> BlockoutParams:
    return BlockoutParams()


def params_from_dict(values: Mapping[str, float]) -> BlockoutParams:
    """Build params from a mapping, coercing n_columns to a positive int."""
    base = BlockoutParams()
    updates: dict[str, float | int] = {}
    for field_name in base.to_dict():
        if field_name in values:
            if field_name == "n_columns":
                updates[field_name] = max(1, int(round(float(values[field_name]))))
            else:
                updates[field_name] = float(values[field_name])
    return replace(base, **updates)  # type: ignore[arg-type]


def with_values(params: BlockoutParams, **overrides: float) -> BlockoutParams:
    """Return a copy with overrides applied, keeping n_columns integral."""
    clean: dict[str, float | int] = {}
    for key, value in overrides.items():
        if key == "n_columns":
            clean[key] = max(1, int(round(float(value))))
        else:
            clean[key] = float(value)
    return replace(params, **clean)  # type: ignore[arg-type]


def render_scad(params: BlockoutParams) -> str:
    """Emit an OpenSCAD program for one blockout instance.

    Top-of-file constants are the tunables (single-line `name = number;`). The geometry
    stands upright (height along Z) so a low-elevation front camera sees a silhouette of
    a podium, a comb of `n_columns` columns, an entablature bar, and a triangular
    pediment — the features the boundary fit and the semantic veto both read.
    """
    p = params
    return f"""// Temple/colonnade blockout — generated by lib/ai/blockout.py.
// The constants below are the match-loop tunables (single-line name = number;).
n_columns = {p.n_columns};
column_radius = {_num(p.column_radius)};
column_height = {_num(p.column_height)};
span = {_num(p.span)};
base_height = {_num(p.base_height)};
pediment_height = {_num(p.pediment_height)};
depth = {_num(p.depth)};
entablature_height = {_num(p.entablature_height)};
dome_radius = {_num(p.dome_radius)};

$fn = 40;

module temple() {{
  // Build the front elevation as a 2D union in XY (X = width, Y = height), extrude it
  // along Z by `depth`, then stand it upright so height runs along Z.
  rotate([90, 0, 0])
  linear_extrude(height = depth)
  union() {{
    // podium
    square([span, base_height]);
    // colonnade: n_columns evenly spaced across the span, sitting on the podium
    for (i = [0 : n_columns - 1]) {{
      cx = span * (i + 0.5) / n_columns;
      translate([cx - column_radius, base_height])
        square([2 * column_radius, column_height]);
    }}
    // entablature bar spanning the colonnade
    translate([0, base_height + column_height])
      square([span, entablature_height]);
    // triangular pediment
    translate([0, base_height + column_height + entablature_height])
      polygon([[0, 0], [span, 0], [span / 2, pediment_height]]);
  }}

  // Optional dome behind the pediment (Pantheon-style variants); 0 disables it.
  if (dome_radius > 0) {{
    translate([span / 2, depth / 2, base_height + column_height])
      sphere(r = dome_radius);
  }}
}}

temple();
"""


def write_scad(params: BlockoutParams, path: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(render_scad(params))


def _num(value: float) -> str:
    """Format a float compactly but always as a numeric literal (never bare int-less)."""
    if float(value).is_integer():
        return str(int(round(value)))
    return f"{value:.4f}".rstrip("0").rstrip(".")
