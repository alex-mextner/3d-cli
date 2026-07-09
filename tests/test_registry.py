"""Unit tests for the command registry + alias resolution."""
from __future__ import annotations

import pytest

from cli.registry import Command, Registry, discover


def _cmd(name: str, *, aliases: tuple[str, ...] = (), group: str = "META") -> Command:
    return Command(name=name, summary="s", run=lambda argv: 0, group=group, aliases=aliases)


def test_add_and_resolve() -> None:
    reg = Registry()
    c = _cmd("foo")
    reg.add(c)
    assert reg.resolve("foo") is c
    assert reg.resolve("nope") is None


def test_alias_resolves_to_canonical() -> None:
    reg = Registry()
    c = _cmd("check", aliases=("acceptance",))
    reg.add(c)
    assert reg.resolve("acceptance") is c
    assert reg.alias_map() == {"acceptance": "check"}


def test_duplicate_name_rejected() -> None:
    reg = Registry()
    reg.add(_cmd("foo"))
    with pytest.raises(ValueError):
        reg.add(_cmd("foo"))


def test_duplicate_alias_rejected() -> None:
    reg = Registry()
    reg.add(_cmd("a", aliases=("x",)))
    with pytest.raises(ValueError):
        reg.add(_cmd("b", aliases=("x",)))


def test_command_name_reusing_existing_alias_rejected() -> None:
    # adding a command whose NAME equals an earlier alias must fail (not silently
    # shadow the alias and make routing depend on discovery order).
    reg = Registry()
    reg.add(_cmd("check", aliases=("acceptance",)))
    with pytest.raises(ValueError):
        reg.add(_cmd("acceptance"))


def test_commands_sorted_by_group_then_name() -> None:
    reg = Registry()
    reg.add(_cmd("zenv", group="ENVIRONMENT"))
    reg.add(_cmd("render", group="RENDER & VIEW"))
    reg.add(_cmd("export", group="GEOMETRY & EXPORT"))
    order = [c.name for c in reg.commands()]
    assert order.index("render") < order.index("export") < order.index("zenv")


def test_names_includes_aliases() -> None:
    reg = Registry()
    reg.add(_cmd("check", aliases=("acceptance",)))
    assert "check" in reg.names()
    assert "acceptance" in reg.names()


def test_real_discovery_finds_core_commands() -> None:
    reg = discover()
    for name in ("render", "export", "check", "mesh", "slice", "doctor", "libs", "score"):
        assert reg.resolve(name) is not None, f"{name} not discovered"
    assert reg.resolve("test") is None, "test is an internal dev script, not a product command"
    # the documented aliases resolve to their canonical commands.
    assert reg.resolve("acceptance") is reg.resolve("check")
    assert reg.resolve("multi") is not None
    assert reg.resolve("section") is not None
