"""Fakes for `3d printer macro|aliases|ALIAS` tests: a fake Moonraker that also answers macro discovery, and a
fleet file whose contactable printers point at loopback fakes (never at a real address). Issue #718."""

from __future__ import annotations

from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from printer_fakes import FakeMoonraker, Recorded

REPO = Path(__file__).resolve().parent.parent


@dataclass
class FakeMoonrakerMacros(FakeMoonraker):
    """FakeMoonraker + `/printer/objects/list` and `/printer/gcode/help` (Moonraker's macro discovery)."""

    macros: dict[str, str] = field(
        default_factory=dict
    )  # `[gcode_macro NAME]` -> description
    commands: dict[str, str] = field(
        default_factory=dict
    )  # other extended commands -> help text

    def handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self
        base = super().handler()

        class H(base):  # type: ignore[valid-type, misc]
            def do_GET(self) -> None:
                u = urlsplit(self.path)
                if u.path == "/printer/objects/list":
                    fake.requests.append(Recorded("GET", u.path, parse_qs(u.query)))
                    objects = [
                        "webhooks",
                        "print_stats",
                        "toolhead",
                        "extruder",
                        "heater_bed",
                        *(f"gcode_macro {m}" for m in fake.macros),
                    ]
                    self._reply({"result": {"objects": objects}})  # type: ignore[attr-defined]
                elif u.path == "/printer/gcode/help":
                    fake.requests.append(Recorded("GET", u.path, parse_qs(u.query)))
                    self._reply({"result": {**fake.commands, **fake.macros}})  # type: ignore[attr-defined]
                else:
                    super().do_GET()

        return H


def write_fleet(
    tmp_path: Path, access: dict[str, str] | None = None, **fakes: FakeMoonraker
) -> Path:
    """A copy of the repo's fleet/printers.yaml whose contactable printers are the named fakes (`u1=moon`): their
    host/port point at the loopback fake and every other contactable entry is dropped, so no test can reach a real
    address. Offline fleet printers stay as they are (never contacted); `access` overrides the access per printer."""
    import yaml

    data = yaml.safe_load(
        (REPO / "fleet" / "printers.yaml").read_text(encoding="utf-8")
    )
    for name in list(data):
        if data[name].get("access") == "offline":
            continue
        if name not in fakes:
            del data[name]
            continue
        data[name].update(host="127.0.0.1", port=fakes[name].port)
        if access and name in access:
            data[name]["access"] = access[name]
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path
