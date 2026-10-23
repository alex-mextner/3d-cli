"""plan.py — the ordered steps that take a printer from its current state to a target state.

WHAT: `build_plan(profile, from_state, to_state, also=...)` finds the shortest chain of the
  profile's transitions (breadth-first; ties go to the transition listed first) and returns the
  steps of every transition on it, in order, plus the optional steps the user selected with
  `--also STEP_ID`. A target equal to the current state is an empty plan. An unknown target state
  is an `InvalidArgument` (exit 2), a known but unreachable one is `Unreachable` (exit 1: the
  request is well-formed, the profile just has no path), an unknown or misplaced `--also` id is
  `InvalidArgument`.

WHY: `plan` must be able to say exactly what `apply` would do without touching the printer, so
  planning is a pure function of profile data + two state ids. Optional steps belong to a
  transition (a `maintenance` transition, from == to, carries steps that act on one state such as
  an in-place upgrade); an `--also` step is valid only when its transition lies on the path.

ACCESSED VIA: `printer_fw.engine`, `printers_cli/fw.py` (`fw plan`).

INVARIANTS: pure, no I/O. Forbidden options are refused by printer_fw.guards BEFORE this module
  looks at `--also`, so a forbidden id gets its documented reason rather than "unknown id".
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass

from errors import GateFailure, InvalidArgument
from printer_fw.profiles import Profile, Step, Transition


class Unreachable(GateFailure):
    """The profile has no transition path to the requested state. Exit 1."""


@dataclass(frozen=True)
class PlannedStep:
    step: Step
    transition: Transition
    optional: bool


@dataclass(frozen=True)
class Plan:
    profile_id: str
    from_state: str
    to_state: str
    transitions: tuple[Transition, ...]
    steps: tuple[PlannedStep, ...]
    also: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.steps

    def step_ids(self) -> list[str]:
        return [p.step.id for p in self.steps]


def _edges(profile: Profile) -> dict[str, list[Transition]]:
    out: dict[str, list[Transition]] = {s.id: [] for s in profile.states}
    for t in profile.transitions:
        if not t.maintenance:
            out[t.from_state].append(t)
    return out


def shortest_path(
    profile: Profile, from_state: str, to_state: str
) -> list[Transition] | None:
    """Breadth-first over non-maintenance transitions; None when `to_state` is unreachable."""
    if from_state == to_state:
        return []
    edges = _edges(profile)
    queue: deque[str] = deque([from_state])
    came: dict[str, tuple[str, Transition]] = {}
    seen = {from_state}
    while queue:
        cur = queue.popleft()
        for t in edges.get(cur, []):
            if t.to_state in seen:
                continue
            seen.add(t.to_state)
            came[t.to_state] = (cur, t)
            if t.to_state == to_state:
                path: list[Transition] = []
                node = to_state
                while node != from_state:
                    node, tr = came[node][0], came[node][1]
                    path.append(tr)
                return path[::-1]
            queue.append(t.to_state)
    return None


def reachable_states(profile: Profile, from_state: str) -> list[str]:
    edges = _edges(profile)
    seen = {from_state}
    queue: deque[str] = deque([from_state])
    while queue:
        for t in edges.get(queue.popleft(), []):
            if t.to_state not in seen:
                seen.add(t.to_state)
                queue.append(t.to_state)
    return [s.id for s in profile.states if s.id in seen and s.id != from_state]


def check_states(profile: Profile, from_state: str, to_state: str) -> None:
    for flag, value in (("--from", from_state), ("--to", to_state)):
        if profile.state(value) is None:
            hint = f" {profile.capabilities.notes}" if flag == "--to" else ""
            raise InvalidArgument(
                flag,
                value,
                profile.state_ids(),
                command="printers fw",
                extra=f"`3d printers fw profiles` lists the states of every profile.{hint}",
            )


def build_plan(
    profile: Profile, from_state: str, to_state: str, also: Sequence[str] = ()
) -> Plan:
    check_states(profile, from_state, to_state)
    path = shortest_path(profile, from_state, to_state)
    if path is None:
        reach = reachable_states(profile, from_state)
        raise Unreachable(
            f"profile {profile.id} has no path from {from_state!r} to {to_state!r}",
            command="printers fw",
            remediation=[
                f"From {from_state!r} the profile can reach: {', '.join(reach) or 'nothing'}.",
                profile.capabilities.notes,
            ],
        )
    states_on_path = [from_state, *(t.to_state for t in path)]
    steps: list[PlannedStep] = []
    optional_by_state: dict[str, list[PlannedStep]] = {}
    chosen = list(dict.fromkeys(also))
    for state in states_on_path:
        for t in profile.transitions:
            if t.maintenance and t.from_state == state:
                optional_by_state.setdefault(state, []).extend(
                    PlannedStep(s, t, True) for s in t.optional_steps if s.id in chosen
                )
    used: set[str] = set()

    def take(t: Transition) -> list[PlannedStep]:
        got = [PlannedStep(s, t, True) for s in t.optional_steps if s.id in chosen]
        used.update(p.step.id for p in got)
        return got

    steps.extend(optional_by_state.get(from_state, []))
    used.update(p.step.id for p in optional_by_state.get(from_state, []))
    for t in path:
        steps.extend(PlannedStep(s, t, False) for s in t.steps)
        steps.extend(take(t))
        extra = optional_by_state.get(t.to_state, [])
        steps.extend(extra)
        used.update(p.step.id for p in extra)
    unplaced = [a for a in chosen if a not in used]
    if unplaced:
        known = sorted({s.id for t, s, opt in profile.all_steps() if opt})
        raise InvalidArgument(
            "--also",
            ", ".join(unplaced),
            known,
            command="printers fw",
            extra=f"Those steps belong to transitions that are not on the path {' -> '.join(states_on_path)}.",
        )
    return Plan(
        profile.id, from_state, to_state, tuple(path), tuple(steps), tuple(chosen)
    )


def optional_step_ids(profile: Profile) -> list[str]:
    return sorted({s.id for _t, s, opt in profile.all_steps() if opt})
