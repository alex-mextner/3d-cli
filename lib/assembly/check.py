"""check.py — `3d assembly check`: blocking intersections and per-mate clearance of the assembled pose.

WHAT: for every pair of parts (one pair at a time) it finds the overlap regions (`overlap.pair_regions`) and
  classifies each one:
    contact   thinner than `seat_tol_mm` or smaller than `eps_mm3`: parts that touch, not a collision (#140);
    allowed   inside a declared mate's zone and within the interference that mate's fit allows
              (press: table interference + tol; snap/tight: the fit tolerance; or the mate's own `allow` caps);
    blocking  everything else: no mate declares it, it is outside the mate's zone, or it is deeper / bigger than
              the declared interference. Blocking regions carry pair, depth, xyz, the offending faces and a
              suggested clearance change.
  Each mate then gets its clearance judged against the resolved fit (`fits_sources.resolve`: table row + printer x
  material corrections, provenance in the report): the measured exact minimum gap in the mate's zone vs
  `clearance - tol`.

INVARIANTS:
  - Verdicts are `pass | warn | fail`; the run FAILS (exit 1, `ok: false`) on any blocking region or failed mate.
  - The only thing that makes an overlap acceptable is a declaration in the spec; nothing is allowed by default.
  - Memory: a part's solid is built when its first pair needs it and dropped after its last pair.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from assembly.overlap import Region, min_gap, pair_regions, sampled_gap
from assembly.scene import Body, Scene, box_intersection, boxes_overlap
from assembly.spec import DEPTH_EPS_MM, SCHEMA, AssemblySpec, Context, MateSpec
from errors import InvalidArgument
from parts import assign_styles, legend

NOMINAL_EPS_MM = (
    0.005  # measured gaps this close to nominal count as nominal (float32 meshes)
)

# Interference that a fit kind tolerates in the CLOSED pose when the mate declares no explicit cap.
_TOL_INTERFERENCE_KINDS = ("snap", "tight")

_MATERIAL_NOTES = {
    "PLA": "PLA is stiff and brittle (strain limit ~2 % along the layers, ~1 % across them): designed interference "
    "only holds if the flexing arm stays inside its strain limit.",
    "PETG": "PETG is tougher and more flexible than PLA (strain limit ~4-5 %): it forgives more interference but "
    "creeps under a sustained press fit.",
    "ABS": "ABS flexes well (strain limit ~3 %) but shrinks more: re-check press fits on the printed part.",
    "ASA": "ASA flexes like ABS (strain limit ~3 %) and shrinks more: re-check press fits on the printed part.",
    "TPU": "TPU is elastic: interference is absorbed by the part, not a defect.",
}


def material_note(material: str | None) -> str | None:
    """A one-line flexibility note for the material, matched by family name (`Elegoo Rapid PLA+` -> PLA)."""
    if not material:
        return None
    upper = material.upper()
    for family, note in _MATERIAL_NOTES.items():
        if family in upper:
            return note
    return None


@dataclass
class MateFit:
    """A mate's resolved fit numbers and where they came from."""

    fit_type: str
    clearance_mm: float  # signed per-face clearance (negative = designed overlap)
    interference_mm: float
    tol_mm: float
    fallback: bool
    source: str  # table | explicit
    provenance: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "fit_type": self.fit_type,
            "clearance_mm": round(self.clearance_mm, 4),
            "interference_mm": round(self.interference_mm, 4),
            "tol_mm": round(self.tol_mm, 4),
            "fallback": self.fallback,
            "source": self.source,
            "provenance": self.provenance,
        }


def resolve_fit(mate: MateSpec, ctx: Context) -> MateFit:
    """The mate's fit through the shared table (printer x material corrections); explicit numbers override it."""
    import fits_sources

    res = fits_sources.resolve(
        mate.fit,
        mate.shape,
        ctx.printer,
        ctx.material,
        ctx.nozzle,
        ctx.layer,
        command="assembly",
    )
    clearance, interference, tol = res.clearance_mm, res.interference_mm, res.tol_mm
    prov = [dict(p) for p in res.provenance]
    source = "table"
    if (
        mate.clearance_mm is not None
        or mate.interference_mm is not None
        or mate.tol_mm is not None
    ):
        source = "explicit"
        if mate.interference_mm is not None:
            interference = mate.interference_mm
            clearance = (
                mate.clearance_mm if mate.clearance_mm is not None else 0.0
            ) - interference
        elif mate.clearance_mm is not None:
            clearance = mate.clearance_mm
            interference = max(0.0, -clearance)
        if mate.tol_mm is not None:
            tol = mate.tol_mm
        prov.insert(
            0,
            {
                "kind": "explicit",
                "note": "the spec declares clearance_mm / interference_mm / tol_mm for this mate; the table value is "
                f"shown below it ({res.clearance_mm:+.3f} mm, tol {res.tol_mm:.3f})",
            },
        )
    return MateFit(
        res.fit_type, clearance, interference, tol, res.fallback, source, prov
    )


@dataclass
class Verdict:
    status: str  # pass | warn | fail
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "reasons": self.reasons}


def _zone_covers(mate: MateSpec, region: Region) -> bool:
    if mate.zone is None:
        return True
    return (
        mate.zone.contains(region.center, 0.0)
        and mate.zone.contains(region.lo, 0.25)
        and mate.zone.contains(region.hi, 0.25)
    )


def _allowed_depth(mate: MateSpec, fit: MateFit) -> tuple[float, str]:
    """Deepest overlap this mate allows in the closed pose, with the reason string."""
    if mate.allow.max_depth_mm is not None:
        return (
            mate.allow.max_depth_mm,
            f"the mate declares allow.max_depth_mm {mate.allow.max_depth_mm:g}",
        )
    if mate.kind == "press" or fit.interference_mm > 0:
        return (
            fit.interference_mm + fit.tol_mm,
            f"press fit: {fit.interference_mm:g} mm interference + {fit.tol_mm:g} mm tolerance",
        )
    if mate.kind in _TOL_INTERFERENCE_KINDS:
        return (
            fit.tol_mm,
            f"{mate.kind} fit: only within the {fit.tol_mm:g} mm fit tolerance",
        )
    return 0.0, f"a {mate.kind} fit has no interference"


def classify_region(
    region: Region,
    mates: Sequence[MateSpec],
    fits: dict[str, MateFit],
    spec: AssemblySpec,
) -> dict[str, Any]:
    """Label one overlap region contact | allowed | blocking with the mate, the reason and a suggestion."""
    cfg = spec.settings
    out = region.to_dict()
    out["mate"] = None
    if region.depth_mm <= cfg.seat_tol_mm:
        out.update(
            classification="contact",
            reason=f"depth {region.depth_mm:.4f} mm is within the seat tolerance {cfg.seat_tol_mm:g} mm: parts touch",
        )
        return out
    covering = [m for m in mates if _zone_covers(m, region)]
    if not covering:
        if mates:
            names = ", ".join(m.name for m in mates)
            reason = f"outside the declared zone of mate(s) {names}"
        else:
            reason = (
                f"no mate between {region.a} and {region.b} declares an interference"
            )
        return _blocking(out, region, reason, spec, None)
    mate = covering[0]
    fit = fits[mate.name]
    limit, why = _allowed_depth(mate, fit)
    out["mate"] = mate.name
    out["allowed_depth_mm"] = round(limit, 4)
    reasons: list[str] = []
    if region.depth_mm > limit + DEPTH_EPS_MM:
        reasons.append(
            f"depth {region.depth_mm:.3f} mm exceeds the allowed {limit:.3f} mm ({why})"
        )
    cap = mate.allow.max_volume_mm3
    if cap is not None and region.volume_mm3 > cap + 1e-9:
        reasons.append(
            f"volume {region.volume_mm3:.2f} mm3 exceeds the declared allow.max_volume_mm3 {cap:g}"
        )
    if reasons:
        return _blocking(out, region, "; ".join(reasons), spec, mate, fit)
    out.update(
        classification="allowed",
        reason=f"designed interference of mate {mate.name}: {why}",
    )
    note = material_note(spec.context.material)
    if note and (mate.kind in ("press", "snap") or fit.interference_mm > 0):
        out["material_note"] = note
    return out


def _blocking(
    out: dict[str, Any],
    region: Region,
    reason: str,
    spec: AssemblySpec,
    mate: MateSpec | None,
    fit: MateFit | None = None,
) -> dict[str, Any]:
    clearance = max(fit.clearance_mm, 0.0) if fit else 0.0
    change = round(region.depth_mm + clearance, 3)
    faces = ", ".join(f"{p} face normal {v['normal']}" for p, v in region.faces.items())
    out.update(
        classification="blocking",
        reason=reason,
        suggested_clearance_change_mm=change,
        suggestion=(
            f"open the clearance by at least {change:g} mm between {region.a} and {region.b} at "
            f"({region.center[0]:.2f}, {region.center[1]:.2f}, {region.center[2]:.2f})"
            + (f" (offending: {faces})" if faces else "")
            + (
                f"; if this overlap is a designed interference, declare it in mate {mate.name} (allow / zone)."
                if mate
                else "; if it is designed (press rib, snap barb), declare a mate with a zone around it."
            )
        ),
    )
    return out


def _clearance_verdict(
    mate: MateSpec,
    fit: MateFit,
    gap: float,
    regions: list[dict[str, Any]],
    search: float,
) -> tuple[Verdict, dict[str, Any]]:
    """Judge the mate's clearance (or its designed interference) against the resolved fit."""
    required = max(0.0, fit.clearance_mm - fit.tol_mm) if fit.clearance_mm > 0 else 0.0
    info: dict[str, Any] = {
        "measured_min_gap_mm": None if gap >= search else round(gap, 4),
        "search_mm": round(search, 3),
        "nominal_mm": round(max(fit.clearance_mm, 0.0), 4),
        "required_min_mm": round(required, 4),
        "tol_mm": round(fit.tol_mm, 4),
    }
    blocking = [r for r in regions if r["classification"] == "blocking"]
    allowed = [r for r in regions if r["classification"] == "allowed"]
    if blocking:
        depth = max(r["depth_mm"] for r in blocking)
        return Verdict(
            "fail", [f"{len(blocking)} blocking overlap(s), deepest {depth:.3f} mm"]
        ), info
    if fit.clearance_mm <= 0 or mate.kind == "press":
        want = fit.interference_mm
        if not allowed:
            return (
                Verdict(
                    "warn",
                    [
                        f"press fit designed for {want:g} mm interference but no overlap exists here "
                        f"(min gap {'>' + format(search, 'g') if gap >= search else format(gap, '.3f')} mm): loose fit"
                    ],
                ),
                info,
            )
        depth = max(r["depth_mm"] for r in allowed)
        info["measured_interference_mm"] = round(depth, 4)
        low = max(0.0, want - fit.tol_mm)
        if depth < low - DEPTH_EPS_MM:
            return Verdict(
                "warn",
                [
                    f"interference {depth:.3f} mm is below the {low:.3f} mm the press fit needs"
                ],
            ), info
        return Verdict(
            "pass",
            [f"interference {depth:.3f} mm within {want:g} +/- {fit.tol_mm:g} mm"],
        ), info
    if allowed:
        return Verdict(
            "pass",
            [
                f"designed interference in the zone ({len(allowed)} region(s)), no defects"
            ],
        ), info
    if mate.seat and gap <= NOMINAL_EPS_MM:
        return Verdict("pass", ["parts seat on each other by design"]), info
    if gap >= search:
        return Verdict("pass", [f"no surface within {search:g} mm in the zone"]), info
    if gap + NOMINAL_EPS_MM >= fit.clearance_mm:
        return Verdict(
            "pass", [f"gap {gap:.3f} mm >= nominal {fit.clearance_mm:.3f} mm"]
        ), info
    if gap + NOMINAL_EPS_MM >= required:
        return (
            Verdict(
                "warn",
                [
                    f"gap {gap:.3f} mm is below nominal {fit.clearance_mm:.3f} mm but inside the "
                    f"{fit.tol_mm:g} mm tolerance (min {required:.3f})"
                ],
            ),
            info,
        )
    return (
        Verdict(
            "fail",
            [
                f"gap {gap:.3f} mm is below the required {required:.3f} mm "
                f"(nominal {fit.clearance_mm:.3f} - tol {fit.tol_mm:g})"
            ],
        ),
        info,
    )


def _mate_gap(
    mate: MateSpec, a: Body, b: Body, fit: MateFit
) -> tuple[float, float, str, float | None]:
    """(min gap, search length, method, sample spacing) of a mate.

    No zone and no insertion axis: the exact `manifold3d` minimum separation of the two solids. Otherwise the gap is
    measured on surface samples INSIDE the mate's window (`overlap.sampled_gap`), so a zone never reads the cut faces
    of a crop. A mate with an insertion axis (not a snap) is measured LATERALLY: the part that moves is lifted out
    along its removal direction by the search length first, so the face it seats on (a floor, a shoulder) does not
    read as a zero gap and the side clearance that the fit table is about is what gets measured.
    """
    search = max(1.0, fit.clearance_mm + fit.tol_mm + 1.0)
    if mate.zone is not None:
        lo, hi = mate.zone.bbox(0.0)
        window = (lo[0], lo[1], lo[2], hi[0], hi[1], hi[2])
    else:
        window = box_intersection(a.box, b.box, search)
    lateral = mate.insertion is not None and mate.kind != "snap"
    if mate.zone is None and not lateral:
        return min_gap(a.solid(), b.solid(), search, window), search, "exact", None
    lift = None
    first, second = a, b
    if lateral:
        assert mate.insertion is not None
        from assembly.sweep import Motion

        ins = mate.insertion
        first, second = (a, b) if ins.part == a.name else (b, a)
        lift = tuple(
            float(v)
            for v in Motion("translate", ins.part, (), ins.axis, search).matrix(search)[:3, 3]
        )
    gap, used = sampled_gap(first.mesh, second.mesh, window, search, lift=lift)
    return gap, search, "lateral samples" if lateral else "zone samples", used


def run_check(
    scene: Scene,
    *,
    ctx: Context | None = None,
    mate_filter: Sequence[str] = (),
    on_region: Callable[[Scene, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Check the whole assembly (or only `mate_filter` mates and their pairs) in the assembled pose."""
    started = time.monotonic()
    spec = scene.spec
    context = ctx or spec.context
    wanted = set(mate_filter)
    unknown = wanted - {m.name for m in spec.mates}
    if unknown:
        raise InvalidArgument(
            "--mate",
            sorted(unknown)[0],
            [m.name for m in spec.mates] or ["(none declared)"],
            command="assembly",
        )
    fits = {m.name: resolve_fit(m, context) for m in spec.mates}
    pairs = scene.pairs()
    if wanted:
        keep = {m.pair for m in spec.mates if m.name in wanted}
        pairs = [(a, b) for a, b in pairs if frozenset((a.name, b.name)) in keep]
    remaining: dict[str, int] = {}
    for a, b in pairs:
        remaining[a.name] = remaining.get(a.name, 0) + 1
        remaining[b.name] = remaining.get(b.name, 0) + 1

    pair_reports: list[dict[str, Any]] = []
    mate_reports: list[dict[str, Any]] = []
    for a, b in pairs:
        mates = [
            m for m in spec.mates_of(a.name, b.name) if not wanted or m.name in wanted
        ]
        regions: list[Region] = []
        # coarse prune: a pair further apart than any mate looks is neither built nor measured
        reach = max(
            [1.0, *(fits[m.name].clearance_mm + fits[m.name].tol_mm + 1.0 for m in mates)]
        )
        near = boxes_overlap(a.box, b.box, reach)
        if near:
            try:
                regions = pair_regions(
                    a.name,
                    a.solid(),
                    b.name,
                    b.solid(),
                    eps_mm3=spec.settings.eps_mm3,
                    pad_mm=spec.settings.pair_pad_mm,
                )
            except MemoryError as exc:
                raise MemoryError(
                    f"out of memory judging {a.name} x {b.name}: "
                    "lower settings.max_faces"
                ) from exc
        classified = [classify_region(r, mates, fits, spec) for r in regions]
        for rep, region in zip(classified, regions, strict=True):
            if on_region is not None and rep["classification"] in (
                "blocking",
                "allowed",
            ):
                rep["_region"] = region
                on_region(scene, rep)
                rep.pop("_region", None)
        for mate in mates:
            mine = [
                c
                for c in classified
                if c["mate"] == mate.name
                or (c["mate"] is None and c["classification"] == "blocking")
            ]
            gap, search, method, spacing = (
                _mate_gap(mate, a, b, fits[mate.name])
                if near
                else (reach, reach, "pruned (far apart)", None)
            )
            verdict, info = _clearance_verdict(
                mate, fits[mate.name], gap, mine, search
            )
            info["method"] = method
            info["sample_spacing_mm"] = None if spacing is None else round(spacing, 3)
            mate_reports.append(
                {
                    "name": mate.name,
                    "a": mate.a,
                    "b": mate.b,
                    "kind": mate.kind,
                    "fit": fits[mate.name].to_dict(),
                    "clearance": info,
                    "regions": [_region_brief(c) for c in mine],
                    "verdict": verdict.to_dict(),
                }
            )
        blocking = [c for c in classified if c["classification"] == "blocking"]
        pair_status = "fail" if blocking else "pass"
        pair_reports.append(
            {
                "a": a.name,
                "b": b.name,
                "verdict": {
                    "status": pair_status,
                    "reasons": [c["reason"] for c in blocking]
                    or (
                        ["only designed interference / contact"]
                        if classified
                        else ["no overlap"]
                    ),
                },
                "regions": [_region_brief(c) for c in classified],
                "mates": [m.name for m in mates],
            }
        )
        for body in (a, b):
            remaining[body.name] -= 1
            if remaining[body.name] == 0:
                body.release()

    blocking_all = [
        r
        for p in pair_reports
        for r in p["regions"]
        if r["classification"] == "blocking"
    ]
    allowed_all = [
        r
        for p in pair_reports
        for r in p["regions"]
        if r["classification"] == "allowed"
    ]
    contacts = [
        r
        for p in pair_reports
        for r in p["regions"]
        if r["classification"] == "contact"
    ]
    failed = [m["name"] for m in mate_reports if m["verdict"]["status"] == "fail"]
    warned = [m["name"] for m in mate_reports if m["verdict"]["status"] == "warn"]
    ok = not blocking_all and not failed
    return {
        "schema": SCHEMA,
        "kind": "check",
        "ok": ok,
        "assembly": {"name": spec.name, "spec": str(spec.path) if spec.path else None},
        "context": {
            **context.to_dict(),
            "fits_fallback": any(f.fallback for f in fits.values()),
        },
        "thresholds": {
            "eps_mm3": spec.settings.eps_mm3,
            "seat_tol_mm": spec.settings.seat_tol_mm,
        },
        "legend": legend(spec.part_list()),
        "styles": {n: s.to_dict() for n, s in assign_styles(spec.part_list()).items()},
        "pairs": pair_reports,
        "mates": mate_reports,
        "blocking": blocking_all,
        "allowed": allowed_all,
        "summary": {
            "pairs_checked": len(pair_reports),
            "pairs_overlapping": sum(1 for p in pair_reports if p["regions"]),
            "blocking": len(blocking_all),
            "allowed": len(allowed_all),
            "contacts": len(contacts),
            "mates": len(mate_reports),
            "mates_failed": failed,
            "mates_warned": warned,
            "blocking_volume_mm3": round(sum(r["volume_mm3"] for r in blocking_all), 4),
            "allowed_volume_mm3": round(sum(r["volume_mm3"] for r in allowed_all), 4),
        },
        "elapsed_s": round(time.monotonic() - started, 2),
    }


def _region_brief(c: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in c.items() if not k.startswith("_")}


def with_context(
    spec: AssemblySpec, printer: str | None, material: str | None
) -> Context:
    """The spec's context with CLI `--printer/--material` laid over it."""
    base = spec.context
    return replace(
        base, printer=printer or base.printer, material=material or base.material
    )
