"""`3d printer -n NAME macro list|run`, `aliases` and the alias subcommands (home, load, park, ...).

Every printer is a loopback fake Moonraker (tests/printer_fakes.py) built from the REAL fleet entries of
`u1` and `k1se` (fleet/printers.yaml with the address swapped for the fake's); a refused command is proven
by the fake having received NO write (often no request at all). Issue #718.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from printer_fakes import FakeCreality, u1_status, write_devices
from printer_macro_fakes import FakeMoonrakerMacros, write_fleet

from commands.printer import USAGE, run
from errors import InvalidArgument, ThreeDError, UsageError
from printer_ops import aliases, macros
from printer_ops.base import PrinterRefused, UnsupportedOperation

REPO = Path(__file__).resolve().parent.parent

U1_MACROS = {
    name: f"{name} macro"
    for name in (
        "PAUSE",
        "RESUME",
        "CANCEL_PRINT",
        "M600",
        "T0",
        "T1",
        "BED_MESH_CALIBRATE",
        "ROUGHLY_CLEAN_NOZZLE",
        "AUTO_FEEDING",
        "SM_PRINT_AUTO_FEED",
        "AUTO_BED_MESH_CALIBRATE",
        "SM_PRINT_FLOW_CALIBRATE",
        "_CLIENT_VARIABLE",
    )
}
U1_COMMANDS = {
    name: f"{name} command"
    for name in (
        "SET_HEATER_TEMPERATURE",
        "TURN_OFF_HEATERS",
        "PARK_EXTRUDER",
        "PARK_EXTRUDER1",
        "PARK_EXTRUDER2",
        "MOVE_TO_XY_IDLE_POSITION_EXTRUDER",
        "SET_PURIFIER_MODE",
        "FIRMWARE_RESTART",
        "GET_POSITION",
        "BED_MESH_CLEAR",
    )
}
K1_MACROS = {
    name: f"{name} macro"
    for name in (
        "PAUSE",
        "RESUME",
        "LOAD_MATERIAL",
        "QUIT_MATERIAL",
        "G29",
        "START_PRINT",
        "END_PRINT",
    )
}
K1_COMMANDS = {
    name: f"{name} command" for name in ("SET_HEATER_TEMPERATURE", "TURN_OFF_HEATERS")
}


class Shop:
    """The two shop printers as fakes, plus a way to run `3d printer ...` and read what it printed."""

    def __init__(
        self, u1: FakeMoonrakerMacros, k1se: FakeMoonrakerMacros, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self.u1, self.k1se, self.capsys = u1, k1se, capsys

    def out(self, *argv: str) -> str:
        assert run(list(argv)) == 0
        return self.capsys.readouterr().out

    def fails(self, exc: type[ThreeDError], *argv: str) -> ThreeDError:
        with pytest.raises(exc) as info:
            run(list(argv))
        return info.value


@pytest.fixture()
def shop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Iterator[Shop]:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)
    u1 = FakeMoonrakerMacros(macros=dict(U1_MACROS), commands=dict(U1_COMMANDS))
    k1 = FakeMoonrakerMacros(macros=dict(K1_MACROS), commands=dict(K1_COMMANDS))
    with u1.serve(), k1.serve():
        monkeypatch.setenv("THREED_FLEET", str(write_fleet(tmp_path, u1=u1, k1se=k1)))
        yield Shop(u1, k1, capsys)


def use_fleet(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **kw: Any) -> None:
    monkeypatch.setenv("THREED_FLEET", str(write_fleet(tmp_path, **kw)))


# ---- macro list -----------------------------------------------------------------------------


def test_macro_list_reads_the_printer_and_marks_trusted_macros(shop: Shop) -> None:
    text = shop.out("-n", "u1", "macro", "list")

    assert "u1: 12 macros defined by the printer, 1 private (_NAME) hidden" in text
    assert any(
        line.split()[:2] == ["SM_PRINT_AUTO_FEED", "*"] for line in text.splitlines()
    )  # in trusted_macros
    assert any(
        line.split()[:1] == ["AUTO_FEEDING"] and "*" not in line.split()[:2]
        for line in text.splitlines()
    )
    assert "_CLIENT_VARIABLE" not in text
    assert [r.path for r in shop.u1.requests] == [
        "/printer/objects/list",
        "/printer/gcode/help",
    ]
    assert shop.u1.writes() == []


def test_macro_list_all_json_includes_private_macros(shop: Shop) -> None:
    rows = json.loads(shop.out("-n", "k1se", "macro", "list", "--all", "--json"))

    assert {r["name"] for r in rows} == set(K1_MACROS)
    start = next(r for r in rows if r["name"] == "START_PRINT")
    assert start["trusted"] is True and start["description"] == "START_PRINT macro"
    assert next(r for r in rows if r["name"] == "LOAD_MATERIAL")["trusted"] is False


def test_macro_list_offline_uses_registry_data_and_contacts_nothing(shop: Shop) -> None:
    text = shop.out("-n", "u1", "macro", "list", "--offline", "--json")
    names = {r["name"] for r in json.loads(text)}

    assert {
        "SM_PRINT_AUTO_FEED",
        "DETECT_BED_PLATE",
    } <= names  # trusted_macros of fleet/printers.yaml
    assert {"AUTO_FEEDING", "ROUGHLY_CLEAN_NOZZLE", "G28"} <= names  # alias targets
    assert shop.u1.requests == []


def test_macro_list_dry_run_prints_the_requests_and_sends_none(shop: Shop) -> None:
    text = shop.out("-n", "u1", "macro", "list", "--dry-run")

    assert f"GET http://127.0.0.1:{shop.u1.port}/printer/objects/list" in text
    assert f"GET http://127.0.0.1:{shop.u1.port}/printer/gcode/help" in text
    assert "dry-run: nothing was sent" in text
    assert shop.u1.requests == []


def test_macro_list_of_an_offline_fleet_printer_uses_the_registry(shop: Shop) -> None:
    text = shop.out("-n", "p1s", "macro", "list")

    assert (
        "macros known from the registry (nothing contacted" in text and "M620" in text
    )
    assert shop.u1.requests == [] and shop.k1se.requests == []


# ---- macro run ------------------------------------------------------------------------------


def test_macro_run_passes_params_and_prints_exactly_the_line_sent(shop: Shop) -> None:
    text = shop.out("-n", "u1", "macro", "run", "sm_print_auto_feed", "extruder=1")

    assert shop.u1.scripts == ["SM_PRINT_AUTO_FEED EXTRUDER=1"]
    assert "gcode:\n  SM_PRINT_AUTO_FEED EXTRUDER=1\n" in text
    assert "sent to u1" in text


def test_macro_run_dry_run_prints_the_request_and_sends_nothing(shop: Shop) -> None:
    text = shop.out(
        "-n", "u1", "macro", "run", "SM_PRINT_AUTO_FEED", "EXTRUDER=1", "--dry-run"
    )

    assert (
        f"POST http://127.0.0.1:{shop.u1.port}/printer/gcode/script?script=SM_PRINT_AUTO_FEED%20EXTRUDER%3D1"
        in text
    )
    assert "dry-run: nothing was sent" in text
    assert shop.u1.scripts == [] and shop.u1.writes() == []


def test_macro_outside_trusted_macros_needs_yes(shop: Shop) -> None:
    err = shop.fails(
        UsageError, "-n", "u1", "macro", "run", "AUTO_FEEDING", "EXTRUDER=0"
    )
    dry = shop.out(
        "-n", "u1", "macro", "run", "AUTO_FEEDING", "EXTRUDER=0", "--dry-run"
    )
    assert shop.u1.writes() == []

    shop.out("-n", "u1", "macro", "run", "AUTO_FEEDING", "EXTRUDER=0", "--yes")

    assert (
        "needs --yes" in err.message
        and "AUTO_FEEDING is not in this printer's trusted_macros" in err.message
    )
    assert "would need --yes" in dry
    assert shop.u1.scripts == ["AUTO_FEEDING EXTRUDER=0"]


def test_dangerous_macro_needs_yes_even_when_trusted(
    shop: Shop, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "xdg" / "3d-cli"
    cfg.mkdir(parents=True)
    (cfg / "printer-devices.yaml").write_text(
        f"u1: {{adapter: moonraker, host: 127.0.0.1, port: {shop.u1.port}, model: Snapmaker U1, "
        "trusted_macros: [FIRMWARE_RESTART]}\n",
        encoding="utf-8",
    )

    err = shop.fails(UsageError, "-n", "u1", "macro", "run", "FIRMWARE_RESTART")

    assert "FIRMWARE_RESTART is dangerous" in err.message
    assert shop.u1.writes() == []


def test_motion_macro_is_refused_while_printing_but_pause_style_is_not(
    shop: Shop, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shop.u1.status = u1_status("printing", "part.gcode", 0.4)

    err = shop.fails(
        PrinterRefused,
        "-n",
        "u1",
        "macro",
        "run",
        "AUTO_FEEDING",
        "EXTRUDER=0",
        "--yes",
    )
    dry = shop.fails(
        PrinterRefused,
        "-n",
        "u1",
        "macro",
        "run",
        "AUTO_FEEDING",
        "EXTRUDER=0",
        "--yes",
        "--dry-run",
    )
    assert shop.u1.writes() == []
    shop.out(
        "-n", "u1", "macro", "run", "PAUSE", "--yes"
    )  # PAUSE is a job macro, but not in this printer's trusted list
    shop.out(
        "-n", "u1", "macro", "run", "M600"
    )  # trusted + job class: no --yes, no idle printer needed

    for e in (err, dry):
        assert "is printing" in e.message and "needs an idle printer" in e.message
    assert shop.u1.scripts == ["PAUSE", "M600"]


def test_paused_and_unreadable_printers_count_as_busy(shop: Shop) -> None:
    shop.u1.status = u1_status("paused", "part.gcode", 0.4)
    assert (
        "is paused"
        in shop.fails(
            PrinterRefused, "-n", "u1", "macro", "run", "AUTO_FEEDING", "--yes"
        ).message
    )
    shop.u1.status = u1_status("error")
    shop.u1.status["webhooks"]["state"] = (
        "shutdown"  # Klippy shut down: the state reads unknown
    )
    assert shop.fails(PrinterRefused, "-n", "u1", "home").message.endswith(
        "needs an idle printer"
    )
    assert shop.u1.writes() == []


def test_read_only_printer_refuses_every_write_but_can_be_listed(
    shop: Shop, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fleet(monkeypatch, tmp_path, access={"u1": "read-only"}, u1=shop.u1)

    run_err = shop.fails(
        PrinterRefused, "-n", "u1", "macro", "run", "SM_PRINT_AUTO_FEED", "EXTRUDER=0"
    )
    alias_err = shop.fails(PrinterRefused, "-n", "u1", "home")
    dry_err = shop.fails(PrinterRefused, "-n", "u1", "home", "--dry-run")
    listed = shop.out("-n", "u1", "macro", "list")

    for e in (run_err, alias_err, dry_err):
        assert "is read-only" in e.message and "refused" in e.message
    assert shop.u1.writes() == [] and "SM_PRINT_AUTO_FEED" in listed


def test_offline_fleet_printer_refuses_run_and_aliases_and_is_never_contacted(
    shop: Shop,
) -> None:
    errs = [
        shop.fails(PrinterRefused, "-n", "p1s", *cmd)
        for cmd in (("macro", "run", "M620"), ("home",), ("pause", "--dry-run"))
    ]

    assert all("is offline" in e.message for e in errs)
    assert shop.u1.requests == [] and shop.k1se.requests == []


def test_macro_names_and_values_that_could_inject_a_second_command_are_rejected_before_any_contact(
    shop: Shop,
) -> None:
    bad_names = [
        "PAUSE\nM112",
        "PAUSE;M112",
        "PAUSE M112",
        "G1 X1 Y1",
        "",
        "1PAUSE",
        "PAUSE\r",
        "SM-PRINT",
        "ÅÄÖ",
        "A" * 65,
    ]
    bad_values = [
        "1;M112",
        "1\nM112",
        "a b",
        "'x'",
        '"x"',
        "{1}",
        "%25",
        "1#c",
        "1*3",
        "",
        "x" * 65,
    ]
    for name in bad_names:
        with pytest.raises(InvalidArgument):
            run(["-n", "u1", "macro", "run", name])
    for value in bad_values:
        with pytest.raises(InvalidArgument):
            run(["-n", "u1", "macro", "run", "SM_PRINT_AUTO_FEED", f"EXTRUDER={value}"])
    for word in ("EXTRUDER", "=1", "1EXTRUDER=1", "EXT RUDER=1"):
        with pytest.raises(InvalidArgument):
            run(["-n", "u1", "macro", "run", "SM_PRINT_AUTO_FEED", word])
    with pytest.raises(InvalidArgument):
        run(
            [
                "-n",
                "u1",
                "macro",
                "run",
                "SM_PRINT_AUTO_FEED",
                "EXTRUDER=1",
                "extruder=2",
            ]
        )  # a key twice

    assert shop.u1.requests == []


@pytest.mark.parametrize(
    "argv",
    [
        ["macro", "run", "M112"],
        ["macro", "run", "m112"],
        ["macro", "run", "EMERGENCY_STOP"],
        ["macro", "run", "SM_PRINT_AUTO_FEED", "EXTRUDER=M112"],
        ["macro", "run", "SM_PRINT_AUTO_FEED", "MSG=a,m112"],
        ["macro", "run", "SM_PRINT_AUTO_FEED", "MSG=EMERGENCY_STOP", "--yes"],
    ],
)
def test_the_emergency_stop_is_never_sent_whatever_the_flags(
    shop: Shop, argv: list[str]
) -> None:
    err = shop.fails(PrinterRefused, "-n", "u1", *argv, "--yes")

    assert "3d never sends the emergency stop" in err.message
    assert shop.u1.requests == []


def test_a_standard_gcode_is_not_a_macro(shop: Shop) -> None:
    err = shop.fails(PrinterRefused, "-n", "u1", "macro", "run", "G28", "--yes")
    shop.fails(PrinterRefused, "-n", "u1", "macro", "run", "M104", "S=200", "--yes")

    assert "standard G-code, not a macro u1 defines" in err.message
    assert shop.u1.writes() == []


def test_a_gcode_macro_that_overrides_a_standard_code_runs_as_a_macro(
    shop: Shop,
) -> None:
    shop.out(
        "-n", "k1se", "macro", "run", "G29", "BED_TEMP=60", "--yes"
    )  # the K1's stock bed-mesh macro

    assert shop.k1se.scripts == ["G29 BED_TEMP=60"]


def test_an_undefined_name_is_refused_with_a_hint(shop: Shop) -> None:
    err = shop.fails(PrinterRefused, "-n", "u1", "macro", "run", "PAUSEE", "--yes")

    assert err.message == "u1 does not define PAUSEE" and "PAUSE" in err.remediation[0]
    assert shop.u1.writes() == []


def test_offline_run_prints_the_script_and_contacts_nothing(shop: Shop) -> None:
    text = shop.out(
        "-n", "u1", "macro", "run", "AUTO_FEEDING", "EXTRUDER=0", "--offline"
    )

    assert (
        "AUTO_FEEDING EXTRUDER=0" in text and "offline: nothing was contacted" in text
    )
    assert shop.u1.requests == []


def test_there_is_no_raw_gcode_passthrough(shop: Shop) -> None:
    err = shop.fails(UsageError, "-n", "u1", "gcode", "G28")

    assert "unknown subcommand 'gcode'" in err.message or "alias `gcode`" in err.message
    assert shop.u1.requests == []


# ---- aliases --------------------------------------------------------------------------------


def test_home_sends_g28_or_only_the_named_axes(shop: Shop) -> None:
    shop.out("-n", "k1se", "home")
    shop.out("-n", "k1se", "home", "xy")
    shop.out("-n", "k1se", "home", "--axes", "Z")

    assert shop.k1se.scripts == ["G28", "G28 X Y", "G28 Z"]
    for bad in ("XX", "A", "X Y"):
        with pytest.raises(ThreeDError):
            run(["-n", "k1se", "home", bad])
    assert len(shop.k1se.scripts) == 3


def test_alias_dry_run_checks_everything_and_sends_nothing(shop: Shop) -> None:
    text = shop.out("-n", "k1se", "level", "--dry-run")

    assert "alias level" in text and "  G29\n" in text
    assert (
        f"POST http://127.0.0.1:{shop.k1se.port}/printer/gcode/script?script=G29"
        in text
    )
    assert shop.k1se.writes() == []
    assert [r.path for r in shop.k1se.requests] == [
        "/printer/objects/list",
        "/printer/gcode/help",
        "/printer/objects/query",
    ]  # reads only: macros, then the idle check


def test_preheat_material_and_explicit_temperatures(shop: Shop) -> None:
    shop.out("-n", "k1se", "preheat", "PETG")
    shop.out("-n", "k1se", "preheat", "--nozzle", "200")
    shop.out("-n", "k1se", "preheat", "pla", "--bed", "55.5")
    shop.out("-n", "u1", "preheat", "ABS", "--head", "T2")

    assert shop.k1se.scripts == [
        "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=235\nSET_HEATER_TEMPERATURE HEATER=heater_bed TARGET=80",
        "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=200",
        "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=210\nSET_HEATER_TEMPERATURE HEATER=heater_bed TARGET=55.5",
    ]
    assert shop.u1.scripts == [
        "SET_HEATER_TEMPERATURE HEATER=extruder1 TARGET=250\nSET_HEATER_TEMPERATURE HEATER=heater_bed TARGET=100"
    ]


@pytest.mark.parametrize(
    "argv",
    [
        ["preheat"],
        ["preheat", "NYLON"],
        ["preheat", "--nozzle", "999"],
        ["preheat", "--bed", "-1"],
        ["preheat", "--nozzle", "hot"],
        ["preheat", "--nozzle", "nan"],
        ["preheat", "PLA", "--head", "T9"],
        ["preheat", "PLA", "--head", "2"],
        ["preheat", "PLA", "extra", "words"],
        ["preheat", "--colour", "red"],
        ["preheat", "--nozzle", "200; M112"],
    ],
)
def test_preheat_rejects_bad_arguments_before_contacting_the_printer(
    shop: Shop, argv: list[str]
) -> None:
    with pytest.raises(ThreeDError):
        run(["-n", "k1se", *argv])

    assert shop.k1se.requests == []


def test_cooldown_pause_resume_alias_scripts(shop: Shop) -> None:
    shop.out("-n", "k1se", "cooldown")
    shop.out("-n", "k1se", "pause")
    shop.out("-n", "k1se", "resume")

    assert shop.k1se.scripts == ["TURN_OFF_HEATERS", "PAUSE", "RESUME"]


def test_pause_and_resume_aliases_work_while_a_job_runs_but_cooldown_does_not(
    shop: Shop,
) -> None:
    shop.k1se.status = u1_status("printing", "part.gcode", 0.4)

    shop.out("-n", "k1se", "pause")
    err = shop.fails(PrinterRefused, "-n", "k1se", "cooldown")

    assert shop.k1se.scripts == ["PAUSE"] and "needs an idle printer" in err.message


def test_u1_specific_aliases_map_to_the_firmware_macros(shop: Shop) -> None:
    shop.out("-n", "u1", "load", "T2")
    shop.out("-n", "u1", "unload", "t1")
    shop.out("-n", "u1", "clean-nozzle")
    shop.out("-n", "u1", "park")
    shop.out("-n", "u1", "park", "T3")
    shop.out("-n", "u1", "purifier", "hot")
    shop.out("-n", "u1", "level-heated")
    shop.out("-n", "u1", "level-heated", "--bed", "60")
    shop.out("-n", "u1", "level")

    assert shop.u1.scripts == [
        "AUTO_FEEDING EXTRUDER=1 LOAD=1",
        "AUTO_FEEDING EXTRUDER=0 UNLOAD=1 STAGE=prepare\nAUTO_FEEDING EXTRUDER=0 UNLOAD=1 STAGE=doing",
        "ROUGHLY_CLEAN_NOZZLE",
        "PARK_EXTRUDER",
        "PARK_EXTRUDER2",
        "SET_PURIFIER_MODE MODE=3",
        "AUTO_BED_MESH_CALIBRATE TEMP=55",
        "AUTO_BED_MESH_CALIBRATE TEMP=60",
        "BED_MESH_CALIBRATE",
    ]


def test_u1_head_arguments_follow_the_fleet_heads(shop: Shop) -> None:
    for bad in (
        ["load"],
        ["load", "T5"],
        ["load", "2"],
        ["load", "0"],
        ["park", "T0"],
        ["purifier", "turbo"],
    ):
        with pytest.raises(ThreeDError):
            run(["-n", "u1", *bad])

    assert shop.u1.requests == []


def test_an_unusable_head_is_refused_with_its_reason(
    shop: Shop, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = yaml.safe_load((tmp_path / "fleet.yaml").read_text(encoding="utf-8"))
    data["u1"]["heads"][1].update(usable=False, reason="clogged")
    (tmp_path / "fleet.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")

    err = shop.fails(PrinterRefused, "-n", "u1", "load", "T2")

    assert (
        "head T2 is marked unusable: clogged" in err.message and shop.u1.requests == []
    )


def test_k1se_filament_aliases_use_the_stock_creality_macros(shop: Shop) -> None:
    shop.out("-n", "k1se", "load")
    shop.out("-n", "k1se", "unload")
    with pytest.raises(UsageError):
        run(["-n", "k1se", "load", "T1"])  # the K1 macro has no head argument

    assert shop.k1se.scripts == ["LOAD_MATERIAL", "QUIT_MATERIAL"]


@pytest.mark.parametrize(
    ("printer", "alias", "words"),
    [
        ("k1se", "clean-nozzle", "the stock K1 SE config has no nozzle-clean macro"),
        ("k1se", "park", "park"),
        ("k1se", "purifier", "purifier"),
    ],
)
def test_an_alias_a_model_does_not_have_says_so_and_lists_what_it_has(
    shop: Shop, printer: str, alias: str, words: str
) -> None:
    err = shop.fails(UsageError, "-n", printer, alias)

    assert (
        f"alias `{alias}` is not defined for k1se (Creality K1 SE, adapter moonraker)"
        in err.message
    )
    assert (
        "Defined for it: " in err.remediation[0]
        and "home" in err.remediation[0]
        and "park" not in err.remediation[0]
    )
    assert shop.k1se.requests == []


def test_an_alias_a_model_marks_unsupported_gives_the_reason(shop: Shop) -> None:
    err = shop.fails(UsageError, "-n", "k1se", "qgl")

    assert (
        "not supported on k1se" in err.message
        and "defines no quad_gantry_level" in err.message
    )
    assert shop.k1se.requests == []


def test_an_alias_whose_macro_the_firmware_lacks_is_refused_not_guessed(shop: Shop) -> None:
    del shop.k1se.macros["LOAD_MATERIAL"]  # a K1 without the stock Creality macro

    err = shop.fails(PrinterRefused, "-n", "k1se", "load")

    assert err.message == "k1se does not define LOAD_MATERIAL"
    assert shop.k1se.writes() == []


def test_u1_has_no_gantry_to_level(shop: Shop) -> None:
    err = shop.fails(UsageError, "-n", "u1", "qgl")

    assert "not supported on u1" in err.message and "defines no quad_gantry_level" in err.message
    assert shop.u1.requests == []


def test_alias_help_and_listing_name_what_each_alias_sends(shop: Shop) -> None:
    one = shop.out("-n", "u1", "unload", "--help")
    table = json.loads(shop.out("-n", "u1", "aliases", "--json"))
    k1 = shop.out("-n", "k1se", "aliases")
    generic = shop.out("home", "--help")
    builtin = shop.out("aliases")

    assert (
        "AUTO_FEEDING EXTRUDER={slot} UNLOAD=1 STAGE=prepare; AUTO_FEEDING EXTRUDER={slot} UNLOAD=1 STAGE=doing"
        in one
    )
    assert (
        table["load"]["gcode"] == ["AUTO_FEEDING EXTRUDER={slot} LOAD=1"]
        and table["qgl"]["unsupported"] is not None
    )
    assert (
        "level" in k1
        and "G29" in k1
        and "qgl: NOT supported - " in k1
        and "park" not in k1
    )
    assert (
        "adapter moonraker" in generic
        and "model snapmaker u1" not in generic.split("home")[0]
    )
    assert "model snapmaker u1:" in builtin and "model creality k1:" in builtin
    assert shop.u1.requests == []


def test_an_alias_needs_the_printer_and_name_is_not_accepted_for_positional_subcommands(
    shop: Shop,
) -> None:
    needs = shop.fails(UsageError, "home")
    named = shop.fails(UsageError, "-n", "u1", "status", "u1")
    unknown = shop.fails(UsageError, "frobnicate")
    unknown_on_printer = shop.fails(UsageError, "-n", "u1", "frobnicate")

    assert "is an alias and needs the printer: 3d printer -n NAME home" in needs.message
    assert "takes the printer as NAME" in named.message
    assert "unknown subcommand 'frobnicate'" in unknown.message
    assert "alias `frobnicate` is not defined for u1" in unknown_on_printer.message


def test_a_printers_own_aliases_extend_replace_and_remove_the_table(
    shop: Shop, tmp_path: Path
) -> None:
    cfg = tmp_path / "xdg" / "3d-cli"
    cfg.mkdir(parents=True)
    (cfg / "printer-aliases.yaml").write_text(
        yaml.safe_dump(
            {
                "u1": {
                    "home": {"summary": "home XY only", "kind": "motion", "gcode": ["G28 X Y"]},
                    "nozzle-wipe": {"summary": "wipe", "kind": "motion", "gcode": ["ROUGHLY_CLEAN_NOZZLE"]},
                    "park": None,
                    "cooldown": {"unsupported": "the chamber heater needs a manual step first"},
                },
                "k1se": {"home": {"summary": "k1 home", "kind": "motion", "gcode": ["G28 Z"]}},
            }
        ),
        encoding="utf-8",
    )
    # the project file wins over the user file, per alias
    (tmp_path / "printer-aliases.yaml").write_text(
        yaml.safe_dump({"u1": {"home": {"summary": "project home", "kind": "motion", "gcode": ["G28 Y"]}}}),
        encoding="utf-8",
    )

    shop.out("-n", "u1", "home")
    shop.out("-n", "u1", "nozzle-wipe")
    shop.out("-n", "k1se", "home")
    parked = shop.fails(UsageError, "-n", "u1", "park")
    cooled = shop.fails(UsageError, "-n", "u1", "cooldown")

    assert shop.u1.scripts == ["G28 Y", "ROUGHLY_CLEAN_NOZZLE"] and shop.k1se.scripts == ["G28 Z"]
    assert "disabled for this printer" in parked.message and "chamber heater" in cooled.message


def test_a_broken_printer_aliases_file_is_a_config_error(shop: Shop, tmp_path: Path) -> None:
    (tmp_path / "printer-aliases.yaml").write_text("u1: [not, a, mapping]\n", encoding="utf-8")

    err = shop.fails(ThreeDError, "-n", "u1", "aliases")

    assert err.exit_code == 2 and "must be a mapping of alias name" in err.message
    assert shop.u1.requests == []


@pytest.mark.parametrize(
    ("entry", "why"),
    [
        ({"summary": "x", "kind": "fast", "gcode": ["G28"]}, "kind"),
        ({"summary": "x", "kind": "motion", "gcode": []}, "gcode"),
        (
            {"summary": "x", "kind": "motion", "gcode": ["G28 {nope}"]},
            "no arg provides",
        ),
        ({"summary": "x", "kind": "motion", "gcode": ["G28; M84"]}, "without `;`"),
        ({"summary": "x", "kind": "motion", "gcode": ["M112"]}, "emergency stop"),
        (
            {
                "summary": "x",
                "kind": "motion",
                "gcode": ["G28"],
                "args": {"a": {"type": "text"}},
            },
            "type",
        ),
        (
            {"summary": "x", "kind": "motion", "gcode": ["G28"], "timeout": 99999},
            "timeout",
        ),
        ({"summary": "x", "kind": "motion", "gcode": ["G28"], "extra": 1}, "keys"),
        ({"kind": "motion", "gcode": ["G28"]}, "summary"),
    ],
)
def test_a_broken_alias_entry_is_a_config_error(
    entry: dict[str, Any], why: str
) -> None:
    with pytest.raises(ThreeDError) as info:
        aliases.parse_entries({"mine": entry}, "printer-aliases.yaml u1")

    assert why in info.value.message


def test_the_builtin_table_parses_and_every_template_is_valid() -> None:
    names = aliases.builtin_names()

    assert {
        "home",
        "level",
        "clean-nozzle",
        "load",
        "unload",
        "preheat",
        "cooldown",
        "pause",
        "resume",
        "park",
    } <= names
    for _label, entries in aliases.builtin_layers():
        for spec in entries.values():
            assert spec.unsupported or spec.kind in macros.KINDS


def test_a_creality_stock_printer_has_no_gcode_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)
    k1 = FakeCreality()
    with k1.serve():
        write_devices(tmp_path / "xdg", k1=k1)
        for argv in (
            ["macro", "run", "PAUSE", "--yes"],
            ["pause"],
            ["macro", "list"],
            ["load"],
        ):
            with pytest.raises(UnsupportedOperation) as info:
                run(["-n", "k1se", *argv])
            assert (
                "does not support macros and aliases (no G-code channel)"
                in info.value.message
            )

    assert k1.writes() == []
    capsys.readouterr()


# ---- the rules in isolation ------------------------------------------------------------------


def test_classification_and_gates() -> None:
    trusted = frozenset({"PAUSE", "START_PRINT"})

    assert [
        macros.classify(n)
        for n in (
            "pause",
            "G28",
            "SET_HEATER_TEMPERATURE",
            "FIRMWARE_RESTART",
            "QUERY_ENDSTOPS",
        )
    ] == [macros.JOB, macros.MOTION, macros.HEATING, macros.DANGEROUS, macros.INERT]
    job = macros.assess(["PAUSE"], trusted, curated=False)
    other = macros.assess(["AUTO_FEEDING EXTRUDER=0"], trusted, curated=False)
    curated = macros.assess(
        ["AUTO_FEEDING EXTRUDER=0"], trusted, curated=True, kind=macros.MOTION
    )
    restart = macros.assess(["FIRMWARE_RESTART"], trusted, curated=True)
    assert (job.needs_idle, job.yes_reasons) == (False, ())
    assert other.needs_idle and other.yes_reasons == (
        "AUTO_FEEDING is not in this printer's trusted_macros",
    )
    assert curated.needs_idle and curated.yes_reasons == ()
    assert restart.kind == macros.DANGEROUS and restart.yes_reasons


def test_parse_catalog_reads_moonraker_replies_and_rejects_other_shapes() -> None:
    cat = macros.parse_catalog(
        {"objects": ["webhooks", "gcode_macro Pause", "gcode_macro _x y"]},
        {"G29": "bed", "pause": "p"},
    )

    assert (
        cat.macros == {"PAUSE", "_X Y"}
        and cat.knows("g29")
        and cat.describe("PAUSE") == "p"
    )
    with pytest.raises(ValueError, match="unexpected shape"):
        macros.parse_catalog([], {})


def test_usage_text_names_every_builtin_alias() -> None:
    for name in aliases.builtin_names():
        assert name in USAGE, f"`3d printer --help` does not mention the alias {name}"
    assert "macro list|run" in USAGE and "aliases [--json]" in USAGE


def test_docs_name_every_builtin_alias_and_the_gcode_decision() -> None:
    doc = (REPO / "docs" / "commands" / "printer.md").read_text(encoding="utf-8")

    for name in aliases.builtin_names():
        assert f"`{name}" in doc, (
            f"docs/commands/printer.md does not document the alias {name}"
        )
    assert "no raw `gcode`" in doc and "3d printer -n u1 macro run" in doc
