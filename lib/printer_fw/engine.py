"""engine.py — status, plan, backup and apply for one printer, with every safety gate.

WHAT: the orchestration behind `3d printers fw`. `status_for` detects a printer's state;
  `plan_for` builds the plan without executing anything; `backup_for` takes the profile's
  read-only backups; `run_apply` is the one function that can change a printer, and only past
  all of these gates (each machine-enforced, each tested with a failing and a passing case):
    1. dry run by default — without `--yes` it prints what WOULD run and sends nothing but reads;
    2. an offline fleet printer is never contacted, a read-only printer is never changed;
    3. the printer's fleet name must be typed (`--confirm NAME` or an interactive prompt);
    4. the plan must be reachable from the detected state, and no profile guard may be violated;
    5. a fresh, intact backup that covers every backup id the plan's steps require must exist;
    6. the printer must be idle (adapter status, else the profile's idle probe; unreadable = busy);
    7. before each step: its preconditions hold, its invariants hold; after it: its verify probe
       holds (polled for reboots) and the invariants still hold — otherwise STOP, exit 1, and
       print the step's rollback. Only `automatable` steps ever run; a manual step is shown, then
       checked by its probe (interactive: after Enter; otherwise the run stops with
       `NEXT: do the step, then re-run the same command` and is resumable: the state is
       re-detected on the next run and satisfied steps are skipped).

WHY: firmware work is the one thing here that can brick a printer. The brief of issue #519 is
  that `apply` cannot execute without `--yes` + a fresh backup + the typed printer name + an idle
  printer, and that nothing printer-specific lives in this code: it only interprets profile data.

ACCESSED VIA: `printers_cli/fw.py`.

INVARIANTS: exit codes — 0 done (or a dry run, or nothing to do), 1 refused-for-safety /
  verification failed / manual step pending, 2 usage (wrong confirm name, missing `--file`),
  127 missing ssh binary (raised by the runner). Reads (probes, backups) are allowed in a dry run;
  writes (ssh scripts, uploads, POSTs) happen only after every gate passed. Events are recorded in
  order and are exactly what `--json` prints.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from errors import GateFailure, InvalidArgument, ThreeDError, UsageError
from printer_fw import guards, render
from printer_fw.backup import (
    DEFAULT_MAX_AGE_S,
    Manifest,
    find_fresh_backup,
    take_backup,
)
from printer_fw.detect import Detection, detect_state
from printer_fw.plan import Plan, PlannedStep, build_plan
from printer_fw.probes import (
    Backends,
    Endpoint,
    ProbeResult,
    evaluate,
    evaluate_group,
    poll,
)
from printer_fw.profiles import Profile, Step
from printer_fw.target import Target

NEXT_HINT = "NEXT: do the step, then re-run the same command"
MAX_OUTPUT = 2000


@dataclass(frozen=True)
class Event:
    kind: str
    message: str
    data: Mapping[str, object] = field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        return {"kind": self.kind, "message": self.message, **self.data}


class EventLog:
    """Records events in order; `out` (when given) also prints each message as it happens."""

    def __init__(self, out: Callable[[str], None] | None = None) -> None:
        self.events: list[Event] = []
        self.out = out

    def emit(self, kind: str, message: str, **data: object) -> None:
        self.events.append(Event(kind, message, data))
        if self.out is not None and message:
            self.out(message)

    def lines(self, lines: list[str]) -> None:
        for ln in lines:
            self.emit("text", ln)


@dataclass(frozen=True)
class IdleVerdict:
    idle: bool
    source: str  # adapter | idle-probe | none
    detail: str


IdleCheck = Callable[[Target, Profile, Endpoint, Backends], IdleVerdict]


def adapter_idle(
    target: Target, profile: Profile, ep: Endpoint, be: Backends
) -> IdleVerdict:
    """Idle according to the registry adapter's status; if that cannot be read, the profile's
    idle probe (it holds only for a readable, idle printer); anything else counts as busy
    (printing, paused, unreadable: never assume the printer is free)."""
    reason = ""
    try:
        from printer_ops import adapter_by_name

        status = adapter_by_name(target.name, command="printers fw").status()
        return IdleVerdict(
            not status.busy, "adapter", f"{status.state} ({status.raw_state})"
        )
    except (ThreeDError, OSError, ValueError) as exc:
        reason = f"adapter status unreadable: {exc}"
    if profile.idle_probe is None:
        return IdleVerdict(False, "none", f"{reason}; the profile has no idle probe")
    result = evaluate(profile.idle_probe, ep, be)
    if result.holds:
        return IdleVerdict(True, "idle-probe", f"idle: {result.detail}")
    return IdleVerdict(
        False, "idle-probe", f"{reason}; not provably idle: {result.detail}"
    )


@dataclass(frozen=True)
class Context:
    profile: Profile
    target: Target
    ep: Endpoint
    be: Backends
    log: EventLog
    idle_check: IdleCheck = adapter_idle
    ask: Callable[[str], str] | None = None  # None: non-interactive


@dataclass(frozen=True)
class StatusEntry:
    printer: str
    model: str | None
    access: str
    profile_id: str | None
    detection: Detection | None
    note: str

    def to_json(self) -> dict[str, object]:
        return {
            "printer": self.printer,
            "model": self.model,
            "access": self.access,
            "profile": self.profile_id,
            "state": self.detection.state if self.detection else None,
            "note": self.note,
            "detection": self.detection.to_json() if self.detection else None,
        }


def no_profile_note(target: Target) -> str:
    if not target.model:
        return f"no firmware profile for printer {target.name}: it has no `model:` in the registry"
    return f"no firmware profile for model {target.model}"


def status_for(
    target: Target,
    profile: Profile | None,
    ep: Endpoint | None,
    be: Backends | None,
    *,
    live: bool,
) -> StatusEntry:
    if profile is None:
        return StatusEntry(
            target.name,
            target.model,
            target.access,
            None,
            None,
            no_profile_note(target),
        )
    if not live:
        return StatusEntry(
            target.name,
            target.model,
            target.access,
            profile.id,
            None,
            "static: no printer contacted (--no-live)",
        )
    if not target.contactable or ep is None or be is None:
        return StatusEntry(
            target.name,
            target.model,
            target.access,
            profile.id,
            None,
            "offline fleet printer: never contacted",
        )
    return StatusEntry(
        target.name,
        target.model,
        target.access,
        profile.id,
        detect_state(profile, ep, be),
        "",
    )


# ---- plan ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanReport:
    plan: Plan
    detection: Detection | None
    violations: tuple[guards.Violation, ...]


def plan_for(
    ctx: Context, to: str, also: tuple[str, ...], from_state: str | None
) -> PlanReport:
    """The plan a printer needs for `to`; never executes anything. `from_state` skips detection
    (the only way to plan for an offline printer)."""
    profile, target = ctx.profile, ctx.target
    detection: Detection | None = None
    if from_state is None:
        if not target.contactable:
            raise UsageError(
                f"{target.name} is an offline fleet printer: it is never contacted, so its state cannot be detected",
                command="printers fw plan",
                remediation=[
                    f"Name the state you assume: `3d printers fw plan {target.name} --from STATE --to {to}` (states: {', '.join(profile.state_ids())})."
                ],
            )
        detection = detect_state(profile, ctx.ep, ctx.be)
        if detection.state is None:
            raise UsageError(
                f"cannot plan for {target.name}: its state was not detected ({'; '.join(detection.notes)})",
                command="printers fw plan",
                remediation=[
                    "Run `3d printers fw status NAME` to see the probes, or pass --from STATE."
                ],
            )
        from_state = detection.state
    bad = guards.forbidden_options(profile, also, from_state, to)
    if bad:
        return PlanReport(
            Plan(profile.id, from_state, to, (), (), also), detection, tuple(bad)
        )
    plan = build_plan(profile, from_state, to, also)
    return PlanReport(plan, detection, tuple(guards.plan_violations(profile, plan)))


# ---- backup ----------------------------------------------------------------------------------


def backup_for(ctx: Context, dest: Path, now: float | None = None) -> Manifest:
    """Detect the state (so state-specific backup items are chosen) and take the backup."""
    if not ctx.target.contactable:
        raise GateFailure(
            f"refusing to back up {ctx.target.name}: it is an offline fleet printer, never contacted",
            command="printers fw backup",
        )
    detection = detect_state(ctx.profile, ctx.ep, ctx.be)
    return take_backup(
        ctx.profile,
        ctx.ep,
        ctx.be,
        printer=ctx.target.name,
        model=ctx.target.model,
        state=detection.state,
        dest_root=dest,
        now=now,
    )


# ---- apply -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class ApplyOptions:
    to: str
    yes: bool = False
    confirm: str | None = None
    backup_dir: Path = Path(".")
    max_age_s: float = DEFAULT_MAX_AGE_S
    also: tuple[str, ...] = ()
    files: Mapping[str, Path] = field(default_factory=dict)  # step id -> firmware file
    default_file: Path | None = None
    now: float | None = None


@dataclass(frozen=True)
class ApplyResult:
    exit_code: int
    result: str  # dry-run | applied | noop | refused | failed | pending-manual
    from_state: str | None
    to_state: str
    final_state: str | None
    events: tuple[Event, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "result": self.result,
            "exit_code": self.exit_code,
            "from": self.from_state,
            "to": self.to_state,
            "final_state": self.final_state,
            "events": [e.to_json() for e in self.events],
        }


def _file_for(plan: Plan, step: Step, opts: ApplyOptions) -> Path | None:
    if step.id in opts.files:
        return opts.files[step.id]
    uploads = [p for p in plan.steps if p.step.kind == "http_upload"]
    return opts.default_file if len(uploads) == 1 else None


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_upload_files(
    plan: Plan, opts: ApplyOptions, log: EventLog
) -> dict[str, Path]:
    """Every upload step needs a readable firmware file BEFORE anything runs (usage error, exit 2)."""
    files: dict[str, Path] = {}
    for ps in plan.steps:
        step = ps.step
        if step.kind != "http_upload" or step.upload is None:
            continue
        path = _file_for(plan, step, opts)
        if path is None:
            raise UsageError(
                f"step {step.id} uploads a firmware file: pass --file {step.id}=PATH",
                command="printers fw apply",
                remediation=[
                    "With one upload step in the plan a bare `--file PATH` is enough."
                ],
            )
        up = step.upload
        if not path.is_file():
            raise UsageError(
                f"--file {path}: not a readable file", command="printers fw apply"
            )
        if up.accept_suffix and not path.name.endswith(up.accept_suffix):
            raise InvalidArgument(
                "--file",
                path.name,
                [f"a file ending in {up.accept_suffix}"],
                command="printers fw apply",
            )
        size = path.stat().st_size
        if size < up.min_bytes:
            raise InvalidArgument(
                "--file",
                f"{path.name} ({size} bytes)",
                [f"at least {up.min_bytes} bytes: a firmware image, not an error page"],
                command="printers fw apply",
            )
        sha = _sha256_file(path)
        known = sha in up.known_sha256
        log.emit(
            "file",
            f"  firmware file {path.name}: {size} bytes, sha256 {sha} "
            + (
                "(listed in the profile)"
                if known
                else "(NOT in the profile's known list: compare it with the release notes)"
            ),
            step=step.id,
            sha256=sha,
            known=known,
            size=size,
        )
        files[step.id] = path
    return files


def _probe_summary(results: list[ProbeResult]) -> str:
    return (
        "; ".join(
            f"{r.probe.title}: {render.clean(r.detail)}" for r in results if not r.holds
        )
        or "all hold"
    )


class _Stop(Exception):
    """Internal: abort the run with a result (refusal, failed step, pending manual step)."""

    def __init__(self, code: int, result: str) -> None:
        super().__init__(result)
        self.code = code
        self.result = result


def _refuse(log: EventLog, message: str, *remediation: str, **data: object) -> _Stop:
    log.emit("refused", f"REFUSED: {message}", **data)
    for r in remediation:
        log.emit("hint", f"  -> {r}")
    return _Stop(1, "refused")


def run_apply(ctx: Context, opts: ApplyOptions) -> ApplyResult:
    log = ctx.log
    state_before: str | None = None
    final: str | None = None
    try:
        state_before, final, result = _apply(ctx, opts)
        code = 0
    except _Stop as stop:
        code, result = stop.code, stop.result
    return ApplyResult(code, result, state_before, opts.to, final, tuple(log.events))


def _apply(ctx: Context, opts: ApplyOptions) -> tuple[str | None, str | None, str]:
    profile, target, log = ctx.profile, ctx.target, ctx.log
    mode = "APPLY" if opts.yes else "DRY RUN (nothing is changed; add --yes to apply)"
    log.emit("start", f"fw apply {target.name} --to {opts.to} [{profile.id}]: {mode}")
    if not target.contactable:
        raise _refuse(
            log,
            f"{target.name} is an offline fleet printer ({target.model}): 3d never contacts it",
            "Do the firmware work by hand; `3d printers fw plan NAME --from STATE --to STATE` prints the runbook steps.",
        )
    if opts.yes and not target.writable:
        raise _refuse(
            log,
            f"{target.name} has access {target.access!r}: 3d may read it but never change it",
        )
    if opts.yes:
        typed = opts.confirm
        if typed is None:
            if ctx.ask is None:
                raise UsageError(
                    f"apply --yes needs the printer name typed explicitly: --confirm {target.name}",
                    command="printers fw apply",
                    remediation=[
                        f"3d printers fw apply {target.name} --to {opts.to} --yes --confirm {target.name}"
                    ],
                )
            typed = ctx.ask(
                f"Type the printer name ({target.name}) to change its firmware: "
            ).strip()
        if typed != target.name:
            raise UsageError(
                f"--confirm {typed!r} is not the printer's name {target.name!r}: refusing",
                command="printers fw apply",
                remediation=[
                    "Type the fleet name exactly (`3d printer list` shows them)."
                ],
            )
    detection = detect_state(profile, ctx.ep, ctx.be)
    log.lines(render.detection_lines(detection))
    if detection.state is None:
        raise _refuse(
            log,
            f"state of {target.name} not detected: " + "; ".join(detection.notes),
            "Run `3d printers fw status NAME` for the probe evidence.",
        )
    state = detection.state
    bad = guards.forbidden_options(profile, opts.also, state, opts.to)
    if bad:
        for v in bad:
            log.emit("guard", "REFUSED: " + v.render(), guard=v.guard.id)
        raise _Stop(1, "refused")
    plan = build_plan(profile, state, opts.to, opts.also)
    if plan.empty:
        log.emit("noop", f"{target.name} is already in state {opts.to}: nothing to do")
        return state, state, "noop"
    violations = guards.plan_violations(profile, plan)
    if violations:
        for v in violations:
            log.emit("guard", "REFUSED: " + v.render(), guard=v.guard.id)
        raise _Stop(1, "refused")
    files = (
        _check_upload_files(plan, opts, log)
        if opts.yes
        else {
            s.step.id: p
            for s in plan.steps
            if s.step.kind == "http_upload"
            and (p := _file_for(plan, s.step, opts)) is not None
        }
    )
    log.lines(render.plan_lines(plan, profile, files, target.host))
    # The first step's backup ids must be in the backup NOW; later steps are checked right before
    # they run, because the printer's state (and so the items that can be taken: ssh ones need
    # root) changes on the way. A refusal there says to take a new backup and re-run.
    required = set(plan.steps[0].step.backup)
    later = sorted({b for s in plan.steps[1:] for b in s.step.backup} - required)
    manifest, why = find_fresh_backup(
        opts.backup_dir,
        target.name,
        profile.id,
        max_age_s=opts.max_age_s,
        required=required,
        now=opts.now,
    )
    idle = ctx.idle_check(target, profile, ctx.ep, ctx.be)
    log.emit(
        "gate",
        f"gate backup: {'OK ' + manifest.directory.name if manifest else 'MISSING — ' + why}",
        gate="backup",
        ok=manifest is not None,
    )
    if later:
        log.emit(
            "gate",
            f"gate backup (later steps): they also need {', '.join(later)} in the backup; "
            "if the printer cannot provide them yet (e.g. ssh needs root), the run stops at that step "
            f"and asks for a new `fw backup {target.name}`",
            gate="backup-later",
            items=later,
        )
    log.emit(
        "gate",
        f"gate idle: {'OK' if idle.idle else 'NOT IDLE'} — {render.clean(idle.detail)} [{idle.source}]",
        gate="idle",
        ok=idle.idle,
    )
    missing_files = [
        s.step.id
        for s in plan.steps
        if s.step.kind == "http_upload" and s.step.id not in files
    ]
    if not opts.yes:
        ready = manifest is not None and idle.idle and not missing_files
        hints: list[str] = []
        if manifest is None or not idle.idle:
            hints.append("fix the gates above")
        if missing_files:
            hints.append("pass --file for " + ", ".join(missing_files))
        log.emit(
            "dry-run",
            "dry run: nothing was changed"
            + (
                ""
                if ready
                else f" (not ready: {'; '.join(hints)}; then add --yes --confirm {target.name})"
            ),
            ready=ready,
        )
        return state, None, "dry-run"
    if manifest is None:
        raise _refuse(
            log,
            f"no fresh backup: {why}",
            f"Run `3d printers fw backup {target.name}` (read-only), then re-run the same command.",
        )
    if not idle.idle:
        guard = guards.any_idle_guard(profile)
        reason = (
            f" — {guard.reason} Source: {', '.join(guard.sources)}" if guard else ""
        )
        raise _refuse(
            log,
            f"{target.name} is not idle ({render.clean(idle.detail)}){reason}",
            "Wait for the job to finish; never flash while printing.",
        )
    if any(p.step.kind.startswith("ssh") for p in plan.steps) and ctx.be.use_ssh:
        ctx.be.runner.check_available()
    for i, ps in enumerate(plan.steps, 1):
        _run_step(ctx, plan, ps, i, files, manifest)
    final_det = detect_state(profile, ctx.ep, ctx.be)
    target_state = profile.state(opts.to)
    if (
        target_state is not None
        and target_state.detection is not None
        and opts.to not in final_det.matched
    ):
        log.emit(
            "verify",
            f"STOPPED: after the last step {target.name} was detected as {final_det.state}, not {opts.to}",
            state=final_det.state,
        )
        raise _Stop(1, "failed")
    if target_state is not None and target_state.detection is None:
        log.emit(
            "info",
            f"note: state {opts.to} is not machine-detectable; the steps' own verification passed",
        )
    log.emit(
        "done", f"done: {target.name} is now in state {opts.to}", state=final_det.state
    )
    return state, final_det.state, "applied"


def _stop_failed(ctx: Context, step: Step, why: str) -> _Stop:
    ctx.log.emit("failed", f"STOPPED at step {step.id}: {why}", step=step.id)
    if step.rollback is not None:
        ctx.log.emit(
            "rollback", "  ROLLBACK: " + step.rollback.instruction.strip(), step=step.id
        )
    return _Stop(1, "failed")


def _invariants(ctx: Context, step: Step, when: str) -> None:
    for g, res in guards.check_invariants(ctx.profile, step, ctx.ep, ctx.be):
        v = guards.Violation(g, f"{when} step {step.id}: {render.clean(res.detail)}")
        ctx.log.emit("guard", "STOPPED: " + v.render(), guard=g.id)
        if step.rollback is not None:
            ctx.log.emit(
                "rollback",
                "  ROLLBACK: " + step.rollback.instruction.strip(),
                step=step.id,
            )
        raise _Stop(1, "failed")


def _verify(ctx: Context, step: Step) -> tuple[bool, list[ProbeResult]]:
    return poll(
        step.verify.probes,
        ctx.ep,
        ctx.be,
        wait_s=step.verify.wait_s,
        interval_s=step.verify.interval_s,
    )


def _run_step(
    ctx: Context,
    plan: Plan,
    ps: PlannedStep,
    index: int,
    files: Mapping[str, Path],
    manifest: Manifest,
) -> None:
    step, log = ps.step, ctx.log
    log.emit(
        "step",
        f"[{index}/{len(plan.steps)}] {step.id}: {step.title} ({step.kind}, risk {step.risk})",
        step=step.id,
    )
    idle = ctx.idle_check(ctx.target, ctx.profile, ctx.ep, ctx.be)
    if not idle.idle:
        raise _refuse(
            log,
            f"before step {step.id}: {ctx.target.name} is not idle ({render.clean(idle.detail)})",
        )
    missing = [b for b in step.backup if b not in manifest.ok_ids()]
    if missing:
        raise _refuse(
            log,
            f"step {step.id} needs backup items {', '.join(missing)}: the backup does not hold them",
        )
    if step.skip_if:
        done, _ = evaluate_group(step.skip_if, "all", ctx.ep, ctx.be)
        if done:
            log.emit(
                "skip",
                f"  skipped: already satisfied ({'; '.join(p.title for p in step.skip_if)})",
                step=step.id,
            )
            return
    if step.preconditions:
        ok, results = evaluate_group(step.preconditions, "all", ctx.ep, ctx.be)
        if not ok:
            raise _stop_failed(
                ctx, step, "precondition failed: " + _probe_summary(results)
            )
    if step.kind == "manual":
        _manual(ctx, step)
    else:
        _invariants(ctx, step, "before")
        if not step.automatable:
            raise _stop_failed(
                ctx,
                step,
                "the step is not automatable and not manual: refusing to run it",
            )
        _execute(ctx, step, files.get(step.id))
    ok, results = _verify(ctx, step)
    if not ok:
        raise _stop_failed(ctx, step, "verification failed: " + _probe_summary(results))
    log.emit(
        "verify",
        f"  verified: {'; '.join(p.title for p in step.verify.probes)}",
        step=step.id,
    )
    _invariants(ctx, step, "after")


def _manual(ctx: Context, step: Step) -> None:
    log = ctx.log
    # A `confirm` probe asks the human; asking before showing the instruction would be absurd.
    observable = all(p.kind != "confirm" for p in step.verify.probes)
    ok = observable and evaluate_group(step.verify.probes, "all", ctx.ep, ctx.be)[0]
    if ok:
        log.emit("skip", "  already done: its verification probe holds", step=step.id)
        return
    log.lines(["  do by hand:", *render.indent(step.instruction)])
    if step.rollback is not None:
        log.emit(
            "rollback",
            "  if it goes wrong: " + step.rollback.instruction.strip().splitlines()[0],
        )
    if ctx.ask is None:
        log.emit("manual", f"  {NEXT_HINT}", step=step.id)
        raise _Stop(1, "pending-manual")
    while True:
        answer = ctx.ask("  Press Enter when done (q to stop): ").strip().lower()
        if answer in ("q", "quit", "abort"):
            log.emit(
                "manual",
                f"  stopped by the user at {step.id}; {NEXT_HINT}",
                step=step.id,
            )
            raise _Stop(1, "pending-manual")
        ok, results = _verify(ctx, step)
        if ok:
            return
        log.emit(
            "verify", "  not verified yet: " + _probe_summary(results), step=step.id
        )


def _execute(ctx: Context, step: Step, file: Path | None) -> None:
    ep, be, log = ctx.ep, ctx.be, ctx.log
    if step.kind in ("ssh_script", "ssh_command"):
        assert ep.ssh is not None
        res = be.runner.run(
            ep.host, ep.ssh.user, step.command, timeout=step.timeout_s, port=ep.ssh_port
        )
        tail = render.clean((res.out + res.err)[-MAX_OUTPUT:]).strip()
        if tail:
            log.lines(render.indent(tail, "    | "))
        if res.rc != 0:
            raise _stop_failed(ctx, step, f"the command exited {res.rc}")
        return
    if step.kind == "http_post" and step.post is not None:
        url = ep.url(step.post.service, step.post.path)
        try:
            status, body = be.http("POST", url, timeout=step.timeout_s, body=b"")
        except OSError as exc:
            raise _stop_failed(ctx, step, f"POST {url}: {exc}") from exc
        _check_http(
            ctx,
            step,
            url,
            status,
            body,
            step.post.expect_status,
            step.post.success_regex,
        )
        return
    if step.kind == "http_upload" and step.upload is not None and file is not None:
        url = ep.url(step.upload.service, step.upload.path)
        log.emit("upload", f"  uploading {file.name} to {url}", step=step.id)
        try:
            status, body = be.upload(url, file, file.name, {}, timeout=step.timeout_s)
        except OSError as exc:
            # The printer reboots into the new firmware and may drop the connection after it took
            # the whole file: the verification probe, not this socket, decides.
            log.emit(
                "upload",
                f"  connection ended during/after the upload ({exc}); verifying",
                step=step.id,
            )
            return
        _check_http(
            ctx,
            step,
            url,
            status,
            body,
            step.upload.expect_status,
            step.upload.success_regex,
        )
        return
    raise _stop_failed(ctx, step, f"step kind {step.kind} cannot run here")


def _check_http(
    ctx: Context,
    step: Step,
    url: str,
    status: int,
    body: bytes,
    expect: tuple[int, ...],
    success: str | None,
) -> None:
    text = body.decode("utf-8", "replace")
    tail = render.clean(text[-MAX_OUTPUT:]).strip()
    if tail:
        ctx.log.lines(render.indent(tail, "    | "))
    if status not in expect:
        raise _stop_failed(
            ctx,
            step,
            f"{url} answered HTTP {status}, expected {'/'.join(map(str, expect))}",
        )
    if success is not None and not re.search(success, text):
        raise _stop_failed(
            ctx, step, f"{url} did not report success (no /{success}/ in the answer)"
        )
