#!/usr/bin/env python3
"""Export every STL and render every guide picture of the C920 top-down mount (issue #338).

WHAT: runs `3d export` for each printable part, each print plate and each fit-check pair of
  camera_mount.scad, and `3d render` for the pictures README.ru.md shows.
WHY: one command rebuilds every artifact the guide and the slicer use from the one SCAD
  source, so a changed parameter (a measured dimension) can never leave a stale STL behind.

Usage:  python3 models/camera_mount/build.py [--stl] [--img] [-D name=value ...]
        (no flag = both; -D overrides a SCAD parameter, e.g. -D k1_rim=24)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCAD = HERE / "camera_mount.scad"

PARTS = [
    "hub",
    "foot",
    "shoe_k1",
    "shoe_u1",
    "post",
    "arm",
    "cam_plate",
    "knob_m5",
    "knob_m4",
    "pin_knob",
    "pi_tray",
    "cable_clip",
    "led_ring",
    "dowel",
]
PLATES = ["plate_hub", "plate_small", "plate_extras", "plate_led"]
FITS = ["fit_foot", "fit_shoe_k1", "fit_shoe_u1", "fit_hub", "fit_post", "fit_arm"]

# (output name, part, printer, camera "ex,ey,ez,cx,cy,cz" or a --view preset)
RENDERS = [
    ("assembly-k1se", "assembly", "k1se", "520,-640,520,0,0,-40"),
    ("assembly-u1", "assembly", "u1", "640,-760,600,0,0,-40"),
    ("hub-closeup", "assembly", "k1se", "170,-230,230,0,0,45"),
    ("corner-k1se", "detail", "k1se", "250,-90,230,-10,10,0"),
    ("pad-u1", "detail", "u1", "250,-90,240,-5,5,0"),
    ("exploded", "exploded", "k1se", "380,-460,330,0,0,90"),
    ("plate_hub", "plate_hub", "k1se", "top"),
    ("plate_small", "plate_small", "k1se", "top"),
    ("plate_extras", "plate_extras", "k1se", "top"),
    ("plate_led", "plate_led", "k1se", "top"),
    ("part-shoe_k1", "fit_shoe_k1", "k1se", "iso"),
    ("part-shoe_u1", "fit_shoe_u1", "k1se", "iso"),
]


def run(cmd: list[str]) -> tuple[list[str], int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return cmd, proc.returncode, (proc.stdout + proc.stderr)[-1500:]


def defines(part: str, extra: list[str], printer: str = "k1se") -> list[str]:
    out = ["-D", f'part="{part}"', "-D", f'printer="{printer}"']
    for d in extra:
        out += ["-D", d]
    return out


def jobs_stl(extra: list[str]) -> list[list[str]]:
    jobs = []
    for sub, names in (("", PARTS), ("plates", PLATES), ("fit", FITS)):
        out = HERE / "stl" / sub
        out.mkdir(parents=True, exist_ok=True)
        for name in names:
            jobs.append(
                [
                    "3d",
                    "export",
                    str(SCAD),
                    "-o",
                    str(out / f"{name}.stl"),
                    *defines(name, extra),
                ]
            )
    return jobs


def jobs_img(extra: list[str]) -> list[list[str]]:
    out = HERE / "img"
    out.mkdir(exist_ok=True)
    jobs = []
    for name, part, printer, cam in RENDERS:
        view = (
            ["--cam", cam]
            if "," in cam
            else ["--view", cam, *(["--ortho"] if cam == "top" else [])]
        )
        jobs.append(
            [
                "3d",
                "render",
                str(SCAD),
                *view,
                "-o",
                str(out / f"{name}.png"),
                *defines(part, extra, printer),
            ]
        )
    return jobs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stl", action="store_true", help="export STLs only")
    ap.add_argument("--img", action="store_true", help="render pictures only")
    ap.add_argument(
        "-D",
        dest="defines",
        action="append",
        default=[],
        help="SCAD override, e.g. k1_rim=24",
    )
    ap.add_argument(
        "-j", "--jobs", type=int, default=3, help="parallel 3d processes (default 3)"
    )
    args = ap.parse_args()
    both = not args.stl and not args.img
    jobs = (jobs_stl(args.defines) if args.stl or both else []) + (
        jobs_img(args.defines) if args.img or both else []
    )
    failed = 0
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for cmd, code, tail in pool.map(run, jobs):
            target = cmd[cmd.index("-o") + 1]
            print(
                ("ok   " if code == 0 else "FAIL ")
                + str(Path(target).relative_to(HERE))
            )
            if code:
                failed += 1
                print(tail, file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
