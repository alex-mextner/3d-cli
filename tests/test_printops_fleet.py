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


def test_device_override_cannot_loosen_fleet_access(env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A user/project printer-devices.yaml entry replaces device settings, never the fleet's access:
    an offline printer stays uncontactable, a read-only one stays read-only (decision D7)."""
    moon = FakeMoonraker(port=7001)
    _use(monkeypatch, write_fleet(tmp_path / "fleet.yaml", u1_entry(moon, access="read-only")))
    (tmp_path / "printer-devices.yaml").write_text(
        "p1s: {adapter: moonraker, host: 127.0.0.1}\nu1: {adapter: moonraker, host: 127.0.0.1, port: 7002}\n",
        encoding="utf-8")
    d = devices.load_devices()
    assert "p1s" not in d
    assert (d["u1"].port, d["u1"].access) == (7002, "read-only")  # settings replaced, access kept
    with pytest.raises(PrinterRefused, match="offline fleet printer"):
        devices.get_device("p1s")


def test_non_string_extruder_is_a_schema_problem_not_a_crash(tmp_path: Path) -> None:
    import yaml

    data = yaml.safe_load("x:\n  model: Snapmaker U1\n  owner: own\n  access: offline\n"
                          "  heads: [{label: T1, index: 0, extruder: [extruder]}, {label: T2, index: 1, extruder: {a: 1}}]\n")
    printers, problems = validate(data, tmp_path / "f.yaml")
    assert printers == {}
    assert [p.message for p in problems] == [
        "x.heads[0].extruder: must be a Klipper object name, e.g. extruder1",
        "x.heads[1].extruder: must be a Klipper object name, e.g. extruder1",
    ]


def test_printer_names_that_collide_as_strings_are_reported(tmp_path: Path) -> None:
    import yaml

    entry = "{model: Snapmaker U1, owner: own, access: offline, heads: [{label: T1, index: 0}]}"
    printers, problems = validate(yaml.safe_load(f"1: {entry}\n'1': {entry}\n"), tmp_path / "f.yaml")
    assert [p.message for p in problems] == ["1: duplicate printer name (YAML keys 1 and '1' are the same name)"]
    assert printers == {}


def test_fleet_devices_carry_the_fleet_model(env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Device.model comes from the fleet entry: `printer vision` picks its bundled camera
    calibration by it, so a fleet-only printer must not lose it."""
    fleet = write_fleet(
        tmp_path / "fleet.yaml",
        u1_entry(FakeMoonraker(port=7001)),
        k1_entry(FakeCreality(http_port=8001, ws_port=9001)),
    )
    _use(monkeypatch, fleet)
    d = devices.load_devices()
    assert d["u1"].model == "Snapmaker U1"
    assert d["k1se"].model == "Creality K1 SE"


def test_override_without_model_keeps_the_fleet_model(env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A printer-devices.yaml override replaces the connection settings; the model is the
    printer's identity and stays the fleet's unless the override names one."""
    _use(monkeypatch, write_fleet(tmp_path / "fleet.yaml", u1_entry(FakeMoonraker(port=7001)),
                                  k1_entry(FakeCreality(http_port=8001, ws_port=9001))))
    (tmp_path / "printer-devices.yaml").write_text(
        "u1: {adapter: moonraker, host: 10.1.1.1}\nk1se: {adapter: creality-stock, host: 10.1.1.2, model: K1 SE custom}\n",
        encoding="utf-8")
    d = devices.load_devices()
    assert (d["u1"].host, d["u1"].model) == ("10.1.1.1", "Snapmaker U1")
    assert d["k1se"].model == "K1 SE custom"


def test_repo_fleet_watch_settings_reach_the_devices(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """u1/k1se `watch:` moved from ~/.config into the repo fleet; `3d printer watch` reads
    Device.watch (key tg_fallback, printer_ops/watch_run.py WATCH_KEYS), and `3d printer vision`
    reads its `vision.snapshot` (the rooted K1 SE's mjpg-streamer; the U1 uses its default)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)
    _use(monkeypatch, REPO_FLEET)
    d = devices.load_devices()
    assert d["u1"].watch["tg_fallback"] is True and d["u1"].watch["bed"]["enabled"] is True
    assert d["k1se"].watch["tg_fallback"] is True
    assert d["k1se"].watch["vision"] == {"snapshot": "http://192.168.0.27:8080/?action=snapshot"}
    assert d["k1se"].watch["bed"]["roi"][0] == [0.31, 0.48]
