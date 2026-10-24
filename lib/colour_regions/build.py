"""build.py — per-face label files -> colour groups -> conformal colour volumes (`3d colour-regions build`).

WHAT: the label files of one or more TRACKS (in increasing priority: a later track wins on a face) are transferred
  onto the target mesh (identity or nearest face, `labels.resolve_labels`), merged, mapped to colour groups by the
  rules file, cleaned of specks, and every group becomes one closed volume per connected piece (`volume.shell_volume`).
OUTPUTS in the work directory:
  patches/<group>.ply   the colour volumes
  regions.json          {"regions": [...], "groups": [...], "labels": [...], "mesh_digest": ...}; each region is
                        {name, assign, shape: {type: stl, path}} with the path relative to regions.json: the `regions`
                        entries of a `3d prep` colour spec
  labels_final.npz      per face of the mesh: face_label + names, face_group + group_names + group_assign, mesh_digest
  transfer.json         per track: how its labels reached the mesh (mode, distances, counts per label)
INVARIANTS:
  * Colour groups: rules of one group form ONE volume per connected piece, competing only against faces of OTHER
    groups and the default skin, so adjacent groups meet on the bisector of their skin faces, i.e. on the label border.
  * Priority on a face labelled by several tracks: the later track. A label with `passive` never overrides; a label
    with `owner` is taken only from that track while it is among the inputs.
  * A coloured label on a body that is not the main surface (a cavity, an inserted part) cannot be coloured: error.
  * A patch is rebuilt only when its faces, depth, pitch or the volume code changed (sidecar `.sha`).
PAST BUGS: none yet.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from colour_regions import CMD
from colour_regions import surface as surface_mod
from colour_regions import volume as volume_mod
from colour_regions.labels import clean_islands, mesh_digest, parse_spec, resolve_labels, write_labels
from colour_regions.rules import Rules
from colour_regions.surface import Array, Surf
from errors import UsageError


@dataclass(frozen=True)
class Track:
    name: str
    spec: str  # labels.npz or labels.npz@mesh


def parse_track(raw: str) -> Track:
    """`NAME=labels.npz[@mesh]` -> Track."""
    name, sep, spec = raw.partition("=")
    if not sep or not name or not spec:
        raise UsageError(
            f"--track {raw!r} is not NAME=labels.npz[@mesh]",
            command=CMD,
            remediation=["e.g. --track features=features.npz --track eyes=eyes.npz@eyes_step.stl"],
        )
    return Track(name, spec)


def merge_tracks(
    s: Surf, tracks: list[Track], rules: Rules
) -> tuple[NDArray[np.int32], list[str], dict[str, Any]]:
    """Tracks in INCREASING priority -> (label id per face over the union name list, names, per-track stats)."""
    if len({t.name for t in tracks}) != len(tracks):
        raise UsageError("two --track options share a name", command=CMD, remediation=["Track names must be unique."])
    present = {t.name for t in tracks}
    names: list[str] = []
    out = np.full(s.nf, -1, np.int32)
    stats: dict[str, Any] = {}
    for track in tracks:
        fl, nm, st = resolve_labels(track.spec, s, rules.transfer_tol_mm)
        rs = [rules.rule_of(n) for n in nm]
        passive = [i for i, r in enumerate(rs) if r.passive]  # "nothing sculpted here": must not override a lower track
        if passive:
            fl = np.where(np.isin(fl, passive), -1, fl).astype(np.int16)
        foreign = [i for i, r in enumerate(rs) if r.owner and r.owner != track.name and r.owner in present]
        if foreign:  # these labels come ONLY from their owner track
            fl = np.where(np.isin(fl, foreign), -1, fl).astype(np.int16)
            st["ignored_owned_labels"] = [nm[i] for i in foreign]
        stats[track.name] = dict(
            st,
            file=str(parse_spec(track.spec)[0]),
            labels={n: int((fl == i).sum()) for i, n in enumerate(nm)},
        )
        for n in nm:
            if n not in names:
                names.append(n)
        remap = np.array([names.index(n) for n in nm] + [-1], np.int32)
        coloured = np.array([r.coloured for r in rs] + [False])  # default-colour labels on a cavity body are harmless
        lost = (fl >= 0) & ~s.outer & coloured[fl]
        if lost.any():
            raise UsageError(
                f"track {track.name!r}: {int(lost.sum())} labelled faces lie on a body that is not the main surface",
                command=CMD,
                remediation=[
                    "A coloured label on a separate shell (a cavity, an inserted part) cannot be coloured: "
                    "union the body into the mesh or remove the label there."
                ],
            )
        m = fl >= 0
        out[m] = remap[fl[m]]
    return out, names, stats


def _code_stamp(digest: str, nf: int) -> str:
    """What invalidates a cached patch besides its own faces: the volume and surface code and the mesh identity."""
    src = b"".join(Path(m.__file__).read_bytes() for m in (volume_mod, surface_mod) if m.__file__)
    return hashlib.sha1(src + f"{digest}{nf}".encode()).hexdigest()


@dataclass(frozen=True)
class BuildResult:
    regions: list[dict[str, Any]]
    groups: list[dict[str, Any]]
    labels: list[str]
    stats: dict[str, Any]


def build(
    mesh: str | Path,
    tracks: list[Track],
    rules: Rules,
    work: str | Path,
    *,
    force: bool = False,
    verbose: bool = True,
    full_reach: bool = False,
) -> BuildResult:
    """Run the whole labels -> regions pipeline; writes the work directory described in the module docstring."""
    if not tracks:
        raise UsageError(
            "no label files",
            command=CMD,
            remediation=["Pass at least one --track NAME=labels.npz[@mesh]."],
        )
    wdir = Path(work)
    (wdir / "patches").mkdir(parents=True, exist_ok=True)
    s = Surf(mesh)
    lab, names, stats = merge_tracks(s, tracks, rules)
    rs = [rules.rule_of(n) for n in names]
    order = rules.group_order()
    keys = sorted({r.group for r in rs if r.coloured}, key=order.index)  # priority = order of the rules
    gid = np.array([keys.index(r.group) if r.coloured else -1 for r in rs] + [-1])
    group = np.where(lab >= 0, gid[lab], -1).astype(np.int16)
    group = clean_islands(s, group, lab, rules.island_mm2)
    digest = mesh_digest(s.mesh.triangles)
    assigns = [dict(rules.group_rule(k).assign or {}) for k in keys]
    write_labels(
        wdir / "labels_final.npz",
        lab,
        names,
        digest=digest,
        face_group=group,
        group_names=np.array(json.dumps(keys)),
        group_assign=np.array(json.dumps(assigns)),
    )
    selections = [(group == gi) & s.outer for gi in range(len(keys))]
    gone = [k for k, sel in zip(keys, selections, strict=True) if not sel.any()]
    if gone:
        print(f"  WARNING: colour groups with no faces after cleaning: {gone}", file=sys.stderr)
    code = _code_stamp(digest, s.nf)
    regions: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    for k, sel in zip(keys, selections, strict=True):
        rule = rules.group_rule(k)
        if not sel.any():
            continue
        n_pieces, piece = s.components(sel)
        areas = np.bincount(piece[piece >= 0], weights=s.area[piece >= 0], minlength=n_pieces)
        if not (areas >= volume_mod.MIN_CELL_AREA_MM2).any():
            print(f"  WARNING: colour group {k!r}: every piece is under {volume_mod.MIN_CELL_AREA_MM2} mm2, no volume made", file=sys.stderr)
            continue
        rel = f"patches/{k}.ply"
        path = wdir / rel
        sha = path.with_name(path.name + ".sha")
        key = json.dumps([rule.depth_mm, rule.pitch_mm, full_reach, code]).encode()
        stamp = hashlib.sha1(np.packbits(sel).tobytes() + key).hexdigest()
        if force or not (path.exists() and sha.exists() and sha.read_text() == stamp):
            if verbose:
                print(
                    f"  group {k}: {int(sel.sum())} faces, {s.area[sel].sum():.0f} mm2, "
                    f"depth {rule.depth_mm} pitch {rule.pitch_mm}",
                    flush=True,
                )
            sha.unlink(missing_ok=True)  # a failed rebuild must not leave an old stamp over a new, bad patch
            volume_mod.shell_volume(s, sel, rule.depth_mm, rule.pitch_mm, path, full_reach=full_reach)
            sha.write_text(stamp)
        regions.append({"name": k, "assign": dict(rule.assign or {}), "shape": {"type": "stl", "path": rel}})
        groups.append(
            {
                "name": k,
                "assign": dict(rule.assign or {}),
                "depth_mm": rule.depth_mm,
                "pitch_mm": rule.pitch_mm,
                "min_speck_mm3": rule.min_speck_mm3,
                "faces": int(sel.sum()),
                "area_mm2": round(float(s.area[sel].sum()), 2),
                "patch": rel,
            }
        )
    (wdir / "transfer.json").write_text(json.dumps(stats, indent=1), encoding="utf-8")
    doc = {"regions": regions, "groups": groups, "labels": names, "mesh_digest": digest}
    (wdir / "regions.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
    return BuildResult(regions, groups, names, stats)


def load_groups(work: str | Path) -> tuple[Array, list[str], list[dict[str, Any]]]:
    """`labels_final.npz` of a work directory -> (face_group, group names, group assigns)."""
    p = Path(work) / "labels_final.npz"
    if not p.is_file():
        raise UsageError(
            f"{p} not found",
            command=CMD,
            remediation=["Run `3d colour-regions build` first; it writes labels_final.npz into its --out directory."],
        )
    with np.load(p, allow_pickle=False) as z:
        return (
            z["face_group"].astype(int),
            list(json.loads(str(z["group_names"]))),
            list(json.loads(str(z["group_assign"]))),
        )
