"""trusted_clients.py — how wide is Moonraker's `[authorization]` trust?

CHECK `trusted-clients-width` reads `GET /server/config` -> `config.authorization` and warns when
  trusted_clients lets a lot more than the home LAN skip authentication: Moonraker serves
  trusted clients without any login and with full control (G-code, restarts, file upload).
RULE per trusted_clients entry (parsed with `ipaddress`, strict=False):
  - loopback (127.0.0.0/8, ::1/128) and link-local (169.254.0.0/16, fe80::/10) are fine: the
    printer itself and its own link;
  - an IPv4 network WIDER than /16 (prefix < 16) is flagged: 10.0.0.0/8 admits 16.7 million
    addresses, 172.16.0.0/12 a million, 192.0.0.0/8 includes public space. A /16 and narrower
    (192.168.0.0/16, a /24 LAN) pass;
  - an IPv4 network outside the private ranges is flagged (a public range is trusted);
  - IPv6: the unspecified/default network (::/0) and global-unicast ranges are flagged, unique-local
    (fc00::/7) is private and passes;
  - a bare IP address (/32) is a single host and passes; a domain name cannot be sized and is
    listed as `unchecked`.
  `cors_domains` containing a bare `*` is flagged too: any website open in a browser on the LAN
  could drive the printer.
The real lists this was written against (read-only GETs, 2026-10-23): a Snapmaker U1 trusts
  172.18.0.0/16, 10.0.0.0/8, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.0.0.0/8, FE80::/10,
  ::1/128 (flagged: 10.0.0.0/8, 172.16.0.0/12, 192.0.0.0/8); a rooted K1 SE trusts 10.0.0.0/8,
  127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16, FE80::/10, ::1/128 (flagged:
  10.0.0.0/8, 172.16.0.0/12). Pinned in tests/test_printer_doctor_checks_config.py.
"""

from __future__ import annotations

import ipaddress

from printer_doctor.checks._common import str_list
from printer_doctor.context import Context
from printer_doctor.model import Check, CheckResult, Fix, JsonValue, Verdict

MAX_PREFIX_V4_WIDTH = 16


def classify(entry: str) -> tuple[str, str | None]:
    """(`ok` | `flag` | `unchecked`, reason) of one trusted_clients entry."""
    try:
        net = ipaddress.ip_network(entry.strip(), strict=False)
    except ValueError:
        return "unchecked", "not an IP network (a name is not sized)"
    if net.is_loopback or net.is_link_local:
        return "ok", None
    if isinstance(net, ipaddress.IPv4Network):
        if net.prefixlen == 0:
            return "flag", "0.0.0.0/0 trusts every address"
        if net.prefixlen < MAX_PREFIX_V4_WIDTH:
            return (
                "flag",
                f"/{net.prefixlen} is wider than /{MAX_PREFIX_V4_WIDTH} ({net.num_addresses:,} addresses)",
            )
        if not net.is_private:
            return "flag", "public address range"
        return "ok", None
    if net.prefixlen == 0:
        return "flag", "::/0 trusts every address"
    if not net.is_private:
        return "flag", "global (public) IPv6 range"
    return "ok", None


def _width(ctx: Context) -> Verdict:
    auth = ctx.config_section("authorization")
    if auth is None:
        return Verdict.ok(
            "no [authorization] section: nothing is trusted without a login"
        )
    entries = str_list(auth.get("trusted_clients"))
    cors = str_list(auth.get("cors_domains"))
    flagged: list[JsonValue] = []
    unchecked: list[str] = []
    for entry in entries:
        verdict, reason = classify(entry)
        if verdict == "flag":
            flagged.append({"entry": entry, "reason": reason})
        elif verdict == "unchecked":
            unchecked.append(entry)
    open_cors = [c for c in cors if c.strip() == "*"]
    if open_cors:
        flagged.append(
            {
                "entry": "cors_domains: *",
                "reason": "any website may call the API from a browser",
            }
        )
    evidence: dict[str, JsonValue] = {
        "trusted_clients": entries,
        "cors_domains": cors,
        "flagged": flagged,
        "unchecked": unchecked,
    }
    if flagged:
        names = ", ".join(str(f["entry"]) for f in flagged if isinstance(f, dict))
        return Verdict.fail(
            f"{len(flagged)} overly wide trust entr{'y' if len(flagged) == 1 else 'ies'}: {names}",
            "warn",
            **evidence,
        )
    return Verdict.ok(
        f"{len(entries)} trusted_clients entr{'y' if len(entries) == 1 else 'ies'}, none wider than /16",
        **evidence,
    )


def _steps(ctx: Context, result: CheckResult) -> list[str]:
    flagged = result.evidence.get("flagged")
    names = (
        [str(f["entry"]) for f in flagged if isinstance(f, dict)]
        if isinstance(flagged, list)
        else []
    )
    return [
        f"Edit `[authorization]` in moonraker.conf (the `config` file root; Fluidd/Mainsail "
        f"'Configuration'): narrow `trusted_clients` to your LAN, e.g. one /24 or /16 per line, "
        f"and remove: {', '.join(names) or 'the flagged entries'}.",
        "Replace a bare `*` in `cors_domains` with the exact origins of your web UIs (scheme://host:port).",
        "Restart Moonraker (web UI Machine tab -> moonraker). On some firmware the config root is "
        "read-only or regenerated on boot: then the change must be made in the firmware's own "
        "config template (see the printer's runbook in docs/printers/).",
        "Make sure you can still log in / reach the UI from your usual machines before closing the session.",
        "Moonraker docs: https://moonraker.readthedocs.io/en/latest/configuration/#authorization",
    ]


CHECKS = (
    Check(
        id="trusted-clients-width",
        title="Moonraker trusted_clients / cors_domains are not wider than the LAN",
        severity="warn",
        applies_to="Moonraker-family printers",
        applies=lambda ctx: ctx.moonraker_only(),
        run=_width,
        fix=Fix(
            "manual-only",
            "narrow [authorization] trusted_clients / cors_domains, restart Moonraker",
            _steps,
        ),
    ),
)
