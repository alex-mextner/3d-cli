"""Unit tests for lib/printer_ops/force_hatch.py: the Telegram approval behind `printer start --force`."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from printer_fakes import FakeHatch

from printer_ops import force_hatch
from printer_ops.base import PrinterRefused
from printer_ops.preflight import FAIL, PASS, WARN, Check

CHECKS = [
    Check("thumbnails", PASS, "300x300"),
    Check(
        "macros", FAIL, "moves inside these commands are not checked:\n  START_PRINT×1"
    ),
    Check("overhangs", WARN, "1 object"),
    Check("loaded_filament_match", FAIL, "T1: #2 black: ΔE 56.6 > 25"),
]


@pytest.fixture()
def real_loader() -> Iterator[None]:
    """The cached real loader, empty before and after a test that exercises it."""
    force_hatch.load_hatch.cache_clear()
    yield
    force_hatch.load_hatch.cache_clear()


def test_failing_summary_lists_only_fails_one_line_each() -> None:
    assert force_hatch.failing_summary(CHECKS) == (
        "- macros: moves inside these commands are not checked: START_PRINT×1\n"
        "- loaded_filament_match: T1: #2 black: ΔE 56.6 > 25"
    )
    long = force_hatch.failing_summary([Check("x", FAIL, "a" * 1000)])
    assert len(long) < 300 and long.endswith("…")


def test_hook_id_and_env_var_follow_the_hatch_convention() -> None:
    assert force_hatch.HOOK_ID == "3d-printer-start-force"
    assert (
        "RIG_HATCH_REQUEST_" + force_hatch.HOOK_ID.upper().replace("-", "_")
        == force_hatch.ENV_VAR
    )


def test_approved_returns_the_reason_and_sends_critical_request_with_the_failing_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeHatch()
    monkeypatch.setattr(force_hatch, "load_hatch", lambda: fake)
    reason = force_hatch.require_force_approval(
        printer="k1se", file="out/a b.gcode", sha256="ab" * 32, checks=CHECKS
    )
    assert reason == "test double approved"
    (req,) = fake.requests
    assert req["critical"] is True and req["hook"] == "3d-printer-start-force"
    assert req["command"] == "3d printer start k1se 'out/a b.gcode' --yes --force"
    assert (
        req["context"]["failing_checks"].count("\n") == 1
        and req["context"]["sha256"] == "ab" * 32
    )


@pytest.mark.parametrize(
    ("verdict", "wording"),
    [
        ("deny", "was not approved (test double denied)"),
        ("not-requested", "no approval requested"),
    ],
)
def test_not_approved_raises_with_how_to_ask_and_the_failing_checks(
    monkeypatch: pytest.MonkeyPatch, verdict: str, wording: str
) -> None:
    monkeypatch.setattr(force_hatch, "load_hatch", lambda: FakeHatch(verdict))
    with pytest.raises(PrinterRefused) as exc:
        force_hatch.require_force_approval(
            printer="k1se", file="a.gcode", sha256="0" * 64, checks=CHECKS
        )
    assert wording in exc.value.message
    text = "\n".join(exc.value.remediation)
    assert (
        'RIG_HATCH_REQUEST_3D_PRINTER_START_FORCE="<why this FAIL is acceptable>"'
        in text
    )
    assert (
        "- macros:" in text
        and "- loaded_filament_match:" in text
        and "root cause" in text
    )
    assert exc.value.exit_code == 1


def test_unavailable_helper_refuses_the_start(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> object:
        raise force_hatch.HatchUnavailable("no checkout")

    monkeypatch.setattr(force_hatch, "load_hatch", boom)
    with pytest.raises(
        PrinterRefused, match="hatch helper is unavailable \\(no checkout\\)"
    ):
        force_hatch.require_force_approval(
            printer="k1se", file="a.gcode", sha256="0" * 64, checks=CHECKS
        )


def _fake_lib(home: Path, checkout: str, mode: int = 0o644) -> Path:
    init = home / checkout / "lib" / "agenttools_hatch_escalation" / "__init__.py"
    init.parent.mkdir(parents=True)
    init.write_text(
        "from types import SimpleNamespace\n"
        "def request_hatch_approval(hook_id, context, *, cwd, command, critical):\n"
        "    return SimpleNamespace(approved=True, should_stop=True, reason='loaded from ' + __file__)\n",
        encoding="utf-8",
    )
    init.chmod(mode)
    init.parent.chmod(0o755)
    return init


def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "realhome"
    home.mkdir()
    monkeypatch.setattr(
        force_hatch.pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_dir=str(home))
    )
    return home


def test_helper_is_loaded_from_the_fixed_path_under_the_real_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, real_loader: None
) -> None:
    home = _home(monkeypatch, tmp_path)
    init = _fake_lib(home, "dev/agent-tools")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "elsewhere"))
    verdict = force_hatch.load_hatch().request_hatch_approval(
        "h", {}, cwd="/", command="c", critical=True
    )
    assert verdict.reason == f"loaded from {init}"


def test_second_checkout_is_used_when_the_first_has_no_helper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, real_loader: None
) -> None:
    home = _home(monkeypatch, tmp_path)
    init = _fake_lib(home, "xp/agent-tools")
    verdict = force_hatch.load_hatch().request_hatch_approval(
        "h", {}, cwd="/", command="c", critical=True
    )
    assert verdict.reason == f"loaded from {init}"


def test_world_writable_helper_is_not_trusted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, real_loader: None
) -> None:
    home = _home(monkeypatch, tmp_path)
    init = _fake_lib(home, "dev/agent-tools", mode=0o666)
    assert os.stat(init).st_mode & stat.S_IWOTH
    with pytest.raises(force_hatch.HatchUnavailable, match="writable by group/others"):
        force_hatch.load_hatch()


def test_missing_helper_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, real_loader: None
) -> None:
    _home(monkeypatch, tmp_path)
    with pytest.raises(force_hatch.HatchUnavailable, match="not found"):
        force_hatch.load_hatch()
