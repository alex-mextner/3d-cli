"""Unit tests for commands.overlay — difference/ghost/canny diagnostics."""
from __future__ import annotations

import pathlib
import subprocess
from typing import Any

import pytest

from commands.overlay import run
from errors import InputNotFound, UsageError


def test_overlay_no_args() -> None:
    assert run([]) == 1


def test_overlay_help() -> None:
    assert run(["--help"]) == 0


def test_overlay_missing_second() -> None:
    assert run(["a.png"]) == 1


def test_overlay_missing_file() -> None:
    with pytest.raises(InputNotFound):
        run(["a.png", "b.png"])


def test_overlay_unknown_option() -> None:
    with pytest.raises(UsageError):
        run(["a.png", "b.png", "--bogus"])


def test_overlay_success(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    r = tmp_path / "render.png"
    ref = tmp_path / "ref.png"
    r.write_text("")
    ref.write_text("")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    monkeypatch.setattr("cli.imaging.magick_identify", lambda p, f: "1200x900")
    monkeypatch.setattr("cli.imaging.compare_ae", lambda a, b, **kw: "12")
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout="", stderr=""))
    assert run([str(r), str(ref)]) == 0


def test_overlay_out_dir(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    r = tmp_path / "render.png"
    ref = tmp_path / "ref.png"
    r.write_text("")
    ref.write_text("")
    out = tmp_path / "out"
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    monkeypatch.setattr("cli.imaging.magick_identify", lambda p, f: "1200x900")
    monkeypatch.setattr("cli.imaging.compare_ae", lambda a, b, **kw: "12")
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout="", stderr=""))
    assert run([str(r), str(ref), "-o", str(out)]) == 0


def test_overlay_magick_fail(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    r = tmp_path / "render.png"
    ref = tmp_path / "ref.png"
    r.write_text("")
    ref.write_text("")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    monkeypatch.setattr("cli.imaging.magick_identify", lambda p, f: "1200x900")
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 1, stderr="err", stdout=""))
    from errors import GateFailure
    with pytest.raises(GateFailure):
        run([str(r), str(ref)])
