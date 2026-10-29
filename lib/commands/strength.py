"""3d strength — structural check of an assembled model: closed-form screens and linear-elastic FEA."""

from __future__ import annotations

from cli.pyrun import exec_tool
from cli.registry import Command

DEPS = "numpy,scipy,trimesh,manifold3d,rtree,pillow,pyyaml"
FEA_DEPS = f"{DEPS},scikit-fem,pytetwild"  # the closed-form beam and hook paths need no FEA stack

USAGE = """3d strength <part.scad|.stl|.3mf | 3d.yaml | project-dir> --material NAME [--load NEWTONS] [options]
3d strength --hook t=T,L=L,w=W,y=Y --material NAME [--orientation SPEC] [--arm-axis X|Y|Z] [options]
  Real structural check of the ASSEMBLED model. `--method beam` is a closed-form screen
  (label ANALYTICAL_SCREEN, one part); `--method fea` is a linear-elastic finite-element solve on
  tetrahedral meshes of every body (label FEA_LINEAR) with tied / frictionless contact between mates.
  `auto` (default) runs FEA when scikit-fem + pytetwild are installed (`uv sync --extra fea`).
  FDM anisotropy comes from the material card and the print orientation. Creep, fatigue and
  plasticity are NOT evaluated and are listed as unverified in every report.

Load cases (one of):
  --load NEWTONS           a classic fixture on a single file (see --fixture)
  3d.yaml `loads:`         anchored force / pressure / displacement / gravity / fixed supports; input is the
                           project (parts as bodies, `mates:` as contacts) or --loads 3d.yaml for a file

Options:
  --material NAME          default material for bodies without their own `material:` (see `3d materials list`)
  --axis X|Y|Z|-X|-Y|-Z    load direction for --fixture (default: Z)
  --fixture TYPE           cantilever | simple (3-point bend) | compression (default: cantilever)
  --span-axis X|Y|Z        beam axis for cantilever/simple (default: longest extent off the load axis)
  --method M               auto | beam | fea (default: auto)
  --orientation SPEC       print orientation: z-up (default) | x-up | y-up | flat | rotate=RX,RY,RZ | FILE.3mf | auto
  --infill PCT             sparse core: 20 or 20% (default: solid); --walls N (3) and --nozzle MM (0.4) set the shell
  --safety-factor N        required safety factor (default: 2); PASS >= N, WEAK 1..N, FAIL < 1
  --repeated-use           snap-fit strain limit x0.6 (frequent opening, Bayer Table 2)
  --hook SPEC              closed-form snap hook, no file: t=1.0,L=7,w=6,y=0.2[,taper=0.5,lead=30,retention=45,mu=0.4]
  --arm-axis X|Y|Z         bending fibre direction of the --hook arm (default: Z)
  --order 1|2              FEA element order (default: 2, quadratic tets)
  --mesh-size MM           target tet edge length;  --max-elements N  total tet budget (default 6000, order 1: 40000)
  --contact tied|frictionless|none   contact for touching bodies without a declared mate (default: tied)
  --crop x0,y0,z0,x1,y1,z1 analyse only this box of the model (sub-model of a big part; cut faces need a support)
  -D NAME=VALUE            OpenSCAD define, repeatable
  --viz [OUT.png]          write the stress picture (default: <input>.strength.png)
  --dry-run                validate inputs and show the model that would be solved; no meshing, no verdict
  --json                   emit the `3d.strength/2` report as JSON

Exit 0 = PASS, 1 = WEAK or FAIL (safety factor below required), 2 = invalid input / unsupported case,
127 = missing dependency.

Examples:
  3d strength bracket.scad --material PLA --load 25 --axis Z
  3d strength part.stl --material PETG --load 12.5 --fixture simple --orientation flat --json
  3d strength 3d.yaml --material PETG --viz --safety-factor 3
  3d strength case_half.stl --loads 3d.yaml --material PLA --orientation x-up
  3d strength --hook t=1.0,L=7,w=6,y=0.2 --material PLA --arm-axis Z --repeated-use"""


def run(argv: list[str]) -> int:
    if any(arg in ("-h", "--help") for arg in argv):
        print(USAGE)
        return 0
    if not argv:
        print(USAGE)
        return 1

    from strength import parse_args

    req = parse_args(argv)  # fail fast (exit 2) before starting the solver runtime
    closed_form = req.hook is not None or req.method == "beam"
    return exec_tool(DEPS if closed_form else FEA_DEPS, "strength_run.py", argv)


COMMAND = Command(
    name="strength",
    group="QA & GATES",
    summary="structural check of the assembled model: closed-form screens, linear FEA, FDM anisotropy, snap hooks",
    usage=USAGE,
    run=run,
    aliases=("structural-check",),
)
