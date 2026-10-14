"""printer_agent.py — the temporary omp agent of one printer, for alerts no subscriber took.

WHAT: `PrinterAgentSink` (LAST_RESORT stage of the watch Dispatcher) gets the warning+ events
  that no live agent (a `--steer` target or a `3d printer subscribe` session) took. Per printer
  it keeps ONE interactive omp session running in tmux:
      [systemd-run --user --scope] tmux -L 3d-printer-agent new-session -d -s 3d-printer-agent-NAME \
          -c <state>/printer-agents/NAME \
          -- omp --append-system-prompt <dir>/role.md "<first alert>" @<frame>...
  The first alert is its opening message; later alerts reach it through `tg-ctl omp-live send`
  (it is found by its cwd, the per-printer dir). It judges each alert (frame, events log,
  `3d printer status`) and decides itself whether to message the user (`tg`). It lives until a
  live session subscribes to every kind of that printer at warning or lower: it is then asked to run
  `3d printer agent handoff NAME --summary ...`, which forwards the summary to the subscriber
  and kills the tmux session.

WHY: no alert may go straight to the user (a false U1 "noodle" alarm did), and no target
  session is guessed: without an explicit live subscriber, a dedicated agent judges first.

FILES (<state>/printer-agents/NAME/): role.md (the standing instructions), events.jsonl (every
  event routed to the agent), pane.log (the tmux pane). Start/handoff/error state lives in
  subscriptions.DeliveryState (`agents`).

LIMITS: one start per printer per START_EVERY_S (shared by all watcher processes). Waiting for
  a starting agent to show up in omp-live (READY_TIMEOUT_S) happens on a per-printer thread,
  never on the dispatcher thread: a slow U1 agent must not delay a K1 alert. If it does not
  show up, the tmux session is killed and the events go back to the Dispatcher's FALLBACK
  sinks (the emergency tg) through `Sink.on_unreached`.

INVARIANTS: stdlib only; never raises into the watch loop (the sink records and logs); this
  module itself never talks to a printer.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from errors import UsageError
from printer_ops.subscriptions import (
    DeliveryState,
    Registry,
    alert_text,
    fresh_frame_hint,
    frames_of,
    iso,
    names,
    parse,
)
from printer_ops.watch import WARNING, Event, severity_rank
from printer_ops.watch_sinks import LAST_RESORT, OmpLive, Runner, Sink

SESSION_PREFIX = "3d-printer-agent-"
# Own tmux server: the user's tmux sessions never share it (nor its cgroup), and a watcher
# restart cannot take them down. Attach with `tmux -L 3d-printer-agent attach -t ...`.
TMUX_SOCKET = "3d-printer-agent"
AGENT_MIN_SEVERITY = WARNING
START_EVERY_S = 600.0
READY_TIMEOUT_S = 90.0
_TOOL_DIRS = ("~/.local/bin", "/home/linuxbrew/.linuxbrew/bin", "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")
_PASS_ENV = ("PATH", "HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME")


def _log(msg: str) -> None:
    print(f"printer-agent: {msg}", file=sys.stderr, flush=True)


def agents_root() -> Path:
    from cli.paths import state_dir

    return state_dir() / "printer-agents"


def agent_dir(printer: str) -> Path:
    return agents_root() / printer


def session_name(printer: str) -> str:
    return SESSION_PREFIX + printer


def find_tool(name: str) -> str | None:
    """PATH first, then the usual install dirs (a systemd user unit has a minimal PATH)."""
    found = shutil.which(name)
    if found:
        return found
    for d in _TOOL_DIRS:
        p = Path(d).expanduser() / name
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


ROLE = """You are the TEMPORARY alert agent for the 3D printer `{printer}`.
No omp session is subscribed to this printer's alerts right now, so you judge them before the
user hears anything. The camera/firmware detectors are noisy: most alerts are false alarms.

For every alert message you get (the first one is below, later ones arrive while you idle):
1. Look at the evidence: open every frame path with your read tool; check
   `3d printer status {printer}`; the full event history is in {events}.
   No frame or too old: {fresh}.
2. Decide: REAL print failure (spaghetti/noodle, detached or knocked-over part, blob on the
   nozzle, layer shift, printing in air, filament runout, heater fault) or FALSE ALARM?
3. Real: tell the user via `tg` — one short Russian message with the frame:
   `tg --photo FRAME "{printer}: <что видно>, <что советуешь>"`. False alarm: do not message
   the user; one line for yourself is enough.
4. Never pause, cancel, or send G-code or any command to the printer without the user's
   explicit ok. ONE exception (policy D5), for serious faults: the wrong material loaded, a
   wrong or dangerous temperature, carbon-fibre (CF) filament on a brass nozzle. Then pause the
   print with `3d printer pause {printer} --yes` if `3d printer --help` lists that command
   (otherwise do not touch the printer), and call the user at once:
   `tg call me --say "<коротко по-русски, что случилось>"`.
5. Do not edit files and do not start other work. Stay idle between alerts.

Handover: when you are told that a session has subscribed to {printer}, run exactly once:
  3d printer agent handoff {printer} --summary '<alerts seen, your verdicts, what you told the user>'
It forwards your summary to that session and ends this temporary session.
"""


def attach_hint(printer: str) -> str:
    return f"tmux -L {TMUX_SOCKET} attach -t {session_name(printer)}"


def config_note(cwd: Path) -> str:
    """The agent runs in its state dir; a project-local printer-devices.yaml of the watcher
    (which overrides the global one by name) must still apply to its `3d printer` commands."""
    local = cwd / "printer-devices.yaml"
    if not local.is_file():
        return ""
    return (f"\nPrinter config: the watcher uses {local}, which overrides the global one. Run every "
            f"`3d printer ...` command from that directory: `cd {shlex.quote(str(cwd))} && 3d printer ...`.\n")


def agent_alert(events: list[Event], printer: str) -> str:
    return alert_text(events, f"[3d printer alert -> temporary {printer} agent]",
                      [f"Event history: {agent_dir(printer) / 'events.jsonl'}. You stay running for the next alert."])


class PrinterAgents:
    """Start, find, message and stop the per-printer temporary agents."""

    def __init__(self, *, runner: Runner = subprocess.run, live: OmpLive | None = None,
                 state: DeliveryState | None = None, tmux: str | None = None, omp: str | None = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 ready_timeout_s: float = READY_TIMEOUT_S, start_every_s: float = START_EVERY_S,
                 scope: bool = True) -> None:
        self.runner = runner
        self.live = live or OmpLive(runner=runner)
        self.state = state or DeliveryState()
        self.tmux_bin = tmux
        self.omp_bin = omp
        self.clock = clock
        self.sleep = sleep
        self.ready_timeout_s = ready_timeout_s
        self.start_every_s = start_every_s
        self.waiting: list[threading.Thread] = []
        self.scope = scope   # start the tmux server in its own systemd scope, outside the watcher unit

    # ---- tmux -----------------------------------------------------------------------------
    def _tmux(self, *args: str, scoped: bool = False) -> subprocess.CompletedProcess[str]:
        tmux = self.tmux_bin or find_tool("tmux")
        if not tmux:
            raise RuntimeError("tmux not found: the temporary printer agent runs in a tmux session")
        prefix: list[str] = []
        systemd_run = find_tool("systemd-run") if scoped and self.scope else None
        if systemd_run:  # else a first server would live in (and die with) the watcher unit's cgroup
            prefix = [systemd_run, "--user", "--scope", "--quiet", "--collect"]
        return self.runner([*prefix, tmux, "-L", TMUX_SOCKET, *args], text=True, capture_output=True, timeout=20)

    def running(self, printer: str) -> bool:
        try:
            return self._tmux("has-session", "-t", f"={session_name(printer)}").returncode == 0
        except (RuntimeError, OSError, subprocess.SubprocessError):
            return False

    def known(self, printers: tuple[str, ...] = ()) -> list[str]:
        """Printers (all, or these) whose temporary agent is running."""
        root = agents_root()
        if not printers and root.is_dir():
            printers = tuple(sorted(p.name for p in root.iterdir() if p.is_dir()))
        return [p for p in printers if self.running(p)]

    # ---- omp-live -------------------------------------------------------------------------
    def host(self, printer: str, hosts: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
        """The live omp session running in the printer's agent dir."""
        want = str(agent_dir(printer).resolve())
        for h in self.live.sessions() if hosts is None else hosts:
            cwd = str(h.get("cwd") or "")
            if cwd and str(Path(cwd).resolve()) == want:
                return h
        return None

    def _wait_ready(self, printer: str) -> dict[str, Any]:
        """The agent's omp-live host once it is up. A transient omp-live failure is retried
        until the deadline; the tmux session may appear late (another watcher is creating it).
        On giving up the session is stopped, so the agent never also gets the alert later."""
        deadline = self.clock() + self.ready_timeout_s
        seen_running = False
        while True:
            try:
                h = self.host(printer)
            except Exception as exc:  # tg-ctl hiccup: retry within the deadline
                _log(f"{printer}: omp-live status failed while the agent starts: {exc}")
                h = None
            if h is not None:
                return h
            if self.running(printer):
                seen_running = True
            elif seen_running:
                raise RuntimeError(f"temporary {printer} agent exited while starting (see "
                                   f"{agent_dir(printer) / 'pane.log'})")
            if self.clock() >= deadline:
                self.stop(printer, release=False)   # a failed start keeps its restart-limit slot
                raise RuntimeError(f"temporary {printer} agent did not come up within {self.ready_timeout_s:g} s "
                                   f"(see {agent_dir(printer) / 'pane.log'}); stopped it")
            self.sleep(2.0)

    # ---- lifecycle ------------------------------------------------------------------------
    def start(self, printer: str, events: list[Event]) -> None:
        omp = self.omp_bin or find_tool("omp")
        if not omp:
            raise RuntimeError("omp not found: cannot start the temporary printer agent")
        d = agent_dir(printer)
        d.mkdir(parents=True, exist_ok=True)
        role = d / "role.md"
        role.write_text(ROLE.format(printer=printer, events=d / "events.jsonl", fresh=fresh_frame_hint(printer))
                        + config_note(Path.cwd()), encoding="utf-8")
        frames = [f"@{f}" for f in dict.fromkeys(f for e in events for f in frames_of(e)) if Path(f).is_file()]
        env = [x for k in _PASS_ENV if os.environ.get(k) for x in ("-e", f"{k}={os.environ[k]}")]
        r = self._tmux("new-session", "-d", "-s", session_name(printer), "-c", str(d), "-x", "200", "-y", "50",
                       *env, "--", omp, "--append-system-prompt", str(role), agent_alert(events, printer), *frames,
                       scoped=True)
        if r.returncode != 0:
            raise RuntimeError(f"tmux new-session failed: {(r.stderr or r.stdout or '').strip()[:300]}")
        self._tmux("pipe-pane", "-t", f"={session_name(printer)}", "-o",
                   f"cat >> {shlex.quote(str(d / 'pane.log'))}")
        self.state.set_agent(printer, started=iso(self.clock()), handoff_asked=None, stopped=None)
        _log(f"started the temporary {printer} agent ({attach_hint(printer)})")

    def stop(self, printer: str, *, release: bool = True) -> bool:
        """Kill the agent's tmux session. `release` frees the restart-limit slot: the limit guards
        against crash-looping starts, not against a new agent after a deliberate stop/handoff."""
        stopped = self._tmux("kill-session", "-t", f"={session_name(printer)}").returncode == 0
        fields: dict[str, Any] = {"start_claimed": None} if release else {}
        if stopped:
            fields["stopped"] = iso(self.clock())
        if fields:
            self.state.set_agent(printer, **fields)
        return stopped

    def deliver(self, printer: str, events: list[Event],
                on_unreached: Callable[[list[Event]], None] | None = None) -> str:
        """Hand `events` to the printer's agent, starting it if needed. -> what happened.
        A live agent gets them now; a starting one is awaited on a background thread, which
        calls `on_unreached(events)` if it never shows up. Raises RuntimeError when no agent
        can take them at all (restart limit, tmux/omp missing)."""
        d = agent_dir(printer)
        d.mkdir(parents=True, exist_ok=True)
        with (d / "events.jsonl").open("a", encoding="utf-8") as fh:
            for e in events:
                fh.write(json.dumps(e.to_json(), ensure_ascii=False, default=str) + "\n")
        h = self.host(printer)
        if h is not None:
            self.live.send(h, agent_alert(events, printer))
            return f"sent to the running {printer} agent (pid {h.get('pid')})"
        if self.running(printer):  # just started (by this or another watcher): send once it is up
            self._await(printer, events, on_unreached, send=True)
            return f"the {printer} agent is starting; the alert follows once it is up"
        now = self.clock()
        if not self.state.claim_start(printer, now, self.start_every_s):
            claimed = self.state.agent(printer).get("start_claimed")
            if isinstance(claimed, (int, float)) and 0 <= now - claimed < self.ready_timeout_s:
                # another watcher (e.g. vision while watch reports the same noodle) is starting it
                self._await(printer, events, on_unreached, send=True)
                return f"another watcher is starting the {printer} agent; the alert follows once it is up"
            raise RuntimeError(f"temporary {printer} agent was started less than {self.start_every_s / 60:g} min ago "
                               "and is not running (restart limit)")
        self.start(printer, events)
        self._await(printer, events, on_unreached, send=False)
        return f"starting the {printer} agent ({attach_hint(printer)})"

    def _await(self, printer: str, events: list[Event], on_unreached: Callable[[list[Event]], None] | None,
               *, send: bool) -> None:
        def wait() -> None:
            try:
                h = self._wait_ready(printer)
                if send:
                    self.live.send(h, agent_alert(events, printer))
                _log(f"{printer}: the agent is up (pid {h.get('pid')})")
            except Exception as exc:  # never raised: the events go to the fallback instead
                _log(f"{printer}: {exc}")
                self.state.set_agent(printer, last_error=str(exc), last_error_ts=iso(self.clock()))
                if on_unreached is not None:
                    on_unreached(events)

        # not a daemon: a watcher that exits while an agent starts still delivers or falls back
        t = threading.Thread(target=wait, name=f"printer-agent-{printer}")
        self.waiting.append(t)
        t.start()

    def join(self, timeout: float | None = None) -> None:
        """Wait for the background readiness checks (tests, shutdown); `timeout` is the total."""
        deadline = None if timeout is None else time.monotonic() + timeout
        for t in list(self.waiting):
            t.join(None if deadline is None else max(0.0, deadline - time.monotonic()))

    def request_handoff(self, printer: str, subscriber: dict[str, Any],
                        hosts: list[dict[str, Any]] | None = None) -> bool:
        """Ask the running agent (once per agent session) to hand over to `subscriber`."""
        if not agent_dir(printer).is_dir():
            return False
        h = self.host(printer, hosts)
        asked = f"{h.get('sessionId')}->{subscriber.get('sessionId')}" if h is not None else ""
        if h is None or self.state.agent(printer).get("handoff_asked") == asked:
            return False  # asked once per (agent, subscriber): a new subscriber asks again
        self.live.send(h, (
            f"[3d printer agent] Session {subscriber.get('sessionId')} (pid {subscriber.get('pid')}, "
            f"{subscriber.get('title')!r}) has subscribed to {printer} alerts and takes over now. Run exactly once:\n"
            f"3d printer agent handoff {printer} --summary '<alerts seen, your verdicts, what you told the user>'"))
        self.state.set_agent(printer, handoff_asked=asked, handoff_asked_ts=iso(self.clock()))
        return True


class PrinterAgentSink(Sink):
    """LAST_RESORT stage: warning+ events no live agent took go to the printer's temporary agent."""

    name = "printer-agent"
    rate = None
    agent = True
    stage = LAST_RESORT

    def __init__(self, *, agents: PrinterAgents | None = None, dry_run: bool = False) -> None:
        super().__init__()
        self.agents = agents
        self.dry_run = dry_run

    def close(self, timeout: float) -> None:
        if self.agents is not None:
            self.agents.join(timeout)

    def deliver(self, events: list[Event], suppressed: int = 0) -> list[Event]:
        mine = [e for e in events if severity_rank(e.severity) >= severity_rank(AGENT_MIN_SEVERITY)]
        by_printer: dict[str, list[Event]] = {}
        for e in mine:
            by_printer.setdefault(e.printer, []).append(e)
        reached: list[Event] = []
        for printer, evs in by_printer.items():
            if self.dry_run:
                _log(f"dry-run temporary {printer} agent ({session_name(printer)}):\n{agent_alert(evs, printer)}")
                reached += evs
                continue
            if self.agents is None:
                self.agents = PrinterAgents()
            agents = self.agents
            try:
                what = agents.deliver(printer, evs, self.on_unreached)
            except Exception as exc:  # never into the watch loop: the tg fallback gets them
                self.stats.last_error = f"{printer}: {exc}"
                _log(f"{printer}: {exc}")
                agents.state.set_agent(printer, last_error=str(exc), last_error_ts=iso(agents.clock()))
                continue
            _log(f"{printer}: {what}")
            agents.state.set_agent(printer, last_ts=iso(agents.clock()), last_kinds=sorted({e.kind for e in evs}))
            reached += evs
        return reached


# ---- CLI ------------------------------------------------------------------------------------

USAGE = """3d printer agent list [--json]
3d printer agent stop PRINTER
3d printer agent handoff PRINTER --summary TEXT

  The temporary omp agent of a printer: warning+ alerts that no live subscribed session took
  (`3d printer subscribe --help`) go to it. It runs in tmux session 3d-printer-agent-PRINTER on
  its own tmux server (watch it: `tmux -L 3d-printer-agent attach -t 3d-printer-agent-u1`),
  cwd ~/.local/state/3d-cli/printer-agents/
  PRINTER/ (role.md, events.jsonl, pane.log), started on the first such alert with
      tmux -L 3d-printer-agent new-session -d -s 3d-printer-agent-PRINTER -c DIR -- omp --append-system-prompt
          DIR/role.md "<alert>" @FRAME
  at most once per 10 min per printer; it decides itself whether to message the user (tg).
  When a live session subscribes to every kind of the printer (at warning or lower), the
  agent is asked to hand over and runs `handoff`.

  list      running agents: tmux session, live omp pid, started, last alert/error, handoff
  stop      kill the printer's agent session
  handoff   (run by the agent) forward --summary to every live session subscribed to every
            kind of the printer (warning+), then stop the agent; exit 1 (agent kept) when no subscriber is live

Examples:
  3d printer agent list
  3d printer agent list --json | jq '.[] | {printer, live}'
  3d printer agent stop u1
  3d printer agent handoff u1 --summary 'two noodle alerts, both false (purge line); user not told'"""


def cmd_list(argv: list[str]) -> int:
    _, opts = parse(argv, {"--json"}, "agent list")
    agents = PrinterAgents()
    root = agents_root()
    printers = sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    try:
        hosts: list[dict[str, Any]] | None = agents.live.sessions()
    except Exception:
        hosts = None
    state = agents.state.read()["agents"]
    rows = []
    for p in printers:
        h = agents.host(p, hosts) if hosts is not None else None
        rows.append({"printer": p, "session": session_name(p), "running": agents.running(p),
                     "live": None if hosts is None else h is not None, "pid": h.get("pid") if h else None,
                     "dir": str(agent_dir(p)), "state": state.get(p, {})})
    if opts.get("--json"):
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    if not rows:
        print("no temporary printer agents")
        return 0
    for r in rows:
        st = r["state"]
        live = {True: f"live pid {r['pid']}", False: "not in omp-live", None: "omp-live unknown"}[r["live"]]
        print(f"{r['printer']}  {r['session']}: {'running' if r['running'] else 'stopped'}, {live}")
        print(f"  started {st.get('started') or '-'}; last alert {st.get('last_ts') or '-'} "
              f"{','.join(st.get('last_kinds') or [])}; handoff asked {st.get('handoff_asked_ts') or '-'}")
        if st.get("last_error"):
            print(f"  last error: {st.get('last_error_ts')} {st['last_error']}")
        print(f"  dir: {r['dir']}")
    return 0


def cmd_stop(argv: list[str]) -> int:
    pos, _ = parse(argv, set(), "agent stop")
    if len(pos) != 1:
        raise UsageError("`printer agent stop` takes one PRINTER", command="printer",
                         remediation=["3d printer agent stop u1"])
    printer = names(pos[0], "PRINTER")[0]
    print(f"stopped {session_name(printer)}" if PrinterAgents().stop(printer) else f"no {session_name(printer)} running")
    return 0


def cmd_handoff(argv: list[str]) -> int:
    pos, opts = parse(argv, {"--summary"}, "agent handoff")
    if len(pos) != 1 or not str(opts.get("--summary") or "").strip():
        raise UsageError("`printer agent handoff` takes one PRINTER and --summary TEXT", command="printer",
                         remediation=["3d printer agent handoff u1 --summary 'one false noodle alarm; user not told'"])
    printer = names(pos[0], "PRINTER")[0]
    agents = PrinterAgents()
    try:
        hosts = agents.live.sessions()
    except RuntimeError as exc:
        print(f"cannot reach the omp sessions ({exc}): the temporary agent stays", file=sys.stderr)
        return 1
    targets: dict[str, dict[str, Any]] = {}
    for s in Registry().load():
        if s.takes_over(printer):
            try:
                h = OmpLive.resolve(s.session, hosts, titles=False)[0]
            except RuntimeError:
                continue
            targets.setdefault(str(h.get("sessionId")), h)
    if not targets:
        print(f"no live session is subscribed to {printer}: the temporary agent stays", file=sys.stderr)
        return 1
    text = (f"[3d printer agent handoff] {printer}: the temporary agent hands its alerts over to you. Its summary:\n"
            f"{opts['--summary'].strip()}\nEvent history: {agent_dir(printer) / 'events.jsonl'}")
    for h in targets.values():
        agents.live.send(h, text)
        print(f"handed over {printer} to session {h.get('sessionId')} (pid {h.get('pid')})")
    agents.state.set_agent(printer, handed_over=iso(agents.clock()))
    agents.stop(printer)  # last: this command usually runs inside that very tmux session
    return 0


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if argv else 1
    sub, rest = argv[0], argv[1:]
    if sub == "list":
        return cmd_list(rest)
    if sub == "stop":
        return cmd_stop(rest)
    if sub == "handoff":
        return cmd_handoff(rest)
    raise UsageError(f"unknown `printer agent` subcommand {sub!r}", command="printer",
                     remediation=["Use list, stop or handoff — see `3d printer agent --help`."])


__all__ = ["USAGE", "PrinterAgentSink", "PrinterAgents", "agent_alert", "agent_dir", "find_tool", "run", "session_name"]
