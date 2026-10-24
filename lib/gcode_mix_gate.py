"""gcode_mix_gate.py — G-code gates for a multi-colour (Full Spectrum / mixed-filament) print, judged on what was printed.

WHAT: streams a sliced G-code once (gcode_scan.py) and decides, from the extrusion itself:
  - COLOUR VOLUMES: every outer-wall point of the real print is attributed to the colour volume (a part STL placed
    in bed coordinates) whose surface it lies on. A pure volume must print one tool; a mixed volume must print the
    pattern's tool at every layer number (pattern index = layer number counted from the first layer, no repeats
    of a layer); an accent (dither) volume must print its accent tool on exactly the pattern's accent layers; a thin
    volume (a few mm3: nostril slits) is read from the extrusion inside it. A mix is also refused when one tool
    prints a stripe thicker than one layer of at most `max_stripe_mm`.
  - A layer of a THICK mixed volume is judged on its OWN outer-wall evidence: the extruded length of outer-wall
    segments that end on an unambiguous point of the volume (>= `min_evidence_mm` = 1.0 mm, 2.5 line widths) is a
    "solid" layer, and a solid layer must match the pattern with zero tolerance. A layer with less evidence (the iris
    top and the lid rim: a 2 mm sliver whose every point is within the margin of another colour) is re-read at the
    small margin and may be off in <= 5 % of the layers; with < 1.0 mm even there it is reported, not judged.
  - PAUSE: with a `pause` in the config exactly one M600, in the layer whose top Z is `pause.print_z`, before that
    layer extruded anything, the previous layer's top Z matching; without it no M600 at all.
  - TOOLS: the heads that used filament (the footer's `filament used [g]`) are exactly the configured heads, a prime
    tower is printed iff expected (default: more than one head), tool-change count and the material per tool and
    feature type (the extrusion moves' own gram accounting next to the slicer's footer).
  - CAVITY: a pocket outline (--cavity JSON) stays void between its floor and the pause, then closes above the ridge.
  - LAYERS / SETTINGS: optional layer-height window and Z == ;HEIGHT, expected footer settings.
  - TEETH: `mix_teeth` / `cavity_teeth` re-run the gate on a deliberately wrong print (the pattern shifted by one
    layer; one solid layer printed with a wrong tool; the pocket outline moved over solid body; the pause 8 mm too
    high). Each control MUST make its gate FAIL; a control that does not trips the verdict "vacuous gate" — a gate
    that cannot fail proves nothing.
WHY: Chekhov cat phone stand on the Snapmaker U1 (2026-10, issue #551). A recipe that validates can print a shifted
  pattern; the gate first failed on false positives at the iris (fringe layers with <1 mm of own outer wall, judged
  by strand/neighbour evidence) and the cavity strip; those fixes are what is ported here.
INVARIANTS: the G-code is streamed, never held as one string; per layer only compact observations are kept, so the
  teeth re-evaluate observations instead of re-querying the meshes; tools are 0-based (slot n = tool T(n-1)); numpy is
  imported at the top, scipy and trimesh when the colour volumes are built; the command runs this file through
  cli.pyrun, so the dispatcher itself stays import-light.
"""

from __future__ import annotations

import json
import math
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from errors import InputNotFound, InvalidArgument, ThreeDError, UsageError
from gcode_scan import Layer, Scan, scan_gcode

CMD = "gcode-mix-gate"

THIN_VOLUME_MM3 = (
    25.0  # mix volumes smaller than this are read from the extrusion inside them
)
DITHER_WRONG_THIN = 0.15  # pattern tolerance of small-margin volumes
DITHER_MISSING = 0.40  # accent layers allowed to show only the base tool at hair tips
DITHER_MARGIN = 0.08  # attribution margin for hairs / fringe layers (mm)
LOOSE_MARGIN = (
    0.45  # second attribution margin when nothing is 0.9 mm from every boundary
)
MIN_EVIDENCE_MM = 1.0
MAX_STRIPE_MM = (
    0.10  # one layer of a mixed region; 1 µm tolerance (G-code prints 0.100003)
)
DEFAULT_MARGIN_MM = 0.9
DEFAULT_BED_TOL_MM = 0.45
SOLID_FRACTION_OFF = 0.05  # fringe layers allowed off the pattern of a thick volume
MIN_CAVITY_LAYERS = 20  # layers that must lie between floor and pause, or the outline is in the wrong Z range
CAVITY_WALL_MARGIN_MM = 0.8
RIDGE_STRIP_X_MARGIN_MM = (
    6.0  # centre strip under a gable ridge along X: |x| < half-width - 6, |y - y0| < 5
)
RIDGE_STRIP_HALF_Y_MM = 5.0
PAUSE_TEETH_OFFSET_MM = 8.0
CAVITY_TEETH_FRACTION = 0.7  # outline moved by 0.7 x its X extent lands over solid body


# ---------------------------------------------------------------- report


@dataclass(frozen=True)
class Row:
    name: str
    status: str  # PASS | FAIL | SKIP
    detail: str
    key: str = ""  # stable id the teeth look rows up by (the display name may be reworded)

    @property
    def ok(self) -> bool:
        return self.status != "FAIL"


@dataclass
class Report:
    rows: list[Row] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str, key: str = "") -> None:
        self.rows.append(Row(name, "PASS" if ok else "FAIL", detail, key))

    def skip(self, name: str, detail: str) -> None:
        self.rows.append(Row(name, "SKIP", detail))

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.rows)

    def render(self) -> str:
        return "\n".join(f"  {r.status}  {r.name}: {r.detail}" for r in self.rows)


# ---------------------------------------------------------------- config


@dataclass(frozen=True)
class Part:
    name: str
    stl: str | None
    slot_tool: int | None  # pure part: its tool
    mix: tuple[int, ...]  # mixed part: tool per layer number
    label: str
    accent: int | None  # dither part: the accent tool


@dataclass(frozen=True)
class PauseSpec:
    print_z: float
    previous_top: float
    note: str = ""


@dataclass(frozen=True)
class MixConfig:
    parts: tuple[Part, ...]
    default_tool: int | None
    heads: frozenset[int] | None
    prime_tower: bool | None
    pause: PauseSpec | None
    shift: tuple[float, float, float]
    settings: Mapping[str, str]
    parts_dir: str | None

    def referenced_tools(self) -> set[int]:
        tools: set[int] = set()
        for p in self.parts:
            tools.update(p.mix)
            if p.slot_tool is not None:
                tools.add(p.slot_tool)
        if self.default_tool is not None:
            tools.add(self.default_tool)
        return tools


def _bad(flag: str, got: object, accepted: Sequence[str]) -> InvalidArgument:
    return InvalidArgument(flag, str(got), accepted, command=CMD)


def _is_slot(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 1


def _is_number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _parse_mix(value: object, where: str) -> tuple[int, ...]:
    """`"121242"` or `[1, 2, 1, 2, 4, 2]` (1-based slots) -> 0-based tools per layer number."""
    seq = (
        [int(ch) for ch in value]
        if isinstance(value, str) and value.isdigit()
        else value
    )
    if (
        not isinstance(seq, list)
        or not seq
        or not all(_is_slot(s) for s in seq)
    ):
        raise _bad(
            f"{where}.mix",
            value,
            ["digit string such as '121242'", "list of 1-based slots"],
        )
    return tuple(s - 1 for s in seq)


def parse_config(
    data: Mapping[str, Any], *, default_slot: int | None = None
) -> MixConfig:
    """Validate the JSON config. `parts[i].assign` is `{"slot": n}` (pure) or `{"mix": "1212", "mix_src": label,
    "accent": n}`; every slot is 1-based (slot n = tool T(n-1))."""
    parts: list[Part] = []
    raw_parts = data.get("parts", [])
    if not isinstance(raw_parts, list):
        raise _bad("parts", raw_parts, ["a list of {'name': str, 'assign': {...}}"])
    for i, raw in enumerate(raw_parts):
        where = f"parts[{i}]"
        assign = raw.get("assign") if isinstance(raw, dict) else None
        name = raw.get("name") if isinstance(raw, dict) else None
        if not isinstance(assign, dict) or not isinstance(name, str):
            raise _bad(
                where,
                raw,
                [
                    "{'name': str, 'assign': {'slot': n} | {'mix': '1212'[, 'accent': n]}}"
                ],
            )
        stl = raw.get("stl")
        if "slot" in assign:
            slot = assign["slot"]
            if not _is_slot(slot):
                raise _bad(f"{where}.assign.slot", slot, ["1-based slot number"])
            parts.append(Part(name, stl, slot - 1, (), f"slot {slot}", None))
        elif "mix" in assign:
            mix = _parse_mix(assign["mix"], f"{where}.assign")
            accent = assign.get("accent")
            if accent is not None and not _is_slot(accent):
                raise _bad(f"{where}.assign.accent", accent, ["1-based slot number"])
            parts.append(
                Part(
                    name,
                    stl,
                    None,
                    mix,
                    str(assign.get("mix_src", assign["mix"])),
                    None if accent is None else accent - 1,
                )
            )
        else:
            raise _bad(f"{where}.assign", assign, ["{'slot': n}", "{'mix': '1212'}"])
    d_slot = default_slot if default_slot is not None else data.get("default_slot")
    if d_slot is not None and not _is_slot(d_slot):
        raise _bad("default_slot", d_slot, ["1-based slot number"])
    heads = data.get("heads_slots")
    if heads is not None and (
        not isinstance(heads, list)
        or not all(_is_slot(s) for s in heads)
    ):
        raise _bad("heads_slots", heads, ["list of 1-based slot numbers"])
    pause = data.get("pause")
    spec: PauseSpec | None = None
    if pause:
        try:
            top = pause["last_layer_before"]["top"]
            spec = PauseSpec(
                float(pause["print_z"]), float(top), str(pause.get("message", ""))
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise _bad(
                "pause", pause, ["{'print_z': Z, 'last_layer_before': {'top': Z}}"]
            ) from exc
    shift = data.get("shift", [0.0, 0.0, 0.0])
    if not (isinstance(shift, list) and len(shift) == 3 and all(_is_number(v) for v in shift)):
        raise _bad("shift", shift, ["[dx, dy, dz] numbers"])
    prime = data.get("prime_tower")
    if prime is not None and not isinstance(prime, bool):
        raise _bad("prime_tower", prime, ["true", "false"])
    parts_dir = data.get("parts_dir")
    if parts_dir is not None and not isinstance(parts_dir, str):
        raise _bad("parts_dir", parts_dir, ["a directory path"])
    settings = data.get("expect_settings", {})
    if not isinstance(settings, dict):
        raise _bad("expect_settings", settings, ["{'wall_loops': '4', ...}"])
    return MixConfig(
        parts=tuple(parts),
        default_tool=None if d_slot is None else d_slot - 1,
        heads=None if heads is None else frozenset(s - 1 for s in heads),
        prime_tower=prime,
        pause=spec,
        shift=(float(shift[0]), float(shift[1]), float(shift[2])),
        settings={str(k): str(v) for k, v in settings.items()},
        parts_dir=parts_dir,
    )


def load_config(path: Path, *, default_slot: int | None = None) -> MixConfig:
    if not path.is_file():
        raise InputNotFound(str(path), command=CMD)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise _bad("--config", f"{path}: {exc}", ["a JSON object"]) from exc
    if not isinstance(data, dict):
        raise _bad("--config", path, ["a JSON object"])
    cfg = parse_config(data, default_slot=default_slot)
    if cfg.parts_dir is not None and not os.path.isabs(cfg.parts_dir):
        cfg = replace(cfg, parts_dir=str(path.parent / cfg.parts_dir))
    return cfg


# ---------------------------------------------------------------- colour volumes


@dataclass
class LayerObs:
    """What one layer's outer wall (and, for thin volumes, inner wall) says about the colour volumes."""

    seen: dict[float, dict[int, set[int]]] = field(
        default_factory=dict
    )  # margin -> part -> tools
    evid: dict[float, dict[int, float]] = field(
        default_factory=dict
    )  # margin -> part -> mm of own outer wall
    inside: dict[int, set[int]] = field(
        default_factory=dict
    )  # thin part -> tools with >= 2 points inside

    def relabel(self, old: int, new: int) -> LayerObs:
        """The same layer as if `old` had been printed with `new` (everywhere in it)."""

        def swap(ts: set[int]) -> set[int]:
            return {new if t == old else t for t in ts}

        return LayerObs(
            seen={
                mg: {pi: swap(ts) for pi, ts in d.items()}
                for mg, d in self.seen.items()
            },
            evid=self.evid,
            inside={pi: swap(ts) for pi, ts in self.inside.items()},
        )


class RegionObserver:
    """Layer consumer: attributes the layer's outer-wall points to the part meshes (KD-tree on 250k surface samples)."""

    def __init__(
        self, cfg: MixConfig, parts_dir: Path, *, margin: float, bed_tol: float
    ) -> None:
        import trimesh
        from scipy.spatial import cKDTree

        files = _part_files(cfg, parts_dir)
        self.margins = sorted({margin, LOOSE_MARGIN, DITHER_MARGIN})
        self.bed_tol = bed_tol
        self.trees: list[Any] = []
        self.thin_meshes: dict[int, Any] = {}
        self.obs: dict[int, LayerObs] = {}
        for i, (part, f) in enumerate(zip(cfg.parts, files, strict=True)):
            mesh: Any = trimesh.load(f, force="mesh", process=False)
            pts, _ = trimesh.sample.sample_surface(mesh, 250000, seed=1)
            self.trees.append(cKDTree(pts))
            if part.mix and part.accent is None and abs(mesh.volume) < THIN_VOLUME_MM3:
                # merged vertices: a closed volume for `contains`
                self.thin_meshes[i] = trimesh.load(f, force="mesh")
        self.thin_z = {
            i: (float(m.bounds[0][2]), float(m.bounds[1][2]))
            for i, m in self.thin_meshes.items()
        }

    def __call__(self, layer: Layer) -> None:
        zc = layer.mid_z()
        if zc is None:
            return
        o = LayerObs()
        self._outer_wall(layer, zc, o)
        self._thin_inside(layer, zc, o)
        if o.seen or o.inside:
            self.obs[layer.index] = o

    def _outer_wall(self, layer: Layer, zc: float, o: LayerObs) -> None:
        xys, segs, tools = [], [], []
        for run in layer.runs:
            if run.type != "Outer wall" or run.tool is None:
                continue
            seg = np.nan_to_num(np.hypot(*np.diff(run.xy, axis=0).T))
            a = run.xy[1:]
            v = ~np.isnan(a[:, 0])
            if v.any():
                xys.append(a[v])
                segs.append(seg[v])
                tools.append(np.full(int(v.sum()), run.tool))
        if not xys:
            return
        a, seg, tl = np.vstack(xys), np.concatenate(segs), np.concatenate(tools)
        a3 = np.column_stack([a, np.full(len(a), zc)])
        d = np.stack([tr.query(a3)[0] for tr in self.trees], 1)
        order = np.sort(d, 1)
        second = order[:, 1] if d.shape[1] > 1 else np.full(len(a), np.inf)
        nearest = d.argmin(1)
        for mg in self.margins:
            ok = (order[:, 0] < self.bed_tol) & (
                second - order[:, 0] > mg
            )  # unambiguous: not within mg mm of a boundary
            for pi in np.unique(nearest[ok]):
                sel = ok & (nearest == pi)
                o.seen.setdefault(mg, {}).setdefault(int(pi), set()).update(
                    int(t) for t in np.unique(tl[sel])
                )
                o.evid.setdefault(mg, {})[int(pi)] = float(seg[sel].sum())

    def _thin_inside(self, layer: Layer, zc: float, o: LayerObs) -> None:
        cand = [i for i, (lo, hi) in self.thin_z.items() if lo - 0.2 <= zc <= hi + 0.2]
        if not cand:
            return
        chunks, tools = [], []
        for run in layer.runs:
            if run.type not in ("Outer wall", "Inner wall") or run.tool is None:
                continue
            a = run.valid_xy()
            if len(a) < 2:
                continue
            ln = np.linalg.norm(np.diff(a, axis=0), axis=1)
            n = np.maximum((ln / 0.2).astype(int), 1)
            idx = np.repeat(np.arange(len(ln)), n)
            frac = (
                np.arange(int(n.sum())) - np.repeat(np.cumsum(n) - n, n)
            ) / np.repeat(n, n)
            chunks.append(a[:-1][idx] + (a[1:][idx] - a[:-1][idx]) * frac[:, None])
            tools.append(np.full(len(idx), run.tool))
        if not chunks:
            return
        xy, tl = np.vstack(chunks), np.concatenate(tools)
        for i in cand:
            lo, hi = self.thin_meshes[i].bounds
            sel = (
                (xy[:, 0] > lo[0] - 0.3)
                & (xy[:, 0] < hi[0] + 0.3)
                & (xy[:, 1] > lo[1] - 0.3)
                & (xy[:, 1] < hi[1] + 0.3)
            )
            if not sel.any():
                continue
            p3 = np.column_stack([xy[sel], np.full(int(sel.sum()), zc)])
            inn = self.thin_meshes[i].contains(p3)
            hit = tl[sel][inn]
            for t in np.unique(hit):
                if int((hit == t).sum()) >= 2:
                    o.inside.setdefault(i, set()).add(int(t))


def _part_files(cfg: MixConfig, parts_dir: Path) -> list[Path]:
    if not parts_dir.is_dir():
        raise InputNotFound(str(parts_dir), command=CMD)
    named = [p for p in cfg.parts if p.stl]
    if named and len(named) != len(cfg.parts):
        raise InvalidArgument(
            "parts[].stl",
            ", ".join(p.name for p in cfg.parts if not p.stl),
            ["`stl` on every part, or on none (then the sorted files of --parts DIR are the parts)"],
            command=CMD,
        )
    if named:
        files = [parts_dir / str(p.stl) for p in cfg.parts]
    else:
        listed = sorted(f for f in parts_dir.iterdir() if f.is_file() and not f.name.startswith("."))
        if len(listed) != len(cfg.parts):
            raise InvalidArgument(
                "--parts",
                str(parts_dir),
                [
                    f"{len(cfg.parts)} files (one per config part, sorted by name) or an `stl` file name on every part"
                ],
                command=CMD,
            )
        files = listed
    for f in files:
        if not f.is_file():
            raise InputNotFound(str(f), command=CMD)
    return files


@dataclass
class VolumeInfo:
    """A thick mixed volume as judged: its solid layers, the layers off the pattern, the pattern tool per solid layer."""

    solid: list[int]
    bad: list[int]
    expected: list[int]


@dataclass(frozen=True)
class Limits:
    margin: float = DEFAULT_MARGIN_MM
    min_evidence_mm: float = MIN_EVIDENCE_MM
    max_stripe_mm: float = MAX_STRIPE_MM


def _pattern_tool(exp: Sequence[int], layer: int, shift: int = 0) -> int:
    """Tool the pattern puts on `layer` (pattern index = layer number, wrapping), `shift` layers late."""
    return exp[(layer + shift) % len(exp)]


def _stripe_height(
    ks: list[int],
    tools_at: Mapping[int, set[int]],
    hs: Sequence[float | None],
    default: int,
) -> float:
    """Thickest run of one tool set over consecutive layers (mm); a default-tool-only run does not count."""
    run_h = cur_h = 0.0
    prev: tuple[frozenset[int], int] | None = None
    for k in ks:
        if k == 0:  # the bed layer's height is the slicer's, not the recipe's
            prev = None
            continue
        t = frozenset(tools_at[k])
        h = float(hs[k] or 0.0)
        cur_h = (
            cur_h + h if (prev is not None and prev[0] == t and prev[1] == k - 1) else h
        )
        if t != frozenset({default}):
            run_h = max(run_h, cur_h)
        prev = (t, k)
    return run_h


def evaluate_regions(
    obs: Mapping[int, LayerObs],
    scan: Scan,
    cfg: MixConfig,
    observer: RegionObserver,
    report: Report,
    *,
    limits: Limits,
    pattern_shift: int = 0,
) -> dict[str, VolumeInfo]:
    """Judge the observations of every colour volume; rows go to `report`, thick mixed volumes' evidence is returned."""
    margin = limits.margin
    hs, zs = scan.layer_h, scan.layer_z
    n_parts = len(cfg.parts)
    mids = [
        float(z) - float(h) / 2 if z is not None and h is not None else math.nan
        for z, h in zip(zs, hs, strict=True)
    ]
    mgs = sorted({margin, LOOSE_MARGIN, DITHER_MARGIN})
    seens: dict[float, list[dict[int, set[int]]]] = {
        mg: [{} for _ in range(n_parts)] for mg in mgs
    }
    evid: dict[float, list[dict[int, float]]] = {
        mg: [{} for _ in range(n_parts)] for mg in mgs
    }
    inside: dict[int, dict[int, set[int]]] = {i: {} for i in observer.thin_meshes}
    for k, o in obs.items():
        for mg, per_part in o.seen.items():
            for pi, ts in per_part.items():
                seens[mg][pi][k] = ts
        for mg, per_part_e in o.evid.items():
            for pi, mm in per_part_e.items():
                evid[mg][pi][k] = mm
        for pi, ts in o.inside.items():
            inside[pi][k] = ts
    dflt = -1 if cfg.default_tool is None else cfg.default_tool

    seen: list[dict[int, set[int]]] = []
    low_margin: list[bool] = []  # read at the small margin (hairs, fringe), not the real thin-volume branch
    solid: dict[int, set[int]] = {}
    unjudged: dict[int, list[int]] = {}
    for i, part in enumerate(cfg.parts):
        if i in observer.thin_meshes:
            lo_z, hi_z = observer.thin_z[i]
            rng = [
                k for k in range(scan.layers) if lo_z + 0.15 <= mids[k] <= hi_z - 0.15
            ]
            cov = sum(1 for k in rng if k in inside[i])
            report.add(
                f"colour volume {part.name}: extrusion inside the thin volume covers its layers",
                len(rng) > 0 and cov >= 0.7 * len(rng),
                f"{cov} of {len(rng)} layers between z {lo_z:.2f} and {hi_z:.2f} have extrusion inside (needs >= 70%)",
            )
            seen.append(inside[i])
            low_margin.append(False)
        elif part.accent is not None or (
            len(seens[margin][i]) < 20 and len(seens[LOOSE_MARGIN][i]) < 20
        ):
            seen.append(seens[DITHER_MARGIN][i])
            low_margin.append(True)
        else:
            base = margin if len(seens[margin][i]) >= 20 else LOOSE_MARGIN
            if part.mix:
                lay: dict[int, set[int]] = {}
                sol: set[int] = set()
                skip: list[int] = []
                for k in sorted(set(seens[base][i]) | set(seens[DITHER_MARGIN][i])):
                    if k not in seens[base][i]:
                        # not one outer-wall point of this layer is unambiguous at the strict margin (the loose points are the fringe
                        # that produced the iris false positives): reported as not judged, never judged on them
                        skip.append(k)
                    elif evid[base][i].get(k, 0.0) >= limits.min_evidence_mm:
                        lay[k] = seens[base][i][k]
                        sol.add(k)
                    elif evid[DITHER_MARGIN][i].get(k, 0.0) >= limits.min_evidence_mm:
                        lay[k] = seens[DITHER_MARGIN][i][k]
                    else:
                        skip.append(k)
                seen.append(lay)
                solid[i], unjudged[i] = sol, skip
            else:
                seen.append(seens[base][i])
            low_margin.append(False)

    info: dict[str, VolumeInfo] = {}
    for pi, part in enumerate(cfg.parts):
        layers = seen[pi]
        if not layers:
            report.add(
                f"colour volume {part.name}: outer wall found",
                False,
                "no outer-wall extrusion attributed to this volume",
            )
            continue
        z_of = lambda k: round(float(zs[k] or 0.0), 2)  # noqa: E731
        if part.slot_tool is not None:
            want = {part.slot_tool}
            bad = [k for k, ts in layers.items() if ts != want]
            report.add(
                f"colour volume {part.name}: pure {part.label} (T{part.slot_tool})",
                len(bad) <= 0.01 * len(layers),
                f"{len(layers)} layers with outer wall, {len(bad)} with another tool {[(z_of(k), sorted(layers[k])) for k in sorted(bad)[:6]]}",
            )
            continue
        exp = list(part.mix)
        # a layer is right when its pattern tool prints there; the default tool may print beside it (pupil slit, edge fringe), nothing else may
        bad = [
            k
            for k, ts in layers.items()
            if _pattern_tool(exp, k, pattern_shift) not in ts
            or not ts <= {_pattern_tool(exp, k, pattern_shift), dflt}
        ]
        ks = sorted(layers)
        if part.accent is not None:
            _dither_rows(report, part, layers, ks, exp, zs, hs)
            continue
        # a default-tool-only layer is no evidence of a thick band (it prints beside / under every mix): the pattern gate covers its adjacency
        same = [
            k
            for k, k2 in zip(ks, ks[1:], strict=False)
            if k2 == k + 1
            and layers[k] == layers[k2]
            and len(layers[k]) == 1
            and layers[k] != {dflt}
        ]
        run_h = _stripe_height(ks, layers, hs, dflt)
        thick = max(float(hs[k] or 0.0) for k in ks)
        if pi in solid:
            bad_solid = [k for k in bad if k in solid[pi]]
            ok_pat = not bad_solid and len(bad) <= SOLID_FRACTION_OFF * len(layers)
            info[part.name] = VolumeInfo(
                sorted(solid[pi]),
                sorted(bad),
                [_pattern_tool(exp, k, pattern_shift) for k in sorted(solid[pi])],
            )
            ev_note = (
                f"; {len(solid[pi])} solid layers (own outer wall >= {limits.min_evidence_mm} mm at the strict margin: none may be off), "
                f"{len(layers) - len(solid[pi])} thin layers re-read at margin {DITHER_MARGIN} (<= 5% off), "
                f"{len(unjudged[pi])} layers not judged (< {limits.min_evidence_mm} mm even there)"
            )
        else:
            ok_pat, ev_note = (
                len(bad)
                <= (DITHER_WRONG_THIN if low_margin[pi] else SOLID_FRACTION_OFF)
                * len(layers),
                "",
            )
        report.add(
            f"colour volume {part.name}: mix {part.label} follows the pattern",
            ok_pat,
            f"{len(layers)} layers with outer wall, {len(bad)} off the pattern at layer offset {pattern_shift % len(exp)} "
            f"{[(z_of(k), k, sorted(layers[k])) for k in sorted(bad)[:6]]}{ev_note}",
            key=f"mix:{part.name}",
        )
        report.add(
            f"colour volume {part.name}: no stripe thicker than one layer <= {limits.max_stripe_mm} mm",
            not same and run_h <= limits.max_stripe_mm + 1e-3,
            f"{len(same)} neighbouring layer pairs with the same tool; thickest same-tool run {run_h:.3f} mm (thickest layer {thick:.3f})",
        )
    return info


def _dither_rows(
    report: Report,
    part: Part,
    layers: Mapping[int, set[int]],
    ks: list[int],
    exp: list[int],
    zs: Sequence[float | None],
    hs: Sequence[float | None],
) -> None:
    """Accent (dither) volume: the accent tool on exactly the pattern's accent layers, never on neighbours, each <= MAX_STRIPE_MM thick;
    the real pattern must explain the layers clearly BETTER than the pattern shifted by one layer."""
    assert part.accent is not None
    acc = part.accent
    ks_acc = [k for k in ks if exp[k % len(exp)] == acc]
    missing = [
        k for k in ks_acc if acc not in layers[k]
    ]  # accent expected, not seen (hair tips have no unambiguous outer-wall point)
    extra = [
        k for k in ks if exp[k % len(exp)] != acc and acc in layers[k]
    ]  # accent seen where the pattern has the base colour
    wrong = missing + extra
    shifted = [k for k in ks if (acc in layers[k]) != (exp[(k + 1) % len(exp)] == acc)]
    n_base = max(len(ks) - len(ks_acc), 1)
    report.add(
        f"colour volume {part.name}: dither {part.label} puts the accent {acc + 1} on its pattern layers",
        len(ks_acc) > 0
        and len(missing) <= DITHER_MISSING * len(ks_acc)
        and len(extra) <= 0.05 * n_base,
        f"{len(layers)} layers with outer wall, {len(ks_acc)} accent layers of which {len(missing)} not seen (limit {DITHER_MISSING:.0%}), "
        f"{len(extra)} accents on base layers (limit 5% of {n_base}) {[(round(float(zs[k] or 0.0), 2), k, sorted(layers[k])) for k in wrong[:6]]}",
    )
    report.add(
        f"colour volume {part.name}: teeth: the pattern shifted by one layer is rejected",
        len(shifted) >= 1.2 * len(ks_acc) and len(wrong) * 3 <= len(shifted),
        f"{len(shifted)} of {len(layers)} layers would be wrong under a shifted pattern vs {len(wrong)} under the real one "
        f"(needs shifted >= 1.2 x the {len(ks_acc)} accent layers and real <= 1/3 of shifted)",
    )
    adj = [k for k in ks_acc if k + 1 in ks_acc]
    thick = max((float(hs[k] or 0.0) for k in ks_acc), default=0.0)
    report.add(
        f"colour volume {part.name}: accent layers single and <= {MAX_STRIPE_MM} mm",
        not adj and thick <= MAX_STRIPE_MM + 1e-3,
        f"{len(adj)} neighbouring accent layer pairs, thickest accent layer {thick:.3f} mm",
    )


# ---------------------------------------------------------------- pause, tools, layers, settings


def gate_pause(scan: Scan, report: Report, spec: PauseSpec | None) -> None:
    """Exactly one M600, in the layer that starts after the pocket (Orca writes it after the new layer's `;Z:` and before its first move)."""
    if spec is None:
        report.add(
            "pause: none requested",
            not scan.pauses,
            f"{len(scan.pauses)} M600 in the G-code",
        )
        return
    ok = len(scan.pauses) == 1
    detail = f"{len(scan.pauses)} M600"
    if ok:
        p = scan.pauses[0]
        z = scan.layer_z[p.layer] if 0 <= p.layer < scan.layers else None
        prev = scan.layer_z[p.layer - 1] if 1 <= p.layer < scan.layers else None
        ok = (
            z is not None
            and prev is not None
            and abs(z - spec.print_z) < 2e-3
            and abs(prev - spec.previous_top) < 2e-3
            and p.runs_before == 0
        )
        detail = (
            f"M600 at line {p.line} in the layer with top z {z} (expected {spec.print_z:.4f}), before any extrusion of it "
            f"({p.runs_before} extrusion runs so far), previous layer top z {prev} (expected {spec.previous_top:.4f})"
        )
    report.add("pause: M600 at the configured layer", ok, detail)


def gate_tools(scan: Scan, report: Report, cfg: MixConfig) -> None:
    """Heads that used filament, prime tower, tool-change stats."""
    if cfg.heads is None:
        report.skip("tools: heads used == assigned", "no heads_slots in the config")
        return
    used = {i for i, v in enumerate(scan.filament_g) if v > 0}
    stats = scan.stats()
    report.add(
        "tools: heads used == assigned",
        used == set(cfg.heads),
        f"filament used [g] per head {scan.filament_g}, expected heads T{sorted(cfg.heads)}, tool changes {scan.tool_changes} "
        f"({stats['changes_per_layer']} per layer), time {scan.time}",
    )
    tower = sorted(k for k in scan.types if "ower" in k)
    want = cfg.prime_tower if cfg.prime_tower is not None else len(cfg.heads) > 1
    report.add(
        "prime tower: printed iff expected",
        bool(tower) == want,
        f"TYPE blocks {tower or 'none'}; expected {'printed' if want else 'absent'}",
    )


def gate_layers(
    scan: Scan, report: Report, hmin: float, hmax: float, max_step: float
) -> None:
    """Layer heights inside the window (first layer exempt), Z steps == ;HEIGHT, neighbours differ by <= max_step."""
    if scan.layers < 2 or any(
        z is None or h is None for z, h in zip(scan.layer_z, scan.layer_h, strict=True)
    ):
        report.add(
            "layers: Z and HEIGHT known",
            False,
            f"{scan.layers} layers, some without `;Z:` / `;HEIGHT:`",
        )
        return
    zs = np.array(scan.layer_z, dtype=float)
    hs = np.array(scan.layer_h, dtype=float)
    body = hs[1:]
    dz = np.diff(np.concatenate([[0.0], zs]))
    report.add(
        f"layers: within {hmin}..{hmax}",
        float(body.min()) >= hmin - 1e-3 and float(body.max()) <= hmax + 1e-3,
        f"{len(hs)} layers, min {body.min():.4f} max {body.max():.4f}, first layer {hs[0]:.3f}",
    )
    report.add(
        "layers: Z steps == ;HEIGHT",
        float(np.abs(dz - hs).max()) < 0.002,
        f"max |dZ - HEIGHT| = {float(np.abs(dz - hs).max()):.5f} mm",
    )
    steps = np.abs(np.diff(body))
    report.add(
        f"layers: neighbours differ <= {max_step}",
        float(steps.max(initial=0.0)) <= max_step + 1e-3,
        f"max neighbour step {float(steps.max(initial=0.0)):.4f} mm",
    )


def gate_settings(scan: Scan, report: Report, expected: Mapping[str, str]) -> None:
    """The slicer's own record of the settings it used (`; key = value` footer of the G-code)."""
    for key, want in expected.items():
        got = scan.header.get(key)
        report.add(
            f"settings: {key}",
            got == want,
            f"G-code header {key} = {got}, expected {want}",
        )


# ---------------------------------------------------------------- cavity


@dataclass(frozen=True)
class Cavity:
    """A pocket in the print: rounded-rectangle plan (object coordinates), vertical walls from `floor_z` to `pause_z`,
    then a roof that closes it at `roof_ridge_z`. `shift` places the object on the bed."""

    x: tuple[float, float]
    y: tuple[float, float]
    corner_r: float
    floor_z: float
    pause_z: float
    roof_ridge_z: float

    def moved(self, dx: float) -> Cavity:
        return replace(self, x=(self.x[0] + dx, self.x[1] + dx))


def load_cavity(path: Path) -> Cavity:
    if not path.is_file():
        raise InputNotFound(str(path), command=CMD)
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return Cavity(
            (float(d["plan_mm"]["x"][0]), float(d["plan_mm"]["x"][1])),
            (float(d["plan_mm"]["y"][0]), float(d["plan_mm"]["y"][1])),
            float(d["plan_mm"]["corner_r"]),
            float(d["floor_z"]),
            float(d["pause_z"]),
            float(d["roof_ridge_z"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _bad(
            "--cavity",
            f"{path}: {exc!r}",
            ["JSON with plan_mm{x,y,corner_r}, floor_z, pause_z, roof_ridge_z"],
        ) from exc


def densify(a: npt.NDArray[np.float64], step: float = 1.0) -> npt.NDArray[np.float64]:
    """Points every <= step mm along a polyline (extrusion lines have only their end points in the G-code)."""
    if len(a) < 2:
        return a
    seg = np.hypot(*np.diff(a, axis=0).T)
    n = np.maximum(1, np.ceil(seg / step).astype(int))
    t = np.concatenate([np.linspace(0, 1, k, endpoint=False) for k in n])
    i = np.repeat(np.arange(len(seg)), n)
    return np.vstack([a[:-1][i] + (a[1:][i] - a[:-1][i]) * t[:, None], a[-1:]])


class CavityObserver:
    """Layer consumer: per layer, extrusion points inside the shrunk pocket outline and in the strip under the ridge."""

    def __init__(self, cav: Cavity, shift: tuple[float, float, float]) -> None:
        self.cav, self.shift = cav, shift
        (x0, x1), (y0, y1) = cav.x, cav.y
        self.cx, self.cy = (x0 + x1) / 2 + shift[0], (y0 + y1) / 2 + shift[1]
        self.hx, self.hy = (x1 - x0) / 2, (y1 - y0) / 2
        self.strip_hx = max(self.hx - RIDGE_STRIP_X_MARGIN_MM, self.hx / 2)  # a narrow pocket keeps half its width
        self.rows: list[
            tuple[float, float, int, int]
        ] = []  # (z top, z mid, hits inside, hits in the ridge strip)

    def _inside(
        self, a: npt.NDArray[np.float64]
    ) -> npt.NDArray[np.bool_]:  # rounded rectangle, shrunk by CAVITY_WALL_MARGIN_MM
        r = self.cav.corner_r
        q = np.abs(a - [self.cx, self.cy]) - [self.hx - r, self.hy - r]
        d = (
            np.hypot(np.maximum(q[:, 0], 0), np.maximum(q[:, 1], 0))
            + np.minimum(np.maximum(q[:, 0], q[:, 1]), 0)
            - r
        )
        return d < -CAVITY_WALL_MARGIN_MM

    def __call__(self, layer: Layer) -> None:
        zc = layer.mid_z()
        if zc is None or layer.z is None:
            return
        hit = hit_c = 0
        for run in layer.runs:
            a = run.valid_xy()
            if len(a):
                a = densify(a)
                hit += int(self._inside(a).sum())
                hit_c += int(
                    (
                        (np.abs(a[:, 0] - self.cx) < self.strip_hx)
                        & (np.abs(a[:, 1] - self.cy) < RIDGE_STRIP_HALF_Y_MM)
                    ).sum()
                )
        self.rows.append((layer.z, zc, hit, hit_c))

    def evaluate(self, report: Report) -> None:
        """No extrusion inside the pocket between floor and pause; open under the roof up to the ridge, closed above it."""
        sh = self.shift
        zf, zp, ridge = (
            self.cav.floor_z + sh[2],
            self.cav.pause_z + sh[2],
            self.cav.roof_ridge_z + sh[2],
        )
        bad: list[tuple[float, int]] = []
        n_open = 0
        centre: dict[float, int] = {}
        for z_top, zc, hit, hit_c in self.rows:
            if zf + 0.3 < zc < zp - 0.05:
                n_open += 1
                if hit:
                    bad.append((round(z_top, 2), hit))
            centre[round(zc, 2)] = hit_c
        report.add(
            "cavity: void stays empty between floor and pause",
            n_open > MIN_CAVITY_LAYERS and not bad,
            f"{n_open} layers inspected (z {zf + 0.3:.1f}..{zp:.1f}), {len(bad)} with extrusion inside the pocket outline {bad[:4]}",
            key="cavity.void",
        )
        open_c = [z for z, h in centre.items() if zp < z < ridge - 0.4 and h > 0]
        shut_c = [h for z, h in centre.items() if ridge + 0.4 <= z <= ridge + 1.6]
        report.add(
            "cavity: pocket stays open under the roof up to the ridge, then closes (no support)",
            not open_c and bool(shut_c) and min(shut_c) > 0,
            f"central strip |x|<{self.strip_hx:.0f}, |y-y0|<{RIDGE_STRIP_HALF_Y_MM:.0f}: {len(open_c)} layers with extrusion between the pause "
            f"(z {zp:.2f}) and the ridge (z {ridge:.1f}) {open_c[:4]}; layers z {ridge + 0.4:.1f}..{ridge + 1.6:.1f} above the ridge have extrusion there: {shut_c}",
        )


# ---------------------------------------------------------------- driver


@dataclass
class TeethControl:
    """One control: a deliberately wrong print the gate must reject (or, for the real print, accept)."""

    label: str
    expect: str  # PASS | FAIL
    got: str
    detail: str

    @property
    def caught(self) -> bool:
        return self.got == self.expect


@dataclass
class GateResult:
    report: Report
    scan: Scan
    teeth: list[TeethControl] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.report.ok and all(t.caught for t in self.teeth)

    @property
    def skipped(self) -> list[str]:
        return [r.name for r in self.report.rows if r.status == "SKIP"]

    def to_json(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "skipped": self.skipped,
            "gates": [
                {"name": r.name, "status": r.status, "detail": r.detail}
                for r in self.report.rows
            ],
            "teeth": [
                {
                    "label": t.label,
                    "expect": t.expect,
                    "got": t.got,
                    "caught": t.caught,
                    "detail": t.detail,
                }
                for t in self.teeth
            ],
            "stats": self.scan.stats(),
        }

    def render(self) -> str:
        lines = [self.report.render()]
        lines.extend(
            f"[teeth {t.label}] expecting {t.expect}: {t.got} - {t.detail[:300]} -> control {'CAUGHT' if t.caught else 'NOT CAUGHT'}"
            for t in self.teeth
        )
        if self.teeth and not all(t.caught for t in self.teeth):
            lines.append(
                "TEETH FAILED: a control the gate did not reject means the gate is vacuous, its PASS proves nothing"
            )
        s = self.scan.stats()
        lines.append(
            f"  material: {s['grams_by_tool']} (slicer footer {s['slicer_footer_g']}), prime tower {s['prime_tower_g']} g, {s['tool_changes']} tool changes"
        )
        skipped = self.skipped
        note = f" (not verified, no input: {', '.join(skipped)})" if skipped else ""
        lines.append(f"OVERALL {'PASS' if self.ok else 'FAIL'}{note}")
        return "\n".join(lines)


@dataclass(frozen=True)
class GateOptions:
    parts_dir: Path | None = None
    cavity: Path | None = None
    layer_heights: tuple[float, float] | None = None
    max_layer_step: float = 0.08
    limits: Limits = Limits()
    bed_tol: float = DEFAULT_BED_TOL_MM
    mix_teeth: bool = False
    teeth_volumes: tuple[str, ...] = ()
    cavity_teeth: bool = False


def run_gates(gcode: Path, cfg: MixConfig, opt: GateOptions) -> GateResult:
    """One streaming pass over `gcode`, then every configured gate and the requested teeth."""
    if not gcode.is_file():
        raise InputNotFound(str(gcode), command=CMD)
    parts_dir = opt.parts_dir or (Path(cfg.parts_dir) if cfg.parts_dir else None)
    if opt.mix_teeth and not (parts_dir and cfg.parts):
        raise InvalidArgument(
            "--mix-teeth",
            "no colour volumes",
            ["a config with `parts` and --parts DIR"],
            command=CMD,
        )
    if opt.cavity_teeth and opt.cavity is None:
        raise InvalidArgument(
            "--cavity-teeth", "no --cavity", ["--cavity cavity.json"], command=CMD
        )
    declared = bool(parts_dir and cfg.parts) or any(
        (opt.cavity, cfg.heads is not None, cfg.settings, opt.layer_heights, cfg.pause)
    )
    if not declared:
        raise InvalidArgument(
            "--config",
            "no checks declared",
            [
                "`parts` with --parts DIR (colour volumes)",
                "`heads_slots`",
                "`pause`",
                "`expect_settings`",
                "--cavity FILE",
                "--layer-heights MIN:MAX",
            ],
            command=CMD,
        )
    regions = (
        RegionObserver(cfg, parts_dir, margin=opt.limits.margin, bed_tol=opt.bed_tol)
        if parts_dir and cfg.parts
        else None
    )
    cavity = load_cavity(opt.cavity) if opt.cavity else None
    cav_obs = CavityObserver(cavity, cfg.shift) if cavity else None
    cav_moved = (
        CavityObserver(
            cavity.moved(CAVITY_TEETH_FRACTION * (cavity.x[1] - cavity.x[0])), cfg.shift
        )
        if cavity and opt.cavity_teeth
        else None
    )
    consumers = [c for c in (regions, cav_obs, cav_moved) if c is not None]
    scan = scan_gcode(gcode, consumers)
    if scan.layers == 0:
        raise InvalidArgument(
            "G-code",
            str(gcode),
            ["a sliced G-code with `;LAYER_CHANGE` markers"],
            command=CMD,
        )

    report = Report()
    info: dict[str, VolumeInfo] = {}
    if opt.layer_heights:
        gate_layers(
            scan, report, opt.layer_heights[0], opt.layer_heights[1], opt.max_layer_step
        )
    if cfg.settings:
        gate_settings(scan, report, cfg.settings)
    gate_pause(scan, report, cfg.pause)
    if cav_obs:
        cav_obs.evaluate(report)
    else:
        report.skip("cavity", "no --cavity")
    if regions:
        info = evaluate_regions(
            regions.obs, scan, cfg, regions, report, limits=opt.limits
        )
    else:
        report.skip("colour volumes", "no `parts` in the config or no --parts DIR")
    gate_tools(scan, report, cfg)
    result = GateResult(report, scan)
    if opt.mix_teeth and regions:
        result.teeth.extend(mix_teeth(regions, scan, cfg, info, opt))
    if opt.cavity_teeth and cav_moved:
        result.teeth.extend(cavity_teeth(cav_moved, scan, cfg))
    return result


def _pattern_row(report: Report, name: str) -> Row | None:
    return next((r for r in report.rows if r.key == f"mix:{name}"), None)


def mix_teeth(
    regions: RegionObserver,
    scan: Scan,
    cfg: MixConfig,
    info: Mapping[str, VolumeInfo],
    opt: GateOptions,
) -> list[TeethControl]:
    """Controls for the colour-volume pattern gate on the real print, for every thick mixed volume with solid layers:
    the real print passes; the pattern shifted by one layer must FAIL; one solid layer printed with a wrong tool must FAIL
    (1 layer of ~30 is inside the 5 % tolerance of thin layers, so only the solid-layer rule can catch it)."""
    names = [n for n in info if not opt.teeth_volumes or n in opt.teeth_volumes]
    out: list[TeethControl] = []
    missing = [n for n in opt.teeth_volumes if n not in info]
    out.extend(
        TeethControl(
            n,
            "FAIL",
            "n/a",
            "no thick mixed volume of this name with solid layers: the controls are impossible",
        )
        for n in missing
    )
    if not names and not missing:
        out.append(
            TeethControl(
                "mix volumes",
                "FAIL",
                "n/a",
                "no thick mixed volume with solid layers in this print: the controls are impossible, the gate cannot prove itself",
            )
        )
    dflt = -1 if cfg.default_tool is None else cfg.default_tool
    heads = set(cfg.heads or ()) | cfg.referenced_tools()

    def verdict(
        label: str, name: str, expect: str, obs: Mapping[int, LayerObs], shift: int = 0
    ) -> None:
        rep = Report()
        evaluate_regions(
            obs, scan, cfg, regions, rep, limits=opt.limits, pattern_shift=shift
        )
        row = _pattern_row(rep, name)
        out.append(
            TeethControl(
                label,
                expect,
                "n/a" if row is None else row.status,
                "no pattern row" if row is None else row.detail,
            )
        )

    for name in names:
        verdict(f"{name} real print passes", name, "PASS", regions.obs)
        verdict(
            f"{name} pattern shifted by one layer", name, "FAIL", regions.obs, shift=1
        )
        vol = info[name]
        for tool_exp in sorted(set(vol.expected) - {dflt}):
            ks = [
                k for k, e in zip(vol.solid, vol.expected, strict=True) if e == tool_exp
            ]
            k = ks[len(ks) // 2]
            others = sorted(heads - {tool_exp, dflt})
            bad_t = others[0] if others else max(heads) + 1
            obs2 = dict(regions.obs)
            obs2[k] = regions.obs[k].relabel(tool_exp, bad_t)
            z = scan.layer_z[k]
            verdict(
                f"{name} layer {k} (z {float(z or 0):.2f}, solid) expected T{tool_exp} printed with T{bad_t}",
                name,
                "FAIL",
                obs2,
            )
        if not (set(vol.expected) - {dflt}):
            out.append(
                TeethControl(
                    f"{name} wrong tool",
                    "FAIL",
                    "n/a",
                    "no solid layer expects a non-default tool: control impossible",
                )
            )
    return out


def cavity_teeth(moved: CavityObserver, scan: Scan, cfg: MixConfig) -> list[TeethControl]:
    """Controls for the cavity and pause gates on the real print: a pocket outline moved over solid body must FAIL the void gate;
    a pause Z that is PAUSE_TEETH_OFFSET_MM too high must FAIL the pause gate."""
    out: list[TeethControl] = []
    rep = Report()
    moved.evaluate(rep)
    void = next(r for r in rep.rows if r.key == "cavity.void")
    out.append(TeethControl("cavity moved over solid body", "FAIL", void.status, void.detail))
    if cfg.pause is None:
        out.append(
            TeethControl(
                "pause too high",
                "FAIL",
                "n/a",
                "no `pause` in the config: control impossible",
            )
        )
        return out
    bad = replace(
        cfg.pause,
        print_z=cfg.pause.print_z + PAUSE_TEETH_OFFSET_MM,
        previous_top=cfg.pause.previous_top + PAUSE_TEETH_OFFSET_MM,
    )
    rep = Report()
    gate_pause(scan, rep, bad)
    out.append(
        TeethControl(
            f"pause {PAUSE_TEETH_OFFSET_MM:g} mm too high",
            "FAIL",
            rep.rows[0].status,
            rep.rows[0].detail,
        )
    )
    return out


# ---------------------------------------------------------------- command line

_VALUE_OPTS = (
    "--config",
    "--parts",
    "--default-slot",
    "--cavity",
    "--layer-heights",
    "--max-layer-step",
    "--margin",
    "--bed-tol",
    "--min-evidence",
    "--max-stripe",
    "--teeth-volume",
)
_FLAGS = ("--mix-teeth", "--cavity-teeth", "--json")


def _number(opts: dict[str, list[str]], flag: str, default: float) -> float:
    if flag not in opts:
        return default
    try:
        value = float(opts[flag][-1])
    except ValueError:
        value = -1.0
    if not (math.isfinite(value) and value > 0):
        raise InvalidArgument(
            flag, opts[flag][-1], ["a positive number of mm"], command=CMD
        )
    return value


def _run(argv: list[str]) -> int:
    pos: list[str] = []
    opts: dict[str, list[str]] = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in _VALUE_OPTS:
            if i + 1 >= len(argv):
                raise UsageError(f"{a} needs a value", command=CMD)
            opts.setdefault(a, []).append(argv[i + 1])
            i += 2
            continue
        if a in _FLAGS:
            opts.setdefault(a, [])
        elif a.startswith("--"):
            raise UsageError(
                f"unknown option {a}",
                command=CMD,
                remediation=[f"See `3d {CMD} --help`."],
            )
        else:
            pos.append(a)
        i += 1
    if len(pos) != 1:
        raise UsageError(f"expected one G-code file, got {len(pos)}", command=CMD)
    if "--config" not in opts:
        raise UsageError(
            "--config GATES.json is required",
            command=CMD,
            remediation=[f"See `3d {CMD} --help` for the config."],
        )

    slot: int | None = None
    if "--default-slot" in opts:
        try:
            slot = int(opts["--default-slot"][-1])
        except ValueError:
            slot = 0
        if slot < 1:
            raise InvalidArgument(
                "--default-slot",
                opts["--default-slot"][-1],
                ["1-based slot number"],
                command=CMD,
            )
    heights: tuple[float, float] | None = None
    if "--layer-heights" in opts:
        lo, _, hi = opts["--layer-heights"][-1].partition(":")
        try:
            heights = (float(lo), float(hi))
        except ValueError:
            heights = None
        if heights is None or not 0 < heights[0] <= heights[1]:
            raise InvalidArgument(
                "--layer-heights",
                opts["--layer-heights"][-1],
                ["MIN:MAX in mm, e.g. 0.08:0.32"],
                command=CMD,
            )
    cfg = load_config(Path(opts["--config"][-1]), default_slot=slot)
    options = GateOptions(
        parts_dir=Path(opts["--parts"][-1]) if "--parts" in opts else None,
        cavity=Path(opts["--cavity"][-1]) if "--cavity" in opts else None,
        layer_heights=heights,
        max_layer_step=_number(opts, "--max-layer-step", 0.08),
        limits=Limits(
            margin=_number(opts, "--margin", DEFAULT_MARGIN_MM),
            min_evidence_mm=_number(opts, "--min-evidence", MIN_EVIDENCE_MM),
            max_stripe_mm=_number(opts, "--max-stripe", MAX_STRIPE_MM),
        ),
        bed_tol=_number(opts, "--bed-tol", DEFAULT_BED_TOL_MM),
        mix_teeth="--mix-teeth" in opts,
        teeth_volumes=tuple(opts.get("--teeth-volume", [])),
        cavity_teeth="--cavity-teeth" in opts,
    )
    result = run_gates(Path(pos[0]), cfg, options)
    print(
        json.dumps(result.to_json(), indent=1) if "--json" in opts else result.render()
    )
    return 0 if result.ok else 1


def main(argv: list[str]) -> int:
    """Entry of `3d gcode-mix-gate` (cli.pyrun runs this file); structured errors render like the dispatcher's."""
    try:
        return _run(argv)
    except ThreeDError as exc:
        print(exc.render(), file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
