"""`3d printer ondemand` end to end (#747): the real `bin/3d`, and the REAL one-shot check process
(`python -S -m printer_ops.ondemand`, exactly what the systemd timer runs) against a loopback fake Moonraker while
a fake `systemctl` on PATH records what it starts and stops. The printer state is SIMULATED: no real printer, systemd
or tg is touched.

The story: idle printer -> nothing runs, nothing is written after the first check and every check is a few
milliseconds; a print starts -> both workers start at the next check; the print ends -> they stay for the grace
period, then stop; the runtime directory vanishing (a reboot) in the middle of a print leaves running workers alone."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from printer_fakes import FakeMoonraker, free_port, u1_status

REPO = Path(__file__).resolve().parents[2]
THREED = REPO / "bin" / "3d"

FLEET = """
p1:
  model: Test Model
  owner: own
  access: full
  adapter: moonraker
  host: 127.0.0.1
  port: {port}
  heads: [{{label: T1, index: 0, extruder: extruder, usable: true}}]
  watch:
    vision: {{camera: {{kind: snapshot-url, url: "http://127.0.0.1:1/snap"}}}}
"""

# fake `systemctl --user is-active|start|stop UNIT...`: $FAKE_SYSTEMD/<unit>.active exists while the unit "runs"
SYSTEMCTL = """#!{python}
import os, sys
d = os.environ["FAKE_SYSTEMD"]
args = sys.argv[1:]
if args and args[0] == "--user":
    args = args[1:]
verb, units = args[0], args[1:]
with open(d + "/calls.log", "a") as log:
    log.write(" ".join(args) + "\\n")
if verb == "is-active":
    rc = 0
    for u in units:
        on = os.path.exists(f"{{d}}/{{u}}.active")
        print("active" if on else "inactive")
        rc = rc or (0 if on else 3)
    sys.exit(rc)
for u in units:
    path = f"{{d}}/{{u}}.active"
    if verb == "start":
        open(path, "w").close()
    elif verb == "stop" and os.path.exists(path):
        os.remove(path)
"""

WORKERS = "3d-printer-watch@p1.service 3d-printer-vision@p1.service"
UNITS = sorted(WORKERS.split())


@dataclass
class Lab:
    env: dict[str, str]
    fake: Path
    runtime: Path

    def calls(self) -> list[str]:
        path = self.fake / "calls.log"
        return path.read_text().splitlines() if path.exists() else []

    def running(self) -> list[str]:
        return sorted(
            p.name.removesuffix(".active") for p in self.fake.glob("*.active")
        )

    def state_files(self) -> list[Path]:
        return sorted(self.runtime.rglob("*")) if self.runtime.exists() else []


@pytest.fixture
def lab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[int], Lab]]:
    fake = tmp_path / "systemd"
    fake.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ctl = bin_dir / "systemctl"
    ctl.write_text(SYSTEMCTL.format(python=sys.executable))
    ctl.chmod(ctl.stat().st_mode | stat.S_IXUSR)

    def make(port: int) -> Lab:
        (tmp_path / "fleet.yaml").write_text(FLEET.format(port=port), encoding="utf-8")
        env = {
            k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"
        }  # the real systemctl guard
        env.update(
            THREED_FLEET=str(tmp_path / "fleet.yaml"),
            XDG_CONFIG_HOME=str(tmp_path / "config"),
            XDG_STATE_HOME=str(tmp_path / "state"),
            XDG_RUNTIME_DIR=str(tmp_path / "run"),
            FAKE_SYSTEMD=str(fake),
            PATH=f"{bin_dir}:{env.get('PATH', '')}",
            PYTHONPATH=str(REPO / "lib"),
            PYTHONUNBUFFERED="1",
        )
        return Lab(env, fake, tmp_path / "run")

    yield make


def run3d(lab: Lab, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(THREED), *args],
        env=lab.env,
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def check(lab: Lab, *flags: str) -> subprocess.CompletedProcess[str]:
    """One scheduled check, as the unit runs it."""
    return subprocess.run(
        [sys.executable, "-S", "-m", "printer_ops.ondemand", "p1", *flags],
        env=lab.env,
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


# ---- the command through bin/3d ----------------------------------------------------------------


def test_help_lists_the_command_and_its_options(lab: Callable[[int], Lab]) -> None:
    """An owner asking the CLI for help finds the one-shot check, its options, and a clear refusal for a bad name or a removed flag."""
    lb = lab(free_port())
    helped = run3d(lb, "printer", "ondemand", "--help")
    assert (
        helped.returncode == 0
        and "--grace" in helped.stdout
        and "--once" in helped.stdout
    )
    assert "ondemand NAME" in run3d(lb, "printer", "--help").stdout
    assert run3d(lb, "printer", "ondemand").returncode == 1  # no NAME: usage, exit 1
    bad = run3d(
        lb, "printer", "ondemand", "p1", "--poll", "1"
    )  # the cadence is the timer's now
    assert bad.returncode == 2 and "poll" in bad.stderr
    odd = run3d(lb, "printer", "ondemand", "../p1", "--once")
    assert odd.returncode == 2 and "not a printer name" in odd.stderr


def test_once_reports_what_the_printer_says_and_what_would_happen(
    lab: Callable[[int], Lab],
) -> None:
    """An owner runs one check by hand and reads what the printer reported and what would start or stop."""
    fake = FakeMoonraker(status=u1_status("standby"))
    with fake.serve():
        lb = lab(fake.port)
        idle = json.loads(run3d(lb, "printer", "ondemand", "p1", "--once").stdout)
        assert (idle["answered"], idle["print_active"], idle["would"]) == (
            True,
            False,
            "nothing",
        )
        fake.status["print_stats"]["state"] = "printing"
        busy = json.loads(run3d(lb, "printer", "ondemand", "p1", "--once").stdout)
        assert (busy["print_active"], busy["would"]) == (True, "start the workers")
        lb.fake.joinpath("3d-printer-watch@p1.service.active").touch()
        lb.fake.joinpath("3d-printer-vision@p1.service.active").touch()
        busy = json.loads(run3d(lb, "printer", "ondemand", "p1", "--once").stdout)
        assert busy["would"] == "nothing" and set(busy["workers"].values()) == {
            "active"
        }
        fake.status["print_stats"]["state"] = "complete"
        done = json.loads(run3d(lb, "printer", "ondemand", "p1", "--once").stdout)
        assert done["would"] == "stop them after the grace period"
    assert not any(
        c.startswith(("start", "stop")) for c in lb.calls()
    )  # --once only looks
    assert lb.state_files() == []  # and writes no state


def test_once_on_an_unreachable_printer_exits_1_and_says_it_will_back_off(
    lab: Callable[[int], Lab],
) -> None:
    """An owner checks a switched-off printer: the command fails and says it will back off."""
    lb = lab(free_port())
    r = run3d(lb, "printer", "ondemand", "p1", "--once")
    doc = json.loads(r.stdout)
    assert (
        r.returncode == 1
        and doc["answered"] is False
        and doc["would"] == "retry with backoff"
    )
    assert doc["error"]


# ---- the real one-shot check -------------------------------------------------------------------


def test_the_check_follows_a_simulated_print_and_a_vanished_runtime_directory_leaves_the_workers_alone(
    lab: Callable[[int], Lab],
) -> None:
    """The timer's checks start workers for a print and stop them after it; losing the runtime directory changes nothing."""
    fake = FakeMoonraker(status=u1_status("standby"))
    with fake.serve():
        lb = lab(fake.port)
        # idle: the first check logs once and writes its state; the next ones say and write nothing
        first = check(lb, "--grace", "2")
        assert first.returncode == 0 and first.stdout.count("\n") == 1, first
        assert "p1: supervising moonraker 127.0.0.1:" in first.stdout
        state = lb.runtime / "3d-cli" / "ondemand" / "p1.json"
        written = state.stat().st_mtime_ns
        assert lb.state_files() == [
            lb.runtime / "3d-cli",
            state.parent,
            state,
            state.with_name("p1.json.lock"),
        ]
        for _ in range(3):
            again = check(lb, "--grace", "2")
            assert (again.returncode, again.stdout, again.stderr) == (0, "", "")
        assert state.stat().st_mtime_ns == written  # not rewritten
        # only looks at the workers (the first check's, plus the random one-in-twenty stray look), never an action
        assert lb.calls() and set(lb.calls()) == {f"is-active {WORKERS}"}
        assert lb.running() == []

        # a print starts (simulated): both workers at the next check, once
        fake.status["print_stats"].update(state="printing", filename="part.gcode")
        started = check(lb, "--grace", "2")
        assert (
            f"p1: print started: starting {WORKERS.replace(' ', ', ')}"
            in started.stdout
        )
        assert lb.running() == UNITS and f"start {WORKERS}" in lb.calls()
        assert check(lb, "--grace", "2").stdout == ""  # running: silent

        # it ends: they stay for the grace period, then stop completely
        fake.status["print_stats"]["state"] = "complete"
        ended = check(lb, "--grace", "2")
        assert "print ended: workers stay 2 s, then stop" in ended.stdout
        assert lb.running() == UNITS
        time.sleep(2.2)
        stopped = check(lb, "--grace", "2")
        assert "p1: grace over: workers stopped" in stopped.stdout
        assert lb.running() == [] and f"stop {WORKERS}" in lb.calls()

        # a second print, then the runtime directory vanishes (a reboot that left the workers up): nothing is
        # restarted and nothing is stopped
        fake.status["print_stats"]["state"] = "printing"
        check(lb, "--grace", "2")
        starts = [c for c in lb.calls() if c.startswith("start")]
        shutil.rmtree(lb.runtime)
        after = check(lb, "--grace", "2")
        assert after.returncode == 0
        assert [c for c in lb.calls() if c.startswith("start")] == starts
        assert len([c for c in lb.calls() if c.startswith("stop")]) == 1
        assert lb.running() == UNITS


def test_workers_that_run_at_the_first_check_with_no_print_are_kept_one_grace_period_then_stopped(
    lab: Callable[[int], Lab],
) -> None:
    """After an install over a running worker, the idle printer's workers get one grace period, then stop."""
    fake = FakeMoonraker(status=u1_status("standby"))
    with fake.serve():
        lb = lab(fake.port)
        for unit in UNITS:
            (
                lb.fake / f"{unit}.active"
            ).touch()  # left over from before the reboot / by hand
        first = check(lb, "--grace", "1")
        assert "workers already running at the first check" in first.stdout
        assert (
            lb.running() == UNITS
        )  # kept for a fresh grace period, not killed at once
        time.sleep(1.2)
        assert "grace over: workers stopped" in check(lb, "--grace", "1").stdout
        assert lb.running() == []


def test_an_unreachable_printer_never_starts_workers_and_is_logged_once(
    lab: Callable[[int], Lab],
) -> None:
    """A printer that never answers gets no workers, and the journal says so only once."""
    lb = lab(free_port())
    runs = [check(lb, "--grace", "1") for _ in range(4)]
    assert all(
        r.returncode == 0 for r in runs
    )  # an off printer is not a failure of the check
    assert sum("does not answer" in r.stdout for r in runs) == 1
    assert [c for c in lb.calls() if c.startswith(("start", "stop"))] == []
    assert lb.running() == []


def test_a_broken_configuration_is_logged_once_and_never_crash_loops(
    lab: Callable[[int], Lab], tmp_path: Path
) -> None:
    """A broken printer configuration is reported once and does not make the unit crash-loop."""
    lb = lab(free_port())
    lb.env["THREED_FLEET"] = str(tmp_path / "missing.yaml")
    runs = [check(lb) for _ in range(4)]
    assert all(r.returncode == 0 for r in runs), [r.stderr for r in runs]
    assert sum("cannot read the printer configuration" in r.stdout for r in runs) == 1
    assert lb.calls() == []


def test_a_printer_the_configuration_does_not_supervise_is_logged_once_and_not_resolved_again(
    lab: Callable[[int], Lab],
) -> None:
    """A printer the configuration excludes from supervision is logged once and not resolved again."""
    lb = lab(free_port())
    fleet = Path(lb.env["THREED_FLEET"])
    fleet.write_text(FLEET.format(port=1).replace("owner: own", "owner: neighbour"), encoding="utf-8")
    runs = [check(lb) for _ in range(3)]
    assert all(r.returncode == 0 for r in runs), [r.stderr for r in runs]
    assert sum("not supervised any more" in r.stdout for r in runs) == 1
    assert json.loads((lb.runtime / "3d-cli" / "ondemand" / "p1.json").read_text())["refused"]
    assert lb.calls() == []  # no worker is touched and no printer is asked


# ---- what one check costs ----------------------------------------------------------------------


def test_a_check_is_a_few_milliseconds_and_a_few_megabytes_and_imports_nothing_heavy(
    lab: Callable[[int], Lab],
) -> None:
    """An idle check costs a few milliseconds and megabytes and imports nothing heavy."""
    fake = FakeMoonraker(status=u1_status("standby"))
    with fake.serve():
        lb = lab(fake.port)
        check(lb)  # the first one resolves the config (yaml): not the steady state
        probe = (
            "import resource, subprocess, sys, time\n"
            "t = time.perf_counter()\n"
            "subprocess.run(sys.argv[1:], check=True)\n"
            "wall = time.perf_counter() - t\n"
            "print(wall, resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024)\n"
        )
        r = subprocess.run(
            [
                sys.executable,
                "-c",
                probe,
                sys.executable,
                "-S",
                "-m",
                "printer_ops.ondemand",
                "p1",
            ],
            env=lb.env,
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        wall, rss = (float(x) for x in r.stdout.split())
        assert wall < 1.0, f"a steady-state check took {wall:.2f} s"
        assert rss < 25, f"a steady-state check peaked at {rss:.1f} MB"


def test_importing_the_check_loads_none_of_the_heavy_modules() -> None:
    """Importing the check module in a bare interpreter loads none of the heavy modules."""
    code = (
        "import sys, printer_ops.ondemand\n"
        "heavy = {'yaml', 'ssl', 'http.client', 'subprocess', 'dataclasses', 'pathlib', 'inspect', 'typing', 'numpy'}\n"
        "print(sorted(heavy & set(sys.modules)))\n"
    )
    r = subprocess.run(
        [sys.executable, "-S", "-c", code],
        env={**os.environ, "PYTHONPATH": str(REPO / "lib")},
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert r.stdout.strip() == "[]", r.stdout
