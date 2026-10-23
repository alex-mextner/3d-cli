"""`3d printers fw` argument parsing and dispatch (in-process; the engine is stubbed where noted)."""

from __future__ import annotations

from pathlib import Path

import pytest
from errors import InvalidArgument, UsageError
from printer_fw import engine
from printers_cli import fw as fw_cli


@pytest.fixture
def fleet(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "fleet.yaml"
    path.write_text(
        "bambu:\n  model: Bambu Lab P1S\n  owner: neighbour\n  access: offline\n  heads: [{label: T1, index: 0}]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("THREED_FLEET", str(path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    return path


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("24h", 86400.0),
        ("90m", 5400.0),
        ("2d", 172800.0),
        ("30s", 30.0),
        ("6", 21600.0),
        ("1.5h", 5400.0),
    ],
)
def test_backup_max_age_units(text: str, seconds: float) -> None:
    assert fw_cli._age_seconds(text) == seconds


@pytest.mark.parametrize("text", ["0h", "0", "-1h", "soon", "", "5w", "h"])
def test_a_bad_backup_max_age_is_an_invalid_argument(text: str) -> None:
    with pytest.raises(InvalidArgument) as err:
        fw_cli._age_seconds(text)
    assert err.value.exit_code == 2 and "--backup-max-age" in err.value.message


def test_repeated_flags_are_all_collected() -> None:
    values, rest = fw_cli._take_multi(
        ["x", "--also", "a", "--json", "--also", "b"], "--also"
    )
    assert values == ["a", "b"] and rest == ["x", "--json"]
    with pytest.raises(UsageError, match="needs a value"):
        fw_cli._take_multi(["--also"], "--also")


def test_no_arguments_print_usage_and_exit_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert fw_cli.run([]) == 1
    assert (
        "3d printers fw <status|plan|backup|apply|profiles>" in capsys.readouterr().out
    )


def test_help_anywhere_prints_usage_and_exit_0_without_touching_anything(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for argv in (
        ["--help"],
        ["-h"],
        ["apply", "--help"],
        ["status", "x", "-h"],
        ["bogus", "--help"],
    ):
        assert fw_cli.run(argv) == 0
    assert capsys.readouterr().out.count("Firmware / root / Klipper-unlock tool") == 5


def test_an_unknown_subcommand_lists_the_accepted_ones() -> None:
    with pytest.raises(InvalidArgument) as err:
        fw_cli.run(["explode"])
    assert (
        err.value.exit_code == 2
        and "status" in err.value.message
        and "profiles" in err.value.message
    )


def test_status_needs_a_printer_or_all_but_not_both(fleet: Path) -> None:
    with pytest.raises(UsageError, match="needs a PRINTER or --all"):
        fw_cli.run(["status"])
    with pytest.raises(UsageError, match="not both"):
        fw_cli.run(["status", "bambu", "--all"])


def test_plan_and_apply_need_a_target_state(fleet: Path) -> None:
    with pytest.raises(UsageError, match="needs --to STATE"):
        fw_cli.run(["plan", "bambu"])
    with pytest.raises(UsageError, match="needs --to STATE"):
        fw_cli.run(["apply", "bambu"])
    with pytest.raises(UsageError):
        fw_cli.run(["plan", "bambu", "--to", "stock", "--bogus"])


def test_a_printer_without_a_profile_is_refused_with_the_message_for_plan_backup_and_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "fleet.yaml"
    path.write_text(
        "ghost:\n  model: Creality CR-200B Pro\n  owner: hackerspace\n  access: offline\n  heads: [{label: T1, index: 0}]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("THREED_FLEET", str(path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    from errors import GateFailure

    for argv in (
        ["plan", "ghost", "--to", "x", "--from", "y"],
        ["backup", "ghost"],
        ["apply", "ghost", "--to", "x"],
    ):
        with pytest.raises(GateFailure) as err:
            fw_cli.run(argv)
        assert (
            err.value.exit_code == 1
            and "no firmware profile for model Creality CR-200B Pro"
            in err.value.message
        )


def test_apply_parses_file_arguments_and_options(
    fleet: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[engine.ApplyOptions] = []

    def fake_run_apply(
        ctx: engine.Context, opts: engine.ApplyOptions
    ) -> engine.ApplyResult:
        seen.append(opts)
        return engine.ApplyResult(0, "dry-run", None, opts.to, None, ())

    monkeypatch.setattr(engine, "run_apply", fake_run_apply)
    argv = [
        "apply",
        "bambu",
        "--to",
        "developer-mode",
        "--yes",
        "--confirm",
        "bambu",
        "--backup-dir",
        str(tmp_path / "b"),
        "--backup-max-age",
        "6h",
        "--also",
        "one",
        "--also",
        "two",
        "--file",
        "enable-lan-only-mode=/tmp/a.bin",
        "--file",
        "/tmp/plain.bin",
    ]
    assert fw_cli.run(argv) == 0
    opts = seen[0]
    assert (opts.to, opts.yes, opts.confirm) == ("developer-mode", True, "bambu")
    assert (
        opts.backup_dir == tmp_path / "b"
        and opts.max_age_s == 6 * 3600.0
        and opts.also == ("one", "two")
    )
    assert opts.files == {
        "enable-lan-only-mode": Path("/tmp/a.bin")
    } and opts.default_file == Path("/tmp/plain.bin")


def test_profiles_command_prints_text_and_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert fw_cli.run(["profiles"]) == 0
    text = capsys.readouterr().out
    assert (
        "snapmaker-u1: Snapmaker U1 — Klipper possible" in text
        and "bambu-lab-x1" in text
    )
    assert fw_cli.run(["profiles", "--json"]) == 0
    assert '"klipper_possible": false' in capsys.readouterr().out


def test_apply_json_reports_every_error_as_one_json_document(
    fleet: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unknown printer, a bad age and a profile-less model all come out as JSON with --json, exit code kept."""
    import json

    cases = [
        (["apply", "nobody", "--to", "x", "--json"], 2, "nobody"),
        (
            ["apply", "bambu", "--to", "stock", "--backup-max-age", "soon", "--json"],
            2,
            "--backup-max-age",
        ),
        (["apply", "bambu", "--json"], 2, "needs --to STATE"),
    ]
    for argv, code, fragment in cases:
        assert fw_cli.run(argv) == code
        doc = json.loads(capsys.readouterr().out)
        assert doc["result"] == "error" and doc["exit_code"] == code
        assert fragment in doc["error"] and doc["events"] == []


def test_a_mistyped_service_in_the_port_override_is_refused(
    fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("THREED_FW_SERVICE_PORTS", "ssh2=22")
    with pytest.raises(InvalidArgument) as err:
        fw_cli.run(["status", "bambu", "--no-live"])
    assert (
        err.value.exit_code == 2
        and "'ssh2' is not a service of any firmware profile"
        in err.value.remediation[0]
    )
    monkeypatch.setenv("THREED_FW_SERVICE_PORTS", "web=8080,ssh=2222,moonraker=7125")
    assert fw_cli.run(["status", "bambu", "--no-live"]) == 0


def test_non_ascii_digits_in_a_port_override_are_an_invalid_argument_not_a_crash() -> (
    None
):
    from printer_fw.target import parse_port_overrides

    for bad in ("web=²", "web=٣٣", "web=0", "web=70000", "=80", "web", "web=8o"):
        with pytest.raises(InvalidArgument):
            parse_port_overrides(bad)
    assert parse_port_overrides("web=80, ssh=22,") == {"web": 80, "ssh": 22}
