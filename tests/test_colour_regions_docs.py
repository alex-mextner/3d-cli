"""`3d colour-regions --help` and docs/commands/colour-regions.md stay in step (AGENTS.md help-sync rule)."""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
USAGE = cast(str, importlib.import_module("commands.colour_regions").USAGE)
DOC = (ROOT / "docs" / "commands" / "colour-regions.md").read_text(encoding="utf-8")


def test_every_flag_in_the_help_is_documented() -> None:
    flags = set(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", USAGE))
    missing = sorted(f for f in flags if f not in DOC)
    assert len(flags) > 30 and not missing, f"flags in `3d colour-regions --help` but not in the doc: {missing}"


def test_every_flag_the_parser_accepts_is_in_the_help() -> None:
    """The flags of the real argparse parsers of `colour_regions.cli` and the USAGE text cannot drift apart."""
    cli = importlib.import_module("colour_regions.cli")
    parsed = {opt for make in cli.PARSERS.values() for action in make()._actions for opt in action.option_strings}
    undocumented = sorted(f for f in parsed if f not in USAGE)
    assert len(parsed) > 30 and not undocumented, f"flags the parser accepts but the help does not list: {undocumented}"


def test_every_help_example_appears_in_the_doc_and_every_subcommand_has_one() -> None:
    assert "Examples:" in USAGE and "Examples:" in DOC
    lines = [ln.strip() for ln in USAGE.split("Examples:", 1)[1].splitlines() if ln.strip().startswith("3d ")]
    assert lines
    for ln in lines:
        assert ln in DOC, f"example missing from docs/commands/colour-regions.md: {ln}"
    for sub in ("build", "shell-volume", "classify", "transfer", "transfer-class", "snap", "verify"):
        assert any(ln.startswith(f"3d colour-regions {sub} ") for ln in lines), f"no example for `{sub}`"
