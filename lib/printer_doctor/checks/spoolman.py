"""spoolman.py — the printer's Spoolman link and the CORS rule Fluidd/Mainsail depend on.

CHECKS
  spoolman-link  Moonraker's `[spoolman]` component: `GET /server/spoolman/status` ->
                 `{spoolman_connected, pending_reports:[{spool_id, filament_used}], spool_id}`.
                 error when Moonraker does not reach Spoolman (usage is not booked), warn when
                 usage reports are queued (they are sent once the link is back). Applies only when
                 `/server/info` lists the `spoolman` component: a printer without `[spoolman]`
                 (its `/server/spoolman/*` answers 404) is skipped with that reason.
  spoolman-cors  the web UI (Fluidd/Mainsail) calls Spoolman from the BROWSER, so Spoolman must
                 admit the UI's origin. Spoolman's URL is `config.spoolman.server` of the printer;
                 the origins are the Fluidd/Mainsail URLs the web-UI probe saw answering
                 (scheme://host:port). A GET with `Origin: <origin>` against `<spoolman>/api/v1/info`
                 must be answered with `Access-Control-Allow-Origin` = that origin (or `*`);
                 Spoolman (FastAPI CORSMiddleware) sends none for an origin it does not allow, and
                 the browser then shows "Spoolman is blocking Fluidd's browser origin".
SOURCE of the fix: services/3d-api/compose.yaml sets Spoolman's `SPOOLMAN_CORS_ORIGIN` from
  `${PRINTOPS_SPOOLMAN_CORS_ORIGIN:-<default origins>}`; services/3d-api/README.md explains it
  (comma-separated full origins, scheme included, no path; `*` disables the check).
"""

from __future__ import annotations

from collections.abc import Mapping

from printer_doctor.checks._common import UI_KINDS, origin_of
from printer_doctor.context import Context
from printer_doctor.model import CannotSee, Check, CheckResult, Fix, JsonValue, Verdict

# ---- spoolman-link --------------------------------------------------------------------------


def _has_spoolman(ctx: Context) -> str | None:
    reason = ctx.moonraker_only()
    if reason:
        return reason
    if "spoolman" not in ctx.components():
        return "no [spoolman] component in this Moonraker (its /server/spoolman/* endpoints are absent)"
    return None


def _spoolman_server(ctx: Context) -> str | None:
    """`config.spoolman.server` of the printer, None when unreadable."""
    try:
        section = ctx.config_section("spoolman")
    except CannotSee:
        return None
    server = section.get("server") if section is not None else None
    return server if isinstance(server, str) and server.strip() else None


def _link(ctx: Context) -> Verdict:
    body = ctx.result_obj("/server/spoolman/status")
    connected = body.get("spoolman_connected") is True
    raw = body.get("pending_reports")
    pending = raw if isinstance(raw, list) else []
    ids: list[JsonValue] = [
        p.get("spool_id") for p in pending if isinstance(p, Mapping)
    ]
    evidence: dict[str, JsonValue] = {
        "spoolman_connected": connected,
        "pending_reports": len(pending),
        "pending_spool_ids": ids,
        "spool_id": body.get("spool_id"),
        "spoolman_server": _spoolman_server(ctx),
    }
    if not connected:
        return Verdict.fail(
            "Moonraker is not connected to Spoolman: filament usage is not booked",
            "error",
            **evidence,
        )
    if pending:
        return Verdict.fail(
            f"{len(pending)} usage report(s) waiting to be sent to Spoolman (spools {ids})",
            "warn",
            **evidence,
        )
    return Verdict.ok(
        f"connected to Spoolman, active spool {body.get('spool_id')!r}, nothing pending",
        **evidence,
    )


def _link_steps(ctx: Context, result: CheckResult) -> list[str]:
    server = result.evidence.get("spoolman_server")
    where = str(server) if server else "the `[spoolman] server:` of moonraker.conf"
    return [
        f"From this host: `curl -fsS {where.rstrip('/')}/api/v1/health` (Spoolman must answer).",
        "If it does not: start Spoolman (its container/service) and check the address and port in "
        "`[spoolman] server:` of moonraker.conf (web UI 'Configuration').",
        "Moonraker reconnects by itself (sync_rate seconds); queued usage reports are sent then. If "
        "it stays disconnected restart Moonraker (web UI Machine tab -> moonraker).",
    ]


# ---- spoolman-cors --------------------------------------------------------------------------


def _cors_applies(ctx: Context) -> str | None:
    reason = _has_spoolman(ctx)
    if reason:
        return reason
    if _spoolman_server(ctx) is None:
        return "moonraker.conf [spoolman] has no server: URL to test"
    return None


def _ui_origins(ctx: Context) -> list[str]:
    origins: list[str] = []
    for ui in ctx.webui().found:
        if ui.kind not in UI_KINDS:
            continue
        origin = origin_of(ui.url)
        if origin is not None and origin not in origins:
            origins.append(origin)
    return origins


def _cors(ctx: Context) -> Verdict:
    server = _spoolman_server(ctx)
    assert server is not None  # `applies` checked it
    origins = _ui_origins(ctx)
    if not origins:
        return Verdict.skip(
            "no Fluidd/Mainsail answered the web-UI probe: no browser origin to test (see webui-presence)"
        )
    url = server.rstrip("/") + "/api/v1/info"
    rows: list[JsonValue] = []
    missing: list[str] = []
    for origin in origins:
        got = ctx.external("GET", url, {"Origin": origin})
        acao = got.reply.headers.get("access-control-allow-origin")
        admitted = got.reply.status < 400 and acao in (origin, "*")
        rows.append(
            {
                "origin": origin,
                "status": got.reply.status,
                "access_control_allow_origin": acao,
                "admitted": admitted,
            }
        )
        if not admitted:
            missing.append(origin)
    evidence: dict[str, JsonValue] = {
        "spoolman_url": url,
        "origins": rows,
        "missing": missing,
    }
    if missing:
        return Verdict.fail(
            f"Spoolman does not admit the web UI origin(s): {', '.join(missing)}",
            "error",
            **evidence,
        )
    return Verdict.ok(f"Spoolman admits {len(origins)} web UI origin(s)", **evidence)


def _cors_steps(ctx: Context, result: CheckResult) -> list[str]:
    missing = result.evidence.get("missing")
    names = ",".join(str(m) for m in missing) if isinstance(missing, list) else ""
    return [
        "On the host that runs the PrintOps stack (compose file services/3d-api/compose.yaml, "
        "service `spoolman`), set PRINTOPS_SPOOLMAN_CORS_ORIGIN to a comma-separated list of "
        f"full origins (scheme included, no path) that includes: {names}",
        "  e.g. in the stack's .env: PRINTOPS_SPOOLMAN_CORS_ORIGIN=<existing origins>,"
        + names,
        "Recreate the container so it reads the variable: `docker compose up -d --force-recreate spoolman` "
        "(in the directory of that compose file).",
        "Explanation and defaults: services/3d-api/README.md (`SPOOLMAN_CORS_ORIGIN`).",
        f"Verify: `curl -si -H 'Origin: {names.split(',')[0] if names else '<origin>'}' "
        f"{result.evidence.get('spoolman_url')}` must show an Access-Control-Allow-Origin header.",
    ]


CHECKS = (
    Check(
        id="spoolman-link",
        title="Moonraker is connected to Spoolman with no queued usage reports",
        severity="error",
        applies_to="Moonraker printers whose /server/info lists the spoolman component",
        applies=_has_spoolman,
        run=_link,
        fix=Fix(
            "manual-only",
            "start Spoolman / fix [spoolman] server: in moonraker.conf",
            _link_steps,
        ),
    ),
    Check(
        id="spoolman-cors",
        title="Spoolman admits the browser origins the printer's web UI is served from",
        severity="error",
        applies_to="Moonraker printers with a [spoolman] server and a Fluidd/Mainsail web UI",
        applies=_cors_applies,
        run=_cors,
        fix=Fix(
            "manual-only",
            "add the origins to SPOOLMAN_CORS_ORIGIN, recreate the spoolman container",
            _cors_steps,
        ),
    ),
)
