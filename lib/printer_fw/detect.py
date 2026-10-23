"""detect.py — which firmware state of its profile is a printer in? (read-only probes only)

WHAT: `detect_state` walks the profile's states in the order the profile lists them (least to most
  modified), evaluates each state's detection probes and reports every state whose evidence is
  present; the DETECTED state is the last one that matched. A state may declare `requires: [ids]`:
  it is probed only after those matched (so an ssh probe never runs against a printer whose ssh
  banner is absent), and a state without `detection` is not machine-detectable.

WHY: `fw status` shows it, `fw plan`/`apply` start from it. Reporting evidence per probe (not just
  a state name) lets the user see WHY the tool thinks a printer is stock — and a probe that could
  not tell (ssh auth missing, printer rebooting) is reported as `partial` instead of silently
  downgrading the printer to an earlier state.

ACCESSED VIA: `printer_fw.engine`, `printers_cli/fw.py` (`fw status`).

INVARIANTS: only probes run (printer_fw.probes: read-only). `state` is None when no state matched
  or none is detectable. `partial` is True when any probe errored or was skipped: a later state
  might be present that the tool could not see.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from printer_fw.probes import (
    ERROR,
    SKIPPED,
    Backends,
    Endpoint,
    ProbeResult,
    evaluate_group,
)
from printer_fw.profiles import Profile


@dataclass(frozen=True)
class StateEvidence:
    state_id: str
    matched: bool
    note: (
        str  # why it was not probed ("not machine-detectable", "requires rooted") or ""
    )
    results: tuple[ProbeResult, ...] = ()


@dataclass(frozen=True)
class Detection:
    state: str | None
    matched: tuple[str, ...]
    evidence: tuple[StateEvidence, ...]
    partial: bool = False
    notes: tuple[str, ...] = field(default=())

    def to_json(self) -> dict[str, object]:
        return {
            "state": self.state,
            "matched": list(self.matched),
            "partial": self.partial,
            "notes": list(self.notes),
            "evidence": [
                {
                    "state": e.state_id,
                    "matched": e.matched,
                    "note": e.note,
                    "probes": [r.to_json() for r in e.results],
                }
                for e in self.evidence
            ],
        }


def detect_state(profile: Profile, ep: Endpoint, be: Backends) -> Detection:
    matched: list[str] = []
    evidence: list[StateEvidence] = []
    partial = False
    for st in profile.states:
        if st.detection is None:
            evidence.append(StateEvidence(st.id, False, "not machine-detectable"))
            continue
        missing = [r for r in st.requires if r not in matched]
        if missing:
            evidence.append(
                StateEvidence(
                    st.id, False, f"not probed: requires {', '.join(missing)}"
                )
            )
            continue
        holds, results = evaluate_group(st.detection.probes, st.detection.mode, ep, be)
        partial = partial or any(r.outcome in (ERROR, SKIPPED) for r in results)
        evidence.append(StateEvidence(st.id, holds, "", tuple(results)))
        if holds:
            matched.append(st.id)
    notes: list[str] = []
    detectable = [s for s in profile.states if s.detection is not None]
    if not detectable:
        notes.append(
            "no state of this profile can be detected read-only; use --from STATE"
        )
    elif not matched:
        notes.append(
            "no state matched: the printer is unreachable or runs something the profile does not describe"
        )
    if partial:
        notes.append(
            "some probes could not tell (error/skipped): a later state may be present"
        )
    return Detection(
        matched[-1] if matched else None,
        tuple(matched),
        tuple(evidence),
        partial,
        tuple(notes),
    )
