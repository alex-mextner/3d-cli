"""fleet_fixtures.py — fleet/printers.yaml fixtures for the fleet tests (issue #157).

`write_fleet` builds a fleet whose contactable printers point at the in-process fakes of
printer_fakes.py, plus two offline entries; BAD_FLEET breaks one schema rule per entry so a
test can assert that `3d lint --printops` reports every one of them.
"""

from __future__ import annotations

from pathlib import Path

from printer_fakes import FakeCreality, FakeMoonraker

REPO_FLEET = Path(__file__).resolve().parents[1] / "fleet" / "printers.yaml"

OFFLINE = """\
p1s:
  model: Bambu Lab P1S
  owner: neighbour
  access: offline
  heads: [{label: T1, index: 0, usable: true}]
  enclosure: full
boris:
  model: Bambu Lab A1 mini
  owner: boris
  access: offline
  heads: [{label: T1, index: 0}]
  ams: [{id: lite, kind: ams_lite, slots: 4}]
"""

BAD_FLEET = """\
nomodel:
  owner: own
  access: offline
  heads: [{label: T1, index: 0}]
offline-with-host:
  model: Bambu Lab P1S
  owner: neighbour
  access: offline
  host: 10.0.0.5
  heads: [{label: T1, index: 0}]
full-without-host:
  model: Snapmaker U1
  owner: own
  access: full
  adapter: moonraker
  heads: [{label: T1, index: 0}]
bad-heads:
  model: Snapmaker U1
  owner: martian
  access: offline
  heads:
    - {label: T1, index: 0, nozzle: {diameter_mm: 0.4, material: gold}}
    - {label: T3, index: 1, usable: false}
typo:
  model: Snapmaker U1
  owner: own
  access: offline
  heds: []
  heads: [{label: T1, index: 0}]
  plates: [velvet]
unknown-model:
  model: Prusa XL
  owner: hackerspace
  access: offline
  heads: [{label: T1, index: 0}]
bad-device:
  model: Snapmaker U1
  owner: own
  access: full
  adapter: octoprint
  host: 10.0.0.6
  heads: [{label: T1, index: 0}]
"""


def u1_entry(moon: FakeMoonraker, access: str = "full", heads: int = 4) -> str:
    """The real U1 shape (4 heads, T2 broken) pointing at a FakeMoonraker."""
    rows = [
        f"    - {{label: T{i + 1}, index: {i}, extruder: extruder{i or ''}, "
        f"nozzle: {{diameter_mm: 0.4, material: stainless, type: std}}"
        + (", usable: false, reason: T2 is broken}" if i == 1 else ", usable: true}")
        for i in range(heads)
    ]
    return "\n".join(
        [
            "u1:",
            "  model: Snapmaker U1",
            "  owner: own",
            f"  access: {access}",
            "  adapter: moonraker",
            "  host: 127.0.0.1",
            f"  port: {moon.port}",
            "  trusted_macros: [T0, T1, T2, T3]",
            "  heads:",
            *rows,
            "",
        ]
    )


def k1_entry(k1: FakeCreality) -> str:
    return "\n".join(
        [
            "k1se:",
            "  model: Creality K1 SE",
            "  owner: own",
            "  access: full",
            "  adapter: creality-stock",
            "  host: 127.0.0.1",
            f"  port: {k1.http_port}",
            f"  ws_port: {k1.ws_port}",
            "  heads: [{label: T1, index: 0, extruder: extruder, nozzle: {diameter_mm: 0.4, material: hardened}}]",
            "",
        ]
    )


def write_fleet(path: Path, *entries: str) -> Path:
    """A fleet file of `entries` + the two offline printers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(entries) + OFFLINE, encoding="utf-8")
    return path
