#!/usr/bin/env python3
"""Stills, installation animations and phone-viewer models of the C920 camera mount (issue #338).

WHAT: renders from camera_mount.scad (printer and C920 scene models included):
  - stills: the mount on the K1 SE and on the U1, close-ups (beam joint, foot wedge, tilt hangers
    with the camera in its cradle, hub), top views, and the camera's own view of each bed;
  - anim-<printer>.mp4 / .gif: the installation (beam from segments -> hub -> feet -> onto the
    printer -> camera head -> top view), 10 s at 24 fps; the GIF is kept under 8 MB for Telegram;
  - mount-<printer>.usdz (iPhone AR Quick Look: coloured, Y-up, real scale) and .3mf (mm).
WHY a separate script: hundreds of OpenSCAD frames are a heavy job; it runs on a Runpod pod
  (README.ru.md §8), not on the shared workstation, and needs only OpenSCAD, ffmpeg and Xvfb.

Usage:  python3 models/camera_mount/media.py OUTDIR [--only stills,anim,viewer]
Env: OPENSCAD = OpenSCAD binary with the manifold backend (default: `openscad`).
"""

from __future__ import annotations

import argparse
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCAD = HERE / "camera_mount.scad"
OPENSCAD = os.environ.get("OPENSCAD", "openscad")
SCHEME = "Tomorrow Night"
FPS = 24
BUILD_FRAMES = 200  # installation, t 0 -> 1
TOP_FRAMES = 40  # camera swings to the top view
GIF_LIMIT = 8 * 1024 * 1024
PRINTERS = ("k1se", "u1")


def scad(
    out: Path,
    part: str,
    printer: str,
    cam: str,
    size: tuple[int, int],
    *defines: str,
    ortho: bool = False,
    viewall: bool = False,
) -> None:
    cmd = [
        OPENSCAD,
        "-o",
        str(out),
        f"--imgsize={size[0]},{size[1]}",
        f"--camera={cam}",
        f"--projection={'o' if ortho else 'p'}",
        f"--colorscheme={SCHEME}",
        "-D",
        f'part="{part}"',
        "-D",
        f'printer="{printer}"',
    ]
    if viewall:
        cmd += ["--viewall", "--autocenter"]
    for d in defines:
        cmd += ["-D", d]
    # one OpenSCAD run normally takes under a second; a hung GL context is retried, not waited on
    for attempt in (1, 2):
        start = time.monotonic()
        try:
            proc = subprocess.run(
                [*cmd, str(SCAD)],
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            print(f"{out.name}: timed out (attempt {attempt})", flush=True)
            continue
        took = time.monotonic() - start
        if took > 20:
            print(f"{out.name}: slow, {took:.0f} s", flush=True)
        if proc.returncode == 0 and out.exists():
            return
        print(f"{out.name}: exit {proc.returncode} (attempt {attempt})", flush=True)
    raise RuntimeError(f"openscad failed for {out.name}")


def scene_facts() -> dict[str, float]:
    """lens height above the printer top and lens-to-bed distances, from the model's own echo."""
    with tempfile.TemporaryDirectory() as tmp:
        echo = Path(tmp) / "facts.echo"
        subprocess.run(
            [OPENSCAD, "-o", str(echo), "-D", 'part="assembly"', str(SCAD)],
            check=True,
            capture_output=True,
        )
        echo_text = echo.read_text()
    m = re.search(
        r"lens above printer top = ([\d.]+) mm; lens to bed: K1 SE ([\d.]+) mm, U1 ([\d.]+) mm",
        echo_text,
    )
    if not m:
        raise RuntimeError("no lens echo in camera_mount.scad")
    lens, k1, u1 = (float(g) for g in m.groups())
    return {"lens": lens, "k1se": k1, "u1": u1}


def stills(out: Path, facts: dict[str, float], jobs: int) -> list[Path]:
    big = (1600, 1000)
    shots: list[tuple[str, str, str, str, tuple[int, int], dict[str, bool]]] = [
        (
            "overview-k1se",
            "assembly",
            "k1se",
            "1050,-1340,720,0,0,-130",
            big,
            {},
        ),
        (
            "overview-u1",
            "assembly",
            "u1",
            "1100,-1400,730,0,0,-170",
            big,
            {},
        ),
        (
            "top-k1se",
            "assembly",
            "k1se",
            "0,-0.01,1500,0,0,-200",
            big,
            {"ortho": True, "viewall": True},
        ),
        (
            "top-u1",
            "assembly",
            "u1",
            "0,-0.01,1500,0,0,-200",
            big,
            {"ortho": True, "viewall": True},
        ),
        ("closeup-hub", "assembly", "k1se", "210,-300,300,0,0,60", big, {}),
        (
            "closeup-joint",
            "detail_joint",
            "k1se",
            "300,-170,110,200,0,12",
            big,
            {},
        ),
        (
            "closeup-wedge",
            "detail_wedge",
            "k1se",
            "120,-160,120,0,0,25",
            big,
            {"viewall": True},
        ),
        (
            "closeup-tilt-cradle",
            "detail_tilt",
            "k1se",
            "-120,-200,-90,0,0,-30",
            big,
            {"viewall": True},
        ),
        ("exploded", "exploded", "k1se", "820,-1000,640,0,0,120", big, {}),
    ]
    for pr in PRINTERS:
        dist = facts[pr]
        bed_z = facts["lens"] - dist
        # gimbal camera at the lens, looking straight down (rotation 0,0,0 = top view)
        shots.append(
            (
                f"camera-view-{pr}",
                "pov",
                pr,
                f"0,0,{bed_z:.1f},0,0,0,{dist:.1f}",
                (1152, 768),
                {},
            )
        )
    outs = []
    with ThreadPoolExecutor(jobs) as pool:
        futs = [
            pool.submit(scad, out / f"{name}.png", part, pr, cam, size, **kw)
            for name, part, pr, cam, size, kw in shots
        ]
        for f, shot in zip(futs, shots, strict=True):
            f.result()
            outs.append(out / f"{shot[0]}.png")
            print("still", shot[0], flush=True)
    return outs


def smooth(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def anim_camera(pr: str, i: int) -> tuple[float, str]:
    """(anim_t, camera) for frame i: an orbit while the mount goes together, then a swing to the
    top view. The look-at point follows the bridge down when it is lowered (t 0.46..0.62)."""
    radius = 2650.0 if pr == "u1" else 2250.0
    t = min(1.0, i / (BUILD_FRAMES - 1))
    lowered = smooth((t - 0.46) / 0.16)
    cz = 130 - 290 * lowered
    theta = math.radians(-55 + 40 * (i / BUILD_FRAMES))
    elev = math.radians(28)
    orbit = (
        radius * math.cos(elev) * math.cos(theta),
        radius * math.cos(elev) * math.sin(theta),
        radius * math.sin(elev),
    )
    if i < BUILD_FRAMES:
        eye = orbit
    else:
        s = smooth((i - BUILD_FRAMES) / (TOP_FRAMES - 1))
        top = (0.0, -1.0, radius * 0.75)
        eye = tuple(a + (b - a) * s for a, b in zip(orbit, top, strict=True))
    return t, f"{eye[0]:.1f},{eye[1]:.1f},{eye[2] + cz:.1f},0,0,{cz:.1f}"


def animation(pr: str, out: Path, jobs: int) -> tuple[Path, Path]:
    frames = out / f"frames-{pr}"
    frames.mkdir(parents=True, exist_ok=True)
    total = BUILD_FRAMES + TOP_FRAMES

    def frame(i: int) -> None:
        t, cam = anim_camera(pr, i)
        scad(frames / f"f{i:04d}.png", "anim", pr, cam, (1280, 720), f"anim_t={t:.4f}")

    with ThreadPoolExecutor(jobs) as pool:
        for n, _ in enumerate(pool.map(frame, range(total)), 1):
            if n % 40 == 0:
                print(f"anim {pr}: {n}/{total} frames", flush=True)
    mp4 = out / f"anim-{pr}.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-framerate",
            str(FPS),
            "-i",
            str(frames / "f%04d.png"),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            "22",
            "-movflags",
            "+faststart",
            str(mp4),
        ],
        check=True,
    )
    gif = out / f"anim-{pr}.gif"
    for width, fps in ((560, 12), (480, 12), (400, 10), (320, 10)):
        vf = (
            f"fps={fps},scale={width}:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128[p];"
            "[b][p]paletteuse=dither=bayer:bayer_scale=4"
        )
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(mp4),
                "-vf",
                vf,
                "-loop",
                "0",
                str(gif),
            ],
            check=True,
        )
        if gif.stat().st_size <= GIF_LIMIT:
            break
    else:
        raise RuntimeError(f"{gif.name} stays over 8 MB even at 320 px")
    shutil.rmtree(frames)
    return mp4, gif


VIEWER_PARTS = {  # camera_mount.scad part -> diffuse colour
    "viewer_printer": (0.59, 0.61, 0.65),
    "viewer_mount": (0.91, 0.45, 0.16),
    "viewer_c920": (0.12, 0.12, 0.13),
}


def viewer_models(pr: str, out: Path) -> list[Path]:
    """mount-<pr>.usdz for an iPhone (AR Quick Look: one coloured mesh per part, Y-up, real
    scale) and mount-<pr>.3mf (millimetres, for a slicer or a desktop viewer)."""
    import trimesh

    sys.path.insert(0, str(HERE.parents[1] / "lib"))
    from usdz import UsdzPart, meshes_to_usdz

    parts = []
    scene = trimesh.Scene()
    for part, color in VIEWER_PARTS.items():
        stl = out / f"{pr}-{part}.stl"
        subprocess.run(
            [OPENSCAD, "--backend=manifold", "-o", str(stl), "-D", f'part="{part}"',
             "-D", f'printer="{pr}"', str(SCAD)],
            check=True,
            capture_output=True,
        )
        name = part.removeprefix("viewer_")
        parts.append(UsdzPart(str(stl), color, name))
        scene.add_geometry(trimesh.load(stl, force="mesh"), node_name=name, geom_name=name)
    usdz = out / f"mount-{pr}.usdz"
    meshes_to_usdz(parts, str(usdz))
    tmf = out / f"mount-{pr}.3mf"
    tmf.write_bytes(scene.export(file_type="3mf"))
    for p in parts:
        Path(p.path).unlink()
    return [usdz, tmf]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out", type=Path)
    ap.add_argument(
        "--only", default="stills,anim,viewer", help="comma list of stills, anim, viewer"
    )
    # the CPUs this process may use (a container's os.cpu_count() reports the whole host)
    ap.add_argument("-j", "--jobs", type=int, default=len(os.sched_getaffinity(0)))
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    only = set(args.only.split(","))
    facts = scene_facts()
    print("facts", facts, flush=True)
    made: list[Path] = []
    if "viewer" in only:
        for pr in PRINTERS:
            made += viewer_models(pr, args.out)
    if "stills" in only:
        made += stills(args.out, facts, args.jobs)
    if "anim" in only:
        for pr in PRINTERS:
            made += animation(pr, args.out, args.jobs)
    for p in made:
        print(f"{p.stat().st_size / 1e6:8.2f} MB  {p.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
