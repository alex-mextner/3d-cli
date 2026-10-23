"""Fleet parity: every printer of fleet/printers.yaml gets a firmware profile or the explicit
no-profile message, and no fleet printer name/model/host is hard-coded in the generic code."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from printer_fw.engine import no_profile_note
from printer_fw.profiles import load_profiles, match_profile
from printer_fw.target import Target, target_from_ref
from printers_cli import discover, resolve
from printers_cli import fw as fw_cli
from printers_cli._common import PrinterRef
from printops.fleet import FleetPrinter, load_fleet

REPO = Path(__file__).resolve().parents[1]
FLEET_FILE = REPO / "fleet" / "printers.yaml"
FLEET = load_fleet(FLEET_FILE)
assert FLEET is not None, "fleet/printers.yaml must load"
PRINTERS: dict[str, FleetPrinter] = FLEET.printers
PROFILES = load_profiles()


@pytest.fixture
def real_fleet(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("THREED_FLEET", str(FLEET_FILE))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)


@pytest.mark.parametrize("name", sorted(PRINTERS))
def test_static_status_gives_a_profile_or_the_explicit_no_profile_message(
    name: str, real_fleet: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert fw_cli.run(["status", name, "--no-live", "--json"]) == 0
    entry = json.loads(capsys.readouterr().out)["printers"][0]
    printer = PRINTERS[name]
    profile = match_profile(PROFILES, printer.model)
    assert entry["printer"] == name and entry["model"] == printer.model
    if profile is not None:
        assert (
            entry["profile"] == profile.id
            and "never contacted" not in entry["note"]
            and entry["state"] is None
        )
    else:
        assert (
            entry["profile"] is None
            and entry["note"] == f"no firmware profile for model {printer.model}"
        )


def test_status_all_covers_the_whole_fleet_without_contacting_anything(
    real_fleet: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert fw_cli.run(["status", "--all", "--no-live", "--json"]) == 0
    entries = json.loads(capsys.readouterr().out)["printers"]
    assert sorted(e["printer"] for e in entries) == sorted(PRINTERS)
    matched = [e for e in entries if e["profile"]]
    assert matched, "no fleet printer matches any firmware profile"
    unmatched = [e for e in entries if not e["profile"]]
    assert all(
        e["note"].startswith("no firmware profile for model ") for e in unmatched
    )


def test_every_fleet_model_matches_at_most_one_profile() -> None:
    for printer in PRINTERS.values():
        assert len([p for p in PROFILES.values() if p.matches(printer.model)]) <= 1


def test_the_no_profile_message_names_the_model_or_the_missing_model() -> None:
    ghost = no_profile_note(target_from_ref(PrinterRef("ghost", None, None)))
    assert (
        ghost
        == "no firmware profile for printer ghost: it has no `model:` in the registry"
    )
    modelled = Target("ghost", "Some Unknown Model", "offline", "", "", 0)
    assert (
        no_profile_note(modelled) == "no firmware profile for model Some Unknown Model"
    )


def test_the_fw_sub_registers_itself_with_its_alias() -> None:
    assert discover()["fw"] is resolve("fw") is resolve("firmware")


# ---- no fleet literal in generic code -----------------------------------------------------------------------


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def fleet_needles() -> list[str]:
    """Every printer name, model and host string of the registry."""
    out: set[str] = set()
    for name, p in PRINTERS.items():
        out |= {name, p.model}
        host = p.device.get("host")
        if isinstance(host, str) and host:
            out.add(host)
    return sorted(out)


def string_literals(source: str) -> list[str]:
    tree = ast.parse(source)
    skip = _docstring_nodes(tree)
    return [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and id(n) not in skip
    ]


def find_fleet_literals(source: str, needles: list[str]) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    for text in string_literals(source):
        hits.extend(
            (needle, text[:60])
            for needle in needles
            if re.search(
                rf"(?<![A-Za-z0-9_-]){re.escape(needle)}(?![A-Za-z0-9_-])",
                text,
                re.IGNORECASE,
            )
        )
    return hits


GENERIC_FILES = sorted((REPO / "lib" / "printer_fw").rglob("*.py")) + [
    REPO / "lib" / "printers_cli" / "fw.py"
]


def test_the_scanner_itself_finds_a_hard_coded_printer() -> None:
    first = sorted(PRINTERS)[0]
    model = PRINTERS[first].model
    bad = f'"""docstring mentioning {first}"""\nX = "{model}"\nY = ["{first}"]\nZ = "unrelated"\n'
    found = {n for n, _ in find_fleet_literals(bad, fleet_needles())}
    assert {first, model} <= found
    assert (
        find_fleet_literals(
            f'"""only a docstring about {first}"""\nZ = 1\n', fleet_needles()
        )
        == []
    )


@pytest.mark.parametrize("path", GENERIC_FILES, ids=lambda p: str(p.relative_to(REPO)))
def test_no_fleet_printer_name_model_or_host_is_a_string_literal_in_generic_code(
    path: Path,
) -> None:
    hits = find_fleet_literals(path.read_text(encoding="utf-8"), fleet_needles())
    assert hits == [], f"{path.name} hard-codes fleet data: {hits}"


def test_generic_code_files_were_found() -> None:
    names = {p.name for p in GENERIC_FILES}
    assert {
        "engine.py",
        "profiles.py",
        "probes.py",
        "plan.py",
        "guards.py",
        "backup.py",
        "runner.py",
        "fw.py",
    } <= names


def test_models_live_only_in_the_yaml_data() -> None:
    """The model matchers of the shipped profiles accept the fleet models (they are data)."""
    accepted = {
        m
        for m in (p.model for p in PRINTERS.values())
        if any(pr.matches(m) for pr in PROFILES.values())
    }
    assert accepted, "no fleet model matched by a profile"


def test_the_help_text_uses_no_fleet_printer() -> None:
    hits = find_fleet_literals(f'X = """{fw_cli.USAGE}"""', fleet_needles())
    assert hits == []
