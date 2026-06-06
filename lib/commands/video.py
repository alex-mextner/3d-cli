"""3d video — render or export simple videos from 3d render artifacts.

The command module stays import-light. Planning and orchestration live in lib/video.py,
and external work is delegated to `3d render` and ffmpeg subprocesses.
"""
from __future__ import annotations

import os
import tempfile

from cli.env import require_ffmpeg, require_openscad, repo_root
from cli.registry import Command
from errors import InputNotFound, InvalidArgument, UsageError

USAGE = """3d video <turntable|progress> ...
  Render/export simple videos from 3d render artifacts.

Modes:
  turntable <file.scad> [options]
      Render a deterministic orbit sequence through `3d render --cam`, then encode it.

  progress <frames-dir> [options]
      Encode an existing directory of PNG artifacts into a progress video.

Options:
  -o, --out PATH        output video path (default: <input>.mp4)
  --workdir DIR         turntable frame directory (default: <output>_frames)
  --frames N            turntable frame count (default: 36)
  --fps N               frames per second (default: 24 turntable, 12 progress)
  --size WxH            turntable render size (default: 800x600)
  --radius N            turntable orbit radius in model units (default: auto from bbox)
  --elevation DEG       turntable camera elevation, -89..89 (default: 25)
  --start-angle DEG     turntable starting azimuth (default: 0)
  --degrees DEG         turntable orbit span (default: 360)
  --pattern GLOB        progress frame glob (default: *.png)
  -D k=v                pass-through define for turntable renders (repeatable)
  --dry-run             print the plan without rendering/encoding

Examples:
  3d video turntable part.scad -o part-spin.mp4 --frames 48
  3d video progress previews/ -o progress.mp4 --fps 12 --pattern '*.png'"""


def _bin3d() -> str:
    return os.path.join(repo_root(), "bin", "3d")


def _default_file_out(path: str) -> str:
    base = os.path.splitext(path.rstrip(os.sep))[0]
    return base + ".mp4"


def _default_directory_out(path: str) -> str:
    normalized = os.path.normpath(path.rstrip(os.sep) or path)
    if normalized in ("", "."):
        return os.path.basename(os.getcwd()) + ".mp4"
    leaf = os.path.basename(normalized)
    if not leaf:
        return "progress.mp4"
    parent = os.path.dirname(normalized)
    return os.path.join(parent, leaf + ".mp4") if parent else leaf + ".mp4"


def _require_value(argv: list[str], index: int, flag: str) -> str:
    if index + 1 >= len(argv):
        raise UsageError(f"option {flag} needs a value", command="video")
    return argv[index + 1]


def _parse_turntable(argv: list[str]) -> int:  # noqa: C901
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    source = argv[0]
    rest = argv[1:]

    # Lazy import keeps command discovery cheap.
    import video

    out = _default_file_out(source)
    workdir = ""
    frames = 36
    fps = 24
    size = (800, 600)
    radius: float | None = None
    elevation = 25.0
    start_angle = 0.0
    degrees = 360.0
    defines: list[str] = []
    dry_run = False

    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg in ("-o", "--out"):
            out = _require_value(rest, i, arg)
            i += 2
        elif arg == "--workdir":
            workdir = _require_value(rest, i, arg)
            i += 2
        elif arg == "--frames":
            frames = video.positive_int("--frames", _require_value(rest, i, arg))
            i += 2
        elif arg == "--fps":
            fps = video.positive_int("--fps", _require_value(rest, i, arg))
            i += 2
        elif arg == "--size":
            size = video.parse_size(_require_value(rest, i, arg))
            i += 2
        elif arg == "--radius":
            radius = video.float_opt("--radius", _require_value(rest, i, arg))
            i += 2
        elif arg == "--elevation":
            elevation = video.float_opt("--elevation", _require_value(rest, i, arg))
            i += 2
        elif arg == "--start-angle":
            start_angle = video.float_opt("--start-angle", _require_value(rest, i, arg))
            i += 2
        elif arg == "--degrees":
            degrees = video.float_opt("--degrees", _require_value(rest, i, arg))
            i += 2
        elif arg == "-D":
            defines.append(_require_value(rest, i, arg))
            i += 2
        elif arg == "--dry-run":
            dry_run = True
            i += 1
        else:
            print(USAGE)
            raise UsageError(f"unknown option '{arg}'", command="video")

    if not os.path.isfile(source):
        raise InputNotFound(source, command="video")
    ext = os.path.splitext(source)[1].lower()
    if ext != ".scad":
        raise InvalidArgument("input extension", ext or "(none)", [".scad"], command="video")
    if not workdir:
        workdir = os.path.splitext(out)[0] + "_frames"

    plan_radius = radius if radius is not None else 250.0
    center = (0.0, 0.0, 0.0)

    plan = video.plan_turntable(
        source,
        workdir,
        frames=frames,
        fps=fps,
        size=size,
        radius=plan_radius,
        elevation=elevation,
        start_angle=start_angle,
        degrees=degrees,
        center=center,
        defines=defines,
    )
    if not dry_run:
        openscad = require_openscad("video")
        with tempfile.TemporaryDirectory(prefix="3d_video_bbox_") as tmp:
            bounds = video.scad_bounds(source, openscad=openscad, tempdir=tmp, defines=defines)
        center = bounds.center
        if radius is None:
            plan_radius = max(bounds.diagonal * 2.5, 1.0)
        plan = video.plan_turntable(
            source,
            workdir,
            frames=frames,
            fps=fps,
            size=size,
            radius=plan_radius,
            elevation=elevation,
            start_angle=start_angle,
            degrees=degrees,
            center=center,
            defines=defines,
        )
    if dry_run:
        print("mode: turntable")
        print(f"source: {source}")
        print(f"frames: {len(plan.frames)}")
        print(f"workdir: {workdir}")
        print(f"output: {out}")
        return 0

    ffmpeg = require_ffmpeg("video")
    video.render_turntable_frames(plan, bin3d=_bin3d())
    video.encode_frames([frame.path for frame in plan.frames], out, fps=fps, ffmpeg=ffmpeg)
    print(f"output: {out} ({len(plan.frames)} frames @ {fps} fps)")
    print(f"frames: {workdir}")
    return 0


def _parse_progress(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    frames_dir = argv[0]
    rest = argv[1:]

    import video

    out = _default_directory_out(frames_dir)
    fps = 12
    pattern = "*.png"
    dry_run = False

    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg in ("-o", "--out"):
            out = _require_value(rest, i, arg)
            i += 2
        elif arg == "--fps":
            fps = video.positive_int("--fps", _require_value(rest, i, arg))
            i += 2
        elif arg == "--pattern":
            pattern = _require_value(rest, i, arg)
            i += 2
        elif arg == "--dry-run":
            dry_run = True
            i += 1
        else:
            print(USAGE)
            raise UsageError(f"unknown option '{arg}'", command="video")

    frames = video.collect_progress_frames(frames_dir, pattern)
    if dry_run:
        print("mode: progress")
        print(f"frames: {len(frames)}")
        print(f"pattern: {pattern}")
        print(f"output: {out}")
        return 0

    ffmpeg = require_ffmpeg("video")
    video.encode_frames(frames, out, fps=fps, ffmpeg=ffmpeg)
    print(f"output: {out} ({len(frames)} frames @ {fps} fps)")
    return 0


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0

    mode = argv[0]
    if mode == "turntable":
        return _parse_turntable(argv[1:])
    if mode == "progress":
        return _parse_progress(argv[1:])

    print(USAGE)
    raise InvalidArgument("mode", mode, ["turntable", "progress"], command="video")


COMMAND = Command(
    name="video",
    group="RENDER & VIEW",
    summary="render turntable videos or encode progress PNG artifacts",
    usage=USAGE,
    run=run,
)
