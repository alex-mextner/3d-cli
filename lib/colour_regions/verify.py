"""verify.py — surface-consistency gates of produced colour regions (`3d colour-regions verify`). Both can FAIL and both have a teeth test.

GATE 1  "boundary on a geometric edge": for every colour group (and every connected piece of it >= `MIN_COMPONENT_MM2`)
        the length share of its boundary (mesh edges between two faces of different colour groups) that lies on a
        geometric edge of the mesh: an edge whose own dihedral angle >= `dihedral` deg, or whose adjacent faces have a
        crease strength (angle between the mean normals 0.6 mm to either side, `Surf.edge_angle`) >= `crease` deg.
        Pass: length-weighted share over the gated groups >= `min_all` and every gated group >= `min_group`
        (`waive`d groups are reported but not gated: labels with no relief to follow).
        TEETH: the same measure on the labels with every colour boundary pushed `shift_mm` away (each region ERODED
        into the surface that holds it, geodesic on the mesh) must be at least `shift_margin` below the real share,
        and the real share must beat the chance level (share of ALL mesh edges that are geometric edges) by
        `chance_margin`. The grown share is information only: a feature enclosed by rims ~1 mm outside its border
        scores as high or higher when grown, so it cannot discriminate. Groups the owner approved (`approved`:
        borders transferred, not derived from relief) are gated only against chance + `APPROVED_MARGIN`.
GATE 2  "volumes follow the labels": for a sample of skin faces away from every colour border, the colour volume
        (`regions.json` patch) that contains the point 0.25 mm under the skin is the one of the face's group (and no
        patch contains it for a default-colour face): agreement >= `agree`. TEETH: with the groups cyclically
        permuted the agreement must collapse below 0.6 * `agree`.
INVARIANTS: a gate that cannot tell real from shifted/permuted labels FAILs; thresholds are options, not constants of
  one model, because on smooth skin without relief any border fails gate 1: that is the point of the gate.
ASSUMPTIONS: the mesh is the one the labels were transferred to (`labels_final.npz` of `build`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import scipy.sparse.csgraph as csg
import trimesh
from numpy.typing import NDArray

from colour_regions import CMD
from colour_regions.surface import Array, Mask, Surf, load_mesh
from errors import UsageError

MIN_COMPONENT_MM2 = 3.0
CREASE_SCALE_MM = 0.6
#: approved-border groups must at least beat chance by this much: "not worse than random relief"
APPROVED_MARGIN = 0.05
SAMPLE_DEPTH_MM = 0.25
BORDER_CLEARANCE_MM = 0.6
DEFAULT_SAMPLES = 40000


@dataclass(frozen=True)
class Thresholds:
    dihedral: float = 30.0  # deg
    crease: float = 30.0  # deg
    min_share: float = 0.50
    min_group: float = 0.35
    chance_margin: float = 0.25
    shift_margin: float = 0.06
    shift_mm: float = 1.5
    agree: float = 0.97


def edge_strength(s: Surf, th: Thresholds) -> Mask:
    """Bool per shared mesh edge (rows of `s.fa`): the edge lies on a geometric edge of the surface."""
    dih = np.degrees(np.asarray(s.mesh.face_adjacency_angles))
    e = s.edge_angle(CREASE_SCALE_MM)
    return np.asarray((dih >= th.dihedral) | (np.maximum(e[s.fa[:, 0]], e[s.fa[:, 1]]) >= th.crease))


def boundary_shares(
    s: Surf, group: Array, on_edge: Mask, names: list[str]
) -> tuple[dict[str, tuple[float, float]], tuple[float, float]]:
    """-> ({group name: (boundary length mm, share on a geometric edge)}, overall (length, share)). A boundary edge is
    shared by two groups: it counts for both."""
    a, b = group[s.fa[:, 0]], group[s.fa[:, 1]]
    m = (a != b) & s.outer[s.fa[:, 0]] & s.outer[s.fa[:, 1]]
    out: dict[str, tuple[float, float]] = {}
    for g in np.unique(np.r_[a[m], b[m]]):
        if g < 0:
            continue
        mg = m & ((a == g) | (b == g))
        length, length_on = s.fa_len[mg].sum(), s.fa_len[mg & on_edge].sum()
        out[names[g]] = (float(length), float(length_on / length))
    mm = m & ((a >= 0) | (b >= 0))
    tot, tot_on = s.fa_len[mm].sum(), s.fa_len[mm & on_edge].sum()
    return out, (float(tot), float(tot_on / max(tot, 1e-9)))


def component_shares(s: Surf, group: Array, on_edge: Mask, names: list[str]) -> list[dict[str, Any]]:
    """Boundary share per connected piece of every group (pieces under `MIN_COMPONENT_MM2` skipped)."""
    rows: list[dict[str, Any]] = []
    a, b = group[s.fa[:, 0]], group[s.fa[:, 1]]
    for g in np.unique(group[s.outer]):
        if g < 0:
            continue
        n, lab = s.components((group == g) & s.outer)
        for k in range(n):
            comp = lab == k
            if s.area[comp].sum() < MIN_COMPONENT_MM2:
                continue
            m = (comp[s.fa[:, 0]] != comp[s.fa[:, 1]]) & (a != b)
            length = s.fa_len[m].sum()
            if length == 0:
                continue
            centre = s.c[comp].mean(0)
            rows.append(
                {
                    "group": names[g],
                    "centre": [round(float(x), 1) for x in centre],
                    "area_mm2": round(float(s.area[comp].sum()), 1),
                    "boundary_mm": round(float(length), 1),
                    "on_edge": round(float(s.fa_len[m & on_edge].sum() / length), 3),
                }
            )
    return rows


def shifted(s: Surf, group: Array, mm: float, grow: bool) -> Array:
    """Colour boundaries pushed `mm` away (geodesic): grow = every coloured region takes the default faces within mm of
    it (nearest region wins); else the default skin takes the coloured faces within mm of it."""
    src = (group >= 0) if grow else (group < 0)
    src &= s.outer
    # scipy returns (dist, predecessors, SOURCES) for min_only=True: `nearest_src` = the nearest source face of every
    # face (needed to pick the winning region)
    dist, _, nearest_src = csg.dijkstra(
        s.adj, directed=False, indices=np.nonzero(src)[0], min_only=True, return_predecessors=True, limit=mm
    )
    out = group.copy()
    reach = np.isfinite(dist) & s.outer
    if grow:
        take = reach & (group < 0)
        out[take] = group[nearest_src[take]]
    else:
        out[reach & (group >= 0)] = -1
    return out


def _weighted_share(per: dict[str, tuple[float, float]]) -> float:
    """Length-weighted on-edge share over groups given as {name: (boundary length, share)}."""
    length = sum(v[0] for v in per.values())
    return sum(v[0] * v[1] for v in per.values()) / max(length, 1e-9)


def gate_edges(
    s: Surf,
    group: Array,
    names: list[str],
    th: Thresholds,
    waive: set[str],
    approved: set[str],
) -> dict[str, Any]:
    """Gate 1 on `group` (id per face, -1 default). The shift teeth are measured on the labels WITHOUT the approved
    groups, so approved borders do not confound the shifted-boundary comparison. With no gated group left (all waived
    or approved) the real-share and teeth checks do not apply and only the approved groups are judged; with nothing to
    judge at all the call is refused instead of passing vacuously."""
    on = edge_strength(s, th)
    chance = float(s.fa_len[on & s.outer[s.fa[:, 0]]].sum() / s.fa_len[s.outer[s.fa[:, 0]]].sum())
    per_all, (total_len, total_share) = boundary_shares(s, group, on, names)
    comps = component_shares(s, group, on, names)
    approved_ids = np.array([i for i, n in enumerate(names) if n in approved], int)
    group_sculpt = np.where(np.isin(group, approved_ids), -1, group)

    def is_gated(g: str) -> bool:
        return g not in waive and g not in approved

    per_sculpt, _ = boundary_shares(s, group_sculpt, on, names)
    gated = {g: v for g, v in per_sculpt.items() if is_gated(g)}
    approved_here = {g: v for g, v in per_all.items() if g in approved}
    if not gated and not approved_here:
        raise UsageError(
            "no colour border left to verify: every group is waived, or no group has a border with another",
            command=CMD,
            remediation=["Drop --waive for at least one group, or build labels with a border between two groups."],
        )

    def gated_share(grp: Array) -> float:
        p, _ = boundary_shares(s, grp, on, names)
        return _weighted_share({g: v for g, v in p.items() if is_gated(g)})

    share = _weighted_share(gated)
    share_grown = gated_share(shifted(s, group_sculpt, th.shift_mm, True))
    share_eroded = gated_share(shifted(s, group_sculpt, th.shift_mm, False))
    app = {g: {"on_edge": round(v[1], 3), "length_mm": round(v[0], 1)} for g, v in approved_here.items()}
    app_info: dict[str, dict[str, float]] = {}
    for g in app:
        only_g = np.where(group == names.index(g), names.index(g), -1)
        real = boundary_shares(s, only_g, on, names)[0][g][1]
        grown = boundary_shares(s, shifted(s, only_g, th.shift_mm, True), on, names)[0].get(g, (0.0, 0.0))[1]
        eroded = boundary_shares(s, shifted(s, only_g, th.shift_mm, False), on, names)[0].get(g, (0.0, 0.0))[1]
        app_info[g] = {"real": round(real, 3), "grown": round(grown, 3), "eroded": round(eroded, 3)}
    res: dict[str, Any] = {
        "chance": chance,
        "per_group": {
            g: {"length_mm": round(v[0], 1), "on_edge": round(v[1], 3), "gated": is_gated(g)} for g, v in per_sculpt.items()
        },
        "approved": app,
        "approved_shift_info": app_info,
        "all_groups": {"length_mm": round(total_len, 1), "on_edge": round(total_share, 3)},
        "gated_groups": len(gated),
        "gated_share": share,
        "shifted_grow": share_grown,
        "shifted_erode": share_eroded,
        "components": comps,
    }
    res["real_pass"] = not gated or bool(share >= th.min_share and all(v[1] >= th.min_group for v in gated.values()))
    res["teeth_pass"] = not gated or bool(
        share_eroded <= share - th.shift_margin and share > chance + th.chance_margin
    )
    res["approved_pass"] = bool(all(v["on_edge"] >= chance + APPROVED_MARGIN for v in app.values()))
    res["pass"] = res["real_pass"] and res["teeth_pass"] and res["approved_pass"]
    return res


def load_regions(path: str | Path) -> list[tuple[str, trimesh.Trimesh]]:
    """`regions.json` -> [(group name, patch mesh)] in priority order (paths relative to the file)."""
    p = Path(path)
    if not p.is_file():
        raise UsageError(
            f"{p} not found",
            command=CMD,
            remediation=["Run `3d colour-regions build` first, or pass --regions FILE.json."],
        )
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        regions = doc["regions"] if isinstance(doc, dict) else doc
        entries = [(str(r["name"]), r["shape"]) for r in regions]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise UsageError(
            f"{p}: not a regions.json ({type(exc).__name__}: {exc})",
            command=CMD,
            remediation=["Expected {\"regions\": [{\"name\", \"assign\", \"shape\": {\"type\": \"stl\", \"path\"}}]}."],
        ) from exc
    out: list[tuple[str, trimesh.Trimesh]] = []
    for name, shape in entries:
        if not isinstance(shape, dict) or shape.get("type") != "stl" or "path" not in shape:
            raise UsageError(f"{p}: region {name!r} is not an stl shape with a path", command=CMD)
        patch = (p.parent / shape["path"]).resolve()
        if not patch.is_file():
            raise UsageError(f"patch mesh not found: {patch}", command=CMD)
        out.append((name, load_mesh(patch)))
    return out


def gate_volumes(
    s: Surf,
    group: Array,
    names: list[str],
    patches: list[tuple[str, trimesh.Trimesh]],
    th: Thresholds,
    n: int = DEFAULT_SAMPLES,
    seed: int = 1,
) -> dict[str, Any]:
    """Gate 2: the patch holding the point `SAMPLE_DEPTH_MM` under the skin is the one of the face's group."""
    none = "<none>"
    a, b = group[s.fa[:, 0]], group[s.fa[:, 1]]
    border = np.zeros(s.nf, bool)
    bm = a != b
    border[s.fa[bm, 0]] = border[s.fa[bm, 1]] = True
    d = s.dist_to_faces(border & s.outer, limit=BORDER_CLEARANCE_MM)
    cand = np.nonzero(s.outer & ~np.isfinite(d))[0]
    colored = cand[group[cand] >= 0]
    plain = cand[group[cand] < 0]
    if len(colored) == 0 or len(plain) == 0:
        raise UsageError(
            "gate 2 needs coloured and default faces away from every border",
            command=CMD,
            remediation=["Skip it with --skip-volumes when the whole surface is one colour group."],
        )
    rng = np.random.default_rng(seed)
    pick = np.r_[
        rng.choice(colored, min(len(colored), n // 2), replace=False),  # the coloured groups are the point of the test
        rng.choice(plain, min(len(plain), n // 2), replace=False),
    ]
    q = s.c[pick] - SAMPLE_DEPTH_MM * s.n[pick]
    got = np.full(len(pick), none, dtype=object)
    for name, m in reversed(patches):  # the first region wins where patches overlap
        lo, hi = m.bounds
        box = np.all((q > lo - 0.1) & (q < hi + 0.1), axis=1)
        if box.any():
            hit = np.zeros(len(pick), bool)
            hit[box] = m.contains(q[box])
            got[hit] = name
    pool = [none, *names]  # class of group g (-1 = default) is pool[g + 1]
    exp = np.array([pool[g + 1] for g in group[pick]], dtype=object)
    agree = float((got == exp).mean())
    # teeth: every face is expected in the NEXT class of the pool (cyclic: the groups plus the default), which is never
    # its own, so the permutation can never be a no-op
    exp_perm = np.array([pool[(g + 2) % len(pool)] for g in group[pick]], dtype=object)
    agree_perm = float((got == exp_perm).mean())
    per: dict[str, dict[str, float]] = {}
    for g in range(-1, len(names)):
        mk = group[pick] == g
        if mk.any():
            per[pool[g + 1]] = {"n": int(mk.sum()), "agree": round(float((got[mk] == exp[mk]).mean()), 3)}
    return {
        "agree": agree,
        "agree_permuted": agree_perm,
        "n": int(len(pick)),
        "per_group": per,
        "pass": bool(agree >= th.agree and agree_perm < 0.6 * th.agree),
    }


def format_edges(r: dict[str, Any], th: Thresholds, waive: set[str]) -> list[str]:
    """Human report of gate 1."""
    lines = [
        f"GATE 1 boundary on a geometric edge (dihedral >= {th.dihedral:.0f} deg or crease >= {th.crease:.0f} deg): "
        f"chance level {r['chance']:.3f}"
    ]
    for g, v in r["per_group"].items():
        lines.append(f"  {g:10s} boundary {v['length_mm']:8.1f} mm  on-edge {v['on_edge']:.3f}  {'' if v['gated'] else '(not gated here)'}")
    for g, v in r["approved"].items():
        info = r["approved_shift_info"][g]
        lines.append(
            f"  {g:10s} APPROVED borders (transferred, not relief-derived) {v['length_mm']:8.1f} mm  on-edge {v['on_edge']:.3f}"
            f"  (needs >= chance + {APPROVED_MARGIN} = {r['chance'] + APPROVED_MARGIN:.3f}): "
            f"{'PASS' if r['approved_pass'] else 'FAIL'};  information, no teeth claimed: its own borders shifted "
            f"{th.shift_mm} mm give grown {info['grown']:.3f} / eroded {info['eroded']:.3f} (real {info['real']:.3f})"
        )
    if r["gated_groups"] == 0:
        lines.append("  gated share n/a, teeth n/a: every group with a border is waived or approved")
    else:
        lines.append(
            f"  gated share {r['gated_share']:.3f} (needs >= {th.min_share}, each gated group >= {th.min_group}): "
            f"{'PASS' if r['real_pass'] else 'FAIL'}"
        )
        lines.append(
            f"  teeth: boundaries pushed {th.shift_mm} mm: eroded {r['shifted_erode']:.3f} (must be >= {th.shift_margin} below "
            f"the real share; grown {r['shifted_grow']:.3f} is information only), real beats chance + {th.chance_margin} = "
            f"{r['chance'] + th.chance_margin:.3f}: "
            f"{'PASS (the gate has teeth)' if r['teeth_pass'] else 'FAIL (the gate cannot tell shifted borders from real ones)'}"
        )
    approved = set(r["approved"])
    lines.extend(
        f"  weak piece: {c}"
        for c in r["components"]
        if c["on_edge"] < th.min_group and c["group"] not in waive and c["group"] not in approved
    )
    return lines


def format_volumes(r: dict[str, Any], th: Thresholds) -> str:
    """Human report of gate 2."""
    return (
        f"GATE 2 volumes follow the labels: {r['agree']:.4f} of {r['n']} sampled faces (>= {th.agree}); "
        f"permuted labels {r['agree_permuted']:.4f} (< {0.6 * th.agree:.2f})  ->  {'PASS' if r['pass'] else 'FAIL'}   "
        f"per group (<none> = default): {r['per_group']}"
    )


def verify_work(
    mesh: str | Path,
    group: NDArray[np.int_],
    names: list[str],
    *,
    th: Thresholds,
    waive: set[str],
    approved: set[str],
    regions: str | Path | None,
    samples: int = DEFAULT_SAMPLES,
) -> tuple[dict[str, Any], list[str]]:
    """Run gate 1 and (with `regions`) gate 2 -> (result dict with `pass`, report lines)."""
    s = Surf(mesh)
    if len(group) != s.nf:
        raise UsageError(
            f"labels_final.npz has {len(group)} faces but {mesh} has {s.nf}",
            command=CMD,
            remediation=["Verify against the mesh `build` was run on."],
        )
    if len(group) and (group.max() >= len(names) or group.min() < -1):
        raise UsageError(
            f"labels_final.npz: face_group ids outside -1..{len(names) - 1}",
            command=CMD,
            remediation=["The file is truncated or edited: run `3d colour-regions build` again."],
        )
    unknown = sorted((waive | approved) - set(names))
    if unknown:
        raise UsageError(
            f"unknown group(s) in --waive/--approved-groups: {', '.join(unknown)}",
            command=CMD,
            remediation=[f"The groups of this build are: {', '.join(names)}"],
        )
    r1 = gate_edges(s, group, names, th, waive, approved)
    lines = format_edges(r1, th, waive)
    result: dict[str, Any] = {"edges": r1}
    ok = bool(r1["pass"])
    if regions is not None:
        r2 = gate_volumes(s, group, names, load_regions(regions), th, n=samples)
        result["volumes"] = r2
        lines.append(format_volumes(r2, th))
        ok = ok and bool(r2["pass"])
    result["pass"] = ok
    return result, lines
