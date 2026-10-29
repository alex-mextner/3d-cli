"""3d assembly — check a whole assembly: blocking intersections, mate clearance, insertion paths, ergonomics, 3MF positions.

WHAT: reads a declarative assembly spec (`assembly.yaml`: parts with a role and a pose, mates with a fit type and an
  insertion axis, loads) or a positioned 3MF and reports, per pair and per mate with `--json` (schema `3d.assembly/1`):
    check       blocking intersections (pair, depth, xyz, offending faces, designed vs defect) + clearance against the
                resolved fit (`3d fits`: table x printer x material), PNG section highlights with `--viz DIR`;
    sweep       move a part along its insertion axis (any direction) and report the first collision, snap deflection
                vs the hook's flex limit (insert / remove);
    order       multi-step assembly order / disassembly feasibility (graph search over straight-line moves);
    ergonomics  finger / tool access to every snap hook and fastener, hook flex clearance, removal path, forces;
    explode     exploded view PNG / USDZ, every part in its own colour with a legend;
    bom         parts (role, reference source, size) and fasteners; stand-ins are labelled;
    position    lay the printed parts on plates and write a project 3MF that `3d arrange` and `3d retarget` accept;
    show        the parsed spec and the colour legend.

WHY: one insert/socket pair at a time (`fit-check`) cannot tell whether a case, its board and its snap hooks go together,
  come apart and can be operated by hand. This is the whole-assembly check with the fit table behind every number.

ACCESSED VIA: `3d assembly <sub> TARGET [options]`. Thin shell: the geometry runs in `lib/assembly_cli.py` +
  `lib/assembly/` through `cli.pyrun` (trimesh, manifold3d, numpy, Pillow), so this module stays stdlib-only.
"""

from __future__ import annotations

from cli.pyrun import run_tool
from cli.registry import Command
from errors import UsageError

SUBS = ("check", "sweep", "order", "ergonomics", "explode", "bom", "position", "show")
DEPS = "trimesh,manifold3d,numpy,scipy,rtree,pyyaml,pillow,networkx"

USAGE = """3d assembly <check|sweep|order|ergonomics|explode|bom|position|show> TARGET [options]
  Whole-assembly checks over a declared spec. TARGET is an assembly spec (assembly.yaml / .json), a project
  directory holding assembly.yaml, or (check / sweep / order / ergonomics / explode / bom / show) a 3MF written by
  `position`, which is then verified as printed. Every subcommand takes --json (schema `3d.assembly/1`, `kind`
  names the report). Exit 0 ok, 1 a verdict failed (blocking intersection, blocked path, no access), 2 bad input,
  127 missing dependency.

Subcommands:
  check TARGET [--mate NAME]... [--printer P] [--material M] [--viz DIR] [--view V] [--mem-limit-mb N]
      per pair: overlap regions classified contact / allowed (declared snap or press interference within the fit
      table tolerance) / blocking (pair, depth, xyz, offending face, suggested clearance change); per mate: the
      measured gap against the resolved fit with its provenance. --viz DIR writes a section PNG per overlap and an
      overview, parts in distinct colours with a legend.
      Example: 3d assembly check assembly.yaml --printer k1se --material "Rapid PLA+" --viz out/check
  sweep TARGET --mate NAME [--direction insert|remove|both] [--step MM] [--distance MM] [--ignore PART]...
      move the mate's part along its declared insertion axis (any direction, or a hinge angle) in steps and report the
      first contact and the first blocking position; for snap mates the peak interference, the implied tip deflection
      vs the hook's flex limit and the force estimate. Sampled, not continuous: the resolution is in the report.
      Example: 3d assembly sweep assembly.yaml --mate lid --direction remove --step 0.05
  order TARGET [--base PART] [--step MM] [--max-travel MM]
      find an assembly order by disassembly search: which part or declared group can leave along a straight line, one
      step at a time; names what blocks each part when there is none.
      Example: 3d assembly order assembly.yaml --base case_back
  ergonomics TARGET [--mate NAME]... [--finger MM] [--nail MM] [--tool MM] [--reach MM]
      finger pad (default 12 mm) / nail (6 mm) / tool (3 mm) probes must reach each snap hook release point and
      fastener; the hook arm must be able to flex clear of neighbours; removal path; snap forces when available.
      Example: 3d assembly ergonomics assembly.yaml --finger 14 --nail 5
  explode TARGET [--viz OUT.png] [--usdz OUT.usdz] [--view V] [--factor F]
      exploded view: every mover is pulled back along its insertion axis; distinct colours and a legend.
      Example: 3d assembly explode assembly.yaml --viz out/exploded.png --usdz out/exploded.usdz --factor 1.5
  bom TARGET
      parts with role, size and reference source (URL + licence); stand-ins are labelled; fasteners and mates.
      Example: 3d assembly bom assembly.yaml --json
  position TARGET --3mf OUT.3mf [--bed MM] [--gap MM] [--margin MM] [--base-project BASE.3mf] [--printer P]
           [--process NAME] [--filament NAME] [--preset-dir DIR]... [--set K=V]... [--include-non-printable]
           [--reason TEXT] [--yes]
      orient each printed part (print.face / print.rotate / auto), pack it on bed-sized plates (the `3d arrange`
      packer), tag every object with its role, colour it, and store the poses + mates in the 3MF. Vitamins,
      reference models and stand-ins are never placed on a plate. Dry run (the plan) unless --yes.
      Example: 3d assembly position assembly.yaml --3mf out/plates.3mf --base-project print/mole.3mf --yes
  show TARGET
      the parsed spec, resolved context and the colour legend.
      Example: 3d assembly show assembly.yaml

Options:
  --mate NAME        restrict to one mate (repeatable). Example: 3d assembly check assembly.yaml --mate lid
  --printer P        printer for the fit lookup (fleet key or model); with `position`, retarget the project to it
                     (needs --base-project). Example: 3d assembly check assembly.yaml --printer k1se
  --material M      material for the fit lookup. Example: 3d assembly check assembly.yaml --material PETG
  --viz DIR|FILE     PNG output: a directory for `check`, a file for `explode`. Example: 3d assembly check assembly.yaml --viz out/
  --view V           iso | front | back | left | right | top. Example: 3d assembly explode assembly.yaml --viz o.png --view front
  --mem-limit-mb N   hard address-space cap for the run (one pair is built at a time anyway).
                     Example: 3d assembly check assembly.yaml --mem-limit-mb 3072
  --direction D      sweep direction: insert | remove | both (default both). Example: 3d assembly sweep a.yaml --mate lid --direction insert
  --step MM          sweep sampling step (mm; degrees for a hinge), default settings.step_mm 0.1.
                     Example: 3d assembly sweep assembly.yaml --mate lid --step 0.05
  --distance MM      override the declared sweep distance. Example: 3d assembly sweep assembly.yaml --mate lid --distance 12
  --ignore PART      leave a part out of a sweep's obstacles (repeatable). Example: 3d assembly sweep a.yaml --mate lid --ignore wires
  --base PART        the part `order` keeps fixed (default: the biggest printed part). Example: 3d assembly order a.yaml --base case_back
  --max-travel MM    how far `order` tries to move a part to clear the rest. Example: 3d assembly order a.yaml --max-travel 80
  --finger MM        finger pad probe diameter (default 12). Example: 3d assembly ergonomics a.yaml --finger 14
  --nail MM          fingernail probe diameter (default 6). Example: 3d assembly ergonomics a.yaml --nail 5
  --tool MM          tool / driver probe diameter (default 3). Example: 3d assembly ergonomics a.yaml --tool 4
  --reach MM         how far outside a target the probes are tried (default 40). Example: 3d assembly ergonomics a.yaml --reach 60
  --factor F         explode distance multiplier (default 1). Example: 3d assembly explode a.yaml --viz o.png --factor 2
  --usdz FILE        also write a coloured USDZ (needs usd-core) + a legend sidecar. Example: 3d assembly explode a.yaml --usdz out/x.usdz
  --3mf FILE         `position` output project. Example: 3d assembly position a.yaml --3mf out/plates.3mf --yes
  --bed MM           square bed size for `position` (default 220). Example: 3d assembly position a.yaml --3mf p.3mf --bed 256
  --gap MM           clearance between footprints (default 6). Example: 3d assembly position a.yaml --3mf p.3mf --gap 4
  --margin MM        keep-out from each bed edge (default 8). Example: 3d assembly position a.yaml --3mf p.3mf --margin 5
  --base-project F   an Orca-family project whose slicer settings are carried. Example: 3d assembly position a.yaml --3mf p.3mf --base-project old.3mf
  --process NAME     process preset for the retarget. Example: 3d assembly position a.yaml --3mf p.3mf --base-project o.3mf --printer k1se --process "0.20mm Standard @Creality K1 SE"
  --filament NAME    filament preset for the retarget. Example: 3d assembly position a.yaml --3mf p.3mf --base-project o.3mf --printer k1se --filament "Generic PLA @K1"
  --preset-dir DIR   extra preset directory for the retarget (repeatable). Example: 3d assembly position a.yaml --3mf p.3mf --base-project o.3mf --printer k1se --preset-dir ./presets
  --set K=V          override one project setting after the retarget (repeatable). Example: 3d assembly position a.yaml --3mf p.3mf --base-project o.3mf --printer k1se --set wall_loops=3
  --include-non-printable
                     override the print guardrail: vitamins / references / stand-ins ARE placed (logged).
                     Example: 3d assembly position a.yaml --3mf p.3mf --include-non-printable --reason "fit test" --yes
  --reason TEXT      why the override (logged with it). Example: 3d assembly position a.yaml --3mf p.3mf --include-non-printable --reason "fit test" --yes
  --yes              write the 3MF (position is a dry run without it). Example: 3d assembly position a.yaml --3mf p.3mf --yes
  --json             one JSON document, schema `3d.assembly/1`. Example: 3d assembly check assembly.yaml --json | jq .summary

Spec: docs/commands/assembly.md (parts, mates, snap hooks, access, zones, loads)."""


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if argv[0] not in SUBS:
        raise UsageError(
            f"unknown subcommand '{argv[0]}'",
            command="assembly",
            remediation=[f"Accepted: {', '.join(SUBS)}. Run `3d assembly --help`."],
        )
    if len(argv) > 1 and argv[1] in ("-h", "--help"):
        print(USAGE)
        return 0
    if len(argv) < 2 or argv[1].startswith("-"):
        raise UsageError(
            f"`3d assembly {argv[0]}` needs a TARGET (assembly spec, project directory or positioned 3MF)",
            command="assembly",
            remediation=[f"Example: 3d assembly {argv[0]} assembly.yaml"],
        )
    return run_tool(DEPS, "assembly_cli.py", argv)


COMMAND = Command(
    name="assembly",
    group="QA & GATES",
    summary="whole-assembly checks: blocking intersections, mate clearance, insertion paths, ergonomics, 3MF positions",
    usage=USAGE,
    run=run,
)
