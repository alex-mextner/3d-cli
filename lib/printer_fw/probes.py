"""probes.py — evaluate a profile's read-only probes against one printer.

WHAT: `evaluate(probe, endpoint, backends)` runs ONE probe (`http_get`, `adapter_kind`,
  `tcp_open`, `tcp_banner`, `ssh_command`, `ssh_file_exists`, `confirm`) and returns a
  `ProbeResult` with a tri-state outcome: `ok` (the evidence is there), `fail` (it is
  definitely not there) or `error` (could not tell: timeout, auth failure, unreadable body).
  `holds()` evaluates a group (`all`/`any`), `poll()` repeats a group until it holds or a deadline.
  `Backends` bundles every side effect (HTTP, sockets, the ssh runner, the clock) so tests inject
  fakes and the engine never reaches the network on its own.

WHY: state detection, step preconditions, `skip_if` and step verification are all the same
  question — "does this evidence hold on the printer right now?" — so they share one evaluator.
  The tri-state matters: a printer that is rebooting must not be read as "firmware not present",
  and a missing ssh key must not be read as "root is off".

ACCESSED VIA: `printer_fw.detect`, `.engine`, `.guards`, `.backup`.

INVARIANTS: probes never write: HTTP probes are GET, ssh probes run profile-validated read-only
  commands (profiles.read_only_command), sockets are only connected and read. A refused
  connection is a definite `fail` (nothing listens); every other transport problem is `error`.
  `negate` flips ok/fail and leaves error/skipped alone.
"""

from __future__ import annotations

import json
import re
import shlex
import socket
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from printer_fw.profiles import Probe, SshSettings
from printer_fw.runner import SSH_ERROR_RC, TIMEOUT_RC, CommandRunner

OK, FAIL, ERROR, SKIPPED = "ok", "fail", "error", "skipped"
MAX_BODY = 256 * 1024


class HttpFn(Protocol):
    def __call__(
        self,
        method: str,
        url: str,
        *,
        timeout: float,
        max_bytes: int | None = None,
        body: bytes | None = None,
    ) -> tuple[int, bytes]: ...


class UploadFn(Protocol):
    def __call__(
        self,
        url: str,
        local: Path,
        remote_name: str,
        fields: dict[str, str],
        *,
        timeout: float,
    ) -> tuple[int, bytes]: ...


def tcp_connect(host: str, port: int, timeout: float) -> None:
    """Raises OSError (ConnectionRefusedError, TimeoutError, ...) unless a TCP connection opens."""
    with socket.create_connection((host, port), timeout=timeout):
        return


def tcp_read(host: str, port: int, timeout: float) -> bytes:
    """What the service at host:port sends on connect (an SSH banner); b'' when it stays silent."""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        try:
            return sock.recv(256)
        except TimeoutError:
            return b""


def _default_http() -> HttpFn:
    from printer_ops.base import http_request

    return http_request


def _default_upload() -> UploadFn:
    from printer_ops.base import http_upload

    return http_upload


@dataclass
class Backends:
    """Every side effect of a firmware run, injectable. `runner` is required (no default: a real
    ssh runner is built by the CLI, a recording fake by tests)."""

    runner: CommandRunner
    http: HttpFn = field(default_factory=_default_http)
    upload: UploadFn = field(default_factory=_default_upload)
    connect: Callable[[str, int, float], None] = tcp_connect
    read_banner: Callable[[str, int, float], bytes] = tcp_read
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    use_ssh: bool = True
    # Asks the user a yes/no question; None when nobody can answer (non-interactive).
    confirm: Callable[[str], bool] | None = None


@dataclass(frozen=True)
class Endpoint:
    """Where one printer's services are: the host, a port per profile service, the registry's
    adapter name (for `adapter_kind`) and the profile's ssh settings."""

    host: str
    ports: Mapping[str, int]
    adapter: str
    ssh: SshSettings | None

    def url(self, service: str, path: str) -> str:
        return f"http://{self.host}:{self.ports[service]}{path}"

    @property
    def ssh_port(self) -> int | None:
        return self.ports.get("ssh")


@dataclass(frozen=True)
class ProbeResult:
    probe: Probe
    outcome: str  # ok | fail | error | skipped (AFTER negate)
    detail: str

    @property
    def holds(self) -> bool:
        return self.outcome == OK

    def to_json(self) -> dict[str, str]:
        return {
            "id": self.probe.id,
            "kind": self.probe.kind,
            "title": self.probe.title,
            "outcome": self.outcome,
            "detail": self.detail,
        }


def _flip(outcome: str, negate: bool) -> str:
    if not negate or outcome not in (OK, FAIL):
        return outcome
    return FAIL if outcome == OK else OK


def _lookup(data: object, dotted: str) -> tuple[bool, object]:
    """`a.b.0.c` -> data["a"]["b"][0]["c"]; (False, None) when a segment is missing."""
    cur = data
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return False, None
    return True, cur


def _as_text(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _http(probe: Probe, ep: Endpoint, be: Backends) -> tuple[str, str]:
    url = ep.url(probe.service, probe.path)
    try:
        status, body = be.http("GET", url, timeout=probe.timeout, max_bytes=MAX_BODY)
    except ConnectionRefusedError:
        return FAIL, f"{url}: connection refused"
    except (OSError, ValueError) as exc:
        return ERROR, f"{url}: {exc}"
    if status not in probe.expect_status:
        return (
            FAIL,
            f"{url}: HTTP {status}, expected {'/'.join(map(str, probe.expect_status))}",
        )
    if not probe.json_path and probe.regex is None:
        return OK, f"{url}: HTTP {status}"
    if len(body) >= MAX_BODY:
        return (
            ERROR,
            f"{url}: answer longer than {MAX_BODY} bytes, cannot be read whole",
        )
    # A body that does not hold the value we look for is ABSENT evidence for a plain probe, but
    # for a negated one it must not read as "the bad thing is absent": that would pass a hardening
    # check against an answer of a shape nobody expected. `absent_ok` opts in for the options that
    # are legitimately missing when unset (e.g. an auth switch left at its default).
    unreadable = FAIL if not probe.negate or probe.absent_ok else ERROR
    if probe.json_path:
        try:
            doc = json.loads(body)
        except ValueError:
            return unreadable, f"{url}: HTTP {status} but the body is not JSON"
        found, value = _lookup(doc, probe.json_path)
        if not found:
            return unreadable, f"{url}: no {probe.json_path} in the answer"
        text = _as_text(value)
        if probe.equals is not None and text != probe.equals:
            return (
                FAIL,
                f"{url}: {probe.json_path} is {text!r}, expected {probe.equals!r}",
            )
        if probe.regex is not None and not re.search(probe.regex, text):
            return (
                FAIL,
                f"{url}: {probe.json_path} is {text!r}, expected /{probe.regex}/",
            )
        return OK, f"{url}: {probe.json_path} = {text[:80]!r}"
    text = body.decode("utf-8", "replace")
    if probe.regex is not None and re.search(probe.regex, text):
        return OK, f"{url}: body matches /{probe.regex}/"
    return FAIL, f"{url}: body does not match /{probe.regex}/"


def _tcp_open(probe: Probe, ep: Endpoint, be: Backends) -> tuple[str, str]:
    port = ep.ports[probe.service]
    try:
        be.connect(ep.host, port, probe.timeout)
    except ConnectionRefusedError:
        return FAIL, f"{ep.host}:{port} refused the connection"
    except OSError as exc:
        return ERROR, f"{ep.host}:{port}: {exc}"
    return OK, f"{ep.host}:{port} accepts connections"


def _tcp_banner(probe: Probe, ep: Endpoint, be: Backends) -> tuple[str, str]:
    if not be.use_ssh and probe.service == "ssh":
        return SKIPPED, "ssh disabled (--no-ssh)"
    port = ep.ports[probe.service]
    try:
        data = be.read_banner(ep.host, port, probe.timeout)
    except ConnectionRefusedError:
        return FAIL, f"{ep.host}:{port} refused the connection"
    except OSError as exc:
        return ERROR, f"{ep.host}:{port}: {exc}"
    text = data.decode("utf-8", "replace").strip()
    if not text:
        return FAIL, f"{ep.host}:{port} sent no banner within {probe.timeout:g}s"
    assert probe.regex is not None
    if re.search(probe.regex, text):
        return OK, f"{ep.host}:{port} banner {text[:60]!r}"
    return FAIL, f"{ep.host}:{port} banner {text[:60]!r} does not match /{probe.regex}/"


def _ssh(probe: Probe, ep: Endpoint, be: Backends, script: str) -> tuple[str, str, str]:
    """(outcome, detail, stdout) of one read-only remote command; rc 1+ is a fail, a transport
    problem (rc 255 / timeout) is an error."""
    if not be.use_ssh:
        return SKIPPED, "ssh disabled (--no-ssh)", ""
    if (
        ep.ssh is None
    ):  # profile validation guarantees this never happens for a shipped profile
        return ERROR, "the profile has no ssh settings", ""
    result = be.runner.run(
        ep.host,
        ep.ssh.user,
        script,
        timeout=max(probe.timeout, ep.ssh.connect_timeout_s),
        port=ep.ssh_port,
    )
    if result.rc in (SSH_ERROR_RC, TIMEOUT_RC):
        why = result.err.strip().splitlines()[-1] if result.err.strip() else "no answer"
        return ERROR, f"ssh {ep.ssh.user}@{ep.host}: {why}", ""
    return (
        (OK if result.rc == 0 else FAIL),
        f"`{script}` exited {result.rc}",
        result.out,
    )


def _ssh_command(probe: Probe, ep: Endpoint, be: Backends) -> tuple[str, str]:
    outcome, detail, out = _ssh(probe, ep, be, probe.command)
    if outcome == OK and probe.regex is not None and not re.search(probe.regex, out):
        return FAIL, f"`{probe.command}` output does not match /{probe.regex}/"
    return outcome, detail


def _ssh_file(probe: Probe, ep: Endpoint, be: Backends) -> tuple[str, str]:
    outcome, detail, _ = _ssh(probe, ep, be, f"test -e {shlex.quote(probe.path)}")
    if outcome == OK:
        return OK, f"{probe.path} exists"
    if outcome == FAIL:
        return FAIL, f"{probe.path} does not exist"
    return outcome, detail


def _confirm(probe: Probe, be: Backends) -> tuple[str, str]:
    if be.confirm is None:
        return SKIPPED, "needs a human answer (non-interactive run)"
    return (OK, "confirmed") if be.confirm(probe.prompt) else (FAIL, "not confirmed")


def evaluate(probe: Probe, ep: Endpoint, be: Backends) -> ProbeResult:
    if probe.kind == "http_get":
        outcome, detail = _http(probe, ep, be)
    elif probe.kind == "adapter_kind":
        same = ep.adapter == probe.equals
        outcome = OK if same else FAIL
        detail = (
            f"registry adapter is {ep.adapter or 'none'!r}, expected {probe.equals!r}"
        )
    elif probe.kind == "tcp_open":
        outcome, detail = _tcp_open(probe, ep, be)
    elif probe.kind == "tcp_banner":
        outcome, detail = _tcp_banner(probe, ep, be)
    elif probe.kind == "ssh_command":
        outcome, detail = _ssh_command(probe, ep, be)
    elif probe.kind == "ssh_file_exists":
        outcome, detail = _ssh_file(probe, ep, be)
    else:
        outcome, detail = _confirm(probe, be)
    return ProbeResult(probe, _flip(outcome, probe.negate), detail)


def evaluate_group(
    probes: Sequence[Probe], mode: str, ep: Endpoint, be: Backends
) -> tuple[bool, list[ProbeResult]]:
    """All/any of `probes`. An empty `all` holds (a baseline); an empty `any` does not. `all`
    stops at the first probe that does not hold (later probes may need what it proves, e.g. ssh
    only after the banner)."""
    results: list[ProbeResult] = []
    if mode == "any":
        for p in probes:
            r = evaluate(p, ep, be)
            results.append(r)
            if r.holds:
                return True, results
        return False, results
    for p in probes:
        r = evaluate(p, ep, be)
        results.append(r)
        if not r.holds:
            return False, results
    return True, results


def poll(
    probes: Sequence[Probe],
    ep: Endpoint,
    be: Backends,
    *,
    wait_s: float,
    interval_s: float,
) -> tuple[bool, list[ProbeResult]]:
    """Evaluate `probes` (all must hold) now and, while they do not, every `interval_s` until
    `wait_s` elapsed — a rebooting printer needs minutes to show its new state."""
    deadline = be.monotonic() + wait_s
    while True:
        ok, results = evaluate_group(probes, "all", ep, be)
        if ok or be.monotonic() >= deadline:
            return ok, results
        be.sleep(min(interval_s, max(0.0, deadline - be.monotonic())))


def wait_down(
    probes: Sequence[Probe],
    ep: Endpoint,
    be: Backends,
    *,
    wait_s: float,
    interval_s: float,
) -> tuple[bool, list[ProbeResult]]:
    """Poll until `probes` STOP holding (the printer went down to reboot) or `wait_s` elapsed;
    True means the drop was seen. A verification that already holds before an action started says
    nothing about the action, so a reboot step must first see its printer disappear."""
    deadline = be.monotonic() + wait_s
    while True:
        up, results = evaluate_group(probes, "all", ep, be)
        if not up:
            return True, results
        if be.monotonic() >= deadline:
            return False, results
        be.sleep(min(interval_s, max(0.0, deadline - be.monotonic())))
