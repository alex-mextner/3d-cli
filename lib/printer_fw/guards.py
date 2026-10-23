"""guards.py — evaluate a profile's machine-checked pitfalls (the `guards:` list).

WHAT: a guard is DATA in the profile (id, kind, applies_when, reason, sources). This module is the
  generic code that enforces each kind:
    forbidden_option           an option/step id the user may not pass with `--also` and no plan
                               may contain (e.g. a helper-script menu action that breaks an adapter)
    forbidden_command_pattern  no automatable command of the plan may match a regex
    step_requires_command      a step's command must contain a pattern (a permissions fix that a
                               runbook says must follow an install)
    step_requires_step         if step A is in the plan, step B must be in it too (a hardening
                               step that must follow an install)
    require_idle               the printer must not be printing/paused/busy before a risky step
    invariant_probe            a probe that must hold before and after every step (e.g. an auth
                               switch that must stay off)
  Plan-time kinds produce `Violation`s; run-time kinds are evaluated by the engine through
  `runtime_guards()` / `check_invariants()`.

WHY: the runbooks' "do not" lists are the expensive lessons of this fleet. Written as data next to
  their source URL and enforced by one piece of generic code, they cannot be forgotten by a new
  profile and cannot be bypassed by a flag (`--yes` does not skip a guard).

ACCESSED VIA: `printer_fw.engine`, `printers_cli/fw.py` (`fw plan` shows violations too).

INVARIANTS: a violation always carries the guard's `reason` and source URLs so the refusal message
  quotes the runbook. `applies_when` limits a guard to plans from/to the listed states; an empty
  `applies_when` means always. Nothing here touches a printer except `check_invariants`, which
  runs one read-only probe.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from printer_fw.plan import Plan
from printer_fw.probes import Backends, Endpoint, ProbeResult, evaluate_group
from printer_fw.profiles import Guard, Profile, Step


@dataclass(frozen=True)
class Violation:
    guard: Guard
    message: str

    def render(self) -> str:
        src = f" Source: {', '.join(self.guard.sources)}" if self.guard.sources else ""
        return f"guard {self.guard.id}: {self.message} — {self.guard.reason}{src}"

    def to_json(self) -> dict[str, object]:
        return {
            "guard": self.guard.id,
            "kind": self.guard.kind,
            "message": self.message,
            "reason": self.guard.reason,
            "sources": list(self.guard.sources),
        }


def _applies(g: Guard, from_state: str, to_state: str) -> bool:
    if g.from_states and from_state not in g.from_states:
        return False
    return not (g.to_states and to_state not in g.to_states)


def forbidden_options(
    profile: Profile, also: Sequence[str], from_state: str, to_state: str
) -> list[Violation]:
    """`--also` ids a `forbidden_option` guard refuses. Checked before the plan is built so the
    user gets the documented reason, not "unknown step"."""
    out: list[Violation] = []
    for g in profile.guards:
        if g.kind != "forbidden_option" or not _applies(g, from_state, to_state):
            continue
        out += [Violation(g, f"--also {o} is refused") for o in also if o in g.options]
    return out


def plan_violations(profile: Profile, plan: Plan) -> list[Violation]:
    out: list[Violation] = []
    ids = set(plan.step_ids())
    commands = {p.step.id: p.step.command for p in plan.steps}
    # A manual step "runs" what its instruction tells the user to run.
    texts = {p.step.id: p.step.command or p.step.instruction for p in plan.steps}
    for g in profile.guards:
        if not _applies(g, plan.from_state, plan.to_state):
            continue
        if g.kind == "forbidden_option":
            out += [
                Violation(g, f"the plan contains {o!r}") for o in g.options if o in ids
            ]
        elif g.kind == "forbidden_command_pattern":
            rx = re.compile(g.pattern)
            out += [
                Violation(g, f"step {sid} matches /{g.pattern}/")
                for sid, cmd in commands.items()
                if cmd and rx.search(cmd)
            ]
        elif g.kind == "step_requires_command" and g.step in ids:
            cmd = texts.get(g.step, "")
            if not re.search(g.pattern, cmd):
                out.append(Violation(g, f"step {g.step} must run /{g.pattern}/"))
        elif (
            g.kind == "step_requires_step"
            and g.step in ids
            and g.requires_step not in ids
        ):
            out.append(
                Violation(g, f"step {g.step} needs {g.requires_step} in the same plan")
            )
    return out


def runtime_guards(profile: Profile, step: Step) -> list[Guard]:
    """The invariant guards in force for `step`: `scope: always` ones, plus the ones the step
    lists in its preconditions. (`require_idle` is checked before EVERY step by the engine.)"""
    return [
        g
        for g in profile.guards
        if g.kind == "invariant_probe"
        and (g.id in step.guard_refs or g.scope == "always")
    ]


def any_idle_guard(profile: Profile) -> Guard | None:
    return next((g for g in profile.guards if g.kind == "require_idle"), None)


def check_invariants(
    profile: Profile, step: Step, ep: Endpoint, be: Backends
) -> list[tuple[Guard, ProbeResult]]:
    """Every invariant probe in force for `step` that does NOT hold (empty: all hold). An
    invariant whose probe could not be evaluated (error/skipped) counts as violated: a guard that
    cannot be checked must not be assumed true."""
    bad: list[tuple[Guard, ProbeResult]] = []
    for g in runtime_guards(profile, step):
        if g.kind != "invariant_probe" or not g.probes:
            continue
        holds, results = evaluate_group(g.probes, "all", ep, be)
        if not holds:
            bad.append((g, next(r for r in results if not r.holds)))
    return bad
