"""Unit tests for commands.score — silhouette AE + IoU."""
from __future__ import annotations

import pathlib
import subprocess
from typing import Any

import pytest

from commands.score import run
from errors import GateFailure, InputNotFound, InvalidArgument, UsageError


def test_score_no_args() -> None:
    assert run([]) == 1


def test_score_help() -> None:
    assert run(["--help"]) == 0


def test_score_missing_second_arg() -> None:
    assert run(["a.png"]) == 1


def test_score_missing_file() -> None:
    with pytest.raises(InputNotFound):
        run(["a.png", "b.png"])


def test_score_unknown_option() -> None:
    with pytest.raises(UsageError):
        run(["a.png", "b.png", "--bogus"])


def test_score_masks_mode(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    a.write_text("")
    b.write_text("")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    monkeypatch.setattr("commands.score._identify_int", lambda p, f: 100)
    monkeypatch.setattr("commands.score.compare_ae", lambda a, b, **kw: "5")
    monkeypatch.setattr("commands.score._m", lambda args, what: "0.5")
    assert run([str(a), str(b), "--masks"]) == 0


def test_score_scad_mode(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    scad = tmp_path / "model.scad"
    scad.write_text("cube(1);")
    ref = tmp_path / "ref.png"
    ref.write_text("")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    monkeypatch.setattr("cli.env.find_openscad", lambda: "/usr/bin/openscad")
    monkeypatch.setattr("commands.score._identify_int", lambda p, f: 100)
    monkeypatch.setattr("commands.score.compare_ae", lambda a, b, **kw: "5")
    monkeypatch.setattr("commands.score._m", lambda args, what: "0.5")
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0))
    assert run([str(scad), str(ref)]) == 0


def test_score_bad_cam(monkeypatch: Any) -> None:
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    with pytest.raises(InvalidArgument):
        run(["model.scad", "ref.png", "--cam", "1,2,3"])


def test_score_zero_area(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    a.write_text("")
    b.write_text("")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    monkeypatch.setattr("commands.score._identify_int", lambda p, f: 0)
    with pytest.raises(GateFailure):
        run([str(a), str(b), "--masks"])


def test_score_ae_parse_fail(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    a.write_text("")
    b.write_text("")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    monkeypatch.setattr("commands.score._identify_int", lambda p, f: 100)
    monkeypatch.setattr("commands.score.compare_ae", lambda a, b, **kw: "nope")
    with pytest.raises(GateFailure):
        run([str(a), str(b), "--masks"])


def test_score_resize_mismatch(monkeypatch: Any, tmp_path: pathlib.Path) -> None:
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    a.write_text("")
    b.write_text("")
    monkeypatch.setattr("os.path.isfile", lambda p: True)
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    calls: list[list[str]] = []
    def fake_identify(p, f):
        return 100 if p == str(a) else 200
    monkeypatch.setattr("commands.score._identify_int", fake_identify)
    monkeypatch.setattr("commands.score.compare_ae", lambda a, b, **kw: "5")
    def fake_m(args, what):
        calls.append(args)
        return "0.5"
    monkeypatch.setattr("commands.score._m", fake_m)
    assert run([str(a), str(b), "--masks"]) == 0
    assert any("-resize" in str(c) for c in calls)
