"""Unit tests for cli.dispatch — the thin typed dispatcher."""
from __future__ import annotations

from typing import Any


from cli.dispatch import _suggest, main, usage, VERSION
from cli.registry import Command, Registry


def test_usage_contains_version() -> None:
    reg = Registry()
    reg.add(Command(name="z", summary="s", run=lambda argv: 0, group="G"))
    text = usage(reg)
    assert VERSION in text
    assert "3d" in text
    assert "z" in text


def test_usage_shows_aliases() -> None:
    reg = Registry()
    reg.add(Command(name="check", summary="s", run=lambda argv: 0, group="G", aliases=("acceptance",)))
    text = usage(reg)
    assert "alias: acceptance" in text


def test_suggest_finds_close_matches() -> None:
    reg = Registry()
    reg.add(Command(name="render", summary="s", run=lambda argv: 0, group="G"))
    reg.add(Command(name="export", summary="s", run=lambda argv: 0, group="G"))
    assert _suggest(reg, "ren") == ["render"]
    assert _suggest(reg, "ex") == ["export"]
    assert _suggest(reg, "xyz") == []


def test_main_help(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr("cli.dispatch.export_openscadpath", lambda: None)
    monkeypatch.setattr("cli.dispatch.maybe_bootstrap", lambda: None)
    rc = main(["help"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "USAGE" in captured.out


def test_main_version(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr("cli.dispatch.export_openscadpath", lambda: None)
    monkeypatch.setattr("cli.dispatch.maybe_bootstrap", lambda: None)
    rc = main(["version"])
    captured = capsys.readouterr()
    assert rc == 0
    assert VERSION in captured.out


def test_main_unknown_command(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr("cli.dispatch.export_openscadpath", lambda: None)
    monkeypatch.setattr("cli.dispatch.maybe_bootstrap", lambda: None)
    rc = main(["nope"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "unknown command" in captured.err


def test_main_routes_command(monkeypatch: Any) -> None:
    monkeypatch.setattr("cli.dispatch.export_openscadpath", lambda: None)
    monkeypatch.setattr("cli.dispatch.maybe_bootstrap", lambda: None)
    rc = main(["render", "--help"])
    assert rc == 0


def test_main_catches_threed_error(monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr("cli.dispatch.export_openscadpath", lambda: None)
    monkeypatch.setattr("cli.dispatch.maybe_bootstrap", lambda: None)
    from errors import InvalidArgument
    def bad_run(argv: list[str]) -> int:
        raise InvalidArgument("--x", "y", ["z"])
    reg = Registry()
    reg.add(Command(name="bad", summary="s", run=bad_run, group="G"))
    monkeypatch.setattr("cli.dispatch.discover", lambda: reg)
    rc = main(["bad"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "bad:" in captured.err or "--x" in captured.err


def test_main_catches_broken_pipe(monkeypatch: Any) -> None:
    monkeypatch.setattr("cli.dispatch.export_openscadpath", lambda: None)
    monkeypatch.setattr("cli.dispatch.maybe_bootstrap", lambda: None)
    def pipe_run(argv: list[str]) -> int:
        raise BrokenPipeError()
    reg = Registry()
    reg.add(Command(name="pipe", summary="s", run=pipe_run, group="G"))
    monkeypatch.setattr("cli.dispatch.discover", lambda: reg)
    import io
    fake_stdout = io.StringIO()
    monkeypatch.setattr("sys.stdout", fake_stdout)
    rc = main(["pipe"])
    assert rc == 0


def test_main_catches_keyboard_interrupt(monkeypatch: Any) -> None:
    monkeypatch.setattr("cli.dispatch.export_openscadpath", lambda: None)
    monkeypatch.setattr("cli.dispatch.maybe_bootstrap", lambda: None)
    def ki_run(argv: list[str]) -> int:
        raise KeyboardInterrupt()
    reg = Registry()
    reg.add(Command(name="ki", summary="s", run=ki_run, group="G"))
    monkeypatch.setattr("cli.dispatch.discover", lambda: reg)
    rc = main(["ki"])
    assert rc == 130
