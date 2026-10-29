"""`3d printers doctor` argument handling, help/doc agreement, exit codes (in-process, loopback fakes)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from errors import GateFailure, InvalidArgument, UsageError
from fleet_fixtures import u1_entry, write_fleet
from printer_doctor.context import Services
from printer_doctor.registry import discover_checks
from printer_doctor_fakes import agent_sources, subscription_sources
from printer_fakes import FakeMoonraker
from printers_cli import dispatch, resolve
from printers_cli.doctor import USAGE, run

DOC = Path(__file__).resolve().parents[1] / "docs" / "commands" / "printers-doctor.md"


@pytest.fixture
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A fleet of one contactable printer (u1 -> a FakeMoonraker) plus two offline ones."""
    moon = FakeMoonraker(components=["file_manager"])
    with moon.serve():
        monkeypatch.setenv(
            "THREED_FLEET", str(write_fleet(tmp_path / "fleet.yaml", u1_entry(moon)))
        )
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        monkeypatch.setenv("THREED_WEBUI_PORTS", "1")
        monkeypatch.delenv("PRINTOPS_API", raising=False)
        monkeypatch.delenv("PRINTOPS_TOKEN", raising=False)
        yield moon


def services() -> Services:
    return Services(
        sleep=lambda seconds: None,
        systemctl_show=lambda unit: "",
        timeout=3.0,
        poll_s=3.0,
        subscriptions=subscription_sources(),
        agents=agent_sources(),
    )


def test_doctor_is_discovered_and_the_printer_alias_forwards_to_it() -> None:
    sub = resolve("doctor")
    assert sub is not None and sub.usage == USAGE
    with pytest.raises(UsageError, match="no printer given"):
        dispatch(["doctor"])


def test_no_printer_and_no_all_is_a_usage_error_listing_the_valid_names(
    fleet: FakeMoonraker,
) -> None:
    with pytest.raises(UsageError) as err:
        run([])
    assert err.value.exit_code == 2 and any(
        "u1" in r and "p1s" in r and "boris" in r for r in err.value.remediation
    )


def test_unknown_printer_check_option_and_values_are_usage_errors(
    fleet: FakeMoonraker,
) -> None:
    with pytest.raises(InvalidArgument, match="accepted: .*boris.*p1s.*u1"):
        run(["nope"])
    with pytest.raises(InvalidArgument, match="klippy-state"):
        run(["u1", "--check", "no-such-check"])
    with pytest.raises(InvalidArgument, match="error, warn, never"):
        run(["u1", "--fail-on", "sometimes"])
    with pytest.raises(InvalidArgument, match="positive number"):
        run(["u1", "--secs", "abc"])
    with pytest.raises(UsageError, match="only applies together with --fix"):
        run(["u1", "--yes"])
    with pytest.raises(UsageError, match="not both"):
        run(["u1", "--all"])
    with pytest.raises(UsageError, match="unknown option"):
        run(["u1", "--frobnicate"])


def test_printer_names_resolve_case_insensitively(
    fleet: FakeMoonraker, capsys: Any
) -> None:
    assert run(["U1", "--check", "klippy-state", "--json"], services()) == 0
    assert json.loads(capsys.readouterr().out)["printers"][0]["name"] == "u1"


def test_healthy_run_exits_0_and_a_finding_exits_1_with_a_silent_gate_failure(
    fleet: FakeMoonraker, capsys: Any
) -> None:
    assert run(["u1", "--check", "klippy-state,disk-space"], services()) == 0
    out = capsys.readouterr().out
    assert "ok   klippy-state" in out and "0 remaining" in out
    fleet.server_info.update(klippy_state="shutdown")
    with pytest.raises(GateFailure) as err:
        run(["u1", "--check", "klippy-state"], services())
    assert err.value.exit_code == 1 and err.value.silent and err.value.render() == ""
    printed = capsys.readouterr().out
    assert (
        "FAIL" in printed and "1 remaining" in printed
    )  # the report was printed once, not by the error


def test_fail_on_never_and_warn_change_the_exit_code(
    fleet: FakeMoonraker, capsys: Any
) -> None:
    fleet.server_info.update(klippy_state="shutdown")
    assert run(["u1", "--check", "klippy-state", "--fail-on", "never"], services()) == 0
    fleet.server_info.update(klippy_state="ready")
    fleet.failed_components = ["spoolman"]  # a warn-severity finding
    assert run(["u1", "--check", "moonraker-components"], services()) == 0
    with pytest.raises(GateFailure):
        run(["u1", "--check", "moonraker-components", "--fail-on", "warn"], services())
    capsys.readouterr()


def test_all_includes_the_offline_printers_and_never_contacts_them(
    fleet: FakeMoonraker, capsys: Any
) -> None:
    assert run(
        [
            "--all",
            "--json",
            "--skip",
            "watch-units-dry-run,camera-snapshot,camera-config,alert-subscribers,alert-fallback-agent,printer-supervision",
        ],
        services(),
    ) in (0,)
    doc = json.loads(capsys.readouterr().out)
    names = {p["name"]: p for p in doc["printers"]}
    assert set(names) == {"u1", "p1s", "boris"}
    for off in ("p1s", "boris"):
        assert names[off]["access"] == "offline"
        net = [
            r
            for r in names[off]["results"]
            if r["summary"] == "never contacted (access: offline)"
        ]
        assert net and all(r["status"] == "skip" for r in net)


def test_list_checks_text_and_json_cover_every_check(capsys: Any) -> None:
    assert run(["--list-checks"]) == 0
    text = capsys.readouterr().out
    ids = [c.id for c in discover_checks()]
    assert all(i in text for i in ids)
    assert run(["--list-checks", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert (
        doc["schema"] == "printers.doctor.checks/1"
        and [c["id"] for c in doc["checks"]] == ids
    )
    assert {c["fix"]["risk"] for c in doc["checks"]} == {
        "safe",
        "needs-yes",
        "manual-only",
    }
    assert all(
        {"id", "title", "severity", "applies_to", "fix"} <= set(c)
        for c in doc["checks"]
    )


def test_help_documents_every_flag_with_an_example_and_the_docs_agree() -> None:
    assert run(["--help"]) == 0
    flags = [
        "--all",
        "--fix",
        "--yes",
        "--json",
        "--check",
        "--skip",
        "--fail-on",
        "--secs",
        "--list-checks",
    ]
    doc = DOC.read_text(encoding="utf-8")
    for flag in flags:
        assert re.search(rf"3d printers doctor [^\n]*{re.escape(flag)}", USAGE), (
            f"{flag}: no example in --help"
        )
        assert re.search(rf"3d printers doctor [^\n]*{re.escape(flag)}", doc), (
            f"{flag}: no example in the doc"
        )
    assert (
        "Exit codes: 0 " in USAGE
        and "1 one remains" in USAGE
        and "2 usage error" in USAGE
    )
    for check in discover_checks():
        assert f"`{check.id}`" in doc, f"{check.id} is not documented in {DOC.name}"
