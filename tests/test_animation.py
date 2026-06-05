"""Unit tests for deterministic animation frame planning."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import animation
from errors import InvalidArgument


def test_build_frame_plan_interpolates_defines_and_names_outputs(tmp_path: Path) -> None:
    outdir = os.path.join(str(tmp_path), "frames")

    plan = animation.build_frame_plan(
        "examples/arm.scad",
        outdir=outdir,
        frames=3,
        view="front",
        size="640x480",
        defines=("angle=0:90", "mode=preview"),
    )

    assert [frame.index for frame in plan.frames] == [0, 1, 2]
    assert [frame.progress for frame in plan.frames] == [0.0, 0.5, 1.0]
    assert [frame.defines for frame in plan.frames] == [
        ("angle=0", "mode=preview"),
        ("angle=45", "mode=preview"),
        ("angle=90", "mode=preview"),
    ]
    assert [os.path.basename(frame.output) for frame in plan.frames] == [
        "frame_0000.png",
        "frame_0001.png",
        "frame_0002.png",
    ]
    assert plan.frames[0].render_argv == [
        "examples/arm.scad",
        "--view",
        "front",
        "-o",
        os.path.join(outdir, "frame_0000.png"),
        "--size",
        "640x480",
        "-D",
        "angle=0",
        "-D",
        "mode=preview",
    ]


def test_build_frame_plan_serializes_to_plain_json_data(tmp_path: Path) -> None:
    plan = animation.build_frame_plan(
        "model.scad",
        outdir=str(tmp_path),
        frames=1,
        view="iso",
        size="800x600",
        defines=(),
    )

    assert plan.to_json_data() == {
        "model": "model.scad",
        "outdir": str(tmp_path),
        "frames": [
            {
                "index": 0,
                "progress": 0.0,
                "output": os.path.join(str(tmp_path), "frame_0000.png"),
                "view": "iso",
                "size": "800x600",
                "defines": [],
                "render_argv": [
                    "model.scad",
                    "--view",
                    "iso",
                    "-o",
                    os.path.join(str(tmp_path), "frame_0000.png"),
                    "--size",
                    "800x600",
                ],
            }
        ],
    }


@pytest.mark.parametrize("frames", [0, -1])
def test_build_frame_plan_rejects_non_positive_frame_counts(frames: int) -> None:
    with pytest.raises(InvalidArgument, match="--frames"):
        animation.build_frame_plan(
            "model.scad",
            outdir="frames",
            frames=frames,
            view="iso",
            size="800x600",
            defines=(),
        )


def test_build_frame_plan_rejects_bad_define_range() -> None:
    with pytest.raises(InvalidArgument, match="-D"):
        animation.build_frame_plan(
            "model.scad",
            outdir="frames",
            frames=2,
            view="iso",
            size="800x600",
            defines=("angle=0:",),
        )


def test_build_frame_plan_rejects_non_numeric_define_range() -> None:
    with pytest.raises(InvalidArgument, match="-D"):
        animation.build_frame_plan(
            "model.scad",
            outdir="frames",
            frames=2,
            view="iso",
            size="800x600",
            defines=("angle=abc:def",),
        )


@pytest.mark.parametrize("bad_range", ["angle=inf:0", "angle=nan:0"])
def test_build_frame_plan_rejects_non_finite_define_ranges(bad_range: str) -> None:
    with pytest.raises(InvalidArgument, match="-D"):
        animation.build_frame_plan(
            "model.scad",
            outdir="frames",
            frames=2,
            view="iso",
            size="800x600",
            defines=(bad_range,),
        )


def test_build_frame_plan_preserves_colon_constants() -> None:
    plan = animation.build_frame_plan(
        "model.scad",
        outdir="frames",
        frames=2,
        view="iso",
        size="800x600",
        defines=('label="a:b"', "x=enabled?10:20", "ticks=[0:5:60]"),
    )

    assert [frame.defines for frame in plan.frames] == [
        ('label="a:b"', "x=enabled?10:20", "ticks=[0:5:60]"),
        ('label="a:b"', "x=enabled?10:20", "ticks=[0:5:60]"),
    ]


def test_build_frame_plan_rejects_unknown_view() -> None:
    with pytest.raises(InvalidArgument, match="--view"):
        animation.build_frame_plan(
            "model.scad",
            outdir="frames",
            frames=1,
            view="bogus",
            size="800x600",
            defines=(),
        )
