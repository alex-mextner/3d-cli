"""Unit tests for commands.printers — printer database."""
from __future__ import annotations

from typing import Any

import pytest
from commands.printers import run
from errors import InvalidArgument


def test_printers_no_args() -> None:
    assert run([]) == 1


def test_printers_help() -> None:
    assert run(["--help"]) == 0


def test_printers_runs(monkeypatch: Any, capsys: Any) -> None:
    p = type("P", (), {"name": "A1", "bed": (220, 220, 250), "nozzle_mm": 0.4, "firmware": "Klipper"})()
    monkeypatch.setattr("printers.load_printers", lambda command: {"A1": p})
    rc = run(["list"])
    assert rc == 0


def test_printers_info(monkeypatch: Any, capsys: Any) -> None:
    p = type("P", (), {"name": "A1", "bed": (220, 220, 250), "nozzle_mm": 0.4, "firmware": "Klipper", "material": "PLA"})()
    monkeypatch.setattr("printers.get_printer", lambda name, command: p)
    rc = run(["show", "A1"])
    assert rc == 0


def test_printers_not_found(monkeypatch: Any, capsys: Any) -> None:
    def raise_invalid(name, command):
        raise InvalidArgument("name", name, ["A1"])
    monkeypatch.setattr("printers.get_printer", raise_invalid)
    with pytest.raises(InvalidArgument):
        run(["show", "X1"])
