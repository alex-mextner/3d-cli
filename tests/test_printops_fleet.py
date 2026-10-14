"""Fleet registry (fleet/printers.yaml, issue #157): schema, the device base layer, offline and
read-only printers. Lint rules: test_printops_fleet_lint.py."""

from __future__ import annotations

from pathlib import Path

import pytest
from fleet_fixtures import BAD_FLEET, REPO_FLEET, k1_entry, u1_entry, write_fleet
from printer_fakes import FakeCreality, FakeMoonraker

from errors import InvalidArgument
from printer_ops import devices
from printer_ops.base import PrinterRefused
from printops import fleet as fleet_mod
from printops.fleet import FleetError, load_fleet, validate
from registries.printers import load_printers


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    monkeypatch.chdir(tmp_path)
    return cfg


def _use(monkeypatch: pytest.MonkeyPatch, path: Path) -> Path:
    monkeypatch.setenv(fleet_mod.ENV, str(path))
    return path


# ---- the repo fleet file ------------------------------------------------------------------


def test_repo_fleet_has_every_printer_with_u1_t2_unusable() -> None:
    fleet = load_fleet(REPO_FLEET)
    assert fleet is not None
    p = fleet.printers
    assert len(p) >= 5
    assert {n for n, x in p.items() if x.access == "offline"} >= {
        "p1s",
        "a1mini",
        "xecut-a1",
    }
    assert {x.owner for x in p.values()} == {"own", "neighbour", "boris", "hackerspace"}
    u1 = p["u1"]
    assert [(h.label, h.index, h.extruder) for h in u1.heads] == [
        ("T1", 0, "extruder"),
        ("T2", 1, "extruder1"),
        ("T3", 2, "extruder2"),
        ("T4", 3, "extruder3"),
    ]
    t2 = u1.heads[1]
    assert not t2.usable and t2.reason and "queue.json" in t2.reason
    assert all(h.usable for h in u1.heads if h.label != "T2")
    assert u1.device["bridge_max_mm"] == 25 and "resume" in u1.device
    assert set(fleet.device_specs()) == {
        "u1",
        "k1se",
    }  # offline printers never reach printer_ops


def test_repo_fleet_models_are_in_the_model_registry() -> None:
    fleet = load_fleet(REPO_FLEET)
    assert fleet is not None
    models = load_printers()
    for p in fleet.printers.values():
        assert p.model in models, p.model
    assert (
        models["Snapmaker U1"].bed == [270.0, 270.0, 270.0]
        and models["Snapmaker U1"].firmware == "klipper"
    )
    assert models["Creality K1 SE"].bed == [220.0, 220.0, 250.0]


# ---- schema --------------------------------------------------------------------------------


def test_bad_fleet_reports_every_problem(tmp_path: Path) -> None:
    import yaml

    printers, problems = validate(yaml.safe_load(BAD_FLEET), tmp_path / "f.yaml")
    text = "\n".join(p.message for p in problems)
    assert "nomodel.model: required" in text
    assert (
        "offline-with-host.access: offline printers are never contacted; remove host"
        in text
    )
    assert "full-without-host.host: required for access full" in text
    assert (
        "bad-heads.owner: 'martian' is not one of own, neighbour, boris, hackerspace"
        in text
    )
    assert "bad-heads.heads[0].nozzle.material: 'gold'" in text
    assert "bad-heads.heads[1].label: 'T3'; tool index 1 is labelled T2" in text
    assert "bad-heads.heads[1].reason: required when usable is false" in text
    assert "typo.heds: unknown key" in text
    assert "typo.plates: 'velvet'" in text
    assert {p.subject for p in problems} >= {
        "nomodel",
        "offline-with-host",
        "full-without-host",
        "bad-heads",
        "typo",
    }
    # schema-valid entries still parse; model/device checks are lint rules
    assert set(printers) == {"unknown-model", "bad-device"}


def test_load_fleet_raises_exit_2_with_all_problems(tmp_path: Path) -> None:
    bad = tmp_path / "printers.yaml"
    bad.write_text(BAD_FLEET, encoding="utf-8")
    with pytest.raises(FleetError) as info:
        load_fleet(bad)
    assert info.value.exit_code == 2
    rendered = info.value.render(color=False)
    assert "schema problem(s)" in rendered and "typo.heds: unknown key" in rendered


def test_fleet_path_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(fleet_mod.ENV, "")
    assert fleet_mod.fleet_path() is None and load_fleet() is None
    _use(monkeypatch, tmp_path / "missing.yaml")
    with pytest.raises(FleetError, match="fleet file not found"):
        load_fleet()
    monkeypatch.delenv(fleet_mod.ENV)
    monkeypatch.setenv("REPO_ROOT", str(REPO_FLEET.parents[1]))
    assert fleet_mod.fleet_path() == REPO_FLEET


# ---- device base layer ---------------------------------------------------------------------


def test_fleet_is_the_device_base_layer_and_user_file_still_overrides(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    moon, k1 = FakeMoonraker(port=7001), FakeCreality(http_port=8001, ws_port=9001)
    _use(
        monkeypatch, write_fleet(tmp_path / "fleet.yaml", u1_entry(moon), k1_entry(k1))
    )
    d = devices.load_devices()
    assert set(d) == {"u1", "k1se"}  # offline printers are not devices
    assert (d["u1"].port, d["u1"].access, d["u1"].trusted_macros) == (
        7001,
        "full",
        frozenset({"T0", "T1", "T2", "T3"}),
    )
    assert d["k1se"].ws_port == 9001 and d["k1se"].source == tmp_path / "fleet.yaml"
    (env / "3d-cli").mkdir(parents=True)
    (env / "3d-cli" / "printer-devices.yaml").write_text(
        "u1: {adapter: moonraker, host: 10.1.1.1, model: Snapmaker U1}\n",
        encoding="utf-8",
    )
    d = devices.load_devices()
    assert (d["u1"].host, d["u1"].port, d["u1"].trusted_macros) == (
        "10.1.1.1",
        7125,
        frozenset(),
    )  # whole-entry replace
    assert d["k1se"].port == 8001


def test_get_device_refuses_offline_printer(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(
        monkeypatch,
        write_fleet(tmp_path / "fleet.yaml", u1_entry(FakeMoonraker(port=7001))),
    )
    with pytest.raises(PrinterRefused) as info:
        devices.get_device("p1s")
    assert "offline fleet printer: 3d never contacts it" in info.value.message
    assert "owner neighbour" in info.value.message
    with pytest.raises(InvalidArgument):
        devices.get_device("nope")


def test_offline_only_fleet_still_refuses_by_name(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, write_fleet(tmp_path / "fleet.yaml"))
    with pytest.raises(PrinterRefused):
        devices.get_device("boris")


def test_device_access_is_validated(env: Path) -> None:
    (env / "3d-cli").mkdir(parents=True)
    (env / "3d-cli" / "printer-devices.yaml").write_text(
        "u1: {adapter: moonraker, host: h, access: offline}\n", encoding="utf-8"
    )
    with pytest.raises(InvalidArgument) as info:
        devices.load_devices()
    assert info.value.exit_code == 2 and "full, read-only" in info.value.message
