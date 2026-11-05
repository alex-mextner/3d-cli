"""E2E: a `3d printer start --force` approval that tg-ctl could not post to Telegram
(`ask-forward not-posted … reason=send-failed`, issue #728) is asked again, then refused with the way to
start from the printer's own screen. The hatch is a test double; the printer a loopback fake."""

from __future__ import annotations

from pathlib import Path

from colour_fix_fixtures import load_beige_petg, write_plate
from printer_fakes import FakeMoonraker, write_devices

from .workflow_helper import hatch_requests, run_cli, run_cli_with_hatch


def test_force_approval_that_telegram_could_not_post_is_retried_then_points_to_the_printer_screen(
    tmp_path: Path,
) -> None:
    """tg-ctl said `ask-forward not-posted … reason=send-failed`: the CLI asks again (4 requests in all),
    then refuses with how to start from the printer's own screen; nothing is started."""
    moon = FakeMoonraker()
    load_beige_petg(moon)
    g = write_plate(tmp_path)
    with moon.serve():
        write_devices(tmp_path / "xdg-config", moon=moon)
        assert run_cli(tmp_path, "printer", "upload", "u1", str(g)).returncode == 0
        refused = run_cli_with_hatch(
            tmp_path,
            "printer",
            "start",
            "u1",
            str(g),
            "--yes",
            "--force",
            verdict="send-failed",
        )

    assert refused.returncode == 1, refused.stdout + refused.stderr
    assert "could not be posted to Telegram after 4 attempts" in refused.stderr
    assert (
        "Start from the printer screen" in refused.stderr and "plate-" in refused.stderr
    )
    assert refused.stderr.count("asking again in 0 s") == 3
    assert len(hatch_requests(tmp_path)) == 4
    assert "/printer/print/start" not in [r.path for r in moon.writes()]
