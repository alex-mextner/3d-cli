"""3d proxy-align -- align a generated proxy mesh to a CAD mesh."""
from __future__ import annotations

import math
import os

from cli.pyrun import exec_tool
from cli.registry import Command
from errors import InputNotFound, InvalidArgument, UsageError

USAGE = """3d proxy-align <cad-mesh> <proxy-mesh> [options]
  Align a generated/reference proxy mesh (for example TRELLIS GLB/PLY/STL output)
  to the CAD mesh and write a reproducible transform + shape/topology scores.

Options:
  --out DIR             output directory (default: ./proxy-align)
  --samples N           surface samples per mesh (default: 2500)
  --yaw-step DEG        coarse yaw grid step in degrees, 1..360 (default: 45)
  --pitch VALUES        comma-separated pitch degrees (default: 0)
  --roll VALUES         comma-separated roll degrees (default: 0)
  --icp-steps N         nearest-neighbor refinement steps per candidate (default: 10)
  --json                print only result.json path

Examples:
  3d proxy-align cad.stl trellis.glb --out match/proxy
  3d proxy-align cad.stl proxy.ply --yaw-step 30 --pitch -20,0,20
  3d proxy-align cad.stl proxy.stl --out work/proxy | jq -r .best.error.chamfer_mean
  3d proxy-align cad.stl proxy.stl --out work/proxy --json | xargs cat | jq .quality_gate.status"""

_VALUE_FLAGS = {"--out", "--samples", "--yaw-step", "--pitch", "--roll", "--icp-steps"}
_BOOL_FLAGS = {"--json"}
_MIN_YAW_STEP = 1.0
_MAX_CANDIDATES = 5000


def _validate_value(flag: str, value: str) -> None:
    if flag == "--samples":
        try:
            samples = int(value)
        except ValueError:
            raise InvalidArgument(flag, value, ["integer >= 100"], command="proxy-align") from None
        if samples < 100:
            raise InvalidArgument(flag, value, ["integer >= 100"], command="proxy-align")
    elif flag == "--icp-steps":
        try:
            steps = int(value)
        except ValueError:
            raise InvalidArgument(flag, value, ["integer >= 0"], command="proxy-align") from None
        if steps < 0:
            raise InvalidArgument(flag, value, ["integer >= 0"], command="proxy-align")
    elif flag == "--yaw-step":
        try:
            step = float(value)
        except ValueError:
            raise InvalidArgument(flag, value, ["0 < degrees <= 360"], command="proxy-align") from None
        if not math.isfinite(step) or step < _MIN_YAW_STEP or step > 360.0:
            raise InvalidArgument(flag, value, ["1 <= degrees <= 360"], command="proxy-align")
    elif flag in {"--pitch", "--roll"}:
        try:
            values = [float(part.strip()) for part in value.split(",") if part.strip()]
        except ValueError:
            raise InvalidArgument(flag, value, ["comma-separated degrees"], command="proxy-align") from None
        if not values:
            raise InvalidArgument(flag, value, ["comma-separated degrees"], command="proxy-align")
        if any(not math.isfinite(v) for v in values):
            raise InvalidArgument(flag, value, ["finite comma-separated degrees"], command="proxy-align")


def _candidate_count(yaw_step: float, pitch: str, roll: str) -> int:
    yaw_count = max(1, int(math.ceil(360.0 / yaw_step)))
    pitch_count = len([part for part in pitch.split(",") if part.strip()])
    roll_count = len([part for part in roll.split(",") if part.strip()])
    return yaw_count * pitch_count * roll_count


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help") or "-h" in argv[1:] or "--help" in argv[1:]:
        print(USAGE)
        return 0
    if len(argv) < 2:
        print(USAGE)
        return 1

    cad, proxy = argv[0], argv[1]
    if not os.path.isfile(cad):
        raise InputNotFound(cad, command="proxy-align")
    if not os.path.isfile(proxy):
        raise InputNotFound(proxy, command="proxy-align")

    args = ["--cad", cad, "--proxy", proxy]
    rest = argv[2:]
    values = {"--yaw-step": "45", "--pitch": "0", "--roll": "0"}
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg in _VALUE_FLAGS:
            if i + 1 >= len(rest):
                raise UsageError(f"option {arg} needs a value", command="proxy-align")
            if rest[i + 1] in _VALUE_FLAGS or rest[i + 1] in _BOOL_FLAGS or rest[i + 1] in {"-h", "--help"}:
                raise UsageError(f"option {arg} needs a value", command="proxy-align")
            _validate_value(arg, rest[i + 1])
            if arg in values:
                values[arg] = rest[i + 1]
            args += [arg, rest[i + 1]]
            i += 2
        elif arg in _BOOL_FLAGS:
            args.append(arg)
            i += 1
        else:
            raise UsageError(f"unknown option '{arg}'", command="proxy-align")
    count = _candidate_count(float(values["--yaw-step"]), values["--pitch"], values["--roll"])
    if count > _MAX_CANDIDATES:
        raise UsageError(
            f"candidate grid is too large ({count} > {_MAX_CANDIDATES})",
            command="proxy-align",
            remediation=["Increase --yaw-step or reduce --pitch/--roll candidate lists."],
        )

    return exec_tool("trimesh,numpy,scipy", "proxy_align.py", args)


COMMAND = Command(
    name="proxy-align",
    group="REFERENCE-MATCH PIPELINE",
    summary="align generated proxy meshes to CAD meshes for spatial camera matching",
    usage=USAGE,
    run=run,
)
