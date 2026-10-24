"""Unit tests for commands.materials — filament database."""
from __future__ import annotations

from typing import Any

import pytest
from commands.materials import run
from errors import InvalidArgument


def test_materials_no_args() -> None:
    assert run([]) == 1


def test_materials_help() -> None:
    assert run(["--help"]) == 0


def test_materials_runs(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr("materials.load_materials", lambda: {"PLA": type("M", (), {"name": "PLA", "density": 1.2, "max_temp_c": 60, "finish": "matte"})()})
    rc = run(["list"])
    assert rc == 0


def test_materials_info(monkeypatch: Any, capsys: Any) -> None:
    m = type("M", (), {"name": "PLA", "density": 1.2, "e_modulus_mpa": 3500, "tensile_mpa": 65, "yield_mpa": 55, "max_temp_c": 60, "color": "#1f9c4b", "finish": "matte", "layer_adhesion": 0.45, "shrink_xy_pct": 0.3, "shrink_z_pct": None})()
    monkeypatch.setattr("materials.get_material", lambda name: m)
    rc = run(["show", "PLA"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "shrink_xy" in out and "0.3 %" in out and "shrink_z" not in out


def test_materials_not_found(monkeypatch: Any, capsys: Any) -> None:
    def raise_invalid(name):
        raise InvalidArgument("name", name, ["PLA"])
    monkeypatch.setattr("materials.get_material", raise_invalid)
    with pytest.raises(InvalidArgument):
        run(["show", "ABS"])
