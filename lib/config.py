#!/usr/bin/env python3
"""Shared config loader for the generic collision tooling.

The collision engine (collision_check.py static gate, frame_check.py per-frame gate,
collision_viz.py renderer) is PROJECT-AGNOSTIC. Every project-specific name/number/path
lives in a JSON config (e.g. projects/ejector/verify/collision.json). NOTHING about a
specific project is hardcoded in tools/collision/ — parts, phases, intended contacts,
thresholds, the placement .scad, the timeline, and the pose-variable mapping all come
from the config.

CRITICAL: every path in the config is resolved RELATIVE TO THE CONFIG FILE'S DIRECTORY,
not relative to this module (which lives in tools/) and not relative to cwd.
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass


@dataclass
class FrameCfg:
    timeline: pathlib.Path     # absolute path to the project's pose(t) timeline .py
    timeline_fn: str           # function name in that module (returns dict of DOFs)
    frames: int                # default frame count (env FRAMES overrides)
    pose_sentinel: int         # phase value that tells pair.scad to use continuous pose
    pose_vars: dict[str, str]  # { pose_dict_key : openscad_-D_var_name }


@dataclass
class VizCfg:
    outdir: pathlib.Path       # absolute output dir for rendered PNGs
    name_prefix: str           # output file name prefix


@dataclass
class CollisionConfig:
    config_path: pathlib.Path
    config_dir: pathlib.Path
    pair: pathlib.Path                 # absolute path to the placement .scad
    parts: list[str]                   # part module names
    phases: dict[int, str]             # { phase_int : phase_name }
    intended: set[frozenset[str]]      # { frozenset((a, b)), ... }
    eps: float                         # mm^3 interpenetration volume threshold
    touch_tol: float                   # mm surface-surface zero-gap threshold
    contact_max: float                 # mm^3 max overlap excused for an intended contact
    frame: FrameCfg | None
    viz: VizCfg | None


def load(config_path: str | pathlib.Path) -> CollisionConfig:
    """Load a project collision config; resolve all paths relative to the config dir."""
    cfg_path = pathlib.Path(config_path).resolve()
    cfg_dir = cfg_path.parent
    with open(cfg_path) as fh:
        d = json.load(fh)

    def rel(p: str) -> pathlib.Path:
        return (cfg_dir / p).resolve()

    parts: list[str] = list(d["parts"])
    # JSON object keys are strings; phases come as a list of {"phase":int,"name":str}.
    phases: dict[int, str] = {int(ph["phase"]): str(ph["name"]) for ph in d["phases"]}
    intended: set[frozenset[str]] = {frozenset(pair) for pair in d["intended"]}

    fc = d.get("frame_check", {})
    frame: FrameCfg | None = FrameCfg(
        timeline=rel(fc["timeline"]),
        timeline_fn=fc.get("timeline_fn", "pose"),
        frames=int(fc.get("frames", 40)),
        pose_sentinel=int(fc.get("pose_phase_sentinel", -1)),
        pose_vars=dict(fc.get("pose_vars", {})),
    ) if fc else None

    vz = d.get("viz", {})
    viz: VizCfg | None = VizCfg(
        outdir=rel(vz.get("outdir", "../previews")),
        name_prefix=vz.get("name_prefix", "collision"),
    ) if vz else None

    return CollisionConfig(
        config_path=cfg_path,
        config_dir=cfg_dir,
        pair=rel(d["pair_scad"]),
        parts=parts,
        phases=phases,
        intended=intended,
        eps=float(d["eps_mm3"]),
        touch_tol=float(d["touch_tol_mm"]),
        contact_max=float(d["contact_max_mm3"]),
        frame=frame,
        viz=viz,
    )
