"""Unit tests for cli.imaging — ImageMagick helpers + pure score math."""
from __future__ import annotations

from typing import Any

import pytest

from cli import imaging
from errors import GateFailure


def test_run_magick_missing(monkeypatch: Any) -> None:
    monkeypatch.setattr("cli.env.find_magick", lambda: None)
    with pytest.raises(GateFailure):
        imaging.run_magick([], what="test")


def test_run_magick_failure(monkeypatch: Any) -> None:
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 1, stderr="boom", stdout=""))
    with pytest.raises(GateFailure):
        imaging.run_magick([], what="test")


def test_run_magick_success(monkeypatch: Any) -> None:
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout="hello"))
    assert imaging.run_magick([], what="test") == "hello"


def test_magick_identify(monkeypatch: Any) -> None:
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout="1200x900"))
    assert imaging.magick_identify("/tmp/x.png", "%wx%h") == "1200x900"


def test_compare_ae(monkeypatch: Any) -> None:
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 1, stderr="42"))
    assert imaging.compare_ae("a.png", "b.png") == "42"


def test_compare_ae_with_fuzz(monkeypatch: Any) -> None:
    monkeypatch.setattr("cli.env.find_magick", lambda: "magick")
    import subprocess
    called: list[list[str]] = []
    def capture(args, **kw):
        called.append(args)
        return subprocess.CompletedProcess(args, 1, stderr="5")
    monkeypatch.setattr(subprocess, "run", capture)
    imaging.compare_ae("a.png", "b.png", fuzz="5%")
    assert "-fuzz" in called[0]
    assert "5%" in called[0]
