"""pipeline.py — the `3d prep` flow: model -> repaired -> oriented -> adapted -> checked -> coloured -> sliced -> gated -> proof.

WHAT: runs the prep steps in order on one STL/3MF, each step cached on disk under the work dir
  (`prep.cache`), so a re-run, or a change to colours / fit items / process settings, recomputes only the steps
  that depend on the change. Every step is a thin adapter over a library module (`prep.meshfix`,
  `prep.orient`, `prep.adapt`, `prep.overhang`, `prep.stability`, `prep.fit`, `prep.regions`, `prep.layers`,
  `prep.project3mf`, `slicing.orca_cli`, `prep.gcode_gates`) and records a `StepRecord` (verdict, one line,
  seconds, cached?). The proof pack (`prep.proof`) is the last step.
WHY: the Chekhov session spent two hours writing and re-running ~27 one-off scripts for these steps in
  sequence, each from scratch (docs/research/prep-session-ledger-2026-10-20.md).
INVARIANTS:
  * Step order and dependencies are `STEPS` / `DEPS`; a step's cache key hashes its parameters and the keys of
    the steps it consumes, nothing else.
  * A FAIL in a mesh stage (`repair`, `orient`, `adapt`) blocks the later steps (they would run on a broken
    mesh); other FAILs are reported and the flow continues, so one run shows every problem.
  * Steps whose tool is missing (no Orca install, no colour spec, no fit table) are SKIP with the reason,
    never a silent PASS.
  * Meshes between steps travel as `.npz` (exact float64, loads in milliseconds); the deliverables are
    `NAME.print.stl`, `NAME.3mf`, `NAME.gcode`, `proof-report.md` + `proof/`, `prep.json`.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import trimesh

from errors import InputNotFound, InvalidArgument, MissingDependency, ThreeDError
from prep import frame
from prep.cache import StepCache, make_key
from prep.contracts import Assign, Filament, Part, StepResult, Verdict, worst

STEPS: tuple[str, ...] = (
    "diagnose",
    "repair",
    "orient",
    "adapt",
    "overhang",
    "fit",
    "stability",
    "colors",
    "layers",
    "project",
    "slice",
    "gates",
    "proof",
)
DEPS: dict[str, tuple[str, ...]] = {
    "diagnose": (),
    "repair": (),
    "orient": ("repair",),
    "adapt": ("orient",),
    "overhang": ("adapt",),
    "fit": ("adapt",),
    "stability": ("adapt", "fit"),
    "colors": ("adapt",),
    "layers": ("colors", "adapt"),
    "project": ("colors", "layers"),
    "slice": ("project",),
    "gates": ("slice", "project"),
    "proof": (
        "diagnose",
        "repair",
        "orient",
        "adapt",
        "overhang",
        "stability",
        "fit",
        "colors",
        "layers",
        "project",
        "slice",
        "gates",
    ),
}
MESH_STEPS: tuple[str, ...] = ("repair", "orient", "adapt")
#: a FAIL here stops the flow: later steps need a sound mesh (a bad orientation FAIL is only a finding: adapt fixes it)
BLOCKING = frozenset({"repair", "adapt"})
PRINTER_BED_MM: dict[str, tuple[float, float, float]] = {
    "u1": (270.0, 270.0, 270.0),
    "k1se": (220.0, 220.0, 250.0),
}
DEFAULT_PROCESS_PRESET = "0.20mm Standard @Snapmaker U1 (0.4 nozzle)"
#: printers sliced from the STL with three flat presets (`prep.flat_profiles`); the others go through the 3MF project
FLAT_PROFILE_PRINTERS = frozenset({"k1se"})


@dataclass
class PrepConfig:
    """Everything a run needs; filled from CLI flags and an optional spec file (`prep.cli`)."""

    model: Path
    out: Path
    printer: str = "u1"
    orient: str = "auto"  # keep | auto
    angle_deg: float = 45.0
    pitch: float = 0.25
    adapt: bool = True
    #: relaxed-angle height ranges for the adaptation, `overhang.Zone` dicts {z_min, z_max, angle_deg, columns}
    adapt_zones: list[dict[str, Any]] = field(default_factory=list)
    #: bridges up to this span (mm) are a WARN, longer ones a FAIL
    bridge_max_mm: float = 10.0
    items: Path | None = None
    holder: dict[str, Any] = field(default_factory=dict)
    loads: list[dict[str, Any]] = field(default_factory=list)
    stability: dict[str, Any] = field(default_factory=dict)
    colors: Path | None = None
    #: None: the printer's own default (U1: `DEFAULT_PROCESS_PRESET`, K1 SE: the fleet process preset)
    process_preset: str | None = None
    filament_preset: str | None = None
    walls: int = 4
    infill_pct: int = 15
    hmin: float = 0.08
    hmax: float = 0.32
    mix_cap: float = 0.12
    brim_mm: float = 0.0
    #: `none` | `auto` (K1 SE only: easy tear-off supports)
    supports: str = "none"
    #: `--set` / `--fset` / `--max-volumetric` and the overlay file: slicer settings over the preset (`flat_profiles`)
    set_process: dict[str, str] = field(default_factory=dict)
    set_filament: dict[str, str] = field(default_factory=dict)
    overlay: Path | None = None
    gate: dict[str, Any] = field(default_factory=dict)
    only: frozenset[str] = frozenset()
    skip: frozenset[str] = frozenset()
    from_step: str | None = None
    force: bool = False
    slice: bool = True

    def validate(self) -> None:
        for path in (self.model, self.items, self.colors, self.overlay):
            if path is not None and not path.is_file():
                raise InputNotFound(str(path), command="prep")
        if self.printer not in PRINTER_BED_MM:
            raise InvalidArgument(
                "--printer", self.printer, sorted(PRINTER_BED_MM), command="prep"
            )
        if self.supports not in ("none", "auto"):
            raise InvalidArgument(
                "--supports", self.supports, ["none", "auto"], command="prep"
            )
        if self.flat_profile:
            if self.colors is not None:
                raise InvalidArgument(
                    "--colors",
                    str(self.colors),
                    [f"no colour spec: the {self.printer} prints one filament"],
                    command="prep",
                )
        else:
            if self.supports != "none":
                raise InvalidArgument(
                    "--supports",
                    self.supports,
                    ["none (the U1 flow adapts the model to need no supports)"],
                    command="prep",
                )
            if self.set_filament or self.overlay:
                raise InvalidArgument(
                    "--fset/--max-volumetric/--overlay",
                    self.printer,
                    [f"--printer {sorted(FLAT_PROFILE_PRINTERS)[0]}"],
                    command="prep",
                )
        if self.orient not in ("keep", "auto"):
            raise InvalidArgument(
                "--orient", self.orient, ["keep", "auto"], command="prep"
            )
        for name in (
            *self.only,
            *self.skip,
            *([self.from_step] if self.from_step else []),
        ):
            if name not in STEPS:
                raise InvalidArgument("step", name, list(STEPS), command="prep")
        if not 0 < self.angle_deg < 90:
            raise InvalidArgument(
                "--angle", str(self.angle_deg), ["0 < degrees < 90"], command="prep"
            )
        if not 0.05 <= self.pitch <= 1.0:
            raise InvalidArgument(
                "--pitch", str(self.pitch), ["0.05 .. 1.0 mm"], command="prep"
            )

    @property
    def flat_profile(self) -> bool:
        """True when the printer is sliced from an STL with flat presets instead of a 3MF project."""
        return self.printer in FLAT_PROFILE_PRINTERS

    @property
    def process_name(self) -> str | None:
        """The process preset the user asked for, else the U1 default; None = the flat-profile printer's own."""
        if self.process_preset is not None:
            return self.process_preset
        return None if self.flat_profile else DEFAULT_PROCESS_PRESET

    @property
    def name(self) -> str:
        """Safe file stem of the model."""
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", self.model.stem).strip("_") or "model"


@dataclass
class StepRecord:
    """What one step did in this run."""

    name: str
    verdict: Verdict
    summary: str
    seconds: float
    cached: bool
    result: dict[str, Any]
    artifacts: list[str] = field(default_factory=list)
    original_seconds: float | None = None  # what the cached result originally cost

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "verdict": self.verdict,
            "summary": self.summary,
            "seconds": round(self.seconds, 2),
            "cached": self.cached,
            "original_seconds": None
            if self.original_seconds is None
            else round(self.original_seconds, 2),
            "artifacts": self.artifacts,
            "result": self.result,
        }


@dataclass
class StepOutput:
    """Return value of a step function."""

    result: dict[str, Any]
    verdict: Verdict
    summary: str
    artifacts: list[Path] = field(default_factory=list)


@dataclass
class PrepOutcome:
    """The whole run."""

    config: PrepConfig
    records: list[StepRecord]
    wall_seconds: float
    verdict: Verdict

    def record(self, name: str) -> StepRecord | None:
        return next((r for r in self.records if r.name == name), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": str(self.config.model),
            "printer": self.config.printer,
            "verdict": self.verdict,
            "wall_seconds": round(self.wall_seconds, 2),
            "steps": [r.to_dict() for r in self.records],
        }

    def table(self) -> str:
        """Fixed-width step table for the console."""
        rows = ["step       verdict  seconds  cache  summary", "-" * 78]
        for r in self.records:
            cache = "hit" if r.cached else "-"
            rows.append(
                f"{r.name:<10} {r.verdict:<7} {r.seconds:>7.1f}  {cache:<5}  {r.summary}"
            )
        rows.append("-" * 78)
        rows.append(f"total {self.wall_seconds:.1f} s, overall {self.verdict}")
        return "\n".join(rows)


def save_mesh(path: Path, mesh: trimesh.Trimesh) -> None:
    np.savez(
        path,
        vertices=np.asarray(mesh.vertices, dtype=np.float64),
        faces=np.asarray(mesh.faces, dtype=np.int64),
    )


def load_mesh_npz(path: Path) -> trimesh.Trimesh:
    with np.load(path) as data:
        return trimesh.Trimesh(data["vertices"], data["faces"], process=False)


def save_parts(path: Path, parts: list[Part]) -> None:
    """Colour parts as one npz: `<i>_v`, `<i>_f` arrays plus a JSON index of names and resolved assigns."""
    arrays: dict[str, Any] = {}
    index = []
    for i, p in enumerate(parts):
        arrays[f"{i}_v"] = np.asarray(p.mesh.vertices, dtype=np.float64)
        arrays[f"{i}_f"] = np.asarray(p.mesh.faces, dtype=np.int64)
        index.append(
            {
                "name": p.name,
                "slot": p.assign.slot,
                "key": p.assign.key,
                "mix": p.assign.mix,
            }
        )
    arrays["index"] = np.array(json.dumps(index))
    np.savez(path, **arrays)


def load_parts(path: Path) -> list[Part]:
    with np.load(path) as data:
        index = json.loads(str(data["index"]))
        return [
            Part(
                e["name"],
                trimesh.Trimesh(data[f"{i}_v"], data[f"{i}_f"], process=False),
                Assign(slot=e["slot"], key=e["key"], mix=e["mix"]),
            )
            for i, e in enumerate(index)
        ]


class Pipeline:
    """Runs `STEPS` with caching; one instance per run."""

    def __init__(self, cfg: PrepConfig, log: Callable[[str], None] = print) -> None:
        cfg.validate()
        self.cfg = cfg
        self.log = log
        self.work = cfg.out
        self.work.mkdir(parents=True, exist_ok=True)
        (self.work / "meshes").mkdir(exist_ok=True)
        self.cache = StepCache(self.work)
        self.records: dict[str, StepRecord] = {}
        self.keys: dict[str, str] = {}
        self._model: trimesh.Trimesh | None = None
        self._mesh_files: dict[str, Path] = {}
        self._memo: dict[str, Any] = {}
        self._model_digest = frame.file_digest(cfg.model)
        self.blocked: str | None = None

    # ------------------------------------------------------------------ meshes
    def model_mesh(self) -> trimesh.Trimesh:
        """The input in the print frame (loaded once)."""
        if self._model is None:
            self._model = frame.to_print_frame(frame.load_mesh(self.cfg.model))
        return self._model

    def mesh_after(self, step: str) -> trimesh.Trimesh:
        """The mesh as it leaves `step`: the newest mesh-stage artifact at or before it, else the input."""
        order = list(MESH_STEPS)
        upto = order.index(step) if step in order else len(order) - 1
        for name in reversed(order[: upto + 1]):
            path = self._mesh_files.get(name)
            if path is not None:
                return load_mesh_npz(path)
        return self.model_mesh()

    def final_mesh(self) -> trimesh.Trimesh:
        return self.mesh_after("adapt")

    def memo(self, key: str, make: Callable[[], Any]) -> Any:
        """Per-run memo for results two steps both need (e.g. the overhang analysis of an unchanged mesh)."""
        if key not in self._memo:
            self._memo[key] = make()
        return self._memo[key]

    # ------------------------------------------------------------------ driving
    def selected(self) -> list[str]:
        """Steps to run: `--only` plus everything they need, minus `--skip`."""
        cfg = self.cfg
        if cfg.only:
            wanted: set[str] = set()
            stack = list(cfg.only)
            while stack:
                s = stack.pop()
                if s not in wanted:
                    wanted.add(s)
                    stack.extend(DEPS[s])
            return [s for s in STEPS if s in wanted and s not in cfg.skip]
        return [s for s in STEPS if s not in cfg.skip]

    def params(self, step: str) -> dict[str, Any]:
        """The parameters that, together with upstream keys, determine a step's result."""
        c = self.cfg
        spec_digest = (
            frame.file_digest(c.colors) if c.colors and c.colors.is_file() else None
        )
        items_digest = (
            frame.file_digest(c.items) if c.items and c.items.is_file() else None
        )
        table: dict[str, dict[str, Any]] = {
            "diagnose": {"model": self._model_digest},
            "repair": {"model": self._model_digest},
            "orient": {"mode": c.orient, "angle": c.angle_deg},
            "adapt": {
                "on": c.adapt,
                "angle": c.angle_deg,
                "pitch": c.pitch,
                "zones": c.adapt_zones,
                "bridge": c.bridge_max_mm,
            },
            "overhang": {
                "angle": c.angle_deg,
                "pitch": c.pitch,
                "zones": c.adapt_zones,
                "bridge": c.bridge_max_mm,
            },
            "stability": {
                "loads": c.loads,
                "opts": c.stability,
                "infill": c.infill_pct,
                "walls": c.walls,
            },
            "fit": {
                "items": items_digest,
                "holder": c.holder,
                "pitch": max(c.pitch, 0.5),
            },
            "colors": {"spec": spec_digest, "mix_cap": c.mix_cap},
            "layers": {"hmin": c.hmin, "hmax": c.hmax, "mix_cap": c.mix_cap},
            "project": {
                "printer": c.printer,
                "preset": c.process_name,
                "filament": c.filament_preset,
                "walls": c.walls,
                "infill": c.infill_pct,
                "brim": c.brim_mm,
                "name": c.name,
                "supports": c.supports,
                "set": c.set_process,
                "fset": c.set_filament,
                "overlay": frame.file_digest(c.overlay)
                if c.overlay and c.overlay.is_file()
                else None,
            },
            "slice": {"on": c.slice},
            "gates": {
                "gate": c.gate,
                "printer": c.printer,
                "walls": c.walls,
                "infill": c.infill_pct,
                "hmin": c.hmin,
                "hmax": c.hmax,
                "mix_cap": c.mix_cap,
            },
            "proof": {},
        }
        return table[step]

    def run(self) -> PrepOutcome:
        t_start = time.perf_counter()
        from prep import steps as step_impl  # late: steps import the heavy libraries

        runners = step_impl.runners(self)
        chosen = self.selected()
        if self.cfg.from_step:
            self.cache.drop_from(STEPS, self.cfg.from_step)
        for name in STEPS:
            if name not in chosen:
                self._record(name, "SKIP", "skipped by --only/--skip", 0.0, False, {})
                continue
            if self.blocked is not None:
                self._record(
                    name, "SKIP", f"blocked: {self.blocked} failed", 0.0, False, {}
                )
                continue
            self._run_step(name, runners[name])
        wall = time.perf_counter() - t_start
        ran: list[Verdict] = [
            r.verdict
            for n, r in self.records.items()
            if n != "diagnose" and r.verdict != "SKIP"
        ]
        outcome = PrepOutcome(
            self.cfg,
            [self.records[s] for s in STEPS],
            wall,
            worst(ran) if ran else "SKIP",
        )
        (self.work / "prep.json").write_text(
            json.dumps(outcome.to_dict(), indent=1, default=str), encoding="utf-8"
        )
        return outcome

    def _run_step(self, name: str, fn: Callable[[], StepOutput]) -> None:
        upstream = "|".join(self.keys.get(d, "") for d in DEPS[name])
        key = make_key(name, self.params(name), upstream)
        self.keys[name] = key
        if name != "proof":
            errored = [
                d
                for d in DEPS[name]
                if (r := self.records.get(d))
                and r.verdict == "FAIL"
                and "error" in r.result
            ]
            if errored:  # its input never came to exist: say so instead of crashing on a missing artifact
                self._record(
                    name,
                    "SKIP",
                    f"needs {', '.join(errored)}, which errored",
                    0.0,
                    False,
                    {},
                )
                return
        hit = None if self.cfg.force else self.cache.load(name, key)
        if hit is not None:
            self._adopt(name, hit.artifacts)
            summary = str(hit.result.get("summary", ""))
            verdict: Verdict = hit.result.get("verdict", "PASS")
            self._record(
                name,
                verdict,
                summary,
                0.0,
                True,
                hit.result.get("data", {}),
                hit.artifacts,
                hit.seconds,
            )
            self.log(f"[{name}] cached ({hit.seconds:.1f} s saved): {summary}")
            if verdict == "FAIL" and name in BLOCKING:
                self.blocked = name
            return
        self.log(f"[{name}] running ...")
        t0 = time.perf_counter()
        try:
            out = fn()
        except MissingDependency as exc:
            out = StepOutput(
                {"missing": exc.message, "fix": exc.remediation},
                "SKIP",
                f"SKIP: {exc.message}; {'; '.join(exc.remediation)}",
            )
        except ThreeDError as exc:  # a structured failure of one step is that step's FAIL, the flow reports the rest
            out = StepOutput(
                {"error": exc.message, "details": exc.remediation},
                "FAIL",
                f"{exc.message}; {' '.join(exc.remediation)}"[:400],
            )
        seconds = time.perf_counter() - t0
        if (
            out.verdict != "SKIP"
        ):  # a SKIP is an environment gap (no Orca, no spec): re-evaluate it next run
            stored = {
                "summary": out.summary,
                "verdict": out.verdict,
                "data": out.result,
            }
            self.cache.store(name, key, seconds, stored, out.artifacts)
        rel = [
            str(a.relative_to(self.work)) if a.is_absolute() else str(a)
            for a in out.artifacts
        ]
        self._adopt(name, rel)
        self._record(name, out.verdict, out.summary, seconds, False, out.result, rel)
        self.log(f"[{name}] {out.verdict} in {seconds:.1f} s: {out.summary}")
        if out.verdict == "FAIL" and name in BLOCKING:
            self.blocked = name

    def _adopt(self, name: str, artifacts: list[str]) -> None:
        """Remember a mesh stage's npz so later steps can load it."""
        if name in MESH_STEPS:
            for a in artifacts:
                if a.endswith(".npz"):
                    self._mesh_files[name] = self.work / a

    def _record(
        self,
        name: str,
        verdict: Verdict,
        summary: str,
        seconds: float,
        cached: bool,
        result: dict[str, Any],
        artifacts: list[str] | None = None,
        original: float | None = None,
    ) -> None:
        self.records[name] = StepRecord(
            name, verdict, summary, seconds, cached, result, artifacts or [], original
        )

    # ------------------------------------------------------------------ helpers for steps
    def mesh_path(self, step: str) -> Path:
        return self.work / "meshes" / f"{step}.npz"

    def record_data(self, name: str) -> dict[str, Any]:
        rec = self.records.get(name)
        return rec.result if rec else {}

    def copy_out(self, src: Path, dest_name: str) -> Path:
        dest = self.work / dest_name
        shutil.copyfile(src, dest)
        return dest


def result_dict(result: StepResult) -> dict[str, Any]:
    """`to_dict()` of a step result with its verdict and summary folded in."""
    data = dict(result.to_dict())
    data.setdefault("verdict", result.verdict)
    return data


def default_filaments(n: int = 4) -> list[Filament]:
    """Stub spools for a mono print on an n-head printer (slot 1 prints, the rest only keep the slot list complete)."""
    stubs = ["#333333", "#ffffff", "#c12e1f", "#1b5e20"]
    return [
        Filament(
            i + 1, "KWRG"[i] if i < 4 else str(i), f"Filament {i + 1}", stubs[i % 4]
        )
        for i in range(n)
    ]
