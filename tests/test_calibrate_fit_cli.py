"""calibrate_fit_cli: help text and docs/commands/calibrate.md agree, every flag has an example, dispatch edges."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fleet_fixtures import REPO_FLEET

import calibrate_fit_cli as cli
from commands import calibrate
from errors import GateFailure, UsageError

DOC = Path(__file__).resolve().parents[1] / "docs" / "commands" / "calibrate.md"
FLAG = re.compile(r"--[a-z][a-z-]*")


@pytest.fixture(autouse=True)
def fleet(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("THREED_FLEET", str(REPO_FLEET))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("THREED_FITS_CORRECTIONS", str(tmp_path / "fits-corrections.yaml"))


def examples(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if line.strip().startswith("3d calibrate fit"))


def test_every_flag_has_an_example_in_the_help_and_in_the_docs() -> None:
    usage = cli.USAGE
    flags = set(FLAG.findall(usage.split("Options:")[0])) - {"--help"}
    doc_fit = DOC.read_text(encoding="utf-8").split("## `fit`")[1].split("## `photo`")[0]
    example_lines = examples(usage.split("Examples:")[1])
    doc_lines = examples(doc_fit)
    assert {"--yes", "--json", "--result", "--measured", "--mating-mm", "--from-readout", "--gap-offset"} <= flags
    for flag in sorted(flags - {"--json", "--token"}):  # --json rides on every plan line of the docs' jq example
        assert flag in example_lines, f"{flag} has no example in `3d calibrate --help`"
        assert flag in doc_lines, f"{flag} has no example in docs/commands/calibrate.md"
    assert "--json" in example_lines and "--json" in doc_lines


def test_the_calibrate_help_carries_the_fit_section_and_the_docs_list_the_subcommands() -> None:
    assert cli.USAGE in calibrate.USAGE and "fit" in calibrate.SUBS
    doc = DOC.read_text(encoding="utf-8")
    for sub in cli.SUBS:
        assert f"3d calibrate fit {sub}" in doc, sub
    assert "calibrate gen pa|flow|combo|temp|shaper|plate1|dense|dense2|fit" in doc
    assert "dense2|fit" in calibrate.USAGE and "| `fit_clearance` |" in doc


def test_no_arguments_print_usage_and_fail_help_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.run([]) == 1 and "3d calibrate fit plan" in capsys.readouterr().out
    assert cli.run(["--help"]) == 0 and cli.run(["plan", "--help"]) == 0
    with pytest.raises(UsageError, match="accepted: plan, gen, quick, read, record, check"):
        cli.run(["wibble"])


def test_a_bad_flag_is_a_structured_usage_error() -> None:
    with pytest.raises(UsageError, match="unrecognized arguments"):
        cli.run(["plan", "--printer", "k1se", "--bogus"])
    with pytest.raises(UsageError, match="--printer is required"):
        cli.run(["plan"])


def test_plan_json_through_the_dispatcher(capsys: pytest.CaptureFixture[str]) -> None:
    assert calibrate.run(["fit", "plan", "--printer", "k1se", "--material", "pla", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema"] == "3d.calibrate_fit/1" and doc["command"] == "plan" and doc["material"] == "PLA"


def test_all_fused_json_is_printed_before_the_silent_exit_1(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(GateFailure) as e:
        cli.run(["read", "--printer", "k1se", "--material", "PLA", "--fit", "sliding", "--shape", "round",
                 "--result", ",".join(["fused"] * 7), "--json"])
    assert e.value.exit_code == 1 and e.value.render() == ""  # the JSON on stdout is the report
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is False and "widen the range" in doc["picks"][0]["problem"] and "before_after" not in doc


def test_record_without_yes_stores_nothing_and_with_yes_stores_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["record", "--printer", "k1se", "--material", "PLA", "--fit", "tight", "--shape", "round", "--result", "step=2"]
    assert cli.run([*args, "--json"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] is True and "stored" not in dry and "offline buffer" in dry["destination"]
    assert not (tmp_path / "data").exists()
    assert cli.run([*args, "--yes", "--json"]) == 0
    done = json.loads(capsys.readouterr().out)
    assert done["dry_run"] is False and done["stored"]["origin"] == "buffer"
    assert len((tmp_path / "data" / "3d-cli" / "printer-history" / "k1se.jsonl").read_text().splitlines()) == 1
    assert done["before_after"][0]["before_mm"] == 0.1 and done["before_after"][0]["after_mm"] == 0.05
