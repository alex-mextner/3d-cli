"""Video planning and export helpers for the `3d video` command.

This module is intentionally stdlib-only. It does not render geometry itself; turntable
frames are delegated to the existing `3d render --cam` path, and final movie export is
delegated to an external encoder such as ffmpeg.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Callable, Sequence
from dataclasses import dataclass
import fnmatch
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import struct
import sys

from errors import GateFailure, InputNotFound, InvalidArgument, UsageError

CommandRunner = Callable[[list[str]], int]


@dataclass(frozen=True)
class VideoFrame:
    """One planned render frame."""

    index: int
    angle_deg: float
    camera: str
    path: str


RenderJob = tuple[VideoFrame, list[str]]


@dataclass(frozen=True)
class TurntablePlan:
    """A deterministic turntable render plan."""

    source: str
    outdir: str
    fps: int
    size: tuple[int, int]
    frames: tuple[VideoFrame, ...]
    defines: tuple[str, ...] = ()


@dataclass(frozen=True)
class Bounds:
    """Axis-aligned model bounds summary."""

    center: tuple[float, float, float]
    diagonal: float


def parse_size(raw: str) -> tuple[int, int]:
    """Parse WxH image size."""
    match = re.fullmatch(r"([1-9][0-9]*)x([1-9][0-9]*)", raw)
    if match is None:
        raise InvalidArgument(
            "--size",
            raw,
            ["WxH with positive integers, e.g. 800x600"],
            command="video",
        )
    return int(match.group(1)), int(match.group(2))


def positive_int(flag: str, raw: str) -> int:
    """Parse a positive integer option."""
    try:
        value = int(raw)
    except ValueError:
        raise InvalidArgument(flag, raw, ["positive integer"], command="video") from None
    if value <= 0:
        raise InvalidArgument(flag, raw, ["positive integer"], command="video")
    return value


def float_opt(flag: str, raw: str) -> float:
    """Parse a float option."""
    try:
        return float(raw)
    except ValueError:
        raise InvalidArgument(flag, raw, ["number"], command="video") from None


def _fmt(value: float) -> str:
    return f"{value:.4f}"


def _orbit_camera(
    angle_deg: float,
    elevation_deg: float,
    radius: float,
    center: tuple[float, float, float],
) -> str:
    az = math.radians(angle_deg)
    el = math.radians(elevation_deg)
    horizontal = radius * math.cos(el)
    cx, cy, cz = center
    x = cx + horizontal * math.sin(az)
    y = cy - horizontal * math.cos(az)
    z = cz + radius * math.sin(el)
    return ",".join(_fmt(v) for v in (x, y, z, cx, cy, cz))


def plan_turntable(
    source: str,
    outdir: str,
    *,
    frames: int,
    fps: int,
    size: tuple[int, int],
    radius: float = 250.0,
    elevation: float = 25.0,
    start_angle: float = 0.0,
    degrees: float = 360.0,
    center: tuple[float, float, float] = (0.0, 0.0, 0.0),
    defines: Sequence[str] = (),
) -> TurntablePlan:
    """Plan a turntable video without rendering any frames.

    The final angle is intentionally not duplicated for a full 360-degree orbit, so a
    loop can wrap from the last frame back to the first without a visual hold.
    """
    if frames <= 0:
        raise InvalidArgument("--frames", str(frames), ["positive integer"], command="video")
    if fps <= 0:
        raise InvalidArgument("--fps", str(fps), ["positive integer"], command="video")
    if radius <= 0:
        raise InvalidArgument("--radius", str(radius), ["positive number"], command="video")
    if not (-89.0 <= elevation <= 89.0):
        raise InvalidArgument(
            "--elevation",
            str(elevation),
            ["-89..89 degrees"],
            command="video",
        )
    if degrees <= 0:
        raise InvalidArgument("--degrees", str(degrees), ["positive number"], command="video")

    planned: list[VideoFrame] = []
    full_loop = math.isclose(degrees % 360.0, 0.0, abs_tol=1e-9)
    divisor = frames if full_loop or frames == 1 else frames - 1
    for index in range(frames):
        angle = start_angle + (degrees * index / divisor)
        planned.append(
            VideoFrame(
                index=index,
                angle_deg=angle,
                camera=_orbit_camera(angle, elevation, radius, center),
                path=os.path.join(outdir, f"frame_{index:04d}.png"),
            )
        )

    return TurntablePlan(
        source=source,
        outdir=outdir,
        fps=fps,
        size=size,
        frames=tuple(planned),
        defines=tuple(defines),
    )


def _bounds_from_vertices(vertices: Sequence[tuple[float, float, float]]) -> Bounds:
    if not vertices:
        raise UsageError("STL contains no vertices", command="video")
    xs = [v[0] for v in vertices]
    ys = [v[1] for v in vertices]
    zs = [v[2] for v in vertices]
    lo = (min(xs), min(ys), min(zs))
    hi = (max(xs), max(ys), max(zs))
    center = tuple((lo[i] + hi[i]) / 2.0 for i in range(3))
    diagonal = math.dist(lo, hi)
    return Bounds(
        center=(float(center[0]), float(center[1]), float(center[2])),
        diagonal=float(diagonal),
    )


def _subprocess_runner(argv: list[str]) -> int:
    return subprocess.run(argv).returncode


def _openscad_prefix() -> list[str]:
    if os.environ.get("DISPLAY"):
        return []
    xvfb = shutil.which("xvfb-run")
    if xvfb:
        return [xvfb, "--auto-servernum", "--server-args=-screen 0 1280x1024x24"]
    return []


def _binary_stl_vertices(data: bytes) -> list[tuple[float, float, float]] | None:
    if len(data) < 84:
        return None
    (triangles,) = struct.unpack_from("<I", data, 80)
    expected = 84 + triangles * 50
    if expected > len(data):
        return None
    vertices: list[tuple[float, float, float]] = []
    offset = 84
    for _ in range(triangles):
        offset += 12  # normal
        for _vertex_index in range(3):
            vertices.append(struct.unpack_from("<3f", data, offset))
            offset += 12
        offset += 2  # attribute byte count
    return vertices


def _ascii_stl_vertices(text: str) -> list[tuple[float, float, float]]:
    vertices: list[tuple[float, float, float]] = []
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) == 4 and parts[0].lower() == "vertex":
            try:
                vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
            except ValueError:
                continue
    return vertices


def stl_bounds(path: str) -> Bounds:
    """Read a binary or ASCII STL and return its center/diagonal."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        raise InputNotFound(path, command="video") from None

    vertices = _binary_stl_vertices(data)
    if vertices is None:
        vertices = _ascii_stl_vertices(data.decode("utf-8", errors="ignore"))
    return _bounds_from_vertices(vertices)


def scad_bounds(
    source: str,
    *,
    openscad: str,
    tempdir: str,
    defines: Sequence[str] = (),
    runner: CommandRunner = _subprocess_runner,
) -> Bounds:
    """Export a temporary STL through OpenSCAD and return model bounds."""
    stl = os.path.join(tempdir, "turntable_bounds.stl")
    argv = [*_openscad_prefix(), openscad, "--export-format", "binstl"]
    for define in defines:
        argv += ["-D", define]
    argv += ["-o", stl, source]
    code = runner(argv)
    if code != 0:
        raise GateFailure(f"bbox export failed with exit code {code}", command="video")
    if not os.path.isfile(stl):
        raise GateFailure("bbox export reported success but produced no STL", command="video")
    return stl_bounds(stl)


def _render_jobs(plan: TurntablePlan, bin3d: str) -> list[RenderJob]:
    size = f"{plan.size[0]}x{plan.size[1]}"
    jobs: list[RenderJob] = []
    for frame in plan.frames:
        argv = [
            bin3d,
            "render",
            plan.source,
            "--cam",
            frame.camera,
            "--size",
            size,
        ]
        for define in plan.defines:
            argv += ["-D", define]
        argv += ["-o", frame.path]
        jobs.append((frame, argv))
    return jobs


def _parallelism(total: int, jobs: int | None) -> int:
    if total <= 0:
        return 1
    requested = jobs if jobs is not None else (os.cpu_count() or 1)
    return max(1, min(total, requested))


def _check_render_result(frame: VideoFrame, code: int) -> None:
    if code != 0:
        raise GateFailure(
            f"render failed for frame {frame.index} with exit code {code}",
            command="video",
        )
    if not os.path.isfile(frame.path):
        raise GateFailure(
            f"render reported success but did not create {frame.path}",
            command="video",
        )


def _render_with_runner(jobs_to_run: Sequence[RenderJob], runner: CommandRunner, workers: int) -> None:
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_frame = {
            pool.submit(runner, argv): frame
            for frame, argv in jobs_to_run
        }
        for future in concurrent.futures.as_completed(future_to_frame):
            frame = future_to_frame[future]
            _check_render_result(frame, future.result())


async def _render_one_async(
    semaphore: asyncio.Semaphore,
    frame: VideoFrame,
    argv: list[str],
) -> None:
    async with semaphore:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    if stdout:
        sys.stdout.write(stdout.decode(errors="replace"))
    if stderr:
        sys.stderr.write(stderr.decode(errors="replace"))
    code = proc.returncode
    if code is None:
        raise GateFailure(f"render did not report an exit code for frame {frame.index}", command="video")
    _check_render_result(frame, code)


async def _render_async(jobs_to_run: Sequence[RenderJob], workers: int) -> None:
    semaphore = asyncio.Semaphore(workers)
    await asyncio.gather(
        *(_render_one_async(semaphore, frame, argv) for frame, argv in jobs_to_run)
    )


def render_turntable_frames(
    plan: TurntablePlan,
    *,
    bin3d: str,
    runner: CommandRunner | None = None,
    jobs: int | None = None,
) -> None:
    """Render each planned turntable frame through `3d render`."""
    os.makedirs(plan.outdir, exist_ok=True)
    jobs_to_run = _render_jobs(plan, bin3d)
    workers = _parallelism(len(jobs_to_run), jobs)
    if runner is None:
        asyncio.run(_render_async(jobs_to_run, workers))
    else:
        _render_with_runner(jobs_to_run, runner, workers)


def _natural_key(path: str) -> list[int | str]:
    name = os.path.basename(path).lower()
    parts: list[int | str] = []
    for part in re.split(r"([0-9]+)", name):
        if part.isdigit():
            parts.append(int(part))
        else:
            parts.append(part)
    return parts


def collect_progress_frames(frames_dir: str, pattern: str) -> list[str]:
    """Collect existing progress frames from a directory using a case-insensitive glob."""
    if not os.path.isdir(frames_dir):
        raise UsageError(f"frames directory not found: {frames_dir}", command="video")
    paths: list[str] = []
    for entry in os.scandir(frames_dir):
        if entry.is_file() and fnmatch.fnmatch(entry.name.lower(), pattern.lower()):
            paths.append(entry.path)
    paths.sort(key=_natural_key)
    if not paths:
        raise UsageError(
            f"no frames matched {pattern!r} in {frames_dir}",
            command="video",
            remediation=["Render frames first, or pass --pattern with the existing PNG naming scheme."],
        )
    return paths


def _quote_concat_path(path: str) -> str:
    absolute = os.path.abspath(path)
    return "file '" + absolute.replace("'", "'\\''") + "'"


def _manifest_path(out: str) -> str:
    return os.fspath(Path(out).with_suffix(Path(out).suffix + ".concat.txt"))


def _container_flags(out: str) -> list[str]:
    suffix = Path(out).suffix.lower()
    if suffix in (".mp4", ".m4v", ".mov"):
        return ["-movflags", "+faststart"]
    return []


def encode_frames(
    frames: Sequence[str],
    out: str,
    *,
    fps: int,
    ffmpeg: str = "ffmpeg",
    runner: CommandRunner = _subprocess_runner,
) -> str:
    """Encode PNG frames into a video using ffmpeg's concat demuxer."""
    if fps <= 0:
        raise InvalidArgument("--fps", str(fps), ["positive integer"], command="video")
    if not frames:
        raise UsageError("no frames to encode", command="video")
    for frame in frames:
        if not os.path.isfile(frame):
            raise InputNotFound(frame, command="video")

    out_parent = os.path.dirname(os.path.abspath(out))
    os.makedirs(out_parent, exist_ok=True)
    manifest = _manifest_path(out)
    duration = 1.0 / fps
    with open(manifest, "w", encoding="utf-8") as fh:
        for frame in frames:
            fh.write(_quote_concat_path(frame) + "\n")
            fh.write(f"duration {duration:.6f}\n")
        fh.write(_quote_concat_path(frames[-1]) + "\n")

    argv = [
        ffmpeg,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        manifest,
        "-r",
        str(fps),
        "-vf",
        "pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p",
        *_container_flags(out),
        out,
    ]
    code = runner(argv)
    if code != 0:
        raise GateFailure(f"ffmpeg failed with exit code {code}", command="video")
    if not os.path.isfile(out):
        raise GateFailure(f"ffmpeg reported success but did not create {out}", command="video")
    return manifest
