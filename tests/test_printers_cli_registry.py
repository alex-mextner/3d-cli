"""The self-registering `3d printers <sub>` registry (lib/printers_cli)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from commands.printers import run
from errors import UsageError
from printers_cli import Sub, discover, dispatch, resolve


def test_discover_is_sorted_and_every_sub_is_complete() -> None:
    subs = discover()
    assert list(subs) == sorted(subs)
    for name, sub in subs.items():
        assert sub.name == name
        assert sub.summary and sub.usage and callable(sub.run)


def test_help_lists_every_discovered_sub(capsys: Any) -> None:
    assert run(["--help"]) == 0
    out = capsys.readouterr().out
    for name in discover():
        assert f"  {name}" in out


def test_unknown_sub_is_a_usage_error() -> None:
    with pytest.raises(UsageError) as err:
        run(["nope-not-a-sub"])
    assert "nope-not-a-sub" in err.value.message
    with pytest.raises(UsageError):
        dispatch(["nope-not-a-sub"])


def test_resolve_matches_names_and_aliases() -> None:
    for sub in discover().values():
        assert resolve(sub.name) is sub
        for alias in sub.aliases:
            assert resolve(alias) is sub
    assert resolve("nope-not-a-sub") is None


def test_duplicate_names_are_refused(tmp_path: Path, monkeypatch: Any) -> None:
    import printers_cli

    pkg = tmp_path / "dup_pkg"
    pkg.mkdir()
    body = (
        "from printers_cli import Sub\n"
        "SUB = Sub(name='twin', summary='s', usage='u', run=lambda argv: 0)\n"
    )
    (pkg / "one.py").write_text(body)
    (pkg / "two.py").write_text(body)
    (pkg / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(printers_cli, "__path__", [str(pkg)])
    monkeypatch.setattr(printers_cli, "__name__", "dup_pkg")
    with pytest.raises(ValueError, match="duplicate"):
        printers_cli.discover()


def test_sub_is_frozen() -> None:
    sub = Sub(name="x", summary="s", usage="u", run=lambda argv: 0)
    with pytest.raises(AttributeError):
        sub.name = "y"  # type: ignore[misc]
