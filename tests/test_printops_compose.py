"""services/3d-api/compose.yaml: Spoolman 0.26+ rejects browser requests from an Origin it does not know,
and Fluidd calls Spoolman from the browser. The compose file must list every Fluidd origin of the fleet in
`SPOOLMAN_CORS_ORIGIN`, else Fluidd shows "Spoolman is blocking Fluidd's browser origin"."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from fleet_fixtures import REPO_FLEET

from printops.fleet import load_fleet

COMPOSE = Path(__file__).resolve().parents[1] / "services" / "3d-api" / "compose.yaml"
VARIABLE = re.compile(r"\$\{PRINTOPS_SPOOLMAN_CORS_ORIGIN:-([^}]*)\}")
K1_FLUIDD = "http://192.168.0.27:4408"


def _origins() -> list[str]:
    spoolman = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["spoolman"]
    raw = spoolman["environment"].get("SPOOLMAN_CORS_ORIGIN")
    assert raw, "the spoolman service sets no SPOOLMAN_CORS_ORIGIN"
    match = VARIABLE.fullmatch(raw)
    assert match, f"SPOOLMAN_CORS_ORIGIN must be ${{PRINTOPS_SPOOLMAN_CORS_ORIGIN:-<default>}}, got {raw!r}"
    return match.group(1).split(",")


def test_spoolman_admits_the_k1_se_and_u1_fluidd_origins() -> None:
    origins = _origins()
    assert K1_FLUIDD in origins and "http://192.168.0.26" in origins


def test_every_origin_is_a_bare_scheme_host_port_origin() -> None:
    """Spoolman parses full origins: a path, a trailing slash or a missing scheme never matches."""
    for origin in _origins():
        parts = urlsplit(origin)
        assert parts.scheme in ("http", "https") and parts.hostname, origin
        assert origin == f"{parts.scheme}://{parts.netloc}", origin


def test_every_contactable_moonraker_printer_of_the_fleet_has_an_origin() -> None:
    fleet = load_fleet(REPO_FLEET)
    assert fleet is not None
    hosts = {urlsplit(o).hostname for o in _origins()}
    moonraker = {
        name: urlsplit("//" + str(p.device.get("host", ""))).hostname
        for name, p in fleet.printers.items()
        if p.contactable and p.device.get("adapter") == "moonraker"
    }
    assert moonraker, "the repo fleet lists no Moonraker printer"
    assert {name for name, host in moonraker.items() if host not in hosts} == set()
