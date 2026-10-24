"""3d gcode-mix-gate — did a multi-colour G-code print the colours it was meant to?

WHAT: the gates of a multi-colour (mixed-filament) print, judged on the extrusion itself — a mixed volume
  must print its pattern tool on every layer number (each layer on its OWN outer-wall evidence, >= 1.0 mm),
  a pure volume one tool, an accent volume its accent layers; the M600 pause, the heads and prime tower, an
  optional pocket void, layer heights and footer settings; `--mix-teeth` / `--cavity-teeth` re-run the gates on
  a deliberately wrong print and FAIL when the gate does not reject it (a vacuous gate).
WHY: issue https://github.com/alex-mextner/3d-cli/issues/551 (the Chekhov cat phone stand on the U1): the slicer
  indexes the pattern by layer number, a recipe that validates can print shifted, and only the G-code says
  what was printed.
INVARIANTS: stdlib-only at import time; the gate logic (numpy / scipy / trimesh) and the option parsing live in
  lib/gcode_mix_gate.py, run through cli.pyrun with those deps; the G-code is streamed layer by layer
  (lib/gcode_scan.py). Never contacts a printer.
"""

from __future__ import annotations

from cli.pyrun import exec_tool
from cli.registry import Command

NAME = "gcode-mix-gate"

USAGE = """3d gcode-mix-gate FILE.gcode --config GATES.json [--parts DIR] [options]
  Gate a multi-colour G-code on what was printed. Never contacts a printer.

  colour volumes  every outer-wall point is attributed to the part STL it lies on (needs --parts DIR, bed
                  coordinates): pure volume = one tool; mixed volume = the pattern's tool at layer number n
                  (pattern index = layer number, offset 0); accent volume = accent tool on exactly its pattern
                  layers; thin volume = read from the extrusion inside it. A thick mixed volume is judged per
                  layer on its OWN outer wall (>= 1.0 mm unambiguous = "solid", zero tolerance; fringe layers
                  re-read at a small margin, <= 5 % off), plus no tool stripe thicker than one layer <= 0.10 mm
  stripe contrast a mixed region's visible banding: its layers' colours at the REAL heights, blurred as the eye
                  sees them (1 arcmin at --view-cm), worst CIEDE2000 vs their mean; > --max-stripe-de FAIL,
                  > --warn-stripe-de WARN (GKGKWK 22.9, GKW 2.6, GW 0.2 at 0.08 mm). Vertical wall: a shallow
                  surface shows its bands wider than modelled. Colours: `filament_colours` or the G-code's
  pause           `pause` in the config: exactly one M600, in the layer with that top Z, before it extrudes;
                  without `pause` no M600 at all
  tools           heads used (footer `filament used [g]`) == `heads_slots`; prime tower printed iff expected
                  (`prime_tower`, default: more than one head); tool changes and grams per tool / feature
  cavity          --cavity: the pocket outline is void between floor and pause, open under the roof, closed above
  layers          --layer-heights MIN:MAX: heights inside the window (first layer exempt), Z steps == ;HEIGHT,
                  neighbours differ <= --max-layer-step
  settings        `expect_settings` in the config: footer `; key = value` equals
  teeth           --mix-teeth: real print passes, the pattern shifted by one layer FAILs, one solid layer printed
                  with a wrong tool FAILs, for every thick mixed volume; --cavity-teeth: pocket outline moved over
                  solid body FAILs, pause 8 mm too high FAILs. A control that does not FAIL = vacuous gate = exit 1

Config (JSON; slots are 1-based, slot n = tool T(n-1); the manifest of a build script works as is):
  {"parts": [{"name": "iris", "stl": "2_iris.stl", "assign": {"mix": "121242", "mix_src": "GKGKWK"}},
             {"name": "base", "assign": {"slot": 2}},
             {"name": "hair", "assign": {"mix": "11211", "accent": 2}}],
   "default_slot": 2, "heads_slots": [1, 2, 3, 4], "prime_tower": true,
   "filament_colours": ["#1B5E20", "#080A0D", "#E72F1D", "#FFFFFF"],
   "pause": {"print_z": 37.2392, "last_layer_before": {"top": 36.9994}},
   "shift": [135.0, 134.5, 0.0], "expect_settings": {"wall_loops": "4"}, "parts_dir": "work/parts"}
  `stl` is optional: without it the files of --parts DIR, sorted by name, are the parts in config order.
  default_slot = the tool that may print beside any mix (pupil slit, edge fringe). Cavity JSON:
  {"plan_mm": {"x": [-20, 20], "y": [-22, 14], "corner_r": 6}, "floor_z": 1.2, "pause_z": 37.0, "roof_ridge_z": 48.0}

Options:
  --config FILE          gate config (required)
  --parts DIR            part STLs in bed coordinates (wins over config `parts_dir`)
  --default-slot N       the default tool's slot (wins over config `default_slot`)
  --cavity FILE          pocket outline JSON; object coordinates + config `shift`
  --layer-heights A:B    layer-height window in mm, e.g. 0.08:0.32
  --max-layer-step MM    largest height step between neighbouring layers (default 0.08)
  --margin MM            strict attribution margin from a colour boundary (default 0.9)
  --bed-tol MM           largest distance of an outer-wall point to its part surface (default 0.45)
  --min-evidence MM      own outer wall a layer needs to be judged strictly (default 1.0)
  --max-stripe MM        thickest single-tool stripe of a mix (default 0.10)
  --max-stripe-de DE     worst stripe contrast (dE00) of a mixed region before FAIL (default 3)
  --warn-stripe-de DE    ... before WARN (default 2, at most --max-stripe-de)
  --view-cm CM           viewing distance of the stripe contrast (default 30)
  --mix-teeth            run the colour-volume controls
  --teeth-volume NAME    restrict --mix-teeth to this volume (repeatable)
  --cavity-teeth         run the cavity / pause controls (needs --cavity)
  --json                 JSON report on stdout

Exit: 0 PASS, 1 FAIL (a gate, or a teeth control that did not trip), 2 usage error or bad / missing input.

Examples:
  3d gcode-mix-gate out/cat.gcode --config out/cat.manifest.json --parts work/cat.parts --default-slot 2
  3d gcode-mix-gate out/cat.gcode --config gates.json --parts parts --mix-teeth --teeth-volume iris
  3d gcode-mix-gate out/cat.gcode --config gates.json --cavity cavity.json --cavity-teeth --layer-heights 0.08:0.32
  3d gcode-mix-gate out/cat.gcode --config gates.json --parts parts --json | jq '.gates[] | select(.status=="FAIL")'"""

DEPS = "numpy,scipy,trimesh,rtree"


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    return exec_tool(DEPS, "gcode_mix_gate.py", argv)


COMMAND = Command(
    name=NAME,
    group="SLICING",
    summary="gate a multi-colour G-code on what was printed: mix pattern per layer, pause, heads, cavity; with teeth self-tests",
    usage=USAGE,
    run=run,
)
