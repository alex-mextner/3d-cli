"""The opt-in guard is explicit at the real CLI boundary; it never turns vision into a writer."""
from __future__ import annotations

from pathlib import Path

import pytest
from printer_fakes import FakeMoonraker, write_devices

from .workflow_helper import run_cli


def test_guard_help_and_missing_confirmation_are_offline(tmp_path: Path) -> None:
    """An operator reads guard help, then tries to arm it without approval or a native job ID.

    Help explains both requirements, and either incomplete command is rejected offline.
    """
    help_result = run_cli(tmp_path, "printer", "guard", "--help")
    assert help_result.returncode == 0, help_result.stderr
    assert "--job-id" in help_result.stdout and "--yes" in help_result.stdout
    no_yes = run_cli(tmp_path, "printer", "guard", "u1", "--job-id", "00006D")
    assert no_yes.returncode == 2 and "needs --yes" in no_yes.stderr
    no_job = run_cli(tmp_path, "printer", "guard", "u1", "--yes")
    assert no_job.returncode == 2 and "--job-id" in no_job.stderr


def test_guard_refuses_read_only_device_before_any_request(tmp_path: Path) -> None:
    """An operator tries to enable automatic pause on a printer configured for observation only.

    The CLI explains the access restriction and refuses the guard before contacting the printer.
    """
    with FakeMoonraker().serve() as fake:
        path = write_devices(tmp_path / "xdg-config", moon=fake)
        path.write_text(path.read_text().replace("adapter: moonraker", "access: read-only\n  adapter: moonraker"))
        result = run_cli(tmp_path, "printer", "guard", "u1", "--job-id", "00006D", "--yes")
    assert result.returncode == 1 and "read-only" in result.stderr
    assert fake.requests == []


@pytest.mark.parametrize("interval", ["1.9", "30.1", "nan"])
def test_guard_rejects_unsupported_capture_intervals_offline(tmp_path: Path, interval: str) -> None:
    """An operator supplies a capture cadence outside the guard's supported finite range.

    The CLI explains the valid interval before loading vision models or contacting a printer.
    """
    result = run_cli(tmp_path, "printer", "guard", "u1", "--job-id", "00006D", "--yes", "--interval", interval)
    assert result.returncode == 2
    assert "--interval must be 2..30 seconds" in result.stderr


@pytest.mark.parametrize('selector', [('-n', 'u1'), ('--name', 'u1'), ('--name=u1',)])
@pytest.mark.parametrize('positional', [(), ('k1se',)])
def test_guard_explains_unsupported_global_printer_selector_offline(
    tmp_path: Path, selector: tuple[str, ...], positional: tuple[str, ...],
) -> None:
    """An operator tries the task-list printer selector before the guard subcommand.

    The CLI rejects that spelling with the positional NAME form, including when
    a conflicting positional printer was supplied, before any device lookup.
    """
    result = run_cli(tmp_path, 'printer', *selector, 'guard', *positional, '--job-id', '00006D', '--yes')
    assert result.returncode == 2
    assert '-n/--name selects the printer for `todo`/`issues`' in result.stderr
    assert '`guard` takes the printer as NAME' in result.stderr
    assert '3d printer guard u1' in result.stderr
