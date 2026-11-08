"""`3d printer ondemand` (printer_ops/ondemand.py, #747): the tiny always-on supervisor that runs the watch and
vision workers only while a print runs. The printers are the loopback fakes, systemd is a recording fake and the
clocks are manual: nothing here reaches a real printer or `systemctl`."""

from __future__ import annotations

import contextlib
import socket
import subprocess
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import pytest
from errors import UsageError
from printer_fakes import FakeCreality, FakeMoonraker, free_port, k1_state, u1_status

from printer_ops import ondemand as od

U1_UNITS = ("3d-printer-watch@u1.service", "3d-printer-vision@u1.service")
GRACE = 300.0
POLL = 30.0


def target(**over: object) -> od.Target:
    base: dict[str, object] = {
        "name": "u1",
        "adapter": "moonraker",
        "host": "127.0.0.1",
        "port": 1,
        "poll_s": POLL,
        "grace_s": GRACE,
    }
    return od.Target(**{**base, **over})  # type: ignore[arg-type]


# ---- the decision ------------------------------------------------------------------------------


def test_every_poll_of_a_running_print_asks_for_the_workers() -> None:
    m = od.Machine(GRACE)
    assert [m.observe(t, True) for t in (0, 30, 60)] == [od.START] * 3
    assert m.phase == od.ACTIVE


def test_the_workers_stay_for_the_grace_period_then_stop() -> None:
    m = od.Machine(GRACE)
    m.observe(0, True)
    assert m.observe(100, False) is None and m.phase == od.GRACE  # deadline 400
    assert m.observe(399, False) is None
    assert m.observe(400, False) == od.STOP
    assert m.phase == od.GRACE  # idle only once the caller confirmed the stop
    m.stopped()
    assert m.phase == od.IDLE
    assert m.observe(430, False) is None


def test_a_print_that_starts_again_inside_the_grace_period_cancels_it() -> None:
    m = od.Machine(GRACE)
    m.observe(0, True)
    m.observe(10, False)
    assert m.observe(200, True) == od.START and m.phase == od.ACTIVE
    assert m.observe(215, False) is None  # a fresh grace period starts at THIS poll
    assert m.observe(214 + GRACE, False) is None
    assert m.observe(215 + GRACE, False) == od.STOP


def test_grace_zero_stops_on_the_first_poll_without_a_print() -> None:
    m = od.Machine(0.0)
    m.observe(0, True)
    assert m.observe(30, False) == od.STOP


def test_a_lost_printer_counts_like_an_ended_print_but_an_idle_one_never_starts_anything() -> (
    None
):
    m = od.Machine(GRACE)
    m.observe(0, True)
    assert m.observe(30, None) is None and m.phase == od.GRACE
    assert m.observe(30 + GRACE, None) == od.STOP
    quiet = od.Machine(GRACE)
    assert [quiet.observe(t, None) for t in range(0, 600, 30)] == [None] * 20
    assert quiet.phase == od.IDLE


def test_adopted_workers_get_one_fresh_grace_period() -> None:
    m = od.Machine(GRACE)
    m.adopt(1000)
    assert m.observe(1100, False) is None
    assert m.observe(1300, False) == od.STOP


# ---- the supervisor with fake systemd and manual clocks ------------------------------------------


class FakeSystemd:
    """`systemctl --user is-active|start|stop UNIT...` over a set of running units."""

    def __init__(self, running: Sequence[str] = ()) -> None:
        self.running = set(running)
        self.calls: list[list[str]] = []
        self.refuse_start: str | None = None  # stderr of a refused start
        self.condition_held: set[str] = (
            set()
        )  # start exits 0 but the unit stays inactive
        self.refuse_stop: str | None = None

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        assert argv[:2] == ["systemctl", "--user"]
        verb, units = argv[2], list(argv[3:])
        self.calls.append([verb, *units])
        if verb == "is-active":
            out = "".join(
                ("active" if u in self.running else "inactive") + "\n" for u in units
            )
            return subprocess.CompletedProcess(
                list(argv), 0 if all(u in self.running for u in units) else 3, out, ""
            )
        refused = self.refuse_start if verb == "start" else self.refuse_stop
        if refused:
            return subprocess.CompletedProcess(list(argv), 1, "", refused)
        if verb == "start":
            self.running |= set(units) - self.condition_held
        else:
            self.running -= set(units)
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    def verbs(self) -> list[str]:
        return [c[0] for c in self.calls]


class Rig:
    """A Supervisor over a scripted printer: `answer` is True/False (a print runs or not) or None (down)."""

    def __init__(self, systemd: FakeSystemd | None = None, **over: object) -> None:
        self.systemd = systemd or FakeSystemd()
        self.now = 1000.0
        self.answer: bool | None = False
        self.log: list[str] = []
        self.notes: list[str] = []
        t = target(**over)
        self.sup = od.Supervisor(
            t,
            self._probe,
            od.Workers(t.units(), self.systemd),
            clock=lambda: self.now,
            wall=lambda: 1_800_000_000.0 + self.now,
            notify=self.notes.append,
            log=self.log.append,
        )

    def _probe(self, _t: od.Target) -> bool:
        if self.answer is None:
            raise od.Unreachable("127.0.0.1:1: Connection refused")
        return self.answer

    def poll(self, answer: bool | None, advance: float = POLL) -> float:
        """Advance the clock, let the printer say `answer`, run one tick; the wait it asks for."""
        self.now += advance
        self.answer = answer
        return self.sup.tick()


def test_an_idle_printer_costs_no_systemctl_call_and_no_log_line_at_all() -> None:
    rig = Rig()
    waits = {rig.poll(False) for _ in range(200)}  # more than an hour of polls
    assert waits == {POLL} and rig.systemd.calls == [] and rig.log == []


def test_a_print_starts_both_workers_once_and_ends_in_a_full_stop_after_the_grace_period() -> (
    None
):
    rig = Rig()
    rig.poll(True)
    assert rig.systemd.running == set(U1_UNITS)
    assert rig.log == ["u1: print started: starting " + ", ".join(U1_UNITS)]
    rig.poll(True)  # running: one is-active, no second start, no second line
    assert rig.systemd.verbs() == ["is-active", "start", "is-active", "is-active"]
    rig.poll(False)
    assert rig.log[-1] == "u1: print ended: workers stay 300 s, then stop"
    rig.poll(False, 299)
    assert rig.systemd.running == set(U1_UNITS)
    rig.poll(False, 1)
    assert (
        rig.systemd.running == set()
        and rig.log[-1] == "u1: grace over: workers stopped"
    )
    calls = len(rig.systemd.calls)
    rig.poll(False)
    assert len(rig.systemd.calls) == calls  # idle again: silent


def test_the_supervisor_publishes_its_state_to_systemd_without_a_file() -> None:
    rig = Rig()
    rig.poll(False)
    rig.poll(True)
    rig.poll(None)
    assert rig.notes[0].startswith("STATUS=printer=idle phase=idle polled=")
    assert "printer=printing phase=active" in rig.notes[2]
    assert "printer=unreachable phase=grace" in rig.notes[4]
    assert rig.notes[1] == "WATCHDOG=1"


def test_a_print_already_running_at_supervisor_start_starts_the_workers_at_the_first_poll() -> (
    None
):
    rig = Rig()
    rig.sup.reconcile()
    assert rig.log == []
    rig.poll(True, 0)
    assert rig.systemd.running == set(U1_UNITS)


def test_workers_left_running_over_a_supervisor_restart_are_kept_for_one_fresh_grace_period() -> (
    None
):
    rig = Rig(FakeSystemd(U1_UNITS))
    rig.sup.reconcile()
    assert "already running" in rig.log[0]
    rig.poll(False, 100)
    assert rig.systemd.running == set(U1_UNITS)
    rig.poll(False, 200)
    assert rig.systemd.running == set()


def test_workers_left_running_over_a_restart_are_not_restarted_when_the_print_still_runs() -> (
    None
):
    rig = Rig(FakeSystemd(U1_UNITS))
    rig.sup.reconcile()
    rig.poll(True)
    assert "start" not in rig.systemd.verbs() and rig.sup.machine.phase == od.ACTIVE


def test_an_unreachable_printer_backs_off_and_never_starts_workers_and_logs_once() -> (
    None
):
    rig = Rig()
    waits = [rig.poll(None) for _ in range(6)]
    assert waits == [30.0, 60.0, 120.0, 120.0, 120.0, 120.0]
    assert rig.systemd.calls == []
    assert len(rig.log) == 1 and "does not answer" in rig.log[0]
    assert rig.poll(False) == POLL  # answers again: back to the normal interval
    assert rig.log[-1] == "u1: printer answers again"


def test_a_printer_lost_during_a_print_keeps_the_workers_for_the_grace_period_at_the_normal_rate() -> (
    None
):
    rig = Rig()
    rig.poll(True)
    assert (
        rig.poll(None) == POLL
    )  # workers run: no backoff, the printer may be back any moment
    assert "printer lost during a print" in rig.log[-1]
    rig.poll(None, GRACE)
    assert rig.systemd.running == set()


def test_a_worker_that_died_or_was_stopped_by_hand_comes_back_while_the_print_runs() -> (
    None
):
    rig = Rig()
    rig.poll(True)
    rig.systemd.running.discard(U1_UNITS[1])
    rig.poll(True)
    assert rig.systemd.running == set(U1_UNITS)
    assert ["start", U1_UNITS[1]] in rig.systemd.calls  # only the missing one


def test_a_refused_start_is_logged_once_and_retried_every_poll() -> None:
    rig = Rig()
    rig.systemd.refuse_start = "Unit 3d-printer-watch@u1.service is masked."
    for _ in range(3):
        rig.poll(True)
    errors = [line for line in rig.log if "cannot start" in line]
    assert len(errors) == 1 and "masked" in errors[0]
    assert rig.systemd.verbs().count("start") == 3
    rig.systemd.refuse_start = None
    rig.poll(True)
    assert rig.systemd.running == set(U1_UNITS)


def test_a_unit_held_by_a_condition_is_reported_not_silently_accepted() -> None:
    rig = Rig()
    rig.systemd.condition_held = {U1_UNITS[0]}
    rig.poll(True)
    assert any(
        "not running after start" in line and U1_UNITS[0] in line for line in rig.log
    )


def test_a_failed_stop_keeps_the_grace_state_and_is_retried_at_the_next_poll() -> None:
    rig = Rig()
    rig.poll(True)
    rig.poll(False)
    rig.systemd.refuse_stop = "Failed to stop: Interactive authentication required."
    rig.poll(False, GRACE)
    assert rig.systemd.running == set(U1_UNITS) and rig.sup.machine.phase == od.GRACE
    rig.systemd.refuse_stop = None
    rig.poll(False)
    assert rig.systemd.running == set() and rig.sup.machine.phase == od.IDLE
    assert sum("cannot stop" in line for line in rig.log) == 1


def test_a_timeout_of_systemctl_is_a_failed_action_not_a_crash() -> None:
    def slow(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(list(argv), 60)

    t = target()
    rig = Rig()
    rig.sup.workers = od.Workers(t.units(), slow)
    rig.poll(True)
    assert any("timed out" in line for line in rig.log)


def test_a_config_change_moves_the_poll_and_grace_but_keeps_the_phase() -> None:
    rig = Rig()
    rig.poll(True)
    rig.sup.retarget(target(poll_s=60.0, grace_s=10.0, workers=("watch",)))
    assert rig.sup.workers.units == (U1_UNITS[0],)
    assert rig.poll(False) == 60.0
    rig.poll(False, 10)
    assert rig.systemd.running == {U1_UNITS[1]}  # only the watch worker is managed now


# ---- the question ------------------------------------------------------------------------------


@contextmanager
def moonraker(state: str = "standby", **over: object) -> Iterator[FakeMoonraker]:
    fake = FakeMoonraker(status=u1_status(state))
    for k, v in over.items():
        setattr(fake, k, v)
    with fake.serve():
        yield fake


@pytest.mark.parametrize(
    ("klipper", "expected"),
    [
        ("standby", False),
        ("printing", True),
        ("paused", True),
        ("complete", False),
        ("cancelled", False),
        ("error", False),
        ("something-new", False),
    ],
)
def test_moonraker_probe_reads_print_stats_state(klipper: str, expected: bool) -> None:
    with moonraker(klipper) as fake:
        assert od.probe_moonraker(target(port=fake.port)) is expected


def test_moonraker_probe_sends_exactly_one_small_get() -> None:
    with moonraker("printing") as fake:
        od.probe_moonraker(target(port=fake.port))
    # (the fake drops the value-less `?print_stats&webhooks` query: one GET on the query endpoint is the point)
    assert [(r.method, r.path) for r in fake.requests] == [
        ("GET", "/printer/objects/query")
    ]


def test_klipper_not_ready_means_no_print_even_when_print_stats_says_printing() -> None:
    with moonraker("printing") as fake:
        fake.status["webhooks"]["state"] = "shutdown"
        assert od.probe_moonraker(target(port=fake.port)) is False


def test_moonraker_up_with_klipper_down_is_an_answer_not_an_outage() -> None:
    with moonraker(
        "printing", query_error=503
    ) as fake:  # Moonraker answers 503 while Klippy is disconnected
        assert od.probe_moonraker(target(port=fake.port)) is False


def test_other_http_errors_and_a_closed_port_are_unreachable() -> None:
    with (
        moonraker(query_error=500) as fake,
        pytest.raises(od.Unreachable, match="HTTP 500"),
    ):
        od.probe_moonraker(target(port=fake.port))
    with pytest.raises(od.Unreachable):
        od.probe_moonraker(target(port=free_port()))


@contextmanager
def raw_server(reply: bytes, *, hold_s: float = 0.0) -> Iterator[int]:
    """A TCP server that answers every connection with `reply` (after `hold_s`) and hangs up."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    stop = threading.Event()

    def serve() -> None:
        srv.settimeout(0.1)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            with conn:
                conn.recv(4096)
                stop.wait(hold_s)
                with contextlib.suppress(OSError):
                    conn.sendall(reply)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        yield int(srv.getsockname()[1])
    finally:
        stop.set()
        t.join(2)
        srv.close()


def test_a_chunked_reply_from_a_proxy_is_read() -> None:
    body = b'{"result": {"status": {"print_stats": {"state": "printing"}, "webhooks": {"state": "ready"}}}}'
    reply = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n%x\r\n%s\r\n0\r\n\r\n"
        % (len(body), body)
    )
    with raw_server(reply) as port:
        assert od.probe_moonraker(target(port=port)) is True


@pytest.mark.parametrize(
    "reply",
    [
        b"",
        b"SSH-2.0-OpenSSH_9.6\r\n",
        b"HTTP/1.0 200 OK\r\n\r\nnot json",
        b'HTTP/1.0 200 OK\r\n\r\n{"result": {}}',
    ],
)
def test_garbage_is_unreachable_not_a_verdict(reply: bytes) -> None:
    with raw_server(reply) as port, pytest.raises(od.Unreachable):
        od.probe_moonraker(target(port=port))


def test_a_server_that_never_answers_times_out_inside_the_deadline() -> None:
    with (
        raw_server(b"", hold_s=5.0) as port,
        pytest.raises(od.Unreachable, match="timed out"),
    ):
        od.http_get("127.0.0.1", port, "/", timeout=0.3)


@pytest.mark.parametrize(
    ("code", "expected"),
    [(0, False), (1, True), (5, True), (7, True), (2, False), (4, False)],
)
def test_creality_probe_maps_the_state_codes_of_the_stock_firmware(
    code: int, expected: bool
) -> None:
    fake = FakeCreality(state=k1_state(code))
    with fake.serve():
        assert (
            od.probe_creality(target(adapter="creality-stock", ws_port=fake.ws_port))
            is expected
        )


def test_creality_probe_without_a_state_in_the_push_is_unreachable_not_idle() -> None:
    fake = FakeCreality(state={"hostname": "K1SE-85B6"})
    with fake.serve(), pytest.raises(od.Unreachable, match="pushed no state"):
        od.probe_creality(target(adapter="creality-stock", ws_port=fake.ws_port))
    with pytest.raises(od.Unreachable):
        od.probe_creality(target(adapter="creality-stock", ws_port=free_port()))


def test_every_watchable_adapter_has_a_probe() -> None:
    from printer_ops.supervise_units import WATCH_ADAPTERS

    assert set(od.PROBES) == WATCH_ADAPTERS


# ---- what to supervise -------------------------------------------------------------------------

FLEET = """
p1:
  model: Test Model
  owner: own
  access: full
  adapter: moonraker
  host: 127.0.0.1
  port: 4711
  heads: [{label: T1, index: 0, extruder: extruder, usable: true}]
  watch:
    vision: {camera: {kind: snapshot-url, url: "http://127.0.0.1:1/snap"}}
p2:
  model: Test Model
  owner: own
  access: full
  adapter: moonraker
  host: 127.0.0.1
  port: 4712
  heads: [{label: T1, index: 0, extruder: extruder, usable: true}]
n1:
  model: Test Model
  owner: neighbour
  access: read-only
  adapter: moonraker
  host: 127.0.0.1
  port: 4713
  heads: [{label: T1, index: 0, extruder: extruder, usable: true}]
"""


@pytest.fixture
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fleet file (p1 with a camera, p2 without, a neighbour's read-only n1) that THREED_FLEET points at; no
    printer-devices.yaml, no real fleet, no timing variables."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)
    for var in (od.ENV_POLL, od.ENV_GRACE):
        monkeypatch.delenv(var, raising=False)
    path = tmp_path / "fleet.yaml"
    path.write_text(FLEET, encoding="utf-8")
    monkeypatch.setenv("THREED_FLEET", str(path))
    return path


def test_resolve_reads_address_workers_and_defaults_from_the_fleet(fleet: Path) -> None:
    t = od.resolve("p1")
    assert (t.name, t.adapter, t.host, t.port) == ("p1", "moonraker", "127.0.0.1", 4711)
    assert t.workers == ("watch", "vision")  # a camera: both workers
    assert (t.poll_s, t.grace_s) == (od.DEFAULT_POLL_S, od.DEFAULT_GRACE_S)
    assert str(fleet) in t.files
    assert t.units() == ("3d-printer-watch@p1.service", "3d-printer-vision@p1.service")
    assert od.resolve("p2").workers == ("watch",)  # no camera: no vision worker


def test_the_timings_come_from_the_environment_a_systemd_dropin_sets_and_flags_win(
    fleet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(od.ENV_POLL, "20")
    monkeypatch.setenv(od.ENV_GRACE, "90")
    t = od.resolve("p1")
    assert (t.poll_s, t.grace_s) == (20.0, 90.0)
    t = od.resolve("p1", poll_s=45.0, grace_s=0.0)
    assert (t.poll_s, t.grace_s) == (45.0, 0.0)


@pytest.mark.parametrize(
    ("var", "value"),
    [
        (od.ENV_POLL, "1"),
        (od.ENV_GRACE, "-5"),
        (od.ENV_POLL, "fast"),
        (od.ENV_GRACE, "nan"),
    ],
)
def test_a_bad_timing_is_refused_naming_the_variable(
    fleet: Path, monkeypatch: pytest.MonkeyPatch, var: str, value: str
) -> None:
    monkeypatch.setenv(var, value)
    with pytest.raises(UsageError, match=var):
        od.resolve("p1")


def test_resolve_refuses_a_printer_that_is_not_supervised(fleet: Path) -> None:
    with pytest.raises(UsageError, match="not a supervised printer") as exc:
        od.resolve("n1")
    assert "p1" in "".join(exc.value.remediation or [])


def test_a_target_survives_the_json_round_trip_to_the_helper_child(fleet: Path) -> None:
    t = od.resolve("p1", poll_s=40.0)
    assert od.Target.from_json(t.to_json()) == t


def test_config_changed_sees_an_edit_a_deletion_and_a_new_file(tmp_path: Path) -> None:
    a, b = tmp_path / "a.yaml", tmp_path / "b.yaml"
    a.write_text("x")
    t = target(files=(str(a), str(b)))
    seen = od.mtimes(t)
    assert not od.config_changed(t, seen)
    a.write_text("longer")
    assert od.config_changed(t, seen)
    seen = od.mtimes(t)
    b.write_text("new")
    assert od.config_changed(t, seen)
    seen = od.mtimes(t)
    a.unlink()
    assert od.config_changed(t, seen)


def test_the_status_text_is_the_contract_with_supervise_status() -> None:
    from printer_ops import supervise as sv

    rig = Rig()
    rig.poll(True)
    text = rig.notes[0].removeprefix("STATUS=")
    assert sv._status_fields(text) == {
        "printer": "printing",
        "phase": "active",
        "polled": str(int(1_800_000_000 + rig.now)),
    }
