"""The printer's light for every camera frame of the bed (issue #723): `printer_ops/bed_light.py`.

It finds the light among Moonraker's objects, reads its state, switches it on when it is off and puts it
back; `watch.bed.light` is validated in printer_ops/bed.py. The printer is `FakePrinter` of test_bed_view
plus a light object (`LightPrinter`, also used by test_bed_light_frames and test_bed_preflight); nothing
here touches a real printer or camera.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from errors import InvalidArgument
from printer_ops import bed as pbed
from printer_ops import bed_light as BL
from printer_ops.devices import Device
from test_bed_view import FRAMES, FakePrinter, ZCamera, device

PIN = "output_pin chamber_light"
NOW = lambda s: None  # noqa: E731 — the settle wait of every test


class LightPrinter(FakePrinter):
    """A FakePrinter that lists and drives a light, and logs what happens in order (`log`)."""

    def __init__(
        self,
        state: str = "complete",
        z: float = 200.0,
        *,
        light: str | None = PIN,
        lit: bool = False,
        others: tuple[str, ...] = (),
        **kw: Any,
    ) -> None:
        super().__init__(state, z, **kw)
        self.light = light
        self.lit = lit
        self.others = others
        self.log: list[str] = []
        self.fail_scripts: set[str] = set()
        self.hidden: set[str] = set()  # status keys the printer does not report

    def _get(self, path: str) -> dict[str, Any]:
        assert path == "/printer/objects/list"
        names = ["print_stats", "toolhead", "fan", *self.others]
        return {"objects": names + ([self.light] if self.light else [])}

    def query(self, objects: str) -> dict[str, Any]:
        status = super().query(objects)
        if self.light and self.light.split()[0] == "output_pin":
            status[self.light] = {"value": 1.0 if self.lit else 0.0}
        elif self.light:
            status[self.light] = {
                "color_data": [[1.0, 1.0, 1.0, 1.0] if self.lit else [0.0] * 4]
            }
        return {k: v for k, v in status.items() if k not in self.hidden}

    def run_gcode(self, script: str, *, timeout: float = 120.0) -> None:
        self.log.append(script.replace("\n", " | "))
        if script in self.fail_scripts:
            raise OSError(f"{script}: connection reset")
        super().run_gcode(script)
        first = script.splitlines()[0]
        if first.startswith(("SET_PIN", "SET_LED")) or first in (
            "LIGHT_ON",
            "LIGHT_OFF",
        ):
            self.lit = "VALUE=1" in first or "RED=1" in first or first == "LIGHT_ON"

    def camera(self, frames: dict[float, str] | None = None) -> ZCamera:
        cam = ZCamera(self, frames or FRAMES)
        cam.on_grab = lambda: self.log.append(f"capture lit={self.lit}")
        return cam


def collapsed(log: list[str]) -> list[str]:
    """The log with repeated captures (the check waits for a fresh frame) folded into one."""
    out: list[str] = []
    for entry in log:
        if not (entry.startswith("capture") and out and out[-1] == entry):
            out.append(entry)
    return out


# ---- finding the light --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("objects", "found"),
    [
        (["extruder", "output_pin chamber_light"], "output_pin chamber_light"),
        (["led cavity_led", "output_pin beeper"], "led cavity_led"),
        (["neopixel chamber"], "neopixel chamber"),
        (["dotstar status_leds"], "dotstar status_leds"),
        (["output_pin caselight"], "output_pin caselight"),
        (["output_pin motor_enabled"], None),  # "led" inside "enabled" is not a light
        (["fan_generic light_fan", "heater_bed"], None),  # not an led/output_pin object
        (["output_pin beeper", "extruder"], None),
        (
            ["output_pin chamber_fan", "output_pin lamp"],
            "output_pin lamp",
        ),  # light/lamp beat chamber
        (["output_pin zz_light", "led aa_light"], "led aa_light"),  # LEDs before pins
        (
            ["output_pin b_light", "output_pin a_light"],
            "output_pin a_light",
        ),  # not the listing order
        ([], None),
    ],
)
def test_the_light_is_found_by_object_kind_and_name(
    objects: list[str], found: str | None
) -> None:
    assert BL.detect_light(objects) == found
    assert BL.detect_light(list(reversed(objects))) == found


def test_a_configured_light_wins_and_must_exist() -> None:
    objects = ["output_pin chamber_light", "led strip"]
    assert BL.detect_light(objects, "led strip") == "led strip"
    assert BL.detect_light(objects, "led nothere") is None


@pytest.mark.parametrize(
    ("status", "on"),
    [
        ({PIN: {"value": 1.0}}, True),
        ({PIN: {"value": 0.0}}, False),
        (
            {"led strip": {"color_data": [[0.0, 0.0, 0.0, 0.0], [0.0, 0.2, 0.0, 0.0]]}},
            True,
        ),
        ({"led strip": {"color_data": [[0.0, 0.0, 0.0, 0.0]]}}, False),
        ({"led strip": {"color_data": []}}, None),
        ({}, None),
        ({PIN: {"value": True}}, None),  # a bool is not a level
    ],
)
def test_the_state_comes_from_value_or_color_data(
    status: dict[str, Any], on: bool | None
) -> None:
    obj = next(iter(status), PIN)
    assert BL.parse_state(obj, status) == BL.LightState(obj, on)


# ---- the session --------------------------------------------------------------------------------


def test_an_off_light_is_switched_on_for_the_body_and_put_back() -> None:
    p = LightPrinter()
    with BL.lit(p, note="the bed check", sleep=NOW) as report:
        assert p.lit is True
        assert p.log == ["SET_PIN PIN=chamber_light VALUE=1"]
    assert p.lit is False and p.log[-1] == "SET_PIN PIN=chamber_light VALUE=0"
    assert (
        report.found,
        report.object,
        report.was_on,
        report.switched,
        report.restored,
    ) == (
        True,
        PIN,
        False,
        True,
        True,
    )
    assert "switched on for the bed check and restored" in report.text
    assert report.to_dict()["on_for_frame"] is True


def test_an_led_is_driven_with_set_led_and_comes_back_off() -> None:
    p = LightPrinter(light="led cavity_led")
    with BL.lit(p, sleep=NOW):
        assert p.log == [
            "SET_LED LED=cavity_led RED=1 GREEN=1 BLUE=1 WHITE=1 TRANSMIT=1"
        ]
    assert (
        p.log[-1] == "SET_LED LED=cavity_led RED=0 GREEN=0 BLUE=0 WHITE=0 TRANSMIT=1"
        and p.lit is False
    )


def test_a_light_that_is_already_on_is_not_touched() -> None:
    p = LightPrinter(lit=True)
    with BL.lit(p, sleep=NOW) as report:
        pass
    assert p.log == [] and p.lit is True
    assert (
        report.was_on is True and report.switched is False and report.restored is None
    )
    assert report.text == f"light {PIN} was already on" and report.on_for_frame is True


def test_the_light_is_restored_when_the_body_raises() -> None:
    p = LightPrinter()
    with pytest.raises(RuntimeError, match="camera"), BL.lit(p, sleep=NOW):
        raise RuntimeError("camera down")
    assert p.lit is False and p.log[-1].endswith("VALUE=0")


def test_the_light_is_restored_on_keyboard_interrupt() -> None:
    p = LightPrinter()
    with pytest.raises(KeyboardInterrupt), BL.lit(p, sleep=NOW):
        raise KeyboardInterrupt
    assert p.lit is False


def test_no_light_says_so_and_the_body_still_runs() -> None:
    p = LightPrinter(light=None)
    ran = []
    with BL.lit(p, sleep=NOW) as report:
        ran.append(1)
    assert ran == [1] and p.log == []
    assert report.found is False and report.text.startswith("no light found")
    assert "watch.bed.light.object" in report.text


def test_an_adapter_that_cannot_list_objects_means_no_light_found() -> None:
    p = FakePrinter("complete", 200.0)  # no `_get`
    with BL.lit(p, sleep=NOW) as report:
        pass
    assert report.found is False and p.scripts == []


def test_an_unreadable_object_list_is_reported_not_raised() -> None:
    p = LightPrinter()

    def boom(path: str) -> dict[str, Any]:
        raise OSError("moonraker down")

    p._get = boom  # type: ignore[method-assign]
    with BL.lit(p, sleep=NOW) as report:
        pass
    assert report.found is False and "moonraker down" in (report.error or "")
    assert report.text.startswith("no light found (")


@pytest.mark.parametrize("raw", ["printing", "paused"])
def test_a_light_is_left_alone_while_a_job_runs(raw: str) -> None:
    p = LightPrinter(raw)
    with BL.lit(p, sleep=NOW) as report:
        pass
    assert p.log == [] and report.found is True and report.switched is False
    assert f"left alone: the printer is {raw}" in report.text


def test_an_unreadable_printer_state_is_not_a_licence_to_touch_the_light() -> None:
    p = LightPrinter()
    p.hidden = {"print_stats"}
    with BL.lit(p, sleep=NOW) as report:
        pass
    assert p.log == [] and "could not be read" in report.text


def test_the_job_watch_frame_lights_an_off_light_during_the_print_and_restores_it() -> (
    None
):
    p = LightPrinter("printing")
    with BL.lit(p, printing_ok=True, note="the job watch frame", sleep=NOW) as report:
        assert p.lit is True
    assert p.lit is False and report.restored is True
    on = LightPrinter("printing", lit=True)
    with BL.lit(on, printing_ok=True, sleep=NOW):
        pass
    assert on.log == [] and on.lit is True  # already on: not touched, not turned off


def test_a_read_only_printer_is_never_written_to() -> None:
    p = LightPrinter()
    with BL.lit(p, blocked="u1 is read-only: no writes", sleep=NOW) as report:
        pass
    assert p.log == [] and report.found is False
    assert report.text == "light not used: u1 is read-only: no writes"


def test_enabled_false_opts_out() -> None:
    p = LightPrinter()
    with BL.lit(p, BL.LightConfig(enabled=False), sleep=NOW) as report:
        pass
    assert p.log == [] and "watch.bed.light.enabled: false" in report.text


def test_a_failed_switch_is_reported_and_the_frame_is_taken_unlit() -> None:
    p = LightPrinter()
    p.fail_scripts = {"SET_PIN PIN=chamber_light VALUE=1"}
    with BL.lit(p, sleep=NOW) as report:
        pass
    assert report.switched is False and "connection reset" in (report.error or "")
    assert "could not be switched on" in report.text and report.on_for_frame is False
    # the half-sent script may have lit it: the light is still sent back off
    assert p.log[-1] == "SET_PIN PIN=chamber_light VALUE=0"


def test_a_failed_restore_is_reported_never_swallowed_silently() -> None:
    p = LightPrinter()
    p.fail_scripts = {"SET_PIN PIN=chamber_light VALUE=0"}
    with BL.lit(p, sleep=NOW) as report:
        pass
    assert (
        report.restored is False
        and "NOT restored" in report.text
        and "connection reset" in report.text
    )


def test_a_macro_replaces_the_builtin_command_and_the_settle_is_waited() -> None:
    p = LightPrinter()
    waits: list[float] = []
    cfg = BL.LightConfig(on_gcode=("LIGHT_ON",), off_gcode=("LIGHT_OFF",), settle_s=2.5)
    with BL.lit(p, cfg, sleep=waits.append):
        assert p.lit is True
    assert p.log == ["LIGHT_ON", "LIGHT_OFF"] and waits == [2.5]


def test_a_light_whose_state_is_unreadable_is_switched_on_and_left_on() -> None:
    p = LightPrinter()
    p.hidden = {PIN}
    with BL.lit(p, sleep=NOW) as report:
        pass
    assert p.log == ["SET_PIN PIN=chamber_light VALUE=1"] and report.restored is None
    assert "previous state is unknown, left on" in report.text


def test_the_session_switches_once_and_restores_once() -> None:
    p = LightPrinter()
    with BL.LightSession(p, sleep=NOW) as session:
        session.on()
        session.on()
        session.restore()
    assert p.log == [
        "SET_PIN PIN=chamber_light VALUE=1",
        "SET_PIN PIN=chamber_light VALUE=0",
    ]


def test_a_session_that_never_needed_a_frame_never_touches_the_light() -> None:
    p = LightPrinter()
    with BL.LightSession(p, sleep=NOW):
        pass
    assert p.log == []


# ---- config -------------------------------------------------------------------------------------


def dev(light: Any, trusted: frozenset[str] = frozenset()) -> Device:
    return replace(device(watch={"bed": {"light": light}}), trusted_macros=trusted)


def test_the_light_config_is_validated_and_defaults_to_autodetect() -> None:
    assert pbed.bed_config(device(watch={"bed": {}})).light == BL.LightConfig()
    cfg = pbed.bed_config(
        dev({"object": "output_pin  chamber_light", "settle_s": 3})
    ).light
    assert cfg.object == PIN and cfg.settle_s == 3.0 and cfg.enabled is True
    assert pbed.bed_config(dev({"enabled": False})).light.enabled is False
    assert "light" in pbed.BED_KEYS


def test_a_macro_in_the_light_config_must_be_trusted_like_clear_ack() -> None:
    with pytest.raises(InvalidArgument, match="trusted_macros"):
        pbed.bed_config(dev({"on_gcode": "LIGHT_ON"}))
    cfg = pbed.bed_config(
        dev(
            {"on_gcode": "LIGHT_ON", "off_gcode": ["LIGHT_OFF"]},
            frozenset({"LIGHT_ON", "LIGHT_OFF"}),
        )
    ).light
    assert cfg.on_gcode == ("LIGHT_ON",) and cfg.off_gcode == ("LIGHT_OFF",)
    # the two Klipper built-ins that only drive LEDs and pins need no entry
    builtin = pbed.bed_config(dev({"on_gcode": "SET_PIN PIN=lamp VALUE=1"})).light
    assert builtin.on_gcode == ("SET_PIN PIN=lamp VALUE=1",)


@pytest.mark.parametrize(
    "bad",
    [
        {"nope": 1},
        {"object": "fan chamber"},
        {"object": 5},
        {"settle_s": -1},
        {"enabled": "yes"},
        {"on_gcode": "LIGHT_ON; M112"},
        {"on_gcode": ["SET_PIN PIN=a VALUE=1\nM112"]},
        {"on_gcode": []},
        "light",
    ],
)
def test_a_bad_light_block_is_refused_when_the_config_is_read(bad: Any) -> None:
    with pytest.raises(InvalidArgument):
        pbed.bed_config(dev(bad, frozenset({"LIGHT_ON"})))
