"""render.py — text and JSON shapes of everything `3d printers fw` prints.

WHAT: pure functions from the engine's data (profiles, detections, plans, manifests, apply events)
  to the lines a terminal shows and the dicts `--json` prints. Nothing here reads a printer.

WHY: the CLI module stays a thin argv/exit-code layer, and the engine stays testable without
  parsing its own prose: tests assert on the JSON shape, humans read the text.

ACCESSED VIA: `printers_cli/fw.py`, `printer_fw.engine` (live step announcements).

INVARIANTS: a plan is rendered with the EXACT command/endpoint each automatable step would run
  (a dry run is only useful if it shows what would be sent). Text from a printer or a profile is
  printed as is; terminal escape bytes in printer-supplied detail are stripped.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from printer_fw.backup import Manifest
from printer_fw.detect import Detection
from printer_fw.plan import Plan, PlannedStep
from printer_fw.profiles import Profile, Step

_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean(text: str) -> str:
    return _CTRL.sub("?", text)


def indent(text: str, pad: str = "      ") -> list[str]:
    return [pad + line if line else "" for line in text.strip("\n").splitlines()]


def action_lines(
    step: Step, file: Path | None, ssh_user: str | None, host: str | None = None
) -> list[str]:
    """What running `step` does, concretely."""
    where = f"{ssh_user}@{host}" if ssh_user and host else (ssh_user or "the printer")
    if step.kind == "manual":
        return ["do by hand:", *indent(step.instruction)]
    if step.kind in ("ssh_script", "ssh_command"):
        return [f"run over ssh as {where}:", *indent(step.command)]
    if step.kind == "http_upload" and step.upload is not None:
        up = step.upload
        target = f"POST {up.service}:{up.path} (multipart part `{up.field}`)"
        shown = str(file) if file is not None else "MISSING: pass --file PATH"
        return [f"upload {shown} -> {target}"]
    if step.kind == "http_post" and step.post is not None:
        return [f"POST {step.post.service}:{step.post.path}"]
    return []


def step_lines(
    ps: PlannedStep,
    index: int,
    total: int,
    *,
    file: Path | None = None,
    ssh_user: str | None = None,
    host: str | None = None,
) -> list[str]:
    s = ps.step
    kind = (
        s.kind
        if s.kind == "manual"
        else f"{s.kind} ({'automatable' if s.automatable else 'not automatable'})"
    )
    head = f"  {index}/{total} {s.id}: {s.title}"
    lines = [
        head,
        f"      kind {kind}, risk {s.risk}"
        + (" [optional --also]" if ps.optional else ""),
    ]
    lines += [
        f"      {ln}" if i == 0 else ln
        for i, ln in enumerate(action_lines(s, file, ssh_user, host))
    ]
    if s.backup:
        lines.append(f"      backups required: {', '.join(s.backup)}")
    if s.preconditions:
        lines.append(
            "      preconditions: " + "; ".join(p.title for p in s.preconditions)
        )
    if s.skip_if:
        lines.append("      skipped when: " + "; ".join(p.title for p in s.skip_if))
    lines.append("      verify: " + "; ".join(p.title for p in s.verify.probes))
    if s.rollback is not None:
        lines.append(
            "      rollback: " + s.rollback.instruction.strip().splitlines()[0]
        )
    if s.sources:
        lines.append("      sources: " + ", ".join(s.sources))
    return lines


def plan_lines(
    plan: Plan,
    profile: Profile,
    files: Mapping[str, Path] | None = None,
    host: str | None = None,
) -> list[str]:
    user = profile.ssh.user if profile.ssh else None
    lines = [f"Plan [{profile.id}]: {plan.from_state} -> {plan.to_state}"]
    if plan.empty:
        lines.append("  nothing to do: already in the target state")
        return lines
    path = " -> ".join([plan.from_state, *(t.to_state for t in plan.transitions)])
    lines.append(f"  path: {path}")
    for i, ps in enumerate(plan.steps, 1):
        lines += step_lines(
            ps,
            i,
            len(plan.steps),
            file=(files or {}).get(ps.step.id),
            ssh_user=user,
            host=host,
        )
    return lines


def step_json(ps: PlannedStep, file: Path | None = None) -> dict[str, object]:
    s = ps.step
    out: dict[str, object] = {
        "id": s.id,
        "title": s.title,
        "kind": s.kind,
        "automatable": s.automatable,
        "risk": s.risk,
        "optional": ps.optional,
        "transition": ps.transition.id,
        "backups": list(s.backup),
        "preconditions": [p.title for p in s.preconditions],
        "skip_if": [p.title for p in s.skip_if],
        "verify": [p.title for p in s.verify.probes],
        "rollback": s.rollback.instruction if s.rollback else None,
        "sources": list(s.sources),
    }
    if s.kind == "manual":
        out["instruction"] = s.instruction
    elif s.kind in ("ssh_script", "ssh_command"):
        out["command"] = s.command
    elif s.kind == "http_upload" and s.upload is not None:
        out["upload"] = {
            "service": s.upload.service,
            "path": s.upload.path,
            "field": s.upload.field,
            "file": str(file) if file else None,
        }
    elif s.kind == "http_post" and s.post is not None:
        out["post"] = {"service": s.post.service, "path": s.post.path}
    return out


def plan_json(plan: Plan, files: Mapping[str, Path] | None = None) -> dict[str, object]:
    return {
        "profile": plan.profile_id,
        "from": plan.from_state,
        "to": plan.to_state,
        "path": [plan.from_state, *(t.to_state for t in plan.transitions)],
        "also": list(plan.also),
        "steps": [step_json(p, (files or {}).get(p.step.id)) for p in plan.steps],
    }


def detection_lines(d: Detection, verbose: bool = False) -> list[str]:
    lines = [
        f"  state: {d.state or 'unknown'}"
        + (" (partial: some probes could not tell)" if d.partial and d.state else "")
    ]
    if d.matched:
        lines.append(f"  matched: {', '.join(d.matched)}")
    lines += [f"  note: {n}" for n in d.notes]
    if verbose:
        for e in d.evidence:
            mark = "yes" if e.matched else "no "
            lines.append(
                f"    [{mark}] {e.state_id}" + (f" ({e.note})" if e.note else "")
            )
            lines += [
                f"          {r.outcome:7} {r.probe.title}: {clean(r.detail)}"
                for r in e.results
            ]
    return lines


def manifest_lines(m: Manifest) -> list[str]:
    lines = [
        f"Backup of {m.printer} [{m.profile_id}] at {m.created_at}",
        f"  directory: {m.directory}",
    ]
    for i in m.items:
        extra = (
            f"{i.size} bytes, sha256 {i.sha256[:12]}"
            if i.status == "ok"
            else clean(i.detail)
        )
        lines.append(f"  {i.status:8} {i.id}" + (f" ({extra})" if extra else ""))
    return lines


def profile_json(p: Profile) -> dict[str, object]:
    return {
        "id": p.id,
        "vendor": p.vendor,
        "family": p.family,
        "models": [m.describe() for m in p.models],
        "capabilities": {
            "klipper_possible": p.capabilities.klipper_possible,
            "notes": p.capabilities.notes,
        },
        "states": [
            {"id": s.id, "title": s.title, "detectable": s.detection is not None}
            for s in p.states
        ],
        "transitions": [
            {
                "id": t.id,
                "from": t.from_state,
                "to": t.to_state,
                "steps": [s.id for s in t.steps],
                "optional_steps": [s.id for s in t.optional_steps],
            }
            for t in p.transitions
        ],
        "guards": [{"id": g.id, "kind": g.kind} for g in p.guards],
        "sources": list(p.sources),
    }


def profiles_lines(profiles: Sequence[Profile]) -> list[str]:
    lines: list[str] = []
    for p in profiles:
        klip = (
            "Klipper possible"
            if p.capabilities.klipper_possible
            else "Klipper NOT possible"
        )
        lines.append(f"{p.id}: {p.vendor} {p.family} — {klip}")
        lines.append("  models: " + "; ".join(m.describe() for m in p.models))
        lines.append("  states: " + ", ".join(s.id for s in p.states))
        for t in p.transitions:
            opt = (
                f" (+ optional: {', '.join(s.id for s in t.optional_steps)})"
                if t.optional_steps
                else ""
            )
            lines.append(
                f"  {t.from_state} -> {t.to_state}: {', '.join(s.id for s in t.steps) or '(maintenance)'}{opt}"
            )
        lines.append("  " + p.capabilities.notes.strip().splitlines()[0])
        lines += [f"  source: {u}" for u in p.sources]
        lines.append("")
    return lines[:-1] if lines else lines
