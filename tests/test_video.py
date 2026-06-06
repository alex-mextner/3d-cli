"""Tests for video planning and export orchestration."""
from __future__ import annotations

import os
from pathlib import Path
import struct
import threading
import time

import pytest

from commands.video import run as run_video_command
from errors import GateFailure, InvalidArgument, UsageError
from video import (
    collect_progress_frames,
    encode_frames,
    parse_size,
    plan_turntable,
    render_turntable_frames,
    scad_bounds,
    stl_bounds,
)


def test_parse_size_requires_positive_width_and_height() -> None:
    assert parse_size("640x480") == (640, 480)

    with pytest.raises(InvalidArgument):
        parse_size("640,480")
    with pytest.raises(InvalidArgument):
        parse_size("0x480")


def test_video_submodes_accept_help_without_positional_inputs(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_video_command(["turntable", "--help"]) == 0
    assert "3d video <turntable|progress>" in capsys.readouterr().out

    assert run_video_command(["progress", "--help"]) == 0
    assert "3d video <turntable|progress>" in capsys.readouterr().out


def test_turntable_rejects_bad_ranges_before_dependency_checks(tmp_path: Path) -> None:
    source = tmp_path / "part.scad"
    source.write_text("cube([1, 1, 1]);", encoding="utf-8")

    with pytest.raises(InvalidArgument):
        run_video_command(["turntable", os.fspath(source), "--radius", "0"])
    with pytest.raises(InvalidArgument):
        run_video_command(["turntable", os.fspath(source), "--elevation", "100"])
    with pytest.raises(InvalidArgument):
        run_video_command(["turntable", os.fspath(source), "--degrees", "0"])


def test_plan_turntable_creates_seamless_orbit_without_duplicate_endpoint(tmp_path: Path) -> None:
    plan = plan_turntable(
        "part.scad",
        os.fspath(tmp_path),
        frames=4,
        fps=12,
        size=(320, 240),
        radius=100.0,
        elevation=30.0,
    )

    assert [frame.index for frame in plan.frames] == [0, 1, 2, 3]
    assert [round(frame.angle_deg, 3) for frame in plan.frames] == [0.0, 90.0, 180.0, 270.0]
    assert plan.frames[0].camera == "0.0000,-86.6025,50.0000,0.0000,0.0000,0.0000"
    assert plan.frames[1].camera == "86.6025,-0.0000,50.0000,0.0000,0.0000,0.0000"
    assert plan.frames[0].path.endswith("frame_0000.png")


def test_plan_turntable_uses_model_center_for_eye_and_lookat(tmp_path: Path) -> None:
    plan = plan_turntable(
        "part.scad",
        os.fspath(tmp_path),
        frames=1,
        fps=12,
        size=(320, 240),
        radius=100.0,
        elevation=0.0,
        center=(10.0, 20.0, 30.0),
    )

    assert plan.frames[0].camera == "10.0000,-80.0000,30.0000,10.0000,20.0000,30.0000"


def test_plan_turntable_includes_endpoint_for_partial_orbit(tmp_path: Path) -> None:
    plan = plan_turntable(
        "part.scad",
        os.fspath(tmp_path),
        frames=2,
        fps=12,
        size=(320, 240),
        radius=100.0,
        degrees=180.0,
    )

    assert [frame.angle_deg for frame in plan.frames] == [0.0, 180.0]


def test_stl_bounds_reads_binary_stl_center_and_diagonal(tmp_path: Path) -> None:
    stl = tmp_path / "box.stl"
    vertices = [
        (-1.0, -2.0, -3.0),
        (3.0, -2.0, -3.0),
        (3.0, 4.0, 5.0),
    ]
    payload = bytearray(b"\0" * 80)
    payload.extend(struct.pack("<I", 1))
    payload.extend(struct.pack("<3f", 0.0, 0.0, 1.0))
    for vertex in vertices:
        payload.extend(struct.pack("<3f", *vertex))
    payload.extend(struct.pack("<H", 0))
    stl.write_bytes(bytes(payload))

    bounds = stl_bounds(os.fspath(stl))

    assert bounds.center == (1.0, 1.0, 1.0)
    assert round(bounds.diagonal, 4) == 10.7703


def test_scad_bounds_wraps_openscad_with_xvfb_when_headless(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr("video.shutil.which", lambda name: "/usr/bin/xvfb-run" if name == "xvfb-run" else None)
    calls: list[list[str]] = []

    def fake_runner(argv: list[str]) -> int:
        calls.append(argv)
        stl = tmp_path / "turntable_bounds.stl"
        payload = bytearray(b"\0" * 80)
        payload.extend(struct.pack("<I", 1))
        payload.extend(struct.pack("<3f", 0.0, 0.0, 1.0))
        for vertex in ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)):
            payload.extend(struct.pack("<3f", *vertex))
        payload.extend(struct.pack("<H", 0))
        stl.write_bytes(bytes(payload))
        return 0

    bounds = scad_bounds("part.scad", openscad="/opt/openscad", tempdir=os.fspath(tmp_path), runner=fake_runner)

    assert bounds.center == (0.5, 0.5, 0.0)
    assert calls[0][:4] == [
        "/usr/bin/xvfb-run",
        "--auto-servernum",
        "--server-args=-screen 0 1280x1024x24",
        "/opt/openscad",
    ]


def test_collect_progress_frames_sorts_pngs_naturally(tmp_path: Path) -> None:
    for name in ("frame_10.png", "frame_1.png", "notes.txt", "frame_2.PNG"):
        (tmp_path / name).write_text("x")

    frames = collect_progress_frames(os.fspath(tmp_path), "*.png")

    assert [Path(path).name for path in frames] == ["frame_1.png", "frame_2.PNG", "frame_10.png"]


def test_collect_progress_frames_rejects_empty_directory(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        collect_progress_frames(os.fspath(tmp_path), "*.png")


def test_progress_default_output_preserves_directory_name(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    frames_dir = tmp_path / "run.v1"
    frames_dir.mkdir()
    (frames_dir / "frame_0001.png").write_text("png")

    assert run_video_command(["progress", os.fspath(frames_dir), "--dry-run"]) == 0
    assert f"output: {frames_dir}.mp4" in capsys.readouterr().out

    monkeypatch.chdir(frames_dir)
    assert run_video_command(["progress", ".", "--dry-run"]) == 0
    assert "output: run.v1.mp4" in capsys.readouterr().out


def test_render_turntable_frames_calls_existing_render_command(tmp_path: Path) -> None:
    plan = plan_turntable(
        "part.scad",
        os.fspath(tmp_path),
        frames=2,
        fps=10,
        size=(200, 100),
        radius=50.0,
        elevation=0.0,
        defines=["quality=2"],
    )
    calls: list[list[str]] = []

    def fake_runner(argv: list[str]) -> int:
        calls.append(argv)
        Path(argv[argv.index("-o") + 1]).write_text("png")
        return 0

    render_turntable_frames(plan, bin3d="/repo/bin/3d", runner=fake_runner)

    assert calls[0] == [
        "/repo/bin/3d",
        "render",
        "part.scad",
        "--cam",
        "0.0000,-50.0000,0.0000,0.0000,0.0000,0.0000",
        "--size",
        "200x100",
        "-D",
        "quality=2",
        "-o",
        os.fspath(tmp_path / "frame_0000.png"),
    ]
    assert len(calls) == 2


def test_render_turntable_frames_raises_gate_failure_on_failed_render(tmp_path: Path) -> None:
    plan = plan_turntable("part.scad", os.fspath(tmp_path), frames=1, fps=10, size=(200, 100))

    with pytest.raises(GateFailure):
        render_turntable_frames(plan, bin3d="/repo/bin/3d", runner=lambda _argv: 42)


def test_render_turntable_frames_runs_independent_frames_concurrently(tmp_path: Path) -> None:
    plan = plan_turntable("part.scad", os.fspath(tmp_path), frames=3, fps=10, size=(200, 100))
    lock = threading.Lock()
    active = 0
    max_active = 0

    def fake_runner(argv: list[str]) -> int:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.03)
        Path(argv[argv.index("-o") + 1]).write_text("png")
        with lock:
            active -= 1
        return 0

    render_turntable_frames(plan, bin3d="/repo/bin/3d", runner=fake_runner, jobs=3)

    assert max_active > 1


def test_encode_frames_writes_concat_manifest_and_invokes_ffmpeg(tmp_path: Path) -> None:
    frames = [tmp_path / "a.png", tmp_path / "b.png"]
    for frame in frames:
        frame.write_text("png")
    calls: list[list[str]] = []

    def fake_runner(argv: list[str]) -> int:
        calls.append(argv)
        Path(argv[-1]).write_text("mp4")
        return 0

    out = tmp_path / "turntable.mp4"
    encode_frames([os.fspath(frame) for frame in frames], os.fspath(out), fps=24, runner=fake_runner)

    assert calls
    assert calls[0][0] == "ffmpeg"
    assert "-f" in calls[0]
    assert calls[0][calls[0].index("-vf") + 1] == "pad=ceil(iw/2)*2:ceil(ih/2)*2,format=yuv420p"
    manifest = Path(calls[0][calls[0].index("-i") + 1])
    text = manifest.read_text()
    assert "file '" in text
    assert "duration 0.041667" in text
    assert out.exists()


def test_encode_frames_does_not_apply_mp4_flags_to_webm(tmp_path: Path) -> None:
    frame = tmp_path / "a.png"
    frame.write_text("png")
    calls: list[list[str]] = []

    def fake_runner(argv: list[str]) -> int:
        calls.append(argv)
        Path(argv[-1]).write_text("webm")
        return 0

    encode_frames([os.fspath(frame)], os.fspath(tmp_path / "clip.webm"), fps=12, runner=fake_runner)

    assert "-movflags" not in calls[0]
