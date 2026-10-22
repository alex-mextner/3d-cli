"""3d mix — colour mixing by layers (Full Spectrum / Snapmaker Orca mixed filaments).

WHAT: `check FILE.3mf` lints a project before slicing: every part printed in a mixed filament
  must show its visible face vertically (lib/slicing/mix_faces.py; the same rule is the `mix face`
  line of `3d print-preflight`). `recipes` ranks layer patterns of the loaded filaments for a
  target colour (lib/slicing/mix_model.py, the recipe model of #116); `tester` writes a small
  numbered U1 plate of the best of them plus the pure spools, with a manifest and a swatch sheet
  (lib/slicing/mix_tester.py, #413).

WHY: a layer mix reads only on walls; a part lying flat shows its last layer, one plain filament
  (tg#22816: a flat tile in mix 431 came out ΔE 16.6 from the mockup). Mixes chosen from the
  prediction alone miss; a tester of a few grams shows the real colours first. Epic #410.

The geometry and colour work lives in lib/slicing/ (stdlib); this module stays import-light.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cli.registry import Command
from errors import GateFailure, InputNotFound, InvalidArgument, UsageError

if TYPE_CHECKING:
    from slicing.mix_model import Candidate
    from slicing.mix_tester import LoadedSlot

USAGE = """3d mix check FILE.3mf [--json]
3d mix recipes --target HEX (--slots N=HEX,... | --printer NAME) [--count N] [--max-cycle N]
               [--layer MM] [--printops-api URL] [--json]
3d mix tester  --target HEX (--slots N=HEX,... | --printer NAME) -o OUT/NAME [--count N]
               [--max-cycle N] [--layer MM] [--height MM] [--base-3mf FILE.3mf]
               [--printops-api URL] [--json]
  Colour mixing by layers (Snapmaker Orca mixed filaments, U1).

  check FILE.3mf [--json]
      Lint a project 3MF before slicing: every part whose filament is a mixed one (a manual
      layer pattern or a Local-Z ratio row) must show its colour on walls. Per part, its
      faces in bed coordinates are summed by normal: top (within 30° of horizontal, looking
      up), side (within 30° of vertical), slope (the rest looking up). FAIL when the top area
      exceeds half the side area (a layer mix reads only on walls, a flat top shows one
      filament); WARN when the upward slopes exceed half the side area (terraces).
      Exit 1 on FAIL, 0 otherwise (WARN is printed), 2 when the file is not a project 3MF.

  recipes
      Rank every layer pattern of the loaded filaments with a cycle of at most --max-cycle
      layers: the wall colour is the mean of the layers in linear light, scored by CIEDE2000
      to the target plus 0.2 × the stripe ΔE seen from 30 cm. Prints the best and its
      variants (lighter, darker, warmer, greener, short), then the next best.

  tester
      Write OUT/NAME.3mf (Snapmaker Orca project: one mixed filament per candidate, one
      standing 20 mm swatch per candidate and per loaded spool, a number in front of each in
      the darkest spool), OUT/NAME.mix.json (manifest, schema printops.mix_tester/1) and
      OUT/NAME.png (target next to each prediction). Slice it in Snapmaker Orca and run
      `3d print-preflight` as usual.

  --target HEX        the colour to match (#RRGGBB)
  --slots N=HEX,...   the loaded filaments by U1 head number (1-9), e.g. 1=#000000,4=#FFFFFF
  --printer NAME      read the loaded spools of NAME from 3d-api instead (colour, spool and
                      Spoolman filament id; the filament ids give the manifest its mix keys)
  --count N           candidates to keep (default 6)
  --max-cycle N       longest layer cycle (default 3; SPEC 58: short cycles read best)
  --layer MM          layer height (default 0.12; a U1 system process: 0.08 0.12 0.16 0.2)
  --height MM         swatch height (tester, default 6)
  --base-3mf FILE     take the project settings from this Snapmaker Orca project instead of
                      exporting the U1 system presets with the installed Snapmaker Orca
  -o OUT/NAME         output path without extension (tester)
  --printops-api URL  3d-api for --printer (default $PRINTOPS_API, else printops.yaml)
  --json              the same summary as JSON

Examples:
  3d mix check out/u1_tiles.3mf
  3d mix check out/u1_tiles.3mf --json
  3d mix recipes --target '#B5654F' --slots 1=#F6C700,3=#E8732A,4=#B0284F
  3d mix recipes --target '#B5654F' --printer u1 --max-cycle 4 --json
  3d mix tester --target '#B5654F' --printer u1 -o out/mix/terracotta
  3d mix tester --target '#808080' --slots 1=#000000,4=#FFFFFF --base-3mf print/u1.3mf -o out/grey"""

_CMD = "mix"
_FLAGS = {
    "--target",
    "--slots",
    "--printer",
    "--count",
    "--max-cycle",
    "--layer",
    "--height",
    "--base-3mf",
    "-o",
    "--printops-api",
}


def _check(argv: list[str]) -> int:
    import json

    from slicing.mix_faces import FAIL, ProjectError, check, format_lines

    as_json = "--json" in argv
    pos = [a for a in argv if a != "--json"]
    unknown = [a for a in pos if a.startswith("-")]
    if unknown:
        raise UsageError(
            f"unknown option {unknown[0]}",
            command=_CMD,
            remediation=["See `3d mix --help`."],
        )
    if len(pos) != 1:
        raise UsageError(
            f"check takes one project 3MF, got {len(pos)}",
            command=_CMD,
            remediation=["3d mix check out/plate.3mf"],
        )
    path = Path(pos[0])
    if not path.is_file():
        raise InputNotFound(str(path), command=_CMD)
    try:
        report = check(str(path))
    except ProjectError as exc:
        raise UsageError(
            str(exc),
            command=_CMD,
            remediation=[
                "Pass the project .3mf saved by Snapmaker Orca / Orca (not an STL export)."
            ],
        ) from exc
    if as_json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(f"mix check {path.name}: {report.status}")
        for line in format_lines(report):
            print(f"  {line}")
    if report.status == FAIL:
        raise GateFailure(
            f"mix check: {report.failures} mixed part(s) with the visible face horizontal",
            command=_CMD,
            silent=True,
        )
    return 0


def _options(argv: list[str], allowed: set[str]) -> tuple[dict[str, str], bool]:
    opts: dict[str, str] = {}
    as_json = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--json":
            as_json = True
            i += 1
            continue
        if a not in allowed:
            raise UsageError(
                f"unknown option {a}",
                command=_CMD,
                remediation=["See `3d mix --help`."],
            )
        if i + 1 >= len(argv):
            raise UsageError(f"{a} needs a value", command=_CMD)
        opts[a] = argv[i + 1]
        i += 2
    return opts, as_json


def _number(
    opts: dict[str, str], flag: str, default: float, lo: float, hi: float
) -> float:
    raw = opts.get(flag)
    if raw is None:
        return default
    try:
        v = float(raw)
    except ValueError:
        raise UsageError(f"{flag} {raw!r} is not a number", command=_CMD) from None
    if not lo <= v <= hi:
        raise UsageError(
            f"{flag} {raw!r} must be a number in {lo:g}..{hi:g}", command=_CMD
        )
    return v


def _integer(opts: dict[str, str], flag: str, default: int, lo: int, hi: int) -> int:
    raw = opts.get(flag)
    if raw is None:
        return default
    if not raw.isdecimal() or not lo <= int(raw) <= hi:
        raise UsageError(
            f"{flag} {raw!r} must be a whole number in {lo}..{hi}", command=_CMD
        )
    return int(raw)


def _parse_slots(text: str) -> list[LoadedSlot]:
    from printops.color import parse_hex
    from slicing.mix_tester import LoadedSlot

    out = []
    for item in filter(None, (s.strip() for s in text.split(","))):
        num, _, hex_ = item.partition("=")
        h = parse_hex(hex_)
        if not num.strip().isdecimal() or h is None:
            raise UsageError(
                f"--slots item {item!r} is not N=#RRGGBB",
                command=_CMD,
                remediation=["--slots 1=#000000,3=#1B5E20,4=#FFFFFF"],
            )
        out.append(LoadedSlot(int(num), h))
    if not out:
        raise UsageError("--slots names no filament", command=_CMD)
    return out


def _printer_slots(name: str, api_url: str | None) -> list[LoadedSlot]:
    """The loaded, usable slots of a printer as 3d-api sees them (read-only). A spool whose
    record cannot be read keeps its slot, without a filament id (no mix key)."""
    from printops.client import ApiClient, ApiConfigError, ApiError
    from printops.color import parse_hex
    from slicing.mix_tester import LoadedSlot

    try:
        api = ApiClient.from_config(api=api_url)
        view = api.get(f"/v1/printers/{name}/slots")
    except (ApiConfigError, ApiError) as e:
        raise UsageError(
            f"cannot read the slots of {name}: {e}",
            command=_CMD,
            remediation=["Pass the loaded colours with --slots N=#RRGGBB,..."],
        ) from e
    out = []
    for s in view.get("slots") or []:
        index = s.get("index")
        if not s.get("usable") or not isinstance(index, int):
            continue
        spools = s.get("spools") or []
        sp = spools[0] if len(spools) == 1 else None  # two bound spools: colour unknown
        printer = s.get("printer") or {}
        hex_ = parse_hex((sp.get("colors") or [None])[0]) if sp else None
        hex_ = hex_ or (
            parse_hex(printer.get("color")) if printer.get("present") else None
        )
        if hex_ is None:
            continue
        spool_id = sp.get("spool_id") if sp else None
        fid = None
        if isinstance(spool_id, int):
            try:
                fid = api.get(f"/v1/spools/{spool_id}").get("filament_id")
            except ApiError:
                fid = None
        out.append(
            LoadedSlot(
                index + 1,
                hex_,
                str(sp.get("name") or "") if sp else "",
                spool_id if isinstance(spool_id, int) else None,
                fid if isinstance(fid, int) else None,
            )
        )
    if not out:
        raise UsageError(f"{name}: no usable slot with a known colour", command=_CMD)
    return out


@dataclass(frozen=True)
class _Plan:
    target: str
    slots: list[LoadedSlot]
    ranked: list[Candidate]
    layer: float
    count: int
    max_cycle: int


def _plan(opts: dict[str, str], layer: float | None = None) -> _Plan:
    """What recipes and tester share; `layer` overrides --layer (a base project's own)."""
    from printops.color import parse_hex
    from slicing.mix_model import MixModelError, Slot, rank

    target = parse_hex(opts.get("--target"))
    if target is None:
        raise UsageError("--target #RRGGBB is required", command=_CMD)
    if ("--slots" in opts) == ("--printer" in opts):
        raise UsageError("pass exactly one of --slots and --printer", command=_CMD)
    slots = (
        _parse_slots(opts["--slots"])
        if "--slots" in opts
        else _printer_slots(opts["--printer"], opts.get("--printops-api"))
    )
    if layer is None:
        layer = _number(opts, "--layer", 0.12, 0.04, 0.4)
    if abs(round(layer, 4) - layer) > 1e-9:
        raise UsageError(f"layer {layer!r} must have at most 4 decimals", command=_CMD)
    count = _integer(opts, "--count", 6, 1, 20)
    max_cycle = _integer(opts, "--max-cycle", 3, 1, 12)
    try:
        ranked = rank(
            target,
            [Slot(s.slot, s.hex, s.name) for s in slots],
            max_cycle=max_cycle,
            layer_h=layer,
        )
    except MixModelError as e:
        raise UsageError(str(e), command=_CMD) from e
    return _Plan(target, slots, ranked, layer, count, max_cycle)


def _emit(summary: dict[str, Any], lines: list[str], as_json: bool) -> None:
    import json

    if as_json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print("\n".join(lines))


def _cand_line(n: int, c: dict[str, Any]) -> str:
    stripes = c["stripe_delta_e_30cm"]
    return (
        f"  {n:>2} {c['role']:<8} {c['pattern']:<8} {c['hex']}  ΔE {c['delta_e']:.2f}"
        + ("" if stripes is None else f"  stripes {stripes:.2f} @30cm")
    )


def _recipes(argv: list[str]) -> int:
    from slicing.mix_model import pick

    opts, as_json = _options(argv, _FLAGS - {"--height", "--base-3mf", "-o"})
    p = _plan(opts)
    target, slots, layer, max_cycle = p.target, p.slots, p.layer, p.max_cycle
    chosen = [c.to_dict() for c in pick(p.ranked, p.count)]
    summary = {
        "target": target,
        "layer_height": layer,
        "max_cycle": max_cycle,
        "slots": [s.to_dict() for s in slots],
        "candidates": [{"number": i + 1, **c} for i, c in enumerate(chosen)],
    }
    lines = [
        f"mix recipes for {target} (layer {layer:g} mm, cycle ≤ {max_cycle}, "
        + ", ".join(f"T{s.slot} {s.hex}" for s in slots)
        + ")"
    ] + [_cand_line(i + 1, c) for i, c in enumerate(chosen)]
    _emit(summary, lines, as_json)
    return 0


def _tester(argv: list[str]) -> int:
    from slicing.mix_model import pick
    from slicing.mix_tester import (
        U1_HEADS,
        MixTesterError,
        build,
        find_slicer,
        layer_height,
        probe_settings,
        read_base,
        slicer_version,
    )

    opts, as_json = _options(argv, _FLAGS)
    if "-o" not in opts:
        raise UsageError("-o OUT/NAME is required", command=_CMD)
    if Path(opts["-o"]).suffix.lower() in (".3mf", ".json", ".png"):
        raise UsageError(
            f"-o {opts['-o']}: give the path without an extension (NAME.3mf, NAME.mix.json and NAME.png are written)",
            command=_CMD,
        )
    height = _number(opts, "--height", 6.0, 3.0, 40.0)
    try:
        if "--base-3mf" in opts:
            base = Path(opts["--base-3mf"])
            if not base.is_file():
                raise InputNotFound(str(base), command=_CMD)
            settings, app = read_base(base)
            base_layer = layer_height(settings)
            asked = opts.get("--layer")
            if (
                asked is not None
                and abs(_number(opts, "--layer", 0, 0.04, 0.4) - base_layer) > 1e-9
            ):
                raise UsageError(
                    f"--layer {asked} but {base} prints at {base_layer:g} mm: drop --layer or "
                    "pass a base project at that height",
                    command=_CMD,
                )
            p = _plan(opts, base_layer)
            source = str(base)
        else:
            p = _plan(opts)
            slicer = find_slicer()
            settings = probe_settings(slicer, p.layer, U1_HEADS)
            app = f"Snapmaker_Orca-{slicer_version(slicer)}"
            source = "Snapmaker Orca U1 system presets"
        mixes = pick([c for c in p.ranked if c.cycle > 1], p.count)
        if not mixes:
            raise UsageError(
                "no layer mix to test: load two or more filaments and allow --max-cycle 2 or more",
                command=_CMD,
            )
        manifest = build(
            target=p.target,
            slots=p.slots,
            candidates=mixes,
            out=Path(opts["-o"]),
            layer=p.layer,
            height=height,
            base_settings=settings,
            app=app,
            source=source,
        )
    except MixTesterError as e:
        raise UsageError(str(e), command=_CMD) from e
    out_dir = Path(opts["-o"]).parent
    summary = {
        "target": manifest["target"],
        "files": {k: str(out_dir / v) for k, v in manifest["files"].items()},
        "settings_source": manifest["settings_source"],
        "label_slot": manifest["label_slot"],
        "candidates": [
            {
                k: c[k]
                for k in ("number", "role", "pattern", "hex", "delta_e", "filament")
            }
            for c in manifest["candidates"]
        ],
        "references": [
            {k: r[k] for k in ("number", "slot", "hex")} for r in manifest["references"]
        ],
    }
    lines = [
        f"mix tester for {summary['target']}: {len(mixes)} mixes + {len(summary['references'])} references, labels on T{summary['label_slot']}",
        *(f"  {k}: {v}" for k, v in summary["files"].items()),
        f"  settings: {summary['settings_source']}",
        *(
            f"  {c['number']:>2} {c['role']:<8} {c['pattern']:<8} {c['hex']}  ΔE {c['delta_e']:.2f}  filament {c['filament']}"
            for c in summary["candidates"]
        ),
        *(
            f"  {r['number']:>2} T{r['slot']} pure  {r['hex']}"
            for r in summary["references"]
        ),
        f"next: slice {summary['files']['3mf']} in Snapmaker Orca, then `3d print-preflight`",
    ]
    _emit(summary, lines, as_json)
    return 0


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    sub, rest = argv[0], argv[1:]
    if sub == "check":
        return _check(rest)
    if sub == "recipes":
        return _recipes(rest)
    if sub == "tester":
        return _tester(rest)
    raise InvalidArgument(
        "subcommand", sub, ["check", "recipes", "tester"], command=_CMD
    )


COMMAND = Command(
    name="mix",
    group="SLICING",
    summary="colour mixing by layers: rank recipes, build a tester plate, lint mixed parts",
    usage=USAGE,
    run=run,
)
