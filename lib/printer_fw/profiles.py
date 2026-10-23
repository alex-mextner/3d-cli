"""profiles.py — the vendor firmware profile schema, its loader and its validator.

WHAT: a profile is a YAML file in `lib/data/firmware/` (one per vendor/family) that describes, as
  DATA, how a family of printers moves between firmware states: which model names it matches, the
  read-only probes that detect each state, the transitions between states and the ordered steps of
  each (manual instructions, HTTP uploads, ssh scripts), the backups a runbook demands first, and
  the machine-checked guards (pitfalls). `load_profiles()` parses and validates every file;
  `match_profile(model)` picks the one whose matchers accept a fleet entry's `model`.

WHY: no printer name, model or address may live in generic code (issue #519): everything a
  vendor runbook says is a profile entry that cites its source URL, and a new printer family is a
  new YAML file, not a code change. Validating at load turns a typo (an unknown probe kind, a step
  that claims `automatable` without a verification probe, a guard naming a step that does not
  exist) into a precise load-time error instead of a wrong action on a printer.

ACCESSED VIA: `printer_fw.detect`, `.plan`, `.backup`, `.engine`, `.guards`, `.render` and the
  thin `printers_cli/fw.py`; schema reference: docs/printers/firmware-profiles.md.

INVARIANTS: stdlib + lazy pyyaml only. Every validation problem is reported at once, each with
  the path of the offending key (`snapmaker-u1.yaml: states[1].detection.all[0].kind: ...`).
  Probes in `detection`, `preconditions`, `skip_if` and `verify` are READ-ONLY by construction:
  an `ssh_command` probe must start with an allow-listed read command and contain no shell
  redirection, chaining or substitution. `automatable: true` requires a `verify` probe and a
  precise command/endpoint; every step needs a `verify`; medium/high-risk steps need a `rollback`.
  Matchers apply to the fleet entry's `model`, never to a printer's name.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from errors import MissingDependency, ThreeDError

DATA_DIR: Final = Path(__file__).resolve().parent.parent / "data" / "firmware"

PROBE_KINDS: Final = (
    "http_get",
    "adapter_kind",
    "tcp_open",
    "tcp_banner",
    "ssh_command",
    "ssh_file_exists",
    "confirm",
)
SSH_PROBE_KINDS: Final = frozenset({"tcp_banner", "ssh_command", "ssh_file_exists"})
STEP_KINDS: Final = ("manual", "http_upload", "http_post", "ssh_script", "ssh_command")
RISKS: Final = ("low", "medium", "high")
BACKUP_VIA: Final = ("ssh_file", "ssh_tar", "http_get", "manual")
GUARD_KINDS: Final = (
    "forbidden_option",
    "forbidden_command_pattern",
    "step_requires_command",
    "step_requires_step",
    "require_idle",
    "invariant_probe",
)
RUNTIME_GUARD_KINDS: Final = frozenset({"require_idle", "invariant_probe"})
# What an `ssh_command` probe may run: reads only (a probe must never change a printer).
READ_ONLY_COMMANDS: Final = frozenset(
    {
        "cat",
        "test",
        "[",
        "ls",
        "grep",
        "head",
        "stat",
        "uname",
        "readlink",
        "wc",
        "df",
        "true",
    }
)
_UNSAFE_SHELL = re.compile(r"[>;&`|]|\$\(|<\(|\b(?:rm|mv|dd|reboot|poweroff|tee)\b")
_ID = re.compile(r"[a-z0-9][a-z0-9._-]*")
_SERVICE = re.compile(r"[a-z][a-z0-9_]*")
DEFAULT_SSH_SERVICE: Final = "ssh"
DEFAULT_SSH_PORT: Final = 22


class ProfileError(ThreeDError):
    """A firmware profile file breaks the schema. Exit 2 (a data bug, not a gate verdict)."""

    exit_code = 2


@dataclass(frozen=True)
class Matcher:
    """One model matcher: case-insensitive substring (`contains`) or regex (`regex`, fullmatch
    is NOT implied — anchor it yourself)."""

    contains: str | None = None
    regex: str | None = None

    def matches(self, model: str) -> bool:
        low = model.casefold()
        if self.contains is not None:
            return self.contains.casefold() in low
        return (
            self.regex is not None
            and re.search(self.regex, model, re.IGNORECASE) is not None
        )

    def describe(self) -> str:
        if self.contains is not None:
            return f'contains "{self.contains}"'
        return f"regex /{self.regex}/"


@dataclass(frozen=True)
class Probe:
    """One read-only check. Only the fields of its `kind` are meaningful (see PROBE_KINDS)."""

    kind: str
    id: str
    title: str
    negate: bool = False
    service: str = ""
    path: str = ""
    expect_status: tuple[int, ...] = (200,)
    json_path: str = ""
    equals: str | None = None
    regex: str | None = None
    command: str = ""
    value: str = ""
    prompt: str = ""
    timeout: float = 5.0

    @property
    def uses_ssh(self) -> bool:
        return self.kind in SSH_PROBE_KINDS


@dataclass(frozen=True)
class Detection:
    mode: str  # "all" | "any"
    probes: tuple[Probe, ...]  # `all` with no probes = baseline: always matches


@dataclass(frozen=True)
class State:
    id: str
    title: str
    detection: (
        Detection | None
    )  # None: not machine-detectable (reachable via --from only)
    requires: tuple[
        str, ...
    ] = ()  # states that must have matched before this one is probed
    notes: str = ""
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class Upload:
    service: str
    path: str
    field: str
    accept_suffix: str
    min_bytes: int
    known_sha256: tuple[str, ...]
    expect_status: tuple[int, ...]
    success_regex: str | None


@dataclass(frozen=True)
class HttpPost:
    service: str
    path: str
    expect_status: tuple[int, ...]
    success_regex: str | None


@dataclass(frozen=True)
class Verify:
    probes: tuple[Probe, ...]
    wait_s: float = 0.0  # poll up to this long (reboots take minutes)
    interval_s: float = 5.0


@dataclass(frozen=True)
class Rollback:
    instruction: str
    to: str | None = None  # state the rollback returns to, when it is one


@dataclass(frozen=True)
class Step:
    id: str
    title: str
    kind: str
    automatable: bool
    risk: str
    verify: Verify
    preconditions: tuple[Probe, ...] = ()
    guard_refs: tuple[str, ...] = ()
    skip_if: tuple[Probe, ...] = ()
    backup: tuple[str, ...] = ()
    command: str = ""
    instruction: str = ""
    upload: Upload | None = None
    post: HttpPost | None = None
    rollback: Rollback | None = None
    sources: tuple[str, ...] = ()
    notes: str = ""
    timeout_s: float = 300.0


@dataclass(frozen=True)
class Transition:
    id: str
    from_state: str
    to_state: str
    title: str
    steps: tuple[Step, ...]
    optional_steps: tuple[Step, ...] = ()
    sources: tuple[str, ...] = ()

    @property
    def maintenance(self) -> bool:
        return self.from_state == self.to_state


@dataclass(frozen=True)
class BackupItem:
    id: str
    description: str
    via: str
    service: str = ""
    path: str = ""
    base: str = ""  # ssh_tar: directory the `paths` are relative to
    paths: tuple[str, ...] = ()
    optional: bool = False
    states: tuple[str, ...] = ()  # taken only in these states; () = every state
    expect_regex: str | None = None
    expect_tar_members: tuple[str, ...] = ()
    instruction: str = ""
    timeout_s: float = 120.0


@dataclass(frozen=True)
class Guard:
    id: str
    kind: str
    reason: str
    sources: tuple[str, ...]
    from_states: tuple[str, ...] = ()
    to_states: tuple[str, ...] = ()
    options: tuple[str, ...] = ()
    step: str = ""
    pattern: str = ""
    requires_step: str = ""
    probes: tuple[Probe, ...] = ()
    risks: tuple[str, ...] = ("medium", "high")
    scope: str = (
        "always"  # always | referenced (only where a step lists it in preconditions)
    )


@dataclass(frozen=True)
class SshSettings:
    user: str
    credentials: str  # where the secret comes from; documentation only, 3d stores none
    connect_timeout_s: float = 30.0


@dataclass(frozen=True)
class Capabilities:
    klipper_possible: bool
    notes: str


@dataclass(frozen=True)
class Profile:
    id: str
    vendor: str
    family: str
    models: tuple[Matcher, ...]
    sources: tuple[str, ...]
    capabilities: Capabilities
    states: tuple[State, ...]
    transitions: tuple[Transition, ...]
    services: Mapping[str, int] = field(default_factory=dict)
    adapter_services: Mapping[str, str] = field(default_factory=dict)
    ssh: SshSettings | None = None
    # Holds only when the printer is READABLE and idle; anything else counts as busy.
    idle_probe: Probe | None = None
    backups: tuple[BackupItem, ...] = ()
    guards: tuple[Guard, ...] = ()
    notes: str = ""
    path: Path | None = None

    def state(self, state_id: str) -> State | None:
        return next((s for s in self.states if s.id == state_id), None)

    def state_ids(self) -> list[str]:
        return [s.id for s in self.states]

    def matches(self, model: str) -> bool:
        return any(m.matches(model) for m in self.models)

    def backup_item(self, item_id: str) -> BackupItem | None:
        return next((b for b in self.backups if b.id == item_id), None)

    def all_steps(self) -> list[tuple[Transition, Step, bool]]:
        """(transition, step, optional?) for every step of every transition."""
        out: list[tuple[Transition, Step, bool]] = []
        for t in self.transitions:
            out += [(t, s, False) for s in t.steps]
            out += [(t, s, True) for s in t.optional_steps]
        return out

    def step(self, step_id: str) -> tuple[Transition, Step, bool] | None:
        return next((e for e in self.all_steps() if e[1].id == step_id), None)

    def guard(self, guard_id: str) -> Guard | None:
        return next((g for g in self.guards if g.id == guard_id), None)


# ---- validation ------------------------------------------------------------------------------


class _Problems:
    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, where: str, message: str) -> None:
        self.items.append(f"{where}: {message}")


def is_url(value: object) -> bool:
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    parts = urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.netloc) and " " not in value


def _mapping(raw: object, where: str, bad: _Problems) -> dict[str, object] | None:
    if not isinstance(raw, dict) or not all(isinstance(k, str) for k in raw):
        bad.add(where, f"must be a mapping, got {type(raw).__name__}")
        return None
    return raw


def _unknown(
    raw: dict[str, object], accepted: tuple[str, ...], where: str, bad: _Problems
) -> None:
    for key in raw:
        if key not in accepted:
            bad.add(f"{where}.{key}", f"unknown key; accepted: {', '.join(accepted)}")


def _str(
    raw: dict[str, object],
    key: str,
    where: str,
    bad: _Problems,
    *,
    required: bool = True,
) -> str:
    value = raw.get(key)
    if value is None:
        if required:
            bad.add(f"{where}.{key}", "is required")
        return ""
    if not isinstance(value, str) or not value.strip():
        bad.add(f"{where}.{key}", "must be a non-empty string")
        return ""
    return value


def _bool(
    raw: dict[str, object], key: str, where: str, bad: _Problems, *, default: bool
) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        bad.add(f"{where}.{key}", f"must be true or false, got {value!r}")
        return default
    return value


def _number(
    raw: dict[str, object], key: str, where: str, bad: _Problems, default: float
) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        bad.add(f"{where}.{key}", f"must be a non-negative number, got {value!r}")
        return default
    return float(value)


def _list(
    raw: dict[str, object],
    key: str,
    where: str,
    bad: _Problems,
    *,
    required: bool = False,
) -> list[object]:
    value = raw.get(key)
    if value is None:
        if required:
            bad.add(f"{where}.{key}", "is required")
        return []
    if not isinstance(value, list):
        bad.add(f"{where}.{key}", f"must be a list, got {type(value).__name__}")
        return []
    return value


def _urls(
    raw: dict[str, object], key: str, where: str, bad: _Problems, *, required: bool
) -> tuple[str, ...]:
    out: list[str] = []
    for i, item in enumerate(_list(raw, key, where, bad, required=required)):
        if is_url(item):
            out.append(str(item))
        else:
            bad.add(f"{where}.{key}[{i}]", f"must be an http(s) URL, got {item!r}")
    if required and not out and raw.get(key) is not None:
        bad.add(f"{where}.{key}", "needs at least one source URL")
    return tuple(out)


def _strings(
    raw: dict[str, object], key: str, where: str, bad: _Problems
) -> tuple[str, ...]:
    out: list[str] = []
    for i, item in enumerate(_list(raw, key, where, bad)):
        if isinstance(item, str) and item.strip():
            out.append(item)
        else:
            bad.add(f"{where}.{key}[{i}]", f"must be a non-empty string, got {item!r}")
    return tuple(out)


def _regex(value: str | None, where: str, bad: _Problems) -> None:
    if value is None:
        return
    try:
        re.compile(value)
    except re.error as exc:
        bad.add(where, f"is not a valid regex: {exc}")


def _statuses(raw: dict[str, object], where: str, bad: _Problems) -> tuple[int, ...]:
    value = raw.get("expect_status", 200)
    items = value if isinstance(value, list) else [value]
    out: list[int] = []
    for item in items:
        if (
            isinstance(item, bool)
            or not isinstance(item, int)
            or not 100 <= item <= 599
        ):
            bad.add(
                f"{where}.expect_status",
                f"must be an HTTP status or a list of them, got {value!r}",
            )
            return (200,)
        out.append(item)
    return tuple(out) or (200,)


_PROBE_KEYS = (
    "kind",
    "id",
    "title",
    "negate",
    "service",
    "path",
    "expect_status",
    "json_path",
    "equals",
    "regex",
    "command",
    "value",
    "prompt",
    "timeout",
)


def read_only_command(command: str) -> str | None:
    """Why `command` is not a read-only probe command, or None when it is."""
    words = command.split()
    if not words:
        return "is empty"
    if words[0] not in READ_ONLY_COMMANDS:
        return (
            f"starts with {words[0]!r}; a probe may only start with one of: "
            + ", ".join(sorted(READ_ONLY_COMMANDS))
        )
    bad = _UNSAFE_SHELL.search(command)
    if bad:
        return f"contains {bad.group(0)!r}: a probe must not redirect, chain, pipe, substitute or modify"
    return None


def _probe(
    raw: object,
    where: str,
    bad: _Problems,
    services: Mapping[str, int],
    ssh: bool,
    default_id: str,
) -> Probe | None:
    data = _mapping(raw, where, bad)
    if data is None:
        return None
    _unknown(data, _PROBE_KEYS, where, bad)
    kind = _str(data, "kind", where, bad)
    if kind and kind not in PROBE_KINDS:
        bad.add(
            f"{where}.kind",
            f"unknown probe kind {kind!r}; accepted: {', '.join(PROBE_KINDS)}",
        )
        return None
    if not kind:
        return None
    probe_id = str(data.get("id") or default_id)
    title = str(data.get("title") or probe_id)
    service = str(data.get("service") or "")
    path = str(data.get("path") or "")
    command = str(data.get("command") or "")
    regex = data.get("regex")
    if regex is not None and not isinstance(regex, str):
        bad.add(f"{where}.regex", "must be a string")
        regex = None
    equals = data.get("equals")
    if equals is not None and not isinstance(equals, (str, int, float, bool)):
        bad.add(f"{where}.equals", "must be a scalar")
        equals = None
    _regex(regex, f"{where}.regex", bad)
    if kind in ("http_get", "tcp_open", "tcp_banner") and service not in services:
        bad.add(
            f"{where}.service",
            f"must name a profile service ({', '.join(sorted(services)) or 'none declared'}), got {service!r}",
        )
    if kind == "http_get" and not path.startswith("/"):
        bad.add(f"{where}.path", "must start with /")
    if kind == "ssh_file_exists" and not path.startswith("/"):
        bad.add(f"{where}.path", "must be an absolute path")
    if kind == "ssh_command":
        why = read_only_command(command)
        if why:
            bad.add(f"{where}.command", why)
    if kind == "tcp_banner" and regex is None:
        bad.add(f"{where}.regex", "is required for tcp_banner")
    if kind == "adapter_kind" and equals is None:
        bad.add(
            f"{where}.equals",
            "is required for adapter_kind (the adapter name to compare with)",
        )
    if kind == "confirm" and not str(data.get("prompt") or ""):
        bad.add(
            f"{where}.prompt", "is required for confirm (the question put to the user)"
        )
    if kind in SSH_PROBE_KINDS and not ssh and kind != "tcp_banner":
        bad.add(f"{where}.kind", f"{kind} needs an `ssh:` block in the profile")
    return Probe(
        kind=kind,
        id=probe_id,
        title=title,
        negate=_bool(data, "negate", where, bad, default=False),
        service=service,
        path=path,
        expect_status=_statuses(data, where, bad),
        json_path=str(data.get("json_path") or ""),
        equals=None
        if equals is None
        else str(equals).lower()
        if isinstance(equals, bool)
        else str(equals),
        regex=regex,
        command=command,
        value=str(data.get("value") or ""),
        prompt=str(data.get("prompt") or ""),
        timeout=_number(data, "timeout", where, bad, 5.0),
    )


def _probes(
    raw: object,
    where: str,
    bad: _Problems,
    services: Mapping[str, int],
    ssh: bool,
    prefix: str,
) -> tuple[Probe, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        bad.add(where, f"must be a list of probes, got {type(raw).__name__}")
        return ()
    out = [
        _probe(item, f"{where}[{i}]", bad, services, ssh, f"{prefix}.{i + 1}")
        for i, item in enumerate(raw)
    ]
    return tuple(p for p in out if p is not None)


def _detection(
    raw: object,
    where: str,
    bad: _Problems,
    services: Mapping[str, int],
    ssh: bool,
    state_id: str,
) -> Detection | None:
    if raw is None:
        return None
    data = _mapping(raw, where, bad)
    if data is None:
        return None
    _unknown(data, ("all", "any"), where, bad)
    if "all" in data and "any" in data:
        bad.add(where, "use `all` or `any`, not both")
        return None
    mode = "any" if "any" in data else "all"
    probes = _probes(data.get(mode), f"{where}.{mode}", bad, services, ssh, state_id)
    if mode == "any" and not probes:
        bad.add(f"{where}.any", "needs at least one probe")
    for i, p in enumerate(probes):
        if p.kind == "confirm":
            bad.add(
                f"{where}.{mode}[{i}].kind",
                "confirm needs a human; a state cannot be detected with it",
            )
    return Detection(mode, probes)


def _verify(
    raw: object,
    where: str,
    bad: _Problems,
    services: Mapping[str, int],
    ssh: bool,
    step_id: str,
) -> Verify:
    wait, interval = 0.0, 5.0
    if isinstance(raw, dict):
        _unknown(raw, ("probes", "wait_s", "interval_s"), where, bad)
        wait = _number(raw, "wait_s", where, bad, 0.0)
        interval = max(0.01, _number(raw, "interval_s", where, bad, 5.0))
        probes = _probes(
            raw.get("probes"),
            f"{where}.probes",
            bad,
            services,
            ssh,
            f"{step_id}.verify",
        )
    else:
        probes = _probes(raw, where, bad, services, ssh, f"{step_id}.verify")
    if not probes:
        bad.add(where, "is required: every step needs at least one verification probe")
    return Verify(probes, wait, interval)


def _upload(
    raw: object, where: str, bad: _Problems, services: Mapping[str, int]
) -> Upload | None:
    data = _mapping(raw, where, bad)
    if data is None:
        return None
    _unknown(
        data,
        (
            "service",
            "path",
            "field",
            "accept_suffix",
            "min_bytes",
            "known_sha256",
            "expect_status",
            "success_regex",
        ),
        where,
        bad,
    )
    service = _str(data, "service", where, bad)
    if service and service not in services:
        bad.add(f"{where}.service", f"must name a profile service, got {service!r}")
    path = _str(data, "path", where, bad)
    if path and not path.startswith("/"):
        bad.add(f"{where}.path", "must start with /")
    success = data.get("success_regex")
    _regex(success if isinstance(success, str) else None, f"{where}.success_regex", bad)
    shas = _strings(data, "known_sha256", where, bad)
    for i, sha in enumerate(shas):
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            bad.add(f"{where}.known_sha256[{i}]", "must be 64 lowercase hex digits")
    if str(data.get("field") or "file") != "file":
        bad.add(
            f"{where}.field",
            "must be `file`: the shared uploader (printer_ops.base.http_upload) sends exactly that part name",
        )
    return Upload(
        service,
        path,
        str(data.get("field") or "file"),
        str(data.get("accept_suffix") or ""),
        int(_number(data, "min_bytes", where, bad, 0)),
        shas,
        _statuses(data, where, bad),
        success if isinstance(success, str) else None,
    )


def _post(
    raw: object, where: str, bad: _Problems, services: Mapping[str, int]
) -> HttpPost | None:
    data = _mapping(raw, where, bad)
    if data is None:
        return None
    _unknown(data, ("service", "path", "expect_status", "success_regex"), where, bad)
    service = _str(data, "service", where, bad)
    if service and service not in services:
        bad.add(f"{where}.service", f"must name a profile service, got {service!r}")
    path = _str(data, "path", where, bad)
    if path and not path.startswith("/"):
        bad.add(f"{where}.path", "must start with /")
    success = data.get("success_regex")
    _regex(success if isinstance(success, str) else None, f"{where}.success_regex", bad)
    return HttpPost(
        service,
        path,
        _statuses(data, where, bad),
        success if isinstance(success, str) else None,
    )


_STEP_KEYS = (
    "id",
    "title",
    "kind",
    "automatable",
    "risk",
    "preconditions",
    "skip_if",
    "backup",
    "command",
    "instruction",
    "upload",
    "post",
    "verify",
    "rollback",
    "sources",
    "notes",
    "timeout_s",
)


def _step(
    raw: object,
    where: str,
    bad: _Problems,
    services: Mapping[str, int],
    ssh: bool,
    seen: set[str],
) -> Step | None:
    data = _mapping(raw, where, bad)
    if data is None:
        return None
    _unknown(data, _STEP_KEYS, where, bad)
    step_id = _str(data, "id", where, bad)
    if step_id and not _ID.fullmatch(step_id):
        bad.add(f"{where}.id", f"{step_id!r} must be lowercase letters, digits, . _ -")
    if step_id in seen:
        bad.add(f"{where}.id", f"duplicate step id {step_id!r}")
    seen.add(step_id)
    title = _str(data, "title", where, bad)
    kind = _str(data, "kind", where, bad)
    if kind and kind not in STEP_KINDS:
        bad.add(
            f"{where}.kind",
            f"unknown step kind {kind!r}; accepted: {', '.join(STEP_KINDS)}",
        )
    risk = str(data.get("risk") or "")
    if risk not in RISKS:
        bad.add(
            f"{where}.risk",
            f"must be one of {', '.join(RISKS)}, got {data.get('risk')!r}",
        )
    automatable = _bool(data, "automatable", where, bad, default=False)
    command = str(data.get("command") or "")
    instruction = str(data.get("instruction") or "")
    if kind == "manual":
        if automatable:
            bad.add(f"{where}.automatable", "a manual step cannot be automatable")
        if not instruction.strip():
            bad.add(f"{where}.instruction", "is required for a manual step")
    if kind in ("ssh_script", "ssh_command"):
        if not command.strip():
            bad.add(f"{where}.command", f"is required for {kind}")
        if not ssh:
            bad.add(f"{where}.kind", f"{kind} needs an `ssh:` block in the profile")
    if kind == "ssh_command" and "\n" in command.strip():
        bad.add(
            f"{where}.command",
            "ssh_command is one line; use ssh_script for a multi-line script",
        )
    upload = (
        _upload(data.get("upload"), f"{where}.upload", bad, services)
        if kind == "http_upload"
        else None
    )
    if kind == "http_upload" and data.get("upload") is None:
        bad.add(f"{where}.upload", "is required for http_upload")
    post = (
        _post(data.get("post"), f"{where}.post", bad, services)
        if kind == "http_post"
        else None
    )
    if kind == "http_post" and data.get("post") is None:
        bad.add(f"{where}.post", "is required for http_post")
    verify = _verify(data.get("verify"), f"{where}.verify", bad, services, ssh, step_id)
    if automatable and not verify.probes:
        bad.add(
            f"{where}.automatable",
            "true needs a verify probe: 3d never runs a step it cannot check",
        )
    pre_probes: list[Probe] = []
    refs: list[str] = []
    for i, entry in enumerate(_list(data, "preconditions", where, bad)):
        if (
            isinstance(entry, dict)
            and set(entry) == {"guard"}
            and isinstance(entry["guard"], str)
        ):
            refs.append(entry["guard"])
        else:
            p = _probe(
                entry,
                f"{where}.preconditions[{i}]",
                bad,
                services,
                ssh,
                f"{step_id}.pre.{i + 1}",
            )
            if p is not None:
                pre_probes.append(p)
    skip_if = _probes(
        data.get("skip_if"), f"{where}.skip_if", bad, services, ssh, f"{step_id}.skip"
    )
    rollback: Rollback | None = None
    raw_rb = data.get("rollback")
    if raw_rb is not None:
        rb = _mapping(raw_rb, f"{where}.rollback", bad)
        if rb is not None:
            _unknown(rb, ("instruction", "to"), f"{where}.rollback", bad)
            to = rb.get("to")
            rollback = Rollback(
                _str(rb, "instruction", f"{where}.rollback", bad),
                str(to) if to else None,
            )
    if risk in ("medium", "high") and rollback is None:
        bad.add(f"{where}.rollback", f"is required for a {risk}-risk step")
    return Step(
        id=step_id,
        title=title,
        kind=kind,
        automatable=automatable,
        risk=risk,
        verify=verify,
        preconditions=tuple(pre_probes),
        guard_refs=tuple(refs),
        skip_if=skip_if,
        backup=_strings(data, "backup", where, bad),
        command=command,
        instruction=instruction,
        upload=upload,
        post=post,
        rollback=rollback,
        sources=_urls(data, "sources", where, bad, required=False),
        notes=str(data.get("notes") or ""),
        timeout_s=_number(data, "timeout_s", where, bad, 300.0),
    )


def _backup(
    raw: object, where: str, bad: _Problems, services: Mapping[str, int], seen: set[str]
) -> BackupItem | None:
    data = _mapping(raw, where, bad)
    if data is None:
        return None
    _unknown(
        data,
        (
            "id",
            "description",
            "via",
            "service",
            "path",
            "base",
            "paths",
            "optional",
            "states",
            "expect_regex",
            "expect_tar_members",
            "instruction",
            "timeout_s",
        ),
        where,
        bad,
    )
    item_id = _str(data, "id", where, bad)
    if item_id in seen:
        bad.add(f"{where}.id", f"duplicate backup id {item_id!r}")
    seen.add(item_id)
    via = _str(data, "via", where, bad)
    if via and via not in BACKUP_VIA:
        bad.add(
            f"{where}.via", f"unknown via {via!r}; accepted: {', '.join(BACKUP_VIA)}"
        )
    service = str(data.get("service") or "")
    path = str(data.get("path") or "")
    paths = _strings(data, "paths", where, bad)
    if via == "http_get":
        if service not in services:
            bad.add(f"{where}.service", f"must name a profile service, got {service!r}")
        if not path.startswith("/"):
            bad.add(f"{where}.path", "must start with /")
    if via == "ssh_file" and not path.startswith("/"):
        bad.add(f"{where}.path", "must be an absolute remote path")
    if via == "ssh_tar":
        if not str(data.get("base") or "").startswith("/"):
            bad.add(f"{where}.base", "must be an absolute remote directory")
        if not paths:
            bad.add(f"{where}.paths", "needs at least one path relative to base")
    if via == "manual" and not str(data.get("instruction") or "").strip():
        bad.add(f"{where}.instruction", "is required for a manual backup item")
    expect = data.get("expect_regex")
    _regex(expect if isinstance(expect, str) else None, f"{where}.expect_regex", bad)
    return BackupItem(
        id=item_id,
        description=_str(data, "description", where, bad),
        via=via,
        service=service,
        path=path,
        base=str(data.get("base") or ""),
        paths=paths,
        optional=_bool(data, "optional", where, bad, default=False),
        states=_strings(data, "states", where, bad),
        expect_regex=expect if isinstance(expect, str) else None,
        expect_tar_members=_strings(data, "expect_tar_members", where, bad),
        instruction=str(data.get("instruction") or ""),
        timeout_s=_number(data, "timeout_s", where, bad, 120.0),
    )


def _guard(
    raw: object,
    where: str,
    bad: _Problems,
    services: Mapping[str, int],
    ssh: bool,
    seen: set[str],
) -> Guard | None:
    data = _mapping(raw, where, bad)
    if data is None:
        return None
    _unknown(
        data,
        (
            "id",
            "kind",
            "applies_when",
            "reason",
            "sources",
            "options",
            "step",
            "pattern",
            "requires_step",
            "probe",
            "risks",
            "scope",
        ),
        where,
        bad,
    )
    guard_id = _str(data, "id", where, bad)
    if guard_id in seen:
        bad.add(f"{where}.id", f"duplicate guard id {guard_id!r}")
    seen.add(guard_id)
    kind = _str(data, "kind", where, bad)
    if kind and kind not in GUARD_KINDS:
        bad.add(
            f"{where}.kind",
            f"unknown guard kind {kind!r}; accepted: {', '.join(GUARD_KINDS)}",
        )
    from_states: tuple[str, ...] = ()
    to_states: tuple[str, ...] = ()
    when = data.get("applies_when")
    if when is not None:
        w = _mapping(when, f"{where}.applies_when", bad)
        if w is not None:
            _unknown(w, ("from", "to"), f"{where}.applies_when", bad)
            from_states = _strings(w, "from", f"{where}.applies_when", bad)
            to_states = _strings(w, "to", f"{where}.applies_when", bad)
    options = _strings(data, "options", where, bad)
    pattern = str(data.get("pattern") or "")
    _regex(pattern or None, f"{where}.pattern", bad)
    step = str(data.get("step") or "")
    requires_step = str(data.get("requires_step") or "")
    probes: tuple[Probe, ...] = ()
    raw_probe = data.get("probe")
    if raw_probe is not None:
        # One mapping, or a list that must ALL hold (e.g. "the printer answers" before "the
        # option is off": a refused connection must not read as "off").
        entries = raw_probe if isinstance(raw_probe, list) else [raw_probe]
        probes = tuple(
            p
            for i, e in enumerate(entries)
            if (
                p := _probe(
                    e, f"{where}.probe[{i}]", bad, services, ssh, f"{guard_id}.probe{i}"
                )
            )
            is not None
        )
    if kind == "forbidden_option" and not options:
        bad.add(
            f"{where}.options", "is required for forbidden_option (the refused ids)"
        )
    if kind == "forbidden_command_pattern" and not pattern:
        bad.add(f"{where}.pattern", "is required for forbidden_command_pattern")
    if kind == "step_requires_command" and (not step or not pattern):
        bad.add(where, "step_requires_command needs `step` and `pattern`")
    if kind == "step_requires_step" and (not step or not requires_step):
        bad.add(where, "step_requires_step needs `step` and `requires_step`")
    if kind == "invariant_probe" and not probes:
        bad.add(f"{where}.probe", "is required for invariant_probe")
    risks = _strings(data, "risks", where, bad) or ("medium", "high")
    for r in risks:
        if r not in RISKS:
            bad.add(
                f"{where}.risks", f"unknown risk {r!r}; accepted: {', '.join(RISKS)}"
            )
    scope = str(data.get("scope") or "always")
    if scope not in ("always", "referenced"):
        bad.add(f"{where}.scope", "must be always or referenced")
    return Guard(
        id=guard_id,
        kind=kind,
        reason=_str(data, "reason", where, bad),
        sources=_urls(data, "sources", where, bad, required=True),
        from_states=from_states,
        to_states=to_states,
        options=options,
        step=step,
        pattern=pattern,
        requires_step=requires_step,
        probes=probes,
        risks=risks,
        scope=scope,
    )


_TOP_KEYS = (
    "id",
    "vendor",
    "family",
    "models",
    "sources",
    "capabilities",
    "services",
    "adapter_services",
    "ssh",
    "idle_probe",
    "backups",
    "guards",
    "states",
    "transitions",
    "notes",
)


def parse_profile(data: object, where: str) -> Profile:
    """Validate one parsed profile document; raises ProfileError listing EVERY problem."""
    bad = _Problems()
    profile = _parse(data, where, bad)
    if bad.items or profile is None:
        shown = "\n  ".join(bad.items[:40])
        more = f"\n  ... and {len(bad.items) - 40} more" if len(bad.items) > 40 else ""
        raise ProfileError(
            f"invalid firmware profile {where}:\n  {shown}{more}",
            command="printers fw",
            remediation=[
                "Fix the file; the schema is documented in docs/printers/firmware-profiles.md."
            ],
        )
    return profile


def _parse(data: object, where: str, bad: _Problems) -> Profile | None:
    top = _mapping(data, where, bad)
    if top is None:
        return None
    _unknown(top, _TOP_KEYS, where, bad)
    profile_id = _str(top, "id", where, bad)
    if profile_id and not _ID.fullmatch(profile_id):
        bad.add(
            f"{where}.id", f"{profile_id!r} must be lowercase letters, digits, . _ -"
        )
    models: list[Matcher] = []
    for i, raw in enumerate(_list(top, "models", where, bad, required=True)):
        m = _mapping(raw, f"{where}.models[{i}]", bad)
        if m is None:
            continue
        _unknown(m, ("contains", "regex"), f"{where}.models[{i}]", bad)
        if ("contains" in m) == ("regex" in m):
            bad.add(
                f"{where}.models[{i}]", "needs exactly one of `contains` or `regex`"
            )
            continue
        text = m.get("contains", m.get("regex"))
        if not isinstance(text, str) or not text.strip():
            bad.add(f"{where}.models[{i}]", "matcher must be a non-empty string")
            continue
        if "regex" in m:
            _regex(text, f"{where}.models[{i}].regex", bad)
            models.append(Matcher(regex=text))
        else:
            models.append(Matcher(contains=text))
    if top.get("models") is not None and not models and not bad.items:
        bad.add(f"{where}.models", "needs at least one matcher")
    services: dict[str, int] = {}
    raw_services = top.get("services")
    if raw_services is not None:
        sv = _mapping(raw_services, f"{where}.services", bad)
        for name, port in (sv or {}).items():
            if not _SERVICE.fullmatch(name):
                bad.add(
                    f"{where}.services.{name}",
                    "service names are lowercase letters, digits, _",
                )
            elif (
                isinstance(port, bool)
                or not isinstance(port, int)
                or not 0 < port < 65536
            ):
                bad.add(
                    f"{where}.services.{name}", f"must be a port 1-65535, got {port!r}"
                )
            else:
                services[name] = port
    ssh: SshSettings | None = None
    if top.get("ssh") is not None:
        s = _mapping(top["ssh"], f"{where}.ssh", bad)
        if s is not None:
            _unknown(
                s, ("user", "credentials", "connect_timeout_s"), f"{where}.ssh", bad
            )
            ssh = SshSettings(
                _str(s, "user", f"{where}.ssh", bad),
                _str(s, "credentials", f"{where}.ssh", bad),
                _number(s, "connect_timeout_s", f"{where}.ssh", bad, 30.0),
            )
            services.setdefault(DEFAULT_SSH_SERVICE, DEFAULT_SSH_PORT)
    has_ssh = ssh is not None
    adapter_services: dict[str, str] = {}
    raw_adapters = top.get("adapter_services")
    if raw_adapters is not None:
        for adapter, svc in (
            _mapping(raw_adapters, f"{where}.adapter_services", bad) or {}
        ).items():
            if not isinstance(svc, str) or svc not in services:
                bad.add(
                    f"{where}.adapter_services.{adapter}",
                    f"must name a profile service ({', '.join(sorted(services)) or 'none declared'}), got {svc!r}",
                )
            else:
                adapter_services[adapter] = svc
    caps_raw = (
        _mapping(top.get("capabilities"), f"{where}.capabilities", bad)
        if top.get("capabilities") is not None
        else None
    )
    if top.get("capabilities") is None:
        bad.add(f"{where}.capabilities", "is required")
    capabilities = Capabilities(False, "")
    if caps_raw is not None:
        _unknown(caps_raw, ("klipper_possible", "notes"), f"{where}.capabilities", bad)
        if not isinstance(caps_raw.get("klipper_possible"), bool):
            bad.add(
                f"{where}.capabilities.klipper_possible",
                "is required and must be true or false",
            )
        capabilities = Capabilities(
            caps_raw.get("klipper_possible") is True,
            _str(caps_raw, "notes", f"{where}.capabilities", bad),
        )
    idle_probe = (
        _probe(top["idle_probe"], f"{where}.idle_probe", bad, services, has_ssh, "idle")
        if top.get("idle_probe") is not None
        else None
    )

    states: list[State] = []
    state_ids: set[str] = set()
    for i, raw in enumerate(_list(top, "states", where, bad, required=True)):
        w = f"{where}.states[{i}]"
        s = _mapping(raw, w, bad)
        if s is None:
            continue
        _unknown(
            s, ("id", "title", "detection", "requires", "notes", "sources"), w, bad
        )
        sid = _str(s, "id", w, bad)
        if sid and not _ID.fullmatch(sid):
            bad.add(f"{w}.id", f"{sid!r} must be lowercase letters, digits, . _ -")
        if sid in state_ids:
            bad.add(f"{w}.id", f"duplicate state id {sid!r}")
        state_ids.add(sid)
        states.append(
            State(
                sid,
                _str(s, "title", w, bad),
                _detection(
                    s.get("detection"), f"{w}.detection", bad, services, has_ssh, sid
                ),
                _strings(s, "requires", w, bad),
                str(s.get("notes") or ""),
                _urls(s, "sources", w, bad, required=False),
            )
        )
    for i, st in enumerate(states):
        for req in st.requires:
            if req not in state_ids:
                bad.add(
                    f"{where}.states[{i}].requires",
                    f"unknown state {req!r}; states: {', '.join(sorted(state_ids))}",
                )

    step_ids: set[str] = set()
    transitions: list[Transition] = []
    for i, raw in enumerate(_list(top, "transitions", where, bad, required=True)):
        w = f"{where}.transitions[{i}]"
        t = _mapping(raw, w, bad)
        if t is None:
            continue
        _unknown(
            t,
            ("id", "from", "to", "title", "steps", "optional_steps", "sources"),
            w,
            bad,
        )
        tid = _str(t, "id", w, bad)
        src, dst = str(t.get("from") or ""), str(t.get("to") or "")
        for key, val in (("from", src), ("to", dst)):
            if val not in state_ids:
                bad.add(
                    f"{w}.{key}",
                    f"unknown state {val!r}; states: {', '.join(sorted(state_ids))}",
                )
        steps = tuple(
            stp
            for j, raw_step in enumerate(_list(t, "steps", w, bad))
            if (
                stp := _step(
                    raw_step, f"{w}.steps[{j}]", bad, services, has_ssh, step_ids
                )
            )
            is not None
        )
        optional = tuple(
            stp
            for j, raw_step in enumerate(_list(t, "optional_steps", w, bad))
            if (
                stp := _step(
                    raw_step,
                    f"{w}.optional_steps[{j}]",
                    bad,
                    services,
                    has_ssh,
                    step_ids,
                )
            )
            is not None
        )
        if src == dst:
            if steps or not optional:
                bad.add(
                    w,
                    "a maintenance transition (from == to) has no `steps` and at least one `optional_steps`",
                )
        elif not steps:
            bad.add(f"{w}.steps", "needs at least one step")
        transitions.append(
            Transition(
                tid,
                src,
                dst,
                _str(t, "title", w, bad),
                steps,
                optional,
                _urls(t, "sources", w, bad, required=False),
            )
        )
    ids = [t.id for t in transitions]
    for tid in {x for x in ids if ids.count(x) > 1}:
        bad.add(f"{where}.transitions", f"duplicate transition id {tid!r}")

    backups: list[BackupItem] = []
    backup_ids: set[str] = set()
    for i, raw in enumerate(_list(top, "backups", where, bad)):
        item = _backup(raw, f"{where}.backups[{i}]", bad, services, backup_ids)
        if item is not None:
            backups.append(item)
            for sid in item.states:
                if sid not in state_ids:
                    bad.add(f"{where}.backups[{i}].states", f"unknown state {sid!r}")
    guards: list[Guard] = []
    guard_ids: set[str] = set()
    for i, raw in enumerate(_list(top, "guards", where, bad)):
        g = _guard(raw, f"{where}.guards[{i}]", bad, services, has_ssh, guard_ids)
        if g is not None:
            guards.append(g)

    profile = Profile(
        id=profile_id,
        vendor=_str(top, "vendor", where, bad),
        family=_str(top, "family", where, bad),
        models=tuple(models),
        sources=_urls(top, "sources", where, bad, required=True),
        capabilities=capabilities,
        states=tuple(states),
        transitions=tuple(transitions),
        services=services,
        adapter_services=adapter_services,
        ssh=ssh,
        idle_probe=idle_probe,
        backups=tuple(backups),
        guards=tuple(guards),
        notes=str(top.get("notes") or ""),
    )
    _cross_check(profile, where, bad)
    return profile


def _cross_check(profile: Profile, where: str, bad: _Problems) -> None:
    """References between parts of the profile must resolve (a guard on a step that does not
    exist, a step needing a backup nobody takes, a rollback to a state that is not there)."""
    state_ids = set(profile.state_ids())
    by_step = {s.id: s for _, s, _ in profile.all_steps()}
    for _t, step, _opt in profile.all_steps():
        w = f"{where}.step {step.id!r}"
        for b in step.backup:
            item = profile.backup_item(b)
            if item is None:
                bad.add(
                    f"{w}.backup",
                    f"unknown backup id {b!r}; defined: {', '.join(x.id for x in profile.backups) or 'none'}",
                )
            elif item.via == "manual":
                bad.add(
                    f"{w}.backup",
                    f"{b!r} is a manual backup: a step may only require backups 3d can take and check",
                )
        if step.rollback and step.rollback.to and step.rollback.to not in state_ids:
            bad.add(f"{w}.rollback.to", f"unknown state {step.rollback.to!r}")
        for ref in step.guard_refs:
            g = profile.guard(ref)
            if g is None:
                bad.add(f"{w}.preconditions", f"unknown guard {ref!r}")
            elif g.kind not in RUNTIME_GUARD_KINDS:
                bad.add(
                    f"{w}.preconditions",
                    f"guard {ref!r} is a {g.kind} guard; only {', '.join(sorted(RUNTIME_GUARD_KINDS))} can be a precondition",
                )
    for g in profile.guards:
        w = f"{where}.guard {g.id!r}"
        for sid in (*g.from_states, *g.to_states):
            if sid not in state_ids:
                bad.add(f"{w}.applies_when", f"unknown state {sid!r}")
        for key, step_id in (("step", g.step), ("requires_step", g.requires_step)):
            if step_id and step_id not in by_step:
                bad.add(
                    f"{w}.{key}",
                    f"unknown step {step_id!r}; steps: {', '.join(sorted(by_step))}",
                )
        if g.kind == "step_requires_command" and g.step in by_step:
            text = by_step[g.step].command or by_step[g.step].instruction
            if g.pattern and not re.search(g.pattern, text):
                bad.add(
                    f"{w}.pattern",
                    f"step {g.step!r} does not match /{g.pattern}/: {g.reason}",
                )


# ---- loading ---------------------------------------------------------------------------------


def load_profile_file(path: Path) -> Profile:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - pyyaml is a core dependency
        raise MissingDependency(
            "pyyaml",
            install="uv sync",
            degrades="firmware profiles cannot be read",
            command="printers fw",
        ) from exc
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ProfileError(
            f"{path}: cannot read firmware profile: {exc}", command="printers fw"
        ) from exc
    except yaml.YAMLError as exc:
        raise ProfileError(
            f"{path}: invalid YAML: {exc}", command="printers fw"
        ) from exc
    return replace(parse_profile(data, path.name), path=path)


def load_profiles(directory: Path | None = None) -> dict[str, Profile]:
    """Every `*.yaml` profile of `directory` (default: lib/data/firmware), by id."""
    folder = directory or DATA_DIR
    out: dict[str, Profile] = {}
    for path in sorted(folder.glob("*.yaml")):
        profile = load_profile_file(path)
        if profile.id in out:
            raise ProfileError(
                f"{path.name}: duplicate profile id {profile.id!r} (also {out[profile.id].path})",
                command="printers fw",
            )
        out[profile.id] = profile
    return out


def match_profile(profiles: Mapping[str, Profile], model: str | None) -> Profile | None:
    """The profile whose matchers accept `model`; two different matches are an error (the data is
    ambiguous), no model or no match is None."""
    if not model:
        return None
    found = [p for p in profiles.values() if p.matches(model)]
    if len(found) > 1:
        raise ProfileError(
            f"model {model!r} matches several firmware profiles: {', '.join(p.id for p in found)}",
            command="printers fw",
            remediation=[
                "Narrow the `models` matchers of those profiles so each model matches one."
            ],
        )
    return found[0] if found else None
