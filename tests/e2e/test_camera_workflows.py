"""`3d camera` end to end: bin/3d → a real topcam node HTTP server around a fake C920 (#340)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from topcam_fixtures import FakeCamera, serve_node, install_fake_camera

from .workflow_helper import run_cli


@pytest.fixture
def fake_camera(monkeypatch: pytest.MonkeyPatch) -> FakeCamera:
    return install_fake_camera(monkeypatch)


def test_help_lists_every_subcommand(tmp_path: Path) -> None:
    """`3d camera --help` lists every subcommand the user can run."""
    result = run_cli(tmp_path, "camera", "--help")
    assert result.returncode == 0
    for word in ("controls", "still", "serve", "--exposure", "--average", "--led"):
        assert word in result.stdout


def test_still_from_a_node_writes_png_and_readback(
    tmp_path: Path, fake_camera: FakeCamera
) -> None:
    """The user takes a still from a camera node: a PNG plus its JSON readback land on disk."""
    out = tmp_path / "plate.png"
    with serve_node(fake_camera) as (url, _):
        result = run_cli(
            tmp_path,
            "camera",
            "still",
            "--camera",
            url,
            "--res",
            "640x480",
            "--focus",
            "40",
            "--exposure",
            "300",
            "--gain",
            "0",
            "--wb",
            "4500",
            "--ctrl",
            "brightness=100",
            "--frames",
            "3",
            "--skip",
            "2",
            "--average",
            "median",
            "--depth",
            "16",
            "--out",
            str(out),
            "--json",
        )
    assert result.returncode == 0, result.stderr
    meta = json.loads(result.stdout)
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert json.loads(out.with_suffix(".json").read_text()) == meta
    assert (
        meta["width"],
        meta["height"],
        meta["frames"],
        meta["average"],
        meta["depth"],
    ) == (640, 480, 3, "median", 16)
    assert meta["controls"]["exposure_time_absolute"] == 300
    assert meta["controls"]["focus_automatic_continuous"] == 0
    assert meta["requested"]["ctrl"] == {"brightness": 100}
    assert meta["source"]["backend"] == "node"
    assert fake_camera.captures[-1]["skip"] == 2


def test_controls_list_and_set_via_node(
    tmp_path: Path, fake_camera: FakeCamera
) -> None:
    """The user lists the camera controls on a node and sets one."""
    with serve_node(fake_camera) as (url, _):
        listing = run_cli(tmp_path, "camera", "controls", "--camera", url)
        setting = run_cli(
            tmp_path,
            "camera",
            "controls",
            "--camera",
            url,
            "--set",
            "focus_auto=0,focus=55",
            "--json",
        )
    assert listing.returncode == 0, listing.stderr
    assert "exposure_time_absolute (exposure)" in listing.stdout
    assert "format YUYV: 640x480 1920x1080 2304x1296 2304x1536" in listing.stdout
    assert json.loads(setting.stdout) == {
        "set": {"focus_automatic_continuous": 0, "focus_absolute": 55}
    }


def test_errors_are_structured(tmp_path: Path, fake_camera: FakeCamera) -> None:
    """Bad requests to a node come back as structured errors, not tracebacks."""
    with serve_node(fake_camera) as (url, _):
        bad_res = run_cli(
            tmp_path,
            "camera",
            "still",
            "--camera",
            url,
            "--res",
            "123x45",
            "--out",
            str(tmp_path / "x.png"),
        )
    assert bad_res.returncode == 1
    assert "offers 640x480" in bad_res.stderr
    usage = run_cli(tmp_path, "camera", "still", "--res", "big")
    assert usage.returncode == 2 and "--res" in usage.stderr
    unreachable = run_cli(
        tmp_path, "camera", "controls", "--camera", "http://127.0.0.1:9"
    )
    assert unreachable.returncode == 1 and "unreachable" in unreachable.stderr
    unknown = run_cli(tmp_path, "camera", "focus")
    assert unknown.returncode == 2 and "unknown subcommand" in unknown.stderr


def test_serve_with_a_missing_device_fails_cleanly(tmp_path: Path) -> None:
    """`3d camera serve` with no camera device fails with a clear message."""
    result = run_cli(
        tmp_path,
        "camera",
        "serve",
        "--device",
        "/dev/video-none",
        "--port",
        "0",
        "--no-stream",
        timeout=30,
    )
    assert result.returncode == 1
    assert "cannot open" in result.stderr


def test_help_examples_and_flags_are_in_the_docs() -> None:
    """Every example and flag in `3d camera --help` is in the docs."""
    import importlib

    usage = importlib.import_module("commands.camera").USAGE
    doc = (Path(__file__).resolve().parents[2] / "docs" / "commands" / "camera.md").read_text()
    examples = [ln.strip() for ln in usage.split("Examples:", 1)[1].splitlines() if ln.strip()]
    assert examples
    for line in examples:
        assert line in doc, f"example missing from docs/commands/camera.md: {line}"
    for flag in sorted({w for w in usage.split() if w.startswith("--")}):
        name = flag.split("=")[0].rstrip(",];)")
        assert f"`{name}" in doc, f"{name} undocumented in docs/commands/camera.md"
