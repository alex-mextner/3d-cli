"""cli.py — argument parsing of `3d prep` (runs inside the prep interpreter, see commands/prep.py).

WHAT: turns flags plus an optional spec file (YAML) into a `PrepConfig`, runs the `Pipeline`, prints the step
  table (or JSON) and returns the exit code: 0 for PASS/WARN/SKIP, 1 when any step FAILed.
WHY: one command with the knobs the Chekhov session kept re-typing as script constants (walls, infill, layer band,
  angle, fit table, colour spec) and the cache switches (`--only`, `--skip`, `--from`, `--force`).
INVARIANTS: paths inside a spec file resolve against the spec file's directory; a flag always wins over the spec.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from errors import InputNotFound, InvalidArgument, UsageError
from prep.flat_profiles import parse_assignments, scalar
from prep.pipeline import DEPS, STEPS, Pipeline, PrepConfig

_SPEC_KEYS = {
    "printer",
    "orient",
    "angle",
    "pitch",
    "adapt",
    "overhang",
    "fit",
    "holder",
    "loads",
    "stability",
    "colors",
    "process",
    "gcode",
}
_PROCESS_KEYS = {
    "preset",
    "filament_preset",
    "walls",
    "infill",
    "layer_min",
    "layer_max",
    "brim",
    "mix_cap",
    "supports",
    "set",
    "fset",
    "overlay",
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="3d prep", add_help=False)
    p.add_argument("model", nargs="?")
    p.add_argument("--out", default=None)
    p.add_argument("--spec", default=None)
    p.add_argument("--printer", default=None)
    p.add_argument("--orient", default=None)
    p.add_argument("--angle", type=float, default=None)
    p.add_argument("--pitch", type=float, default=None)
    p.add_argument("--bridge-max", type=float, default=None)
    p.add_argument("--no-adapt", action="store_true")
    p.add_argument("--fit", default=None)
    p.add_argument("--colors", default=None)
    p.add_argument("--process", default=None)
    p.add_argument("--filament", default=None)
    p.add_argument("--walls", type=int, default=None)
    p.add_argument("--infill", type=int, default=None)
    p.add_argument("--layer-min", type=float, default=None)
    p.add_argument("--layer-max", type=float, default=None)
    p.add_argument("--brim", type=float, default=None)
    p.add_argument("--supports", default=None)
    p.add_argument("--set", action="append", default=[], dest="set_process")
    p.add_argument("--fset", action="append", default=[])
    p.add_argument("--max-volumetric", type=float, default=None)
    p.add_argument("--overlay", default=None)
    p.add_argument("--only", default="")
    p.add_argument("--skip", default="")
    p.add_argument("--from", dest="from_step", default=None)
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-slice", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true")
    return p


def _csv(text: str) -> frozenset[str]:
    return frozenset(s.strip() for s in text.split(",") if s.strip())


def load_spec(path: Path) -> dict[str, Any]:
    """The spec file as a dict with every path value resolved against the file's directory."""
    import yaml

    if not path.is_file():
        raise InputNotFound(str(path), command="prep")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise InvalidArgument(
            "--spec", str(path), ["a mapping of settings"], command="prep"
        )
    unknown = sorted(set(data) - _SPEC_KEYS)
    if unknown:
        raise InvalidArgument(
            "--spec key", ", ".join(unknown), sorted(_SPEC_KEYS), command="prep"
        )
    process = data.get("process") or {}
    bad = sorted(set(process) - _PROCESS_KEYS)
    if bad:
        raise InvalidArgument(
            "--spec process key", ", ".join(bad), sorted(_PROCESS_KEYS), command="prep"
        )
    base = path.parent
    for key in ("fit", "colors"):
        if data.get(key):
            data[key] = str((base / str(data[key])).resolve())
    if process.get("overlay"):
        process["overlay"] = str((base / str(process["overlay"])).resolve())
    return data


def _adapt_spec(raw: Any) -> tuple[bool, list[dict[str, Any]]]:
    """`adapt:` of a spec file: a bool, or {enabled, zones: [{z_min, z_max, angle_deg, columns}]}."""
    if isinstance(raw, bool):
        return raw, []
    if isinstance(raw, dict):
        unknown = sorted(set(raw) - {"enabled", "zones"})
        if unknown:
            raise InvalidArgument(
                "--spec adapt key",
                ", ".join(unknown),
                ["enabled", "zones"],
                command="prep",
            )
        return bool(raw.get("enabled", True)), [dict(z) for z in raw.get("zones") or []]
    raise InvalidArgument(
        "--spec adapt", str(raw), ["true | false | {enabled, zones}"], command="prep"
    )


def _spec_assignments(raw: Any, where: str) -> dict[str, str]:
    """`set:` / `fset:` of a spec file: a mapping of setting -> scalar, normalised the way --overlay values are."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise InvalidArgument(
            where, str(raw), ["a mapping of setting: value"], command="prep"
        )
    for key, value in raw.items():
        if isinstance(value, (dict, list)):
            raise InvalidArgument(
                f"{where} {key}",
                str(value),
                ["a string, number or boolean"],
                command="prep",
            )
    return {str(k): scalar(v) for k, v in raw.items()}


def make_config(ns: argparse.Namespace) -> PrepConfig:
    if not ns.model:
        raise UsageError(
            "prep needs a model: 3d prep MODEL.stl|.3mf [options]", command="prep"
        )
    spec = load_spec(Path(ns.spec)) if ns.spec else {}
    process = spec.get("process") or {}
    model = Path(ns.model)
    out = Path(ns.out) if ns.out else Path(f"{model.stem}-prep")

    def pick(flag: Any, spec_value: Any, default: Any) -> Any:
        return (
            flag
            if flag is not None
            else (spec_value if spec_value is not None else default)
        )

    fit = pick(ns.fit, spec.get("fit"), None)
    colors = pick(ns.colors, spec.get("colors"), None)
    adapt_on, adapt_zones = _adapt_spec(spec.get("adapt", True))
    overhang = spec.get("overhang") or {}
    set_process = {
        **_spec_assignments(process.get("set"), "--spec process set"),
        **parse_assignments(ns.set_process, "--set"),
    }
    set_filament = {
        **_spec_assignments(process.get("fset"), "--spec process fset"),
        **parse_assignments(ns.fset, "--fset"),
    }
    if ns.max_volumetric is not None:
        set_filament["filament_max_volumetric_speed"] = f"{ns.max_volumetric:g}"
    overlay = pick(ns.overlay, process.get("overlay"), None)
    cfg = PrepConfig(
        model=model,
        out=out,
        printer=pick(ns.printer, spec.get("printer"), "u1"),
        orient=pick(ns.orient, spec.get("orient"), "auto"),
        angle_deg=float(pick(ns.angle, spec.get("angle"), 45.0)),
        pitch=float(pick(ns.pitch, spec.get("pitch"), 0.25)),
        adapt=False if ns.no_adapt else adapt_on,
        adapt_zones=adapt_zones,
        bridge_max_mm=float(pick(ns.bridge_max, overhang.get("bridge_max_mm"), 10.0)),
        items=Path(fit) if fit else None,
        holder=dict(spec.get("holder") or {}),
        loads=list(spec.get("loads") or []),
        stability=dict(spec.get("stability") or {}),
        colors=Path(colors) if colors else None,
        process_preset=pick(ns.process, process.get("preset"), None),
        filament_preset=pick(ns.filament, process.get("filament_preset"), None),
        walls=int(pick(ns.walls, process.get("walls"), 4)),
        infill_pct=int(pick(ns.infill, process.get("infill"), 15)),
        hmin=float(pick(ns.layer_min, process.get("layer_min"), 0.08)),
        hmax=float(pick(ns.layer_max, process.get("layer_max"), 0.32)),
        mix_cap=float(pick(None, process.get("mix_cap"), 0.12)),
        brim_mm=float(pick(ns.brim, process.get("brim"), 0.0)),
        supports=str(pick(ns.supports, process.get("supports"), "none")),
        set_process=set_process,
        set_filament=set_filament,
        overlay=Path(overlay) if overlay else None,
        gate=dict(spec.get("gcode") or {}),
        only=_csv(ns.only),
        skip=_csv(ns.skip),
        from_step=ns.from_step,
        force=ns.force,
        slice=not ns.no_slice,
    )
    return cfg


def show_steps() -> str:
    lines = [
        "step       needs",
        *(f"{s:<10} {', '.join(DEPS[s]) or '-'}" for s in STEPS),
    ]
    return "\n".join(lines)


def show_status(out: Path) -> int:
    path = out / "prep.json"
    if not path.is_file():
        raise InputNotFound(str(path), command="prep")
    data = json.loads(path.read_text(encoding="utf-8"))
    print(
        f"{data['model']}  printer {data['printer']}  overall {data['verdict']}  wall {data['wall_seconds']} s"
    )
    for r in data["steps"]:
        cache = "cache hit" if r["cached"] else f"{r['seconds']} s"
        print(f"  {r['name']:<10} {r['verdict']:<5} {cache:<10} {r['summary']}")
    return 1 if data["verdict"] == "FAIL" else 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == "steps":
        print(show_steps())
        return 0
    if argv and argv[0] == "status":
        if len(argv) != 2:
            raise UsageError("usage: 3d prep status OUT_DIR", command="prep")
        return show_status(Path(argv[1]))
    ns, extra = build_parser().parse_known_args(argv)
    if extra:
        raise InvalidArgument(
            "option", " ".join(extra), ["see `3d prep --help`"], command="prep"
        )
    cfg = make_config(ns)
    log = (
        (lambda _msg: None)
        if (ns.quiet or ns.json)
        else (lambda msg: print(msg, flush=True))
    )
    outcome = Pipeline(cfg, log=log).run()
    if ns.json:
        print(json.dumps(outcome.to_dict(), indent=1, default=str))
    else:
        print(outcome.table())
        print(f"output: {cfg.out}  (report: {cfg.out / 'proof-report.md'})")
    return 1 if outcome.verdict == "FAIL" else 0
