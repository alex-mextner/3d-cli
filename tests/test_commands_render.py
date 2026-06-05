"""Unit tests for commands.render — unified view/multi/section render."""
from __future__ import annotations

import pathlib
from typing import Any

import pytest

from commands.render import run
from errors import InputNotFound, UsageError


def test_render_no_args() -> None:
    assert run([]) == 1


def test_render_help() -> None:
    assert run(["--help"]) == 0


def test_render_missing_file() -> None:
    with pytest.raises(InputNotFound):
        run(["missing.scad"])


def test_render_unknown_option() -> None:
    with pytest.raises(UsageError):
        run(["model.scad", "--bogus"])


def test_render_single_view(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("commands.render.run_tool", lambda d, s, a: 0)
    assert run([str(scad), "--view", "left", "-o", str(tmp_path / "out.png")]) == 0


def test_render_multi(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("commands.render.run_tool", lambda d, s, a: 0)
    assert run([str(scad), "--multi", str(tmp_path / "previews")]) == 0


def test_render_section(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("commands.render.run_tool", lambda d, s, a: 0)
    assert run([str(scad), "--section", "--plane", "YZ", "-o", str(tmp_path / "sec.png")]) == 0


def test_render_option_needs_value() -> None:
    with pytest.raises(UsageError):
        run(["model.scad", "--view"])
    with pytest.raises(UsageError):
        run(["model.scad", "--size"])
    with pytest.raises(UsageError):
        run(["model.scad", "-D"])
    with pytest.raises(UsageError):
        run(["model.scad", "-o"])


def test_render_multi_no_outdir(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("commands.render.run_tool", lambda d, s, a: 0)
    assert run([str(scad), "--multi"]) == 0


def test_render_ortho(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("commands.render.run_tool", lambda d, s, a: 0)
    assert run([str(scad), "--ortho"]) == 0


def test_render_color(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("commands.render.run_tool", lambda d, s, a: 0)
    assert run([str(scad), "--section", "--color"]) == 0
