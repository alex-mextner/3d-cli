"""3d animate — deterministic frame-plan generation over render workflows."""
from __future__ import annotations

import json
import os

from cli.registry import Command
from errors import InputNotFound, InvalidArgument, UsageError

USAGE = """3d animate <file.scad> [options]
  Generate a deterministic animation frame plan, optionally rendering each frame
  through the existing `3d render` workflow. This slice plans PNG frames only;
  video assembly and per-frame verification are intentionally out of scope.

Options:
  --plan                print the frame plan as JSON and do not render
  --frames N            number of frames (default 24)
  --outdir DIR          frame output directory (default animations/frames)
  --view NAME           render view passed to `3d render --view` (default iso)
  --size WxH            render size passed to `3d render --size` (default 800x600)
  -D k=v                pass-through define for every frame (repeatable)
  -D k=start:end        linearly interpolate a numeric define across frames

Examples:
  3d animate model.scad --plan --frames 12 -D angle=0:90
  3d animate model.scad --plan --size 1024x768
  3d animate model.scad --frames 24 --view front --outdir anim/frames"""


def _parse_positive_int(flag: str, value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise InvalidArgument(flag, value, ["a positive integer"], command="animate") from None
    if parsed < 1:
        raise InvalidArgument(flag, value, ["a positive integer"], command="animate")
    return parsed


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0

    model = argv[0]
    rest = argv[1:]
    plan_only = False
    frames = 24
    outdir = os.path.join("animations", "frames")
    view = "iso"
    size = "800x600"
    defines: list[str] = []

    i = 0
    n = len(rest)
    while i < n:
        arg = rest[i]
        if arg == "--plan":
            plan_only = True
            i += 1
        elif arg == "--frames":
            if i + 1 >= n:
                raise UsageError("option --frames needs a value", command="animate")
            frames = _parse_positive_int("--frames", rest[i + 1])
            i += 2
        elif arg == "--outdir":
            if i + 1 >= n:
                raise UsageError("option --outdir needs a value", command="animate")
            outdir = rest[i + 1]
            i += 2
        elif arg == "--view":
            if i + 1 >= n:
                raise UsageError("option --view needs a value", command="animate")
            view = rest[i + 1]
            i += 2
        elif arg == "--size":
            if i + 1 >= n:
                raise UsageError("option --size needs a value", command="animate")
            size = rest[i + 1]
            i += 2
        elif arg == "-D":
            if i + 1 >= n:
                raise UsageError("option -D needs a value", command="animate")
            defines.append(rest[i + 1])
            i += 2
        else:
            raise UsageError(f"unknown option '{arg}'", command="animate")

    if not os.path.isfile(model):
        raise InputNotFound(model, command="animate")

    import animation

    plan = animation.build_frame_plan(
        model,
        outdir=outdir,
        frames=frames,
        view=view,
        size=size,
        defines=defines,
    )
    if plan_only:
        print(json.dumps(plan.to_json_data(), indent=2, sort_keys=True))
        return 0

    from commands.render import run as render_run

    try:
        os.makedirs(outdir, exist_ok=True)
    except OSError as exc:
        raise UsageError(
            f"cannot create output directory: {outdir}",
            command="animate",
            remediation=["Choose a directory path, not an existing file."],
        ) from exc
    for frame in plan.frames:
        print(f"animate: frame {frame.index + 1}/{len(plan.frames)} -> {frame.output}")
        code = render_run(frame.render_argv)
        if code != 0:
            return code
    return 0


COMMAND = Command(
    name="animate",
    group="RENDER & VIEW",
    summary="deterministic animation frame planning over render frames",
    usage=USAGE,
    run=run,
)
