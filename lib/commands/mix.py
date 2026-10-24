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
    from slicing.mix_shelf import Spool
    from slicing.mix_pattern import MixRules
    from slicing.mix_shelf import ShelfPlan
    from slicing.mix_tester import LoadedSlot

USAGE = """3d mix check FILE.3mf [--layer-cap MM] [--max-run-mm MM] [--json]
3d mix profile FILE.3mf -o OUT.3mf [--layer-cap MM] [--min-layer MM] [--max-layer MM] [--json]
3d mix gcode FILE.gcode [--project FILE.3mf] [--layer-cap MM] [--max-run-mm MM] [--json]
3d mix pattern PATTERN [--layer MM | --heights MM,MM,...] [--layer-cap MM] [--max-run-mm MM] [--json]
3d mix target IMAGE --box x0,y0,x1,y1 (--white x0,y0,x1,y1 | --no-wb) [--white-level N] [--json]
3d mix recipes TARGET (--slots N=HEX,... | --printer NAME [--shelf]) [--count N] [--max-cycle N]
               [--layer MM] [--layer-cap MM] [--max-run-mm MM] [--printops-api URL] [--json]
3d mix tester  TARGET (--slots N=HEX,... | --printer NAME [--shelf]) -o OUT/NAME [--count N]
               [--max-cycle N] [--layer MM] [--layer-cap MM] [--max-run-mm MM] [--height MM]
               [--base-3mf FILE.3mf] [--printops-api URL] [--json]
  TARGET: --target HEX | --target-photo IMAGE --box x0,y0,x1,y1 (--white x0,y0,x1,y1 | --no-wb)
          [--white-level N]
               [--shelf: --max-swaps N --material NAME --min-g G]
3d mix readout PHOTO --manifest NAME.mix.json -o OUT/NAME [--white card|N] [--white-level N]
               [--no-qr-check] [--json]
  Colour mixing by layers (Snapmaker Orca mixed filaments, U1).

  check FILE.3mf
      Lint a project 3MF before slicing. Faces: every part whose filament is a mixed one (a
      manual layer pattern or a Local-Z ratio row) must show its colour on walls. Per part, its
      faces in bed coordinates are summed by normal: top (within 30° of horizontal, looking
      up), side (within 30° of vertical), slope (the rest looking up). FAIL when the top area
      exceeds half the side area (a layer mix reads only on walls, a flat top shows one
      filament); WARN when the upward slopes exceed half the side area (terraces).
      Layers: every mixed pattern passes the layer rules (see `pattern`), and the layers sliced
      over each mixed part (the object's layer height profile, else layer_height) are at most
      --layer-cap thick; an object profile Orca would drop (last z is not the object height) or
      a prime tower over objects with different layer heights is a FAIL.
      Exit 1 on FAIL, 0 otherwise (WARN is printed), 2 when the file is not a project 3MF.

  profile FILE.3mf -o OUT.3mf
      Write OUT.3mf: the project with a variable layer height profile (Metadata/layer_heights_
      profile.txt) that keeps the project's own layer height everywhere except over the mixed
      parts, where layers are at most --layer-cap thick (the transition is smooth: neighbouring
      layers differ by at most 20 %). Orca takes one profile per object and, with a prime tower,
      needs the same on every object: with a tower and several objects make the parts one
      multi-part object (error otherwise). Then re-checks OUT.3mf like `check`. The input is
      never overwritten.

  gcode FILE.gcode
      Measure what a sliced G-code printed in every mixed object: the head of each layer and
      the real layer Z (variable layer heights included), against the layer rules below. FAIL
      (exit 1) when the additional colour is printed on consecutive layers (the layers, the
      head and the band in mm are named), when a colour forms a band thicker than --max-run-mm,
      or when a mixed layer is thicker than --layer-cap (the first layer is exempt; raft layers
      are skipped). --project FILE.3mf (the plate project the G-code was sliced from) says which
      heads each object's mix uses; without it only objects that print one head per layer and
      alternate are measured (stretches of short alternating runs). Also the
      `mix layers` line of `3d print-preflight`.

  pattern PATTERN
      Check one repeating layer pattern (a character per layer: GWG, 431, WR) against the
      layer rules and print the verdict. Exit 1 when it breaks one. The rules (the same for
      recipes, the tester and the G-code gate):
        - the ADDITIONAL (minor) colour is one thin layer between the others: it is never on
          two consecutive layers, the repeat seam included (WGGGW repeats as ...WW...);
        - no colour forms a band thicker than --max-run-mm (default: one capped layer, so a
          colour does not repeat on adjacent layers; GWG repeats as GWGGWG, a double G band);
        - every layer is at most --layer-cap thick (default 0.12 mm).
      --layer MM is the layer height (default: the cap); --heights lists one per pattern
      character for a variable layer height.

  target IMAGE
      The colour to match, read from a region of a mockup photo or a render: the pixels of --box
      are white-balanced on the --white region (a per-channel gain in linear light that maps the
      median of the white region to --white-level, which removes the colour cast and the
      exposure), converted to CIELAB one by one, and the target is the median in Lab. A render
      (Blender, a mockup tool) has no cast: --no-wb skips the balance. Prints the target hex, its
      Lab, the image sha256 and the regions; with --json the `mix-target/1` record. `recipes`
      and `tester` take the same region as --target-photo and print the target they used;
      the tester manifest records the image, boxes, balance and sha256 as `target_source`.

  recipes
      Rank every layer pattern of the loaded filaments with a cycle of at most --max-cycle
      layers that passes the layer rules: the wall colour is the mean of the layers in linear
      light, scored by CIEDE2000 to the target plus 0.2 × the stripe ΔE seen from 30 cm.
      Patterns that break the rules are rejected, not ranked low (with the default limits only
      1:1 pairs, 1:1:1 triples, 2:1:1 ... qualify; pure filaments always do). Prints the best
      and its variants (lighter, darker, warmer, greener, short), then the next best.

  tester
      Write OUT/NAME.3mf (Snapmaker Orca project: one mixed filament per candidate, one
      standing 20 mm swatch per candidate and per loaded spool, a number in front of each in
      the darkest spool), OUT/NAME.mix.json (manifest, schema printops.mix_tester/1) and
      OUT/NAME.png (target next to each prediction). Slice it in Snapmaker Orca and run
      `3d print-preflight` as usual. OUT/NAME.card.png is the white card to print at 100 %: the
      printed swatches lie on it, face up, for the photo of `readout`.

  readout PHOTO
      Measure the printed tester from one photo of its card (NAME.card.png on paper, the swatches
      laid in their numbered frames, face up). A top-camera frame (`3d camera still`) and a phone
      photo go the same way: the four corner fiducials rectify the photo to millimetres (3 of 4
      are enough), the QR must name this manifest (another tester's card is refused), the white
      balance is fitted on the blank paper regions (--white card) or on a pure reference swatch
      (--white N: its colour is the spool's), and each swatch is the median in CIELAB of its patch
      (the face without its border; the number lies outside the face). Prints, per swatch, the
      measured hex, ΔE2000 to the prediction and to the target; writes OUT/NAME.readout.json
      (`mix-readout/1`), OUT/NAME.readout.png (annotated) and OUT/NAME.rectified.png.
      Exit 1: the photo cannot be read (reasons say what to reshoot); 2: bad arguments or manifest.

  --target HEX        the colour to match (#RRGGBB)
  --target-photo IMAGE  take the target from a region of this image instead of --target
  --box x0,y0,x1,y1   the region to match in pixels (x1, y1 exclusive; origin top-left as a viewer
                      shows the image, EXIF orientation applied)
  --white x0,y0,x1,y1 a region of white on the same photo (trim, paper): the white balance
  --no-wb             the image is a render, not a photo: no colour cast, skip the balance
  --white-level N     the 8-bit value the white region is mapped to (default 235)
  --manifest FILE     the tester manifest NAME.mix.json the photo belongs to (readout)
  --white card|N      readout white balance: the card's blank paper (default) or the number N of a
                      pure reference swatch (a white spool): --white 9
  --no-qr-check       readout: read the card without confirming its QR names the manifest
  --slots N=HEX,...   the loaded filaments by U1 head number (1-9), e.g. 1=#000000,4=#FFFFFF
  --printer NAME      read the loaded spools of NAME from 3d-api instead (colour, spool and
                      Spoolman filament id; the filament ids give the manifest its mix keys)
  --count N           candidates to keep (default 6)
  --max-cycle N       longest layer cycle (default 3; SPEC 58: short cycles read best)
  --layer MM          layer height (default 0.12; a U1 system process: 0.08 0.12 0.16 0.2);
                      above --layer-cap no mix is possible (error), pure filaments still are
  --layer-cap MM      thickest layer allowed in a mixed region (default 0.12)
  --min-layer MM      thinnest layer a profile may use (profile, default 0.08)
  --max-layer MM      thickest layer a profile may use (profile, default 0.32)
  --max-run-mm MM     thickest band of one colour (default: --layer-cap = one layer). 0.24 lets a
                      majority colour double (GWG, KKW); a minor colour never may
  --height MM         swatch height (tester, default 6)
  --base-3mf FILE     take the project settings from this Snapmaker Orca project instead of
                      exporting the U1 system presets with the installed Snapmaker Orca
  -o OUT/NAME         output path without extension (tester)
  --shelf             (with --printer) also use the spools in stock but not loaded (Spoolman through
                      3d-api): rank loadouts of the loaded spools plus up to --max-swaps shelf spools by
                      the best reachable mix and print the load plan (which spool on which head, ΔE
                      before/after; `3d printops spool load ID PRINTER/Tn` performs it). Equal results
                      prefer fewer swaps. Empty spools (under --min-g, default 30 g) and spools of
                      another material than the loaded ones (--material overrides) are left out; a
                      measured `filament_color` record replaces a spool's Spoolman colour. `tester`
                      builds the plate for the best loadout, its manifest names the spools
  --max-swaps N       shelf spools a loadout may add (--shelf, default 2, at most 4)
  --material NAME     material the shelf spools must have (--shelf; default: the loaded spools')
  --min-g G           spools lighter than this are empty (--shelf, default 30)
  --printops-api URL  3d-api for --printer (default $PRINTOPS_API, else printops.yaml)
  --json              the same summary as JSON

Examples:
  3d mix gcode out/cat.gcode --project out/cat.3mf
  3d mix gcode out/cat.gcode --project out/cat.3mf --max-run-mm 0.24 --json
  3d mix pattern GWG
  3d mix pattern GW --layer 0.12
  3d mix pattern GWG --max-run-mm 0.24
  3d mix pattern WGW --heights 0.12,0.08,0.12 --json
  3d mix check out/u1_tiles.3mf
  3d mix check out/u1_tiles.3mf --layer-cap 0.16 --json
  3d mix profile out/cat.3mf -o out/cat.capped.3mf
  3d mix profile out/cat.3mf -o out/cat.capped.3mf --layer-cap 0.1 --min-layer 0.06
  3d mix recipes --target '#B5654F' --slots 1=#F6C700,3=#E8732A,4=#B0284F
  3d mix recipes --target '#B5654F' --printer u1 --max-cycle 4 --json
  3d mix recipes --target '#B5654F' --printer u1 --shelf
  3d mix tester --target '#B5654F' --printer u1 -o out/mix/terracotta
  3d mix target mockup.jpg --box 410,220,470,300 --white 20,30,80,90
  3d mix target render.png --box 100,100,160,160 --no-wb --json
  3d mix readout phone.jpg --manifest out/mix/wall.mix.json -o out/mix/wall-readout
  3d mix readout frame.png --manifest out/mix/wall.mix.json -o out/mix/wall-readout --white 9 --json
  3d mix tester --target-photo mockup.jpg --box 410,220,470,300 --white 20,30,80,90 --printer u1 -o out/mix/wall
  3d mix tester --target '#808080' --slots 1=#000000,4=#FFFFFF --base-3mf print/u1.3mf -o out/grey"""

_CMD = "mix"
# flags without a value (--json is handled on its own)
_SWITCHES = {"--no-wb", "--no-qr-check"}  # readout alone takes --no-qr-check
_FLAGS = {
    "--target",
    "--target-photo",
    "--box",
    "--white",
    "--white-level",
    "--slots",
    "--printer",
    "--count",
    "--max-cycle",
    "--layer",
    "--layer-cap",
    "--max-run-mm",
    "--height",
    "--base-3mf",
    "-o",
    "--printops-api",
    "--no-wb",
    "--max-swaps",
    "--material",
    "--min-g",
}
_BOOL_FLAGS = {"--shelf"}


def _check(argv: list[str]) -> int:
    import json

    from slicing import mix_project
    from slicing.mix_faces import FAIL, ProjectError, check, format_lines

    if not argv or argv[0].startswith("-"):
        raise UsageError(
            "check takes the project 3MF first",
            command=_CMD,
            remediation=["3d mix check out/plate.3mf"],
        )
    path, rest = Path(argv[0]), argv[1:]
    opts, as_json = _options(rest, {"--layer-cap", "--max-run-mm"})
    rules = _rules(opts)
    if not path.is_file():
        raise InputNotFound(str(path), command=_CMD)
    try:
        report = check(str(path))
        layers = mix_project.check(str(path), rules)
    except ProjectError as exc:
        raise UsageError(
            str(exc),
            command=_CMD,
            remediation=[
                "Pass the project .3mf saved by Snapmaker Orca / Orca (not an STL export)."
            ],
        ) from exc
    failed = report.status == FAIL or layers.status == FAIL
    status = FAIL if failed else report.status if report.parts else layers.status
    if as_json:
        print(
            json.dumps(
                {
                    **report.to_dict(),
                    "status": status,
                    "layers": layers.to_dict(),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print(f"mix check {path.name}: {status}")
        for line in format_lines(report) + mix_project.format_lines(layers):
            print(f"  {line}")
    if failed:
        raise GateFailure(
            f"mix check: {report.failures} mixed part(s) with the visible face horizontal, "
            f"{layers.failures} layer-rule finding(s)",
            command=_CMD,
            silent=True,
        )
    return 0


def _profile(argv: list[str]) -> int:
    from slicing import layer_heights, mix_project
    from slicing.head_sequence import ProjectError

    if not argv or argv[0].startswith("-"):
        raise UsageError(
            "profile takes the project 3MF first",
            command=_CMD,
            remediation=["3d mix profile out/plate.3mf -o out/plate.capped.3mf"],
        )
    src, rest = Path(argv[0]), argv[1:]
    opts, as_json = _options(rest, {"-o", "--layer-cap", "--min-layer", "--max-layer"})
    if "-o" not in opts:
        raise UsageError(
            "-o OUT.3mf is required (the input is never overwritten)", command=_CMD
        )
    dst = Path(opts["-o"])
    if dst.suffix.lower() != ".3mf":
        raise UsageError(f"-o {dst}: the output must be a .3mf file", command=_CMD)
    if not src.is_file():
        raise InputNotFound(str(src), command=_CMD)
    if dst.exists() and dst.samefile(src):
        raise UsageError(
            "-o must differ from the input: the input is never overwritten",
            command=_CMD,
        )
    rules = _rules(opts)
    hmin = _number(opts, "--min-layer", layer_heights.HMIN_MM, 0.02, 0.4)
    hmax = _number(opts, "--max-layer", layer_heights.HMAX_MM, 0.04, 0.8)
    if not hmin <= rules.layer_cap_mm <= hmax:
        raise UsageError(
            f"--layer-cap {rules.layer_cap_mm:g} must lie between --min-layer {hmin:g} and --max-layer {hmax:g}",
            command=_CMD,
        )
    try:
        plans = mix_project.plan_profiles(
            str(src), cap=rules.layer_cap_mm, hmin=hmin, hmax=hmax
        )
        layer_heights.write_profiles_3mf(
            src, dst, {p.object_id: p.profile for p in plans}
        )
        after = mix_project.check(str(dst), rules)
    except ProjectError as exc:
        raise UsageError(str(exc), command=_CMD) from exc
    except OSError as exc:
        raise UsageError(
            f"cannot write {dst}: {exc.strerror or exc}",
            command=_CMD,
            remediation=[
                "Create the output directory first, or pick a writable -o path."
            ],
        ) from exc
    except ValueError as exc:  # a mixed part Orca cannot cap (an empty z range, a cap below the thinnest layer)
        raise UsageError(
            f"cannot cap the layers of {src.name}: {exc}", command=_CMD
        ) from exc
    objects = [
        {
            "object": p.object,
            "object_id": p.object_id,
            "height_mm": round(p.height, 3),
            "mixed_zones_mm": [list(z) for z in p.zones],
            "profile": p.profile,
        }
        for p in plans
    ]
    summary = {
        "output": str(dst),
        "layer_cap_mm": rules.layer_cap_mm,
        "status": after.status,
        "objects": objects,
        "findings": [f.to_dict() for f in after.findings],
    }
    lines = [
        f"mix profile: {src.name} -> {dst} (mixed layers capped at {rules.layer_cap_mm:g} mm)",
        *(
            f"  {p.object}: object {p.object_id}, {p.height:.1f} mm, mixed z "
            + ", ".join(f"{lo:g}-{hi:g}" for lo, hi in p.zones)
            for p in plans
        ),
        *(f"  {line}" for line in mix_project.format_lines(after)),
    ]
    _emit(summary, lines, as_json)
    if after.status == "FAIL":
        raise GateFailure(
            f"mix profile: the written project still breaks the layer rules ({after.failures})",
            command=_CMD,
            silent=True,
        )
    return 0


def _options(
    argv: list[str], allowed: set[str], flags: set[str] | None = None
) -> tuple[dict[str, str], bool]:
    """(--flag value pairs and `flags` as {flag: "1"}, --json)."""
    opts: dict[str, str] = {}
    as_json = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--json":
            as_json = True
            i += 1
            continue
        if flags and a in flags:
            opts[a] = "1"
            i += 1
            continue
        if a not in allowed:
            raise UsageError(
                f"unknown option {a}",
                command=_CMD,
                remediation=["See `3d mix --help`."],
            )
        if a in _SWITCHES:
            opts[a] = "1"
            i += 1
            continue
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


def _rules(opts: dict[str, str]) -> MixRules:
    """The layer rules of --layer-cap / --max-run-mm (defaults: 0.12 mm, one capped layer)."""
    from slicing.mix_pattern import LAYER_CAP_MM, MixRules

    cap = _number(opts, "--layer-cap", LAYER_CAP_MM, 0.04, 0.4)
    run = (
        _number(opts, "--max-run-mm", 0.0, 0.0, 5.0) if "--max-run-mm" in opts else None
    )
    if run is not None and run < cap:
        raise UsageError(
            f"--max-run-mm {run:g} is below the layer cap {cap:g}: one layer is already {cap:g} mm thick",
            command=_CMD,
        )
    return MixRules(cap, run)


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


def _shelf_inputs(
    name: str, api_url: str | None, loaded: list[LoadedSlot]
) -> tuple[list[int], list[Spool], list[Spool]]:
    """(usable heads, loaded spools, shelf spools) of `name` from 3d-api: the loaded ones with their
    material and weight from the spool list, the shelf ones = in stock, on no printer head. A measured
    `filament_color` record replaces the Spoolman colour of its filament (read-only)."""
    from printops.client import ApiClient, ApiConfigError, ApiError
    from printops.color import parse_hex
    from slicing.mix_shelf import Spool

    try:
        api = ApiClient.from_config(api=api_url)
        heads_view = api.get(f"/v1/printers/{name}/slots")
        listed = api.get("/v1/spools")
    except (ApiConfigError, ApiError) as e:
        raise UsageError(
            f"cannot read the shelf of {name}: {e}",
            command=_CMD,
            remediation=["--shelf needs 3d-api: --printops-api URL or $PRINTOPS_API"],
        ) from e
    try:
        records = api.get(
            "/v1/calibrations", kind="filament_color", current="true", limit=1000
        ).get("calibrations")
    except ApiError:
        records = []  # no calibration engine: Spoolman colours only
    measured: dict[int, str] = {}
    for rec in records or []:
        fil = rec.get("filament") or {}
        hex_ = parse_hex((rec.get("values") or {}).get("hex"))
        if isinstance(fil.get("filament_id"), int) and hex_:
            measured.setdefault(fil["filament_id"], hex_)  # newest first
    heads = sorted(
        s["index"] + 1
        for s in heads_view.get("slots") or []
        if s.get("usable") and isinstance(s.get("index"), int)
    )
    views = {
        v["id"]: v for v in listed.get("spools") or [] if isinstance(v.get("id"), int)
    }

    def remaining(v: dict[str, Any]) -> float | None:
        r = v.get("remaining") or {}
        mid = r.get("mid_g") if isinstance(r, dict) else None
        return float(mid) if isinstance(mid, (int, float)) else None

    def spool(v: dict[str, Any], hex_: str | None, head: int | None) -> Spool | None:
        fid = v.get("filament_id") if isinstance(v.get("filament_id"), int) else None
        base = hex_ or parse_hex(v.get("color_hex"))
        if base is None:
            return None
        hex_now, source = (
            (measured[fid], "measured") if fid in measured else (base, "spoolman")
        )
        return Spool(
            v["id"],
            hex_now,
            str(v.get("name") or ""),
            str(v.get("material") or ""),
            fid,
            remaining(v),
            head,
            source,
        )

    loaded_out = []
    for ls in loaded:
        v = views.get(ls.spool_id) if ls.spool_id is not None else None
        if v is None:
            loaded_out.append(
                Spool(ls.spool_id, ls.hex, ls.name, "", ls.filament_id, None, ls.slot)
            )
        else:
            sp = spool(v, ls.hex, ls.slot)
            if sp is not None:
                loaded_out.append(sp)
    shelf = [
        sp
        for v in views.values()
        if not v.get("slot") and not v.get("archived")
        for sp in [spool(v, None, None)]
        if sp is not None
    ]
    shelf.sort(key=lambda s: s.spool_id or 0)
    return heads, loaded_out, shelf


def _shelf_lines(plan: ShelfPlan, printer: str, target: str) -> list[str]:
    base = plan.baseline
    lines = [
        f"shelf for {target} on {printer} (material {plan.material or 'any'}): loaded spools reach "
        f"ΔE {base.best.delta_e:.2f} ({base.best.pattern})"
    ]
    if not plan.options:
        lines.append("  no shelf spool gets closer: keep the loaded spools")
    for i, lo in enumerate(plan.options, 1):
        gone = {u.head: u for u in lo.unloads}
        steps = []
        for s in lo.loads:
            out = gone.get(s.head)
            label = f'spool {s.spool_id} "{s.name}" {s.hex}'
            where = f"T{s.head}" + (
                f', taking out spool {out.spool_id} "{out.name}"' if out else ""
            )
            steps.append(f"{label} on {where}")
        lines.append(
            f"  option {i}: load {'; '.join(steps)} -> ΔE {lo.best.delta_e:.2f} ({lo.best.pattern}), "
            f"was {base.best.delta_e:.2f}; {lo.swaps} swap(s)"
        )
        for s in lo.loads:
            out = gone.get(s.head)
            lines.append(
                f"    3d printops spool load {s.spool_id} {printer}/T{s.head}"
                + (" --replace-to LOCATION" if out else "")
            )
    for s, why in plan.excluded:
        lines.append(f"  left out: spool {s.spool_id} {s.name or s.hex}: {why}")
    return lines


@dataclass(frozen=True)
class _Plan:
    target: str
    slots: list[LoadedSlot]
    ranked: list[Candidate]
    layer: float
    count: int
    max_cycle: int
    rules: MixRules
    target_source: dict[str, Any] | None = None

    shelf: ShelfPlan | None = None


def _run_reader(
    deps: str, script: str, args: list[str], what: str
) -> tuple[int, dict[str, Any]]:
    """Run a lib/ reader script through cli.pyrun: (exit code, its JSON record). A reader that
    printed no JSON is a failure (exit 2: its arguments were refused; else it crashed)."""
    import json
    import subprocess

    from cli.pyrun import tool_argv

    proc = subprocess.run(  # noqa: S603 — argv from cli.pyrun
        tool_argv(deps, script, args), capture_output=True, text=True, check=False
    )
    try:
        record = json.loads(proc.stdout)
    except json.JSONDecodeError:
        record = None
    if not isinstance(record, dict):
        if proc.returncode == 2:
            raise UsageError(
                proc.stderr.strip() or f"{what}: the options are invalid", command=_CMD
            )
        raise GateFailure(
            f"{what} failed: {proc.stderr.strip() or 'the reader printed no result'}",
            command=_CMD,
        )
    return proc.returncode, record


def _photo_reading(opts: dict[str, str], image: Path) -> dict[str, Any]:
    """The `mix-target/1` record of the region of `image` per --box / --white / --no-wb /
    --white-level. numpy and Pillow are needed, so the reading runs through cli.pyrun."""
    from slicing.mix_target import DEFAULT_WHITE_LEVEL

    if "--box" not in opts:
        raise UsageError(
            "--box x0,y0,x1,y1 (the region to match, in pixels) is required",
            command=_CMD,
            remediation=[
                "3d mix target mockup.jpg --box 410,220,470,300 --white 20,30,80,90"
            ],
        )
    if ("--white" in opts) == ("--no-wb" in opts):
        raise UsageError(
            "pass exactly one of --white x0,y0,x1,y1 (a photo: white balance on that region) "
            "and --no-wb (a render: no colour cast)",
            command=_CMD,
        )
    if "--no-wb" in opts and "--white-level" in opts:
        raise UsageError(
            "--white-level needs --white (--no-wb skips the balance)", command=_CMD
        )
    level = _integer(opts, "--white-level", DEFAULT_WHITE_LEVEL, 1, 255)
    if not image.is_file():
        raise InputNotFound(str(image), command=_CMD)
    # `--opt=value` and `--` keep a value or a file name that starts with "-" from reading as an option
    args = [f"--box={opts['--box']}", f"--white-level={level}"]
    if "--white" in opts:
        args.append(f"--white={opts['--white']}")
    args += ["--", str(image)]
    code, record = _run_reader("numpy,pillow", "mix_target_cli.py", args, f"reading {image}")
    if code != 0:
        raise UsageError(
            str(record.get("error", "the target region cannot be read")), command=_CMD
        )
    return record


def _resolve_target(opts: dict[str, str]) -> tuple[str, dict[str, Any] | None]:
    """--target HEX, or --target-photo IMAGE with its region: (hex, the manifest's target_source)."""
    from printops.color import parse_hex
    from slicing.mix_target import source_of

    photo_flags = {"--box", "--white", "--white-level", "--no-wb"} & opts.keys()
    if "--target-photo" in opts:
        if "--target" in opts:
            raise UsageError("pass --target or --target-photo, not both", command=_CMD)
        record = _photo_reading(opts, Path(opts["--target-photo"]))
        return record["target"], source_of(record)
    if photo_flags:
        raise UsageError(
            f"{', '.join(sorted(photo_flags))} only goes with --target-photo IMAGE",
            command=_CMD,
        )
    target = parse_hex(opts.get("--target"))
    if target is None:
        raise UsageError(
            "--target #RRGGBB or --target-photo IMAGE --box x0,y0,x1,y1 is required",
            command=_CMD,
        )
    return target, None


def _source_line(src: dict[str, Any]) -> str:
    wb = (
        f"white balance on {src['white_box']} to {src['white_level']}"
        if src["white_balance"]
        else "no white balance"
    )
    return (
        f"  target from {src['kind']} {src['image']} (sha256 {src['sha256'][:12]}) "
        f"box {src['box']}, {wb}"
    )


def _plan(opts: dict[str, str], layer: float | None = None) -> _Plan:
    """What recipes and tester share; `layer` overrides --layer (a base project's own)."""
    from slicing.mix_model import MixModelError, Slot, rank

    target, target_source = _resolve_target(opts)
    if ("--slots" in opts) == ("--printer" in opts):
        raise UsageError("pass exactly one of --slots and --printer", command=_CMD)
    slots = (
        _parse_slots(opts["--slots"])
        if "--slots" in opts
        else _printer_slots(opts["--printer"], opts.get("--printops-api"))
    )
    shelf_opts = ("--max-swaps", "--material", "--min-g")
    if "--shelf" not in opts and any(f in opts for f in shelf_opts):
        raise UsageError(f"{', '.join(shelf_opts)} need --shelf", command=_CMD)
    if "--shelf" in opts and "--printer" not in opts:
        raise UsageError(
            "--shelf reads the spools in stock from 3d-api: pass --printer NAME, not --slots",
            command=_CMD,
        )
    if layer is None:
        layer = _number(opts, "--layer", 0.12, 0.04, 0.4)
    if abs(round(layer, 4) - layer) > 1e-9:
        raise UsageError(f"layer {layer!r} must have at most 4 decimals", command=_CMD)
    count = _integer(opts, "--count", 6, 1, 20)
    max_cycle = _integer(opts, "--max-cycle", 3, 1, 12)
    rules = _rules(opts)
    shelf = None
    if "--shelf" in opts:
        from slicing.mix_shelf import MIN_SPOOL_G, plan as shelf_plan

        heads, loaded, stock = _shelf_inputs(
            opts["--printer"], opts.get("--printops-api"), slots
        )
        shelf = shelf_plan(
            target,
            loaded,
            stock,
            heads,
            max_swaps=_integer(opts, "--max-swaps", 2, 0, 4),
            max_cycle=max_cycle,
            layer_h=layer,
            rules=rules,
            material=opts.get("--material"),
            min_g=_number(opts, "--min-g", MIN_SPOOL_G, 0.0, 1000.0),
        )
        from slicing.mix_tester import LoadedSlot as _Loaded

        slots = [
            _Loaded(s.head or 0, s.hex, s.name, s.spool_id, s.filament_id)
            for s in shelf.chosen.placed
        ]
    try:
        ranked = rank(
            target,
            [Slot(s.slot, s.hex, s.name) for s in slots],
            max_cycle=max_cycle,
            layer_h=layer,
            rules=rules,
        )
    except MixModelError as e:
        raise UsageError(str(e), command=_CMD) from e
    return _Plan(target, slots, ranked, layer, count, max_cycle, rules, target_source, shelf)


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


def _gcode(argv: list[str]) -> int:
    import json

    from slicing.head_sequence import ProjectError
    from slicing.mix_gcode import check, format_lines

    if not argv or argv[0].startswith("-"):
        raise UsageError(
            "gcode takes the G-code file first",
            command=_CMD,
            remediation=["3d mix gcode out/plate.gcode --project out/plate.3mf"],
        )
    path, rest = Path(argv[0]), argv[1:]
    opts, as_json = _options(rest, {"--project", "--layer-cap", "--max-run-mm"})
    if not path.is_file():
        raise InputNotFound(str(path), command=_CMD)
    project = opts.get("--project")
    if project is not None and not Path(project).is_file():
        raise InputNotFound(project, command=_CMD)
    rules = _rules(opts)
    try:
        report = check(str(path), project, rules)
    except ProjectError as exc:
        raise UsageError(
            str(exc),
            command=_CMD,
            remediation=["Pass the plate project .3mf saved by Snapmaker Orca / Orca."],
        ) from exc
    if as_json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(
            f"mix gcode {path.name}: {report.status} (cap {rules.layer_cap_mm:g} mm, "
            f"run limit {rules.run_limit_mm:g} mm)"
        )
        for line in format_lines(report) or ["no mixed object found"]:
            print(f"  {line}")
    if report.failures:
        raise GateFailure(
            f"mix gcode: {report.failures} mixed object(s) break the layer rules",
            command=_CMD,
            silent=True,
        )
    return 0


def _pattern(argv: list[str]) -> int:
    import json

    from slicing.mix_pattern import analyse_pattern

    if not argv or argv[0].startswith("-"):
        raise UsageError(
            "pattern takes the layer pattern first",
            command=_CMD,
            remediation=["3d mix pattern GWG", "3d mix pattern 431 --layer 0.12"],
        )
    text, rest = argv[0], argv[1:]
    opts, as_json = _options(
        rest, {"--layer", "--heights", "--layer-cap", "--max-run-mm"}
    )
    rules = _rules(opts)
    if "--heights" in opts and "--layer" in opts:
        raise UsageError("pass --layer or --heights, not both", command=_CMD)
    thickness: float | list[float]
    if "--heights" in opts:
        try:
            thickness = [float(x) for x in opts["--heights"].split(",")]
        except ValueError:
            raise UsageError(
                f"--heights {opts['--heights']!r} is not a comma list of numbers",
                command=_CMD,
            ) from None
        if len(thickness) != len(text):
            raise UsageError(
                f"--heights lists {len(thickness)} layers for a pattern of {len(text)}",
                command=_CMD,
            )
    else:
        thickness = _number(opts, "--layer", rules.layer_cap_mm, 0.01, 1.0)
    try:
        report = analyse_pattern(text, thickness, rules)
    except ValueError as e:
        raise UsageError(str(e), command=_CMD) from e
    if as_json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        verdict = "PASS" if report.ok else "FAIL"
        kind = "repeating" if report.mixed else "single colour (no mix)"
        print(
            f"mix pattern {text}: {verdict} ({kind}, cap {rules.layer_cap_mm:g} mm, run limit {rules.run_limit_mm:g} mm)"
        )
        if report.minor:
            print(f"  minor colour(s): {', '.join(report.minor)}")
        for v in report.violations:
            print(f"  FAIL {v.rule}: {v.detail}")
    if not report.ok:
        raise GateFailure(
            f"mix pattern {text}: {len(report.violations)} rule violation(s)",
            command=_CMD,
            silent=True,
        )
    return 0


def _readout(argv: list[str]) -> int:
    import json

    from slicing.mix_target import DEFAULT_WHITE_LEVEL
    from slicing.mix_tester import validate_manifest

    if not argv or argv[0].startswith("-"):
        raise UsageError(
            "readout takes the photo first",
            command=_CMD,
            remediation=[
                "3d mix readout phone.jpg --manifest out/wall.mix.json -o out/wall-readout"
            ],
        )
    photo = Path(argv[0])
    opts, as_json = _options(
        argv[1:], {"--manifest", "-o", "--white", "--white-level", "--no-qr-check"}
    )
    for flag, what in (
        ("--manifest", "the tester manifest NAME.mix.json"),
        ("-o", "OUT/NAME"),
    ):
        if flag not in opts:
            raise UsageError(f"readout needs {flag} {what}", command=_CMD)
    if Path(opts["-o"]).suffix.lower() in (".json", ".png"):
        raise UsageError(
            f"-o {opts['-o']}: give the path without an extension (NAME.readout.json, NAME.readout.png and NAME.rectified.png are written)",
            command=_CMD,
        )
    white = opts.get("--white", "card")
    if white != "card" and not white.isdecimal():
        raise UsageError(
            f"--white {white!r}: card, or the number of a pure reference swatch",
            command=_CMD,
        )
    if white != "card" and "--white-level" in opts:
        raise UsageError("--white-level goes with --white card", command=_CMD)
    level = _integer(opts, "--white-level", DEFAULT_WHITE_LEVEL, 1, 255)
    mpath = Path(opts["--manifest"])
    if not mpath.is_file():
        raise InputNotFound(str(mpath), command=_CMD)
    if not photo.is_file():
        raise InputNotFound(str(photo), command=_CMD)
    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise UsageError(f"{mpath} is not JSON: {e}", command=_CMD) from e
    problems = validate_manifest(manifest)
    if problems:
        raise UsageError(
            f"{mpath} is not a valid tester manifest: {'; '.join(problems)}",
            command=_CMD,
        )
    if "readout" not in manifest:
        raise UsageError(
            f"{mpath} has no readout card layout (a tester written before #416)",
            command=_CMD,
            remediation=[
                "Write the tester again with `3d mix tester`: it now also writes NAME.card.png."
            ],
        )
    args = [
        f"--manifest={mpath}",
        f"--out={opts['-o']}",
        f"--white={white}",
        f"--white-level={level}",
    ]
    if "--no-qr-check" in opts:
        args.append("--no-qr-check")
    args += ["--", str(photo)]
    code, record = _run_reader(
        "numpy,pillow,opencv-python-headless", "mix_readout_cli.py", args, f"reading {photo}"
    )
    if code == 2:
        raise UsageError(str(record.get("error", "bad readout arguments")), command=_CMD)
    if code != 0:
        raise GateFailure(
            f"{photo}: {record.get('error', 'the photo cannot be read as this tester card')}",
            command=_CMD,
            remediation=["Fix what the message says and shoot the card again."],
        )
    fid, qr, wb = record["fiducials"], record["qr"], record["white"]
    if wb["mode"] == "card":
        wb_line = f"card paper ({wb['model']} over {len(wb['regions'])} regions) mapped to {wb['level']}"
    else:
        wb_line = f"reference swatch {wb['number']} mapped to its spool colour {wb['maps_to']}"
    lines = [
        f"mix readout {record['manifest']['file']} (manifest {record['manifest']['id']}): "
        f"{record['summary']['swatches']} swatches from {Path(record['photo']['file']).name}",
        f"  fiducials {fid['found']}/{fid['of']} (rms {fid['rms_mm']:.2f} mm, {fid['photo_px_per_mm']:.1f} px/mm in the photo), QR {qr['status']}",
        f"  white balance: {wb_line}",
        f"  {'n':>2}  {'swatch':<10} {'predicted':<9}  {'measured':<9}  ΔE pred  ΔE target",
    ]
    for r in record["swatches"]:
        name = r["pattern"] or f"T{r['slot']} pure"
        lines.append(
            f"  {r['number']:>2}  {name:<10} {r['predicted_hex']:<9}  {r['measured_hex']:<9}  "
            f"{r['delta_e_predicted']:>7.2f}  {r['delta_e_target']:>9.2f}"
        )
    best = record["summary"].get("closest_to_target")
    if best:
        lines.append(
            f"  closest to the target {record['manifest']['target']}: {best['number']} ({best['pattern']}) "
            f"{best['measured_hex']}, ΔE {best['delta_e_target']:.2f}"
        )
    lines += [f"  warning: {w}" for w in record["warnings"]]
    out_dir = Path(opts["-o"]).parent
    lines += [f"  {k}: {out_dir / v}" for k, v in record["files"].items()]
    _emit(record, lines, as_json)
    return 0


def _target_cmd(argv: list[str]) -> int:
    if not argv or argv[0].startswith("-"):
        raise UsageError(
            "target takes the image first",
            command=_CMD,
            remediation=[
                "3d mix target mockup.jpg --box 410,220,470,300 --white 20,30,80,90"
            ],
        )
    opts, as_json = _options(argv[1:], {"--box", "--white", "--white-level", "--no-wb"})
    d = _photo_reading(opts, Path(argv[0]))
    lines = [
        f"mix target {d['target']}  ({d['kind']} {d['image']}, {d['image_size'][0]}x{d['image_size'][1]}, "
        f"box {d['box']}, {d['samples']} px)",
        f"  Lab {d['lab'][0]:.1f} {d['lab'][1]:.1f} {d['lab'][2]:.1f} (median of the box)",
        (
            f"  white balance: box {d['white_box']} median RGB {tuple(d['white_rgb'])} -> {d['white_level']}, "
            f"gain {d['gain'][0]:.3f} {d['gain'][1]:.3f} {d['gain'][2]:.3f} (linear light)"
            if d["white_balance"]
            else "  white balance: none (render)"
        ),
        f"  sha256 {d['sha256']}",
    ]
    _emit(d, lines, as_json)
    return 0


def _recipes(argv: list[str]) -> int:
    from slicing.mix_model import pick

    opts, as_json = _options(
        argv, _FLAGS - {"--height", "--base-3mf", "-o"}, _BOOL_FLAGS
    )
    p = _plan(opts)
    target, slots, layer, max_cycle = p.target, p.slots, p.layer, p.max_cycle
    chosen = [c.to_dict() for c in pick(p.ranked, p.count)]
    summary: dict[str, Any] = {
        "target": target,
        "layer_height": layer,
        "layer_cap_mm": p.rules.layer_cap_mm,
        "max_run_mm": p.rules.run_limit_mm,
        "max_cycle": max_cycle,
        "slots": [s.to_dict() for s in slots],
        "candidates": [{"number": i + 1, **c} for i, c in enumerate(chosen)],
    }
    if p.target_source is not None:
        summary["target_source"] = p.target_source
    if p.shelf is not None:
        summary["shelf"] = p.shelf.to_dict()
    lines = [
        f"mix recipes for {target} (layer {layer:g} mm, cycle ≤ {max_cycle}, "
        f"no colour band over {p.rules.run_limit_mm:g} mm, "
        + ", ".join(f"T{s.slot} {s.hex}" for s in slots)
        + ")"
    ]
    if p.target_source is not None:
        lines.append(_source_line(p.target_source))
    lines += [_cand_line(i + 1, c) for i, c in enumerate(chosen)]
    if p.shelf is not None:
        lines = _shelf_lines(p.shelf, opts["--printer"], target) + lines
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

    opts, as_json = _options(argv, _FLAGS, _BOOL_FLAGS)
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
            target_source=p.target_source,
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
    if p.target_source is not None:
        summary["target_source"] = p.target_source
    lines = [
        f"mix tester for {summary['target']}: {len(mixes)} mixes + {len(summary['references'])} references, labels on T{summary['label_slot']}",
        *(f"  {k}: {v}" for k, v in summary["files"].items()),
    ]
    if p.target_source is not None:
        lines.append(_source_line(p.target_source))
    lines += [
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
    if p.shelf is not None:
        summary["shelf"] = p.shelf.to_dict()
        lines = (
            _shelf_lines(p.shelf, opts["--printer"], p.target)
            + (
                ["the plate is built for option 1: load its spools first"]
                if p.shelf.options
                else []
            )
            + lines
        )
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
    if sub == "profile":
        return _profile(rest)
    if sub == "gcode":
        return _gcode(rest)
    if sub == "pattern":
        return _pattern(rest)
    if sub == "target":
        return _target_cmd(rest)
    if sub == "recipes":
        return _recipes(rest)
    if sub == "tester":
        return _tester(rest)
    if sub == "readout":
        return _readout(rest)
    raise InvalidArgument(
        "subcommand",
        sub,
        ["check", "gcode", "pattern", "profile", "readout", "recipes", "target", "tester"],
        command=_CMD,
    )


COMMAND = Command(
    name="mix",
    group="SLICING",
    summary="colour mixing by layers: rank recipes, build a tester plate, lint mixed parts",
    usage=USAGE,
    run=run,
)
