"""`--help` text and docs/commands/*.md stay in step for the prep commands (AGENTS.md help-sync rule)."""
from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import cast

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMMANDS = ("prep", "heavy", "remote")


def _flags(usage: str) -> set[str]:
    return set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", usage))


@pytest.mark.parametrize("name", COMMANDS)
def test_every_flag_in_the_help_is_documented_and_exemplified(name: str) -> None:
    usage = cast(str, importlib.import_module(f"commands.{name}").USAGE)
    doc = (ROOT / "docs" / "commands" / f"{name}.md").read_text(encoding="utf-8")
    missing = sorted(f for f in _flags(usage) if f not in doc)
    assert not missing, f"flags in `3d {name} --help` but not in docs/commands/{name}.md: {missing}"
    assert "Examples:" in usage and "Examples:" in doc


@pytest.mark.parametrize("name", COMMANDS)
def test_every_help_example_appears_in_the_doc(name: str) -> None:
    usage = cast(str, importlib.import_module(f"commands.{name}").USAGE)
    doc = (ROOT / "docs" / "commands" / f"{name}.md").read_text(encoding="utf-8")
    examples = usage.split("Examples:", 1)[1].strip().splitlines()
    lines = [ln.strip() for ln in examples if ln.strip().startswith("3d ")]
    assert lines
    for ln in lines:
        assert ln.split("  #")[0].strip() in doc, f"example missing from docs/commands/{name}.md: {ln}"


def test_the_ledger_and_u1_quirk_docs_exist() -> None:
    assert (ROOT / "docs" / "research" / "prep-session-ledger-2026-10-20.md").is_file()
    assert (ROOT / "docs" / "printers" / "u1-orca-cli.md").is_file()
