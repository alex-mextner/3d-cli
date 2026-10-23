"""Fleet-driven, not hardcoded: nothing that works for one printer may be special-cased in generic code.

(a) an AST scan: no string constant in lib/printer_doctor/** or lib/printers_cli/doctor.py equals a
    printer name or model, or contains a host, of the REAL fleet/printers.yaml (docstrings excluded);
(b) every printer of the real fleet registry (iterated, never listed here) runs through the engine
    against loopback fakes built from its adapter kind: no crash, a sensible applies/skip decision
    per adapter kind, head count and access, and an offline printer is never contacted.
"""

from __future__ import annotations

import ast
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fleet_fixtures import REPO_FLEET
from printer_doctor.context import Context, Services
from printer_doctor.engine import NEVER_CONTACTED, Options, run_printer
from printer_doctor.registry import discover_checks
from printer_doctor.targets import Target
from printer_doctor_fakes import (
    FakeApi,
    ManualClock,
    agent_sources,
    printer_view,
    subscription_sources,
)
from printer_fakes import FakeCreality, FakeMoonraker
from printer_ops.devices import Device, fleet_devices
from printer_ops.webui import WebUiProbe
from printops.fleet import Fleet, FleetPrinter, load_fleet

ROOT = Path(__file__).resolve().parents[1]
GENERIC = [
    *sorted((ROOT / "lib" / "printer_doctor").rglob("*.py")),
    ROOT / "lib" / "printers_cli" / "doctor.py",
]
_LOADED = load_fleet(REPO_FLEET)
assert _LOADED is not None and _LOADED.printers
FLEET: Fleet = _LOADED
PRINTERS = sorted(FLEET.printers)
MOONRAKER_ONLY = {
    "klippy-state", "moonraker-components", "trusted-clients-width", "spoolman-link", "spoolman-cors",
    "active-spool-binding", "print-stuck", "disk-space", "webui-presence",
}  # fmt: skip


def _fleet_values(fleet: Fleet) -> tuple[set[str], set[str]]:
    """(exact values that must not appear as a literal: names + models, host strings)."""
    exact: set[str] = set()
    hosts: set[str] = set()
    for p in fleet.printers.values():
        exact |= {p.name, p.model}
        host = p.device.get("host")
        if isinstance(host, str):
            hosts.add(host)
    return exact, hosts


def _string_constants(path: Path) -> Iterator[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
            ):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            yield node.lineno, node.value


def test_no_fleet_printer_name_model_or_host_is_a_literal_in_generic_code() -> None:
    assert GENERIC and len(GENERIC) > 10
    exact, hosts = _fleet_values(FLEET)
    assert exact and hosts, (
        "the real fleet must have printers with names, models and hosts"
    )
    found = [
        f"{path.relative_to(ROOT)}:{line}: {value!r}"
        for path in GENERIC
        for line, value in _string_constants(path)
        if value.strip() in exact or any(h in value for h in hosts)
    ]
    assert found == []


def test_the_scan_catches_a_planted_literal(tmp_path: Path) -> None:
    exact, _ = _fleet_values(FLEET)
    name = sorted(exact)[0]
    planted = tmp_path / "x.py"
    planted.write_text(f'"""doc {name}"""\nVALUE = {name!r}\n')
    assert [v for _, v in _string_constants(planted) if v in exact] == [name]


# ---- (b) every fleet printer through the engine ---------------------------------------------


@pytest.fixture(autouse=True)
def only_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real fleet names LAN hosts: nothing in this file may ever open a socket to them."""
    real = socket.socket.connect

    def guarded(self: socket.socket, address: Any) -> None:
        host = address[0] if isinstance(address, tuple) else address
        assert host in ("127.0.0.1", "::1", "localhost"), (
            f"non-loopback connect to {address!r}"
        )
        real(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


def _loopback_device(
    printer: FleetPrinter, adapter: str, port: int, ws_port: int | None
) -> Device:
    real = fleet_devices(FLEET)[printer.name]
    return Device(
        printer.name,
        adapter,
        "127.0.0.1",
        port,
        None,
        real.source,
        ws_port,
        access=printer.access,
    )  # no model, no watch: no camera, so no URL of the real fleet is ever used


@pytest.mark.parametrize("name", PRINTERS)
def test_every_fleet_printer_runs_through_every_check_without_crashing(
    name: str,
) -> None:
    printer = FLEET.printers[name]
    adapter = printer.device.get("adapter")
    clock = ManualClock()
    api = FakeApi(routes={f"/v1/printers/{name}": printer_view(name, heads=len(printer.heads)),
                          "/v1/anomalies": {"anomalies": []}, f"/v1/printers/{name}/faults": {"faults": []}})  # fmt: skip
    services = Services(
        clock=clock, sleep=clock.sleep, api=lambda: api.client(), journal=lambda e, a: "buffered",
        systemctl_show=lambda unit: "", webui=lambda d: WebUiProbe(), wake_camera=lambda d, s: None,
        timeout=3.0, listen_s=0.3, subscriptions=subscription_sources(), agents=agent_sources(),
    )  # fmt: skip
    checks = discover_checks()
    if not printer.contactable:
        target = Target(name, printer, None, None)
        report = run_printer(Context(target, services), checks, Options())
        for rec in report.records:
            assert rec.result.status != "error", rec.result
            if rec.result.check in {c.id for c in checks if c.contacts_printer}:
                assert (rec.result.status, rec.result.summary) == (
                    "skip",
                    NEVER_CONTACTED,
                )
        assert all(m == "GET" for m, _ in api.requests)
        assert [
            r for r in api.requests if "/v1/printers/" in r[1] and "faults" not in r[1]
        ] == []  # slots: never polled
        return
    fake: FakeMoonraker | FakeCreality = (
        FakeMoonraker(components=["file_manager"])
        if adapter == "moonraker"
        else FakeCreality()
    )
    with fake.serve():
        port = fake.port if isinstance(fake, FakeMoonraker) else fake.http_port
        ws = fake.ws_port if isinstance(fake, FakeCreality) else None
        device = _loopback_device(printer, str(adapter), port, ws)
        target = Target(name, printer, device, fleet_devices(FLEET).get(name))
        report = run_printer(Context(target, services), checks, Options())
    by = {rec.result.check: rec.result for rec in report.records}
    assert set(by) == {c.id for c in checks}
    for res in by.values():
        assert res.status != "error" or "check crashed" not in res.summary, res
    assert by["reachable"].status == "pass", by["reachable"]
    for cid in MOONRAKER_ONLY:
        if adapter == "moonraker":
            assert "not Moonraker" not in by[cid].summary, (cid, by[cid])
        else:
            assert by[cid].status == "skip" and "not Moonraker" in by[cid].summary, (
                cid,
                by[cid],
            )
    if adapter == "moonraker":
        # skip decided from the fleet entry's head count, never from the name
        heads = len(printer.heads)
        spool = by["active-spool-binding"]
        assert spool.status == "skip" and (
            "heads and Moonraker keeps ONE" in spool.summary
            if heads != 1
            else "[spoolman]" in spool.summary
        )
    assert by["spoolman-link"].status == "skip"  # the fake has no spoolman component
    assert (
        by["camera-snapshot"].status == "skip"
        and "no camera" in by["camera-snapshot"].summary
    )
    assert fake.writes() == []  # diagnosis is read-only


def test_the_real_fleet_has_offline_and_contactable_printers_so_both_paths_are_exercised() -> (
    None
):
    access = {p.access for p in FLEET.printers.values()}
    assert "offline" in access and access & {"full", "read-only"}
