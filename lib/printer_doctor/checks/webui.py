"""webui.py — does a web UI answer? (`webui-presence`)

CHECK: Moonraker printers should serve Fluidd or Mainsail; `printer_ops.webui.probe` GETs the
  usual places (80, 4408, 4409 or THREED_WEBUI_PORTS) and reports what answered. `info` when
  neither does: the printer works, but nobody can configure it, pick a spool in the UI, or see
  the UI that `spoolman-cors` needs the origin of. Manual fix: install one by the printer's vendor
  path (a runbook in docs/printers/ for the model, e.g. the Creality Helper Script for K1-family
  printers: https://github.com/Guilouz/Creality-Helper-Script).
"""

from __future__ import annotations

from printer_doctor.checks._common import UI_KINDS, origin_of
from printer_doctor.context import Context
from printer_doctor.model import Check, CheckResult, Fix, JsonValue, Verdict


def _run(ctx: Context) -> Verdict:
    probe = ctx.webui()
    uis = [u for u in probe.found if u.kind in UI_KINDS]
    evidence: dict[str, JsonValue] = {
        "tried": probe.tried,
        "found": [
            {"kind": u.kind, "url": u.url, "http_status": u.http_status}
            for u in probe.found
        ],
        "origins": [o for o in (origin_of(u.url) for u in uis) if o],
    }
    if uis:
        return Verdict.ok(
            "web UI: " + ", ".join(f"{u.kind} {u.url}" for u in uis), **evidence
        )
    return Verdict.fail(
        f"no Fluidd or Mainsail answered ({probe.tried} places tried)",
        "info",
        **evidence,
    )


def _steps(ctx: Context, result: CheckResult) -> list[str]:
    return [
        "Check the probe's places by hand: `3d printers show "
        + ctx.name
        + "` lists what answers.",
        "Install Fluidd or Mainsail by the vendor path of this model: see the runbook for it in docs/printers/ "
        "(the Creality Helper Script installs Fluidd/Mainsail on K1-family printers: "
        "https://github.com/Guilouz/Creality-Helper-Script).",
        "A UI on an unusual port: set THREED_WEBUI_PORTS=80,PORT,... so the probe looks there.",
    ]


CHECKS = (
    Check(
        id="webui-presence",
        title="Fluidd or Mainsail answers",
        severity="info",
        applies_to="Moonraker-family printers",
        applies=lambda ctx: ctx.moonraker_only(),
        run=_run,
        fix=Fix("manual-only", "install Fluidd/Mainsail by the vendor path", _steps),
    ),
)
