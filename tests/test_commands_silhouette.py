"""Unit tests for commands.silhouette — binary silhouette mask."""
from __future__ import annotations

import pathlib
import subprocess
from typing import Any

import pytest

from commands.silhouette import run
from errors import GateFailure, InputNotFound, InvalidArgument, UsageError


def test_silhouette_no_args() -> None:
    assert run([]) == 1


def test_silhouette_help() -> None:
    assert run(["--help"]) == 0


def test_silhouette_missing_file() -> None:
    with pytest.raises(InputNotFound):
        run(["missing.scad"])


def test_silhouette_bad_cam(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    with pytest.raises(InvalidArgument):
        run([str(scad), "--cam", "1,2,3"])


def test_silhouette_success(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("commands.silhouette.magick_identify", lambda p, f: "1200x900")
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout="", stderr=""))
    assert run([str(scad), "-o", str(tmp_path / "mask.png")]) == 0


def test_silhouette_render_fail(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 1, stdout="", stderr=""))
    with pytest.raises(GateFailure):
        run([str(scad)])


def test_silhouette_mask_fail(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    calls: list[list[str]] = []

    def fake_run(args, **kw):
        calls.append(args)
        if "--render" in " ".join(args):
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="magick err")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    with pytest.raises(GateFailure):
        run([str(scad)])


def test_silhouette_ortho(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("commands.silhouette.magick_identify", lambda p, f: "1200x900")
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout="", stderr=""))
    assert run([str(scad), "--ortho"]) == 0


def test_silhouette_unknown_option() -> None:
    with pytest.raises(UsageError):
        run(["model.scad", "--bogus"])


def test_silhouette_d(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("commands.silhouette.magick_identify", lambda p, f: "1200x900")
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout="", stderr=""))
    assert run([str(scad), "-D", "x=1"]) == 0
