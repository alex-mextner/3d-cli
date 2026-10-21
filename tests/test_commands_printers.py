"""Unit tests for commands.printers — printer database."""
from __future__ import annotations

from typing import Any

import pytest
from commands.printers import run
from errors import InvalidArgument, UsageError
from registries.printers import Printer


def test_printers_no_args() -> None:
    assert run([]) == 1


def test_printers_help() -> None:
    assert run(["--help"]) == 0


def test_printers_runs(monkeypatch: Any, capsys: Any) -> None:
    p = type("P", (), {"name": "A1", "bed": (220, 220, 250), "nozzle_mm": 0.4, "firmware": "Klipper"})()
    monkeypatch.setattr("printers.load_printers", lambda command: {"A1": p})
    rc = run(["list"])
    assert rc == 0


def _isolated_registry(monkeypatch: Any, tmp_path: Any) -> Printer:
    """One model `A1` and no fleet/devices: `show` resolves it without touching host config."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("THREED_FLEET", "")  # not the repo fleet: its printers are real ones on the LAN
    monkeypatch.chdir(tmp_path)
    p = Printer(name="A1", bed=[220.0, 220.0, 250.0], nozzle_mm=0.4, firmware="klipper", material="PLA")
    monkeypatch.setattr("printers.load_printers", lambda command: {"A1": p})
    return p


def test_printers_show_a_model_prints_its_spec(monkeypatch: Any, tmp_path: Any, capsys: Any) -> None:
    _isolated_registry(monkeypatch, tmp_path)

    assert run(["show", "A1"]) == 0
    out = capsys.readouterr().out
    assert "build volume   220 x 220 x 250 mm" in out and "default mat.   PLA" in out


def test_printers_show_unknown_name_is_invalid_argument(monkeypatch: Any, tmp_path: Any) -> None:
    _isolated_registry(monkeypatch, tmp_path)

    with pytest.raises(InvalidArgument) as err:
        run(["show", "X1"])
    assert "A1" in err.value.accepted


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (["show"], UsageError),
        (["show", "a", "b"], UsageError),
        (["show", "A1", "--bogus"], UsageError),
        (["show", "A1", "--history"], UsageError),
        (["show", "A1", "--history", "many"], InvalidArgument),
        (["show", "A1", "--history", "-1"], InvalidArgument),
        (["show", "A1", "--history", "9" * 5000], InvalidArgument),
        (["show", "A1", "--secs", "0"], InvalidArgument),
        (["show", "A1", "--secs", "nan"], InvalidArgument),
        (["show", "A1", "--secs", "inf"], InvalidArgument),
    ],
)
def test_printers_show_rejects_bad_arguments_before_reading_anything(
    monkeypatch: Any, tmp_path: Any, argv: list[str], error: type[Exception]
) -> None:
    _isolated_registry(monkeypatch, tmp_path)

    with pytest.raises(error):
        run(argv)


def test_printers_show_help_prints_usage_without_reading_anything(capsys: Any) -> None:
    assert run(["show", "--help"]) == 0
    assert "--no-live" in capsys.readouterr().out
