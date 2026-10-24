"""mix_calibration.py — the `mix_calibration` line of `3d print-preflight` (issue #418, plan §20.7).

WHAT: for every mixed filament the project 3MF's plate prints (slicing.mix_uses: manual layer
  pattern or Local-Z row), the heads it mixes → the spools bound to them (PrintOps state) → their
  Spoolman filament ids → the mix key of printops.mix_key → 3d-api `POST /v1/calibrations/mix-check`
  (printops.mix_gate) → one finding per mix:
    never measured / stale / expired   soft (WARN): `mix 431 (T4 Snapdragon, T3 orange, T1 bordeaux)
                                       never measured: 3d mix tester --target '#…' --printer u1`
    measured, ΔE to the part's colour  hard (FAIL under policy level `block`, else WARN) when above
                                       gates `mix_color.max_delta_e`; the part's colour is the model
                                       colour the project carries (slicing.mix_uses), if any
    measured and fresh                 ok
  `--accept mix_calibration --reason TEXT` turns a hard finding into a WARN (run_checks) and
  logs it in 3d-api (`calibration.accepted`, who/why/which G-code).
WHY: the ΔE 16.6 tile of 2026-10-17 was printed on predictions alone (tg#22816); a quarter hour of
  tester is cheap beside a long print in a colour nobody checked.
INVARIANTS: read-only except the accept log; no project 3MF → no line at all (the plate's mixes are
  unknown, and a plate without mixes needs none); a mix whose spool bindings or 3d-api are missing
  is UNVERIFIED and says what is missing, never PASS.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from printer_ops.printops_checks import Finding, _Ctx, _f, _hex
from printops.client import ApiError
from printops.color import hex_delta_e
from printops.mix_key import MixKeyError
from slicing.head_sequence import ProjectError
from slicing.mix_uses import MixUse, mix_key, read_uses

NAME = "mix_calibration"
ENDPOINT = "/v1/calibrations/mix-check"
ACCEPT = "mix_calibration"  # `--accept mix_calibration --reason TEXT`


@dataclass(frozen=True)
class _Keyed:
    use: MixUse
    key: str
    label: str  # `mix 431 (T4 Snapdragon, T3 orange, T1 bordeaux)`


def tester_command(printer: str, target: str | None) -> str:
    return f"3d mix tester --target '{target or '#RRGGBB'}' --printer {printer}"


def _sha256(path: str) -> str | None:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
    except OSError:
        return None
    return h.hexdigest()


def _bindings(c: _Ctx) -> dict[int, Mapping[str, Any]]:
    """1-based head -> the spool bound to its tool (only the bound ones)."""
    out: dict[int, Mapping[str, Any]] = {}
    for entry in c.state.get("tools") or []:
        spool = entry.get("spool")
        if isinstance(spool, dict):
            out[int(entry["tool"]) + 1] = spool
    return out


def _spool_name(spool: Mapping[str, Any]) -> str:
    return str(spool.get("name") or spool.get("material") or f"spool {spool.get('id')}")


def _key_uses(c: _Ctx, uses: list[MixUse], out: list[Finding]) -> list[_Keyed]:
    """The uses whose heads all carry a spool with a filament id; the others become UNVERIFIED."""
    bound = _bindings(c)
    keyed: list[_Keyed] = []
    for use in uses:
        names = []
        ids: dict[int, int] = {}
        gaps: list[str] = []
        for head in use.distinct_heads():
            spool = bound.get(head)
            if spool is None:
                gaps.append(f"T{head}: {c.unbound_reason}")
                names.append(f"T{head} ?")
                continue
            names.append(f"T{head} {_spool_name(spool)}")
            fid = spool.get("filament_id")
            if isinstance(fid, int) and not isinstance(fid, bool) and fid > 0:
                ids[head] = fid
            else:
                gaps.append(f"T{head}: its spool has no Spoolman filament id")
        label = f"mix {use.name} ({', '.join(names)})"
        if gaps:
            out.append(
                _f(
                    NAME,
                    "unknown",
                    f"{label} cannot be looked up: {'; '.join(gaps)}",
                    kind=use.name,
                )
            )
            continue
        try:
            keyed.append(_Keyed(use, mix_key(use, ids), label))
        except MixKeyError as e:
            out.append(
                _f(NAME, "unknown", f"{label} has no mix key: {e}", kind=use.name)
            )
    return keyed


def _judge(c: _Ctx, k: _Keyed, fact: Mapping[str, Any], printer: str) -> Finding:
    status = str(fact.get("status"))
    use = k.use
    cmd = tester_command(printer, use.target_hex)
    detail = str(fact.get("detail") or "")
    if status == "never":
        return _f(NAME, "soft", f"{k.label} never measured: {cmd}", kind=k.key)
    if status in ("stale", "expired"):
        return _f(
            NAME,
            "soft",
            f"{k.label} measurement is {status}: {detail}; measure again: {cmd}",
            kind=k.key,
        )
    if status != "current":
        return _f(
            NAME, "unknown", f"{k.label}: 3d-api answered status {status!r}", kind=k.key
        )
    record = fact.get("record")
    record = record if isinstance(record, dict) else {}
    measured = _hex(record.get("hex"))
    when = str(record.get("recorded_at") or "?")[:10]
    head = f"{k.label} #{record.get('id')} measured {measured or '?'} on {when}"
    if use.target_hex and measured:
        de = hex_delta_e(measured, use.target_hex)
        limit = c.policy.mix_max_delta_e
        part = f"the part's colour {use.target_hex} ({use.target_from})"
        if de > limit:
            return _f(
                NAME,
                "hard",
                f"{head}: ΔE {de:.1f} from {part}, above the limit {limit:g}; pick another mix: {cmd}",
                kind=k.key,
            )
        return _f(NAME, "ok", f"{head}: ΔE {de:.1f} from {part}", kind=k.key)
    return _f(NAME, "ok", head, kind=k.key)


def _log_accept(
    c: _Ctx, findings: list[Finding], printer: str, keyed: list[_Keyed]
) -> list[Finding]:
    """`--accept mix_calibration --reason TEXT`: run_checks downgrades the hard findings to WARN; this
    records the acceptance in 3d-api (`calibration.accepted`: who, why, which G-code). A write that
    fails is said on the finding, the downgrade stands."""
    hard = [f for f in findings if f.grade == "hard"]
    if ACCEPT not in c.accept or c.client is None or not hard:
        return findings
    keys = [f.kind for f in hard]
    body = {
        "printer": printer,
        "mixes": [
            {"key": k.key, "filament_ids": _filament_ids(c, k.use)}
            for k in keyed
            if k.key in keys
        ],
        "accept": keys,
        "reason": c.reason,
        "gcode_sha": _sha256(c.job.path),
        "file": c.origin,
        "notes": {f.kind: f.text for f in hard},
    }
    try:
        c.client.post(ENDPOINT, body)
    except ApiError as e:
        return [
            replace(f, text=f"{f.text}; acceptance not logged in 3d-api: {e}")
            if f in hard
            else f
            for f in findings
        ]
    return findings


def _filament_ids(c: _Ctx, use: MixUse) -> list[int]:
    bound = _bindings(c)
    return sorted(
        {int(bound[h]["filament_id"]) for h in use.distinct_heads() if h in bound}
    )


def check_mix_calibration(c: _Ctx) -> list[Finding]:
    if c.project is None:
        return []
    try:
        uses = read_uses(str(Path(c.project)))
    except ProjectError as e:
        return [_f(NAME, "unknown", f"mixes of the plate unknown: {e}")]
    if not uses:
        return []
    findings: list[Finding] = []
    keyed = _key_uses(c, uses, findings)
    if not keyed:
        return findings
    printer = str(c.state.get("printer") or "")
    if c.client is None:
        why = (
            "offline: the snapshot holds no calibration records"
            if c.state.get("source") != "fleet"
            else "no 3d-api"
        )
        return findings + [
            _f(
                NAME,
                "unknown",
                f"{k.label} cannot be looked up ({why}): pass --printops-api URL / set PRINTOPS_API",
                kind=k.key,
            )
            for k in keyed
        ]
    body = {
        "printer": printer,
        "mixes": [
            {"key": k.key, "filament_ids": _filament_ids(c, k.use)} for k in keyed
        ],
    }
    try:
        answer = c.client.post(ENDPOINT, body)
    except ApiError as e:
        return findings + [
            _f(
                NAME,
                "unknown",
                f"{k.label} cannot be looked up: 3d-api {e}",
                kind=k.key,
            )
            for k in keyed
        ]
    facts = {
        str(f.get("key")): f for f in answer.get("mixes") or [] if isinstance(f, dict)
    }
    for k in keyed:
        fact = facts.get(k.key)
        findings.append(
            _judge(c, k, fact, printer)
            if fact is not None
            else _f(
                NAME,
                "unknown",
                f"{k.label}: 3d-api did not answer for {k.key}",
                kind=k.key,
            )
        )
    return _log_accept(c, findings, printer, keyed)
