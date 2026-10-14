"""lint_spools.py — spool data rules of `3d lint --printops` (plan §9.3; issues #160, #163).

RULES (run by printops.datalint over every non-archived Spoolman spool):
  spools/remaining-negative  error  the ledger books more than the spool had (mid < 0): an
                                    estimate source over-counts or the spool was swapped — weigh it
  spools/no-tare             warn   no empty-spool weight anywhere in the tare chain, so the spool
                                    cannot be weighed (`3d printops tare add` / receive --tare)
  spools/not-weighed         warn   never weighed, or last weighed more than ctx.weigh_days ago,
                                    while the ledger drifts with every estimate. A sealed spool
                                    received with a known net and not used yet is exempt.
"""

from __future__ import annotations

from printops import ledger, tares
from printops.datalint import ERROR, WARN, Finding, LintContext, parse_utc
from printops.spoolman import decode_extra


def _label(sp: dict) -> str:
    fil = sp.get("filament") or {}
    vendor = (fil.get("vendor") or {}).get("name")
    return " ".join(str(x) for x in (vendor, fil.get("material"), fil.get("name")) if x)


def lint(ctx: LintContext) -> list[Finding]:
    out: list[Finding] = []
    for sp in ctx.spools:
        sid = int(sp["id"])
        subject = f"spool:{sid}"
        label = _label(sp)
        rem = sp.get("remaining_weight")
        est = ledger.estimate(
            ctx.conn, sid, fallback_g=None if rem is None else float(rem)
        )
        if est.anchored and est.mid < 0:
            out.append(
                Finding(
                    "spools/remaining-negative",
                    ERROR,
                    subject,
                    f"#{sid} {label}: remaining {est.mid:.0f} ± {est.sigma:.0f} g — more booked than "
                    f"the spool had; weigh it: 3d printops spool measure {sid} GROSS_G",
                )
            )
        res = tares.resolve(ctx.conn, sp)
        if res.tare is None:
            out.append(
                Finding(
                    "spools/no-tare",
                    WARN,
                    subject,
                    f"#{sid} {label}: no empty-spool weight ({res.reason}); add one: "
                    "3d printops tare add VENDOR GRAMS --source …",
                )
            )
        opened = decode_extra((sp.get("extra") or {}).get("opened_at"))
        sealed = est.anchor_source == "receive" and est.deltas == 0 and not opened
        if sealed:
            continue
        if est.last_weighed_at is None:
            out.append(
                Finding(
                    "spools/not-weighed",
                    WARN,
                    subject,
                    f"#{sid} {label}: never weighed; remaining is {est.text()}",
                )
            )
            continue
        age = (ctx.now - parse_utc(est.last_weighed_at)).days
        if age > ctx.weigh_days:
            out.append(
                Finding(
                    "spools/not-weighed",
                    WARN,
                    subject,
                    f"#{sid} {label}: last weighed {age} days ago (> {ctx.weigh_days}); "
                    f"remaining is {est.text()}",
                )
            )
    return out
