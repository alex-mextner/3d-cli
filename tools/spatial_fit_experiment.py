#!/usr/bin/env python3
"""Emit spatial-aware fit-camera experiment commands.

This is a small stdlib-only harness. It does not import or modify fit-camera internals.
It records local optional assets, checks whether they exist, and prints reproducible
command sequences for the light fallback path or the heavier model-backed paths.
"""
from __future__ import annotations

import argparse
import json
import shlex
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class ExperimentCase:
    name: str
    model: str
    reference: str
    notes: str


CASES: tuple[ExperimentCase, ...] = (
    ExperimentCase(
        name="pantheon-gflash-front",
        model="/Users/ultra/xp/3d-tests/gflash-3dcli/pantheon.scad",
        reference="/Users/ultra/xp/3d-tests/gflash-3dcli/references/front.jpg",
        notes="Known negative control; old area-IoU-only fit can look better than it is.",
    ),
    ExperimentCase(
        name="pantheon-gflash-oblique",
        model="/Users/ultra/xp/3d-tests/gflash-3dcli/pantheon.scad",
        reference="/Users/ultra/xp/3d-tests/gflash-3dcli/references/oblique.jpg",
        notes="Oblique Pantheon view; useful for multi-view aggregate checks.",
    ),
    ExperimentCase(
        name="pantheon-gpro-front",
        model="/Users/ultra/xp/3d-tests/gpro-3dcli/pantheon.scad",
        reference="/Users/ultra/xp/3d-tests/gpro-3dcli/references/front.jpg",
        notes="Second generated Pantheon model for cross-run metric ranking.",
    ),
    ExperimentCase(
        name="lego-loco-emerald-side",
        model="/Users/ultra/xp/garage-band/projects/lego-loco/assembly.scad",
        reference="/Users/ultra/xp/garage-band/projects/lego-loco/references/ref_emerald_night_side.jpg",
        notes="Side-elevation real object; good depth/channel candidate.",
    ),
    ExperimentCase(
        name="lego-loco-orient-side",
        model="/Users/ultra/xp/garage-band/projects/lego-loco/assembly.scad",
        reference="/Users/ultra/xp/garage-band/projects/lego-loco/references/ref_orient_express_side.jpg",
        notes="Second side reference for same model family.",
    ),
)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def shell_join(args: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in args)


def find_case(name: str) -> ExperimentCase:
    for case in CASES:
        if case.name == name:
            return case
    valid = ", ".join(case.name for case in CASES)
    raise SystemExit(f"unknown case {name!r}; valid: {valid}")


def case_status(case: ExperimentCase) -> dict[str, object]:
    model = Path(case.model)
    reference = Path(case.reference)
    return {
        **asdict(case),
        "model_exists": model.is_file(),
        "reference_exists": reference.is_file(),
        "ready": model.is_file() and reference.is_file(),
    }


def fallback_commands(case: ExperimentCase, out: Path) -> list[list[str]]:
    bin_3d = str(repo_root() / "bin" / "3d")
    prep = out / "preprocess"
    compare = out / "compare"
    camera_json = out / "camera.json"
    return [
        ["mkdir", "-p", str(out), str(prep), str(compare)],
        [bin_3d, "preprocess", case.reference, "-o", str(prep), "--force-fallback"],
        [
            bin_3d,
            "fit-camera",
            case.model,
            str(prep / "mask.png"),
            "--out",
            str(camera_json),
            "--rand",
            "250",
            "--refine",
            "100",
            "--seed",
            "11",
        ],
        [
            bin_3d,
            "compare",
            case.model,
            case.reference,
            "--out",
            str(compare),
            "--rand",
            "250",
            "--refine",
            "100",
        ],
    ]


def rembg_commands(case: ExperimentCase, out: Path) -> list[list[str]]:
    prep = out / "rembg"
    bin_3d = str(repo_root() / "bin" / "3d")
    preprocess_tool = str(repo_root() / "lib" / "preprocess_reference.py")
    return [
        ["mkdir", "-p", str(prep)],
        [
            "uv",
            "run",
            "--python",
            "3.12",
            "--with",
            "opencv-python-headless,numpy,pillow",
            "--with",
            "rembg[cpu]",
            preprocess_tool,
            case.reference,
            "--out-dir",
            str(prep),
        ],
        [
            bin_3d,
            "fit-camera",
            case.model,
            str(prep / "mask.png"),
            "--out",
            str(out / "rembg-camera.json"),
            "--rand",
            "250",
            "--refine",
            "100",
            "--seed",
            "11",
        ],
    ]


def depth_commands(case: ExperimentCase, out: Path) -> list[list[str]]:
    prep = out / "depth-anything"
    preprocess_tool = str(repo_root() / "lib" / "preprocess_reference.py")
    return [
        ["mkdir", "-p", str(prep)],
        [
            "uv",
            "run",
            "--python",
            "3.12",
            "--with",
            "opencv-python-headless,numpy,pillow",
            "--with",
            "transformers>=4.45",
            "--with",
            "torch",
            preprocess_tool,
            case.reference,
            "--out-dir",
            str(prep),
        ],
    ]


def emit_shell(case: ExperimentCase, out: Path, include_heavy: bool) -> str:
    blocks = [
        "# light fallback path: OpenCV mask, pseudo-depth, fit-camera, compare",
        *[shell_join(cmd) for cmd in fallback_commands(case, out)],
    ]
    if include_heavy:
        blocks.extend(
            [
                "",
                "# optional rembg mask tier",
                *[shell_join(cmd) for cmd in rembg_commands(case, out)],
                "",
                "# optional Depth Anything tier; run one image at a time",
                *[shell_join(cmd) for cmd in depth_commands(case, out)],
            ]
        )
    return "\n".join(blocks)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-assets", action="store_true", help="print local asset readiness JSON")
    parser.add_argument("--emit", choices=("json", "shell"), help="emit experiment commands")
    parser.add_argument("--case", default=CASES[0].name, help="case name for --emit")
    parser.add_argument("--out", default="/tmp/3d-spatial/experiment", help="output directory for --emit")
    parser.add_argument("--include-heavy", action="store_true", help="include rembg and Depth Anything commands")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.check_assets:
        print(json.dumps([case_status(case) for case in CASES], indent=2))
    if args.emit:
        case = find_case(args.case)
        out = Path(args.out).expanduser().resolve()
        if args.emit == "json":
            payload = {
                "case": case_status(case),
                "out": str(out),
                "fallback_commands": fallback_commands(case, out),
                "rembg_commands": rembg_commands(case, out) if args.include_heavy else [],
                "depth_commands": depth_commands(case, out) if args.include_heavy else [],
            }
            print(json.dumps(payload, indent=2))
        else:
            print(emit_shell(case, out, args.include_heavy))
    if not args.check_assets and not args.emit:
        build_parser().print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
