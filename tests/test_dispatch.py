"""Unit tests for cli.dispatch — the thin typed dispatcher."""
from __future__ import annotations

import importlib.metadata
import pathlib
import re
from typing import Any


import cli.dispatch as dispatch
from cli.dispatch import (
    _resolve_version,
    _suggest,
    _version_from_pyproject,
    main,
    usage,
    VERSION,
)
from cli.registry import Command, Registry


def _pyproject_version() -> str:
    """The declared version, read independently of dispatch's own parser, so the
    drift guard would still catch a parser bug that happens to agree with itself.
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    # The `[project]` block: from its header up to the next top-level table header.
    after = text.split("[project]", 1)[1]
    block = re.split(r"(?m)^\[", after, maxsplit=1)[0]
    m = re.search(r'(?m)^version\s*=\s*["\']([^"\']+)["\']', block)
    assert m is not None, "could not find [project] version in pyproject.toml"
    return m.group(1)


def test_version_matches_pyproject_no_drift() -> None:
    # Drift guard: the version the CLI reports MUST equal pyproject's declared
    # version. Pinned to the checkout path (the dist isn't installed in CI).
    assert _version_from_pyproject() == _pyproject_version()
    assert VERSION == _pyproject_version()


def test_version_is_not_the_stale_hardcoded_literal() -> None:
    # The bug being fixed: a frozen `VERSION = "0.1.0"` literal. The version must
    # come from pyproject, which has since moved off 0.1.0.
    assert VERSION != "0.1.0"
    assert _pyproject_version() != "0.1.0"


def test_resolve_version_prefers_checkout_pyproject(monkeypatch: Any) -> None:
    # `bin/3d` runs THIS checkout's lib/, so the checkout pyproject is authoritative
    # and a stale installed `.dist-info` (here 0.1.0) must NOT shadow it.
    monkeypatch.setattr(dispatch, "_version_from_pyproject", lambda: "3.4.5")
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "0.1.0")
    assert _resolve_version() == "3.4.5"


def test_resolve_version_falls_back_to_metadata_when_no_pyproject(
    monkeypatch: Any,
) -> None:
    # No pyproject on disk (a real installed-wheel run): use installed metadata.
    monkeypatch.setattr(dispatch, "_version_from_pyproject", lambda: None)
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "7.8.9")
    assert _resolve_version() == "7.8.9"


def test_resolve_version_unknown_when_no_source(monkeypatch: Any) -> None:
    # Neither pyproject nor installed metadata: a sentinel, never a stale literal.
    def _not_found(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(dispatch, "_version_from_pyproject", lambda: None)
    monkeypatch.setattr(importlib.metadata, "version", _not_found)
    assert _resolve_version() == "0+unknown"


def test_version_from_pyproject_is_project_scoped(tmp_path: Any, monkeypatch: Any) -> None:
    # The parser must take `[project]` version, never a `version =` in another
    # table, and must tolerate TOML's optional leading indentation.
    (tmp_path / "pyproject.toml").write_text(
        "[tool.poetry]\n"
        'version = "9.9.9"\n'
        "\n"
        "[project]\n"
        '  name = "x"\n'
        '  version = "1.2.3"\n'
        "\n"
        "[tool.other]\n"
        'version = "8.8.8"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(dispatch, "repo_root", lambda: str(tmp_path))
    assert _version_from_pyproject() == "1.2.3"


def test_version_from_pyproject_missing_file(tmp_path: Any, monkeypatch: Any) -> None:
    # No pyproject.toml on disk -> None (so _resolve_version yields the sentinel).
    monkeypatch.setattr(dispatch, "repo_root", lambda: str(tmp_path))
    assert _version_from_pyproject() is None


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
