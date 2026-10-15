"""Tests for `3d printer watch` (lib/printer_ops/watch.py, watch_sinks.py, watch_run.py).

Detectors are fed synthetic payloads shaped like what the printers push (the U1 noodle pause
with print_stats.exception code 2 from defect_detection.py, a macro error that ends the job,
homing lost while paused, the bed target dropped to 0); stream tests run against loopback
fakes only (tests/printer_fakes.py) and assert the watcher only asks read-only questions.
Issue https://github.com/alex-mextner/3d-cli/issues/145.
"""
from __future__ import annotations

import io
import json
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from errors import InvalidArgument, UsageError
from printer_fakes import FakeCreality, FakeMoonrakerWS, k1_state, u1_ws_status, write_devices
from printer_ops import devices, watch_run
from printer_ops.watch import (
    CRITICAL,
    INFO,
    WARNING,
    CrealityDetector,
    Event,
    MoonrakerDetector,
    WatchLimits,
    moonraker_subscription,
)
from printer_ops.watch_sinks import (
    Dispatcher,
    ExecSink,
    Sink,
    SteerSink,
    StdoutSink,
    TgSink,
    batch_text,
    sinks_for,
)


def kinds(events: list[Event]) -> list[tuple[str, str]]:
    return [(e.kind, e.severity) for e in events]


def started(state: str = "printing", **over: Any) -> tuple[MoonrakerDetector, list[Event]]:
    d = MoonrakerDetector("u1", WatchLimits(stall_s=600, heat_timeout_s=300, drift_hold_s=30))
    return d, d.on_status(u1_ws_status(state, **over), 1000.0)


# ---- Moonraker detector -----------------------------------------------------------------------

def test_snapshot_of_a_healthy_print_is_one_info_event() -> None:
    _, ev = started()
    assert kinds(ev) == [("snapshot", INFO)]
    assert "printing" in ev[0].text and "layer 828/1193" in ev[0].text


def test_noodle_pause_reports_exception_code_2_and_the_pause_with_its_reason() -> None:
    d, _ = started()
    exc = {"id": 532, "index": 0, "code": 2, "message": "detected noodle", "level": 2}
    ev = d.on_status({"print_stats": {"state": "paused", "exception": exc}, "pause_resume": {"is_paused": True}}, 1010.0)
    assert kinds(ev) == [("exception", CRITICAL), ("state", WARNING)]
    assert "detected noodle (code 2, id 532)" in ev[0].text
    assert ev[0].data["exception"]["code"] == 2
    assert "PAUSED" in ev[1].text and "detected noodle" in ev[1].text
    assert ev[1].data["from"] == "printing" and ev[1].data["to"] == "paused"
    # the same exception still present in later deltas is not reported again
    assert d.on_status({"exception_manager": {"exceptions": [exc]}}, 1011.0) == []


def test_exception_already_present_at_watch_start_is_reported() -> None:
    exc = {"code": 2, "message": "detected noodle"}
    _, ev = started("paused", print_stats={"exception": exc})
    assert kinds(ev) == [("exception", CRITICAL), ("snapshot", INFO), ("state", WARNING)]
    paused = [e for e in ev if e.kind == "state"][0]
    assert "is PAUSED" in paused.text and "detected noodle" in paused.text


def test_cancel_after_macro_error_names_the_macro() -> None:
    d, _ = started()
    line = ("!! Error evaluating 'gcode_macro INNER_PREEXTRUDE_FILAMENT:gcode': "
            "gcode.CommandError: Extrude below minimum temp")
    ev = d.on_gcode(line, 1020.0)
    assert kinds(ev) == [("gcode_error", CRITICAL)]
    assert "INNER_PREEXTRUDE_FILAMENT" in ev[0].text
    ev = d.on_status({"print_stats": {"state": "cancelled"}}, 1021.0)
    assert kinds(ev) == [("state", WARNING)]
    assert "CANCELLED" in ev[0].text and "INNER_PREEXTRUDE_FILAMENT" in ev[0].text
    assert "INNER_PREEXTRUDE_FILAMENT" in ev[0].data["reason"]


def test_homing_lost_while_paused_is_critical_once_after_confirmation() -> None:
    d, _ = started()
    d.on_status({"print_stats": {"state": "paused"}}, 1010.0)
    assert d.on_status({"toolhead": {"homed_axes": ""}}, 1020.0) == []    # held
    assert d.tick(1025.0) == []
    ev = d.tick(1030.0)
    assert kinds(ev) == [("homing_lost", CRITICAL)]
    assert "XYZ" in ev[0].text and "re-home" in ev[0].text
    assert d.on_status({"toolhead": {"homed_axes": "x"}}, 1040.0) == []
    assert d.tick(1060.0) == []


def test_homing_lost_found_at_watch_start_while_paused() -> None:
    _, ev = started("paused", toolhead={"homed_axes": "xy"})
    assert ("homing_lost", CRITICAL) in kinds(ev)


def test_homing_is_not_an_anomaly_when_idle() -> None:
    d, _ = started("standby")
    assert d.on_status({"toolhead": {"homed_axes": ""}}, 1010.0) == []


def test_bed_target_dropped_to_zero_while_paused() -> None:
    d, _ = started()
    d.on_status({"print_stats": {"state": "paused"}}, 1010.0)
    assert d.on_status({"heater_bed": {"target": 0.0}}, 1020.0) == []
    ev = d.on_status({"heater_bed": {"temperature": 60.0}}, 1031.0)
    assert kinds(ev) == [("bed_off", WARNING)]
    assert "was 65C" in ev[0].text


def test_bed_off_mid_print_fires_after_confirmation() -> None:
    d, _ = started()  # printing at 62%
    assert d.on_status({"heater_bed": {"target": 0.0}}, 1010.0) == []
    assert kinds(d.tick(1020.0)) == [("bed_off", WARNING)]


def test_bed_off_cleared_within_the_delay_is_dropped() -> None:
    d, _ = started()
    d.on_status({"heater_bed": {"target": 0.0}}, 1010.0)
    d.on_status({"heater_bed": {"target": 65.0}}, 1015.0)
    assert d.tick(1030.0) == []


@pytest.mark.parametrize("end", ["complete", "cancelled", "standby"])
def test_moonraker_end_macro_bed_off_and_motors_off_then_job_end_is_silent(end: str) -> None:
    d, _ = started()  # printing at 62%: PRINT_END/CANCEL_PRINT runs before the state changes
    out = d.on_status({"heater_bed": {"target": 0.0}}, 1010.0)
    out += d.on_status({"toolhead": {"homed_axes": ""}}, 1011.0)
    out += d.on_status({"print_stats": {"state": end}}, 1013.0)
    out += d.tick(1030.0) + d.tick(1060.0)
    assert [k for k, _ in kinds(out)] == ["state"]


def test_moonraker_end_of_print_at_full_progress_is_silent_even_if_state_lags() -> None:
    d, _ = started()
    d.on_status({"virtual_sdcard": {"progress": 1.0, "file_position": 9281802}}, 1010.0)
    out = d.on_status({"heater_bed": {"target": 0.0}, "toolhead": {"homed_axes": ""}}, 1011.0)
    out += d.tick(1030.0)
    assert out == []


def test_cold_bed_found_at_watch_start_while_paused() -> None:
    _, ev = started("paused", heater_bed={"target": 0.0, "temperature": 30.0})
    assert ("bed_off", WARNING) in kinds(ev)


def test_bed_target_zero_after_complete_is_normal() -> None:
    d, _ = started()
    assert kinds(d.on_status({"print_stats": {"state": "complete"}}, 1010.0)) == [("state", INFO)]
    assert d.on_status({"heater_bed": {"target": 0.0}}, 1011.0) == []


def test_filament_motion_stop_while_printing() -> None:
    d, _ = started()
    ev = d.on_status({"filament_motion_sensor e0_filament": {"filament_detected": False}}, 1010.0)
    assert kinds(ev) == [("filament", CRITICAL)]
    assert "motion stopped on e0_filament" in ev[0].text


def test_progress_stall_fires_once_and_clears() -> None:
    d, _ = started()
    assert d.tick(1500.0) == []
    ev = d.tick(1601.0)
    assert kinds(ev) == [("stall", WARNING)]
    assert d.tick(1700.0) == []
    ev = d.on_status({"virtual_sdcard": {"file_position": 600000, "progress": 0.63}}, 1710.0)
    assert kinds(ev) == [("stall", INFO)]


def test_heater_not_reaching_target() -> None:
    d, _ = started()
    d.on_status({"extruder1": {"target": 250.0, "temperature": 40.0}}, 1100.0)
    assert d.tick(1300.0) == []
    ev = d.tick(1401.0)
    assert kinds(ev) == [("temperature", WARNING)]
    assert "extruder1 not reaching target: 40/250C" in ev[0].text
    assert d.tick(1500.0) == []


def test_parked_head_cooling_slowly_to_standby_is_not_an_anomaly() -> None:
    d, _ = started()
    d.on_status({"extruder1": {"target": 40.0, "temperature": 198.0}}, 1100.0)
    assert [e for e in d.tick(3000.0) if e.kind == "temperature"] == []


def test_heater_drift_after_reaching_is_critical_while_printing() -> None:
    d, _ = started()
    d.on_status({"extruder": {"temperature": 190.0}}, 1100.0)
    assert d.tick(1120.0) == []
    ev = d.on_status({"extruder": {"temperature": 189.0}}, 1131.0)
    assert kinds(ev) == [("temperature", CRITICAL)]


def test_drift_rearms_after_recovery_and_after_a_not_reaching_alert() -> None:
    d = MoonrakerDetector("u1", WatchLimits(heat_timeout_s=60, drift_hold_s=30))
    d.on_status(u1_ws_status("printing", extruder={"temperature": 150.0}), 1000.0)
    assert kinds(d.tick(1061.0)) == [("temperature", WARNING)]          # slow heater
    d.on_status({"extruder": {"temperature": 219.0}}, 1100.0)              # finally reached
    d.on_status({"extruder": {"temperature": 190.0}}, 1110.0)
    assert kinds(d.tick(1141.0)) == [("temperature", CRITICAL)]          # drift still reported
    d.on_status({"extruder": {"temperature": 220.0}}, 1150.0)              # recovered: re-arms
    d.on_status({"extruder": {"temperature": 190.0}}, 1160.0)
    second = d.tick(1191.0)
    assert kinds(second) == [("temperature", CRITICAL)]
    assert second[0].key != "u1:temperature:extruder:drift:220:1110"


def test_klippy_shutdown_and_recovery() -> None:
    d, _ = started()
    ev = d.on_status({"webhooks": {"state": "shutdown", "state_message": "MCU 'e2' shutdown: Timer too close\nmore"}},
                     1010.0)
    assert kinds(ev) == [("klippy", CRITICAL)]
    assert "Timer too close" in ev[0].text
    assert d.on_klippy("shutdown", 1011.0) == []
    ev = d.on_klippy("ready", 1100.0)
    assert kinds(ev) == [("klippy", INFO)]


def test_gcode_warning_and_plain_responses() -> None:
    d, _ = started()
    assert kinds(d.on_gcode("// Warning: filament low on T2", 1010.0)) == [("gcode_warning", WARNING)]
    assert d.on_gcode("// Klipper state: Ready", 1011.0) == []
    assert d.on_gcode("ok", 1012.0) == []


def test_state_transitions_resume_complete_and_restart() -> None:
    d, _ = started()
    assert "PAUSED" in d.on_status({"print_stats": {"state": "paused"}}, 1010.0)[0].text
    assert d.on_status({"print_stats": {"state": "printing"}}, 1020.0)[0].text.startswith("u1: resumed")
    assert "COMPLETE" in d.on_status({"print_stats": {"state": "complete"}}, 1030.0)[0].text
    assert "started printing" in d.on_status({"print_stats": {"state": "printing"}}, 1040.0)[0].text


def test_second_pause_in_the_same_job_has_a_new_key() -> None:
    d, _ = started()
    a = d.on_status({"print_stats": {"state": "paused"}}, 1010.0)[0]
    d.on_status({"print_stats": {"state": "printing"}}, 1020.0)
    b = d.on_status({"print_stats": {"state": "paused"}}, 1030.0)[0]
    assert a.key != b.key


def test_milestones() -> None:
    d = MoonrakerDetector("u1", WatchLimits(milestones=(0.5, 0.9)))
    d.on_status(u1_ws_status("printing", virtual_sdcard={"progress": 0.4}), 1000.0)
    ev = d.on_status({"virtual_sdcard": {"progress": 0.55, "file_position": 9}}, 1010.0)
    assert kinds(ev) == [("milestone", INFO)] and "50%" in ev[0].text


def test_connection_events() -> None:
    d, _ = started()
    ev = d.on_connection(False, "ConnectionRefused", 1010.0)
    assert kinds(ev) == [("connection", WARNING)]
    assert d.on_connection(False, "again", 1020.0) == []
    assert kinds(d.on_connection(True, "", 1030.0)) == [("connection", INFO)]


def test_first_connect_after_unreachable_start_is_not_called_reconnected() -> None:
    d = MoonrakerDetector("u1")
    assert "cannot connect" in d.on_connection(False, "refused", 1000.0)[0].text
    assert "connected (was unreachable at start)" in d.on_connection(True, "", 1010.0)[0].text


def test_full_status_replaces_objects_gone_after_restart() -> None:
    d, _ = started()
    status = u1_ws_status("printing")
    del status["filament_motion_sensor e0_filament"]
    d.on_status(status, 1010.0, full=True)
    assert "filament_motion_sensor e0_filament" not in d.status


def test_subscription_covers_sensors_heaters_and_skips_macros() -> None:
    sub = moonraker_subscription(["extruder", "extruder3", "extruder_offset_calibration", "heater_bed",
                                  "filament_motion_sensor e2_filament", "toolhead", "gcode_macro PAUSE"])
    assert sub["extruder3"] == ["temperature", "target"]
    assert "extruder_offset_calibration" not in sub
    assert sub["filament_motion_sensor e2_filament"] == ["filament_detected", "enabled"]
    assert sub["toolhead"] == ["homed_axes"]
    assert "exception" in (sub["print_stats"] or [])
    assert not any(k.startswith("gcode_macro") for k in sub)


# ---- Creality detector -------------------------------------------------------------------------

def k1(**over: Any) -> dict[str, Any]:
    s = k1_state(1, 1, "part.gcode")
    s.update({"layer": 40, "TotalLayer": 120, "autohome": "X:1 Y:1 Z:1", "materialDetect": 1, "materialStatus": 0,
              "usedMaterialLength": 100})
    s.update(over)
    return s


def test_k1_pause_error_and_homing() -> None:
    d = CrealityDetector("k1se")
    assert kinds(d.on_push(k1(), 1000.0)) == [("snapshot", INFO)]
    ev = d.on_push({"state": 5, "deviceState": 0}, 1010.0)
    assert kinds(ev) == [("state", WARNING)] and "layer 40/120" in ev[0].text
    assert d.on_push({"autohome": "X:0 Y:0 Z:0"}, 1020.0) == []
    ev = d.on_push({"layer": 40}, 1031.0)
    assert kinds(ev) == [("homing_lost", CRITICAL)]
    ev = d.on_push({"err": {"errcode": 2052, "key": 12, "value": "nozzle"}}, 1040.0)
    assert ("exception", CRITICAL) in kinds(ev)
    assert "2052" in ev[0].text


def test_k1_end_print_at_100_percent_is_silent() -> None:
    """END_PRINT on the K1 SE: bed target 0 and M84 while state is still 1 at 100 %."""
    d = CrealityDetector("k1se")
    d.on_push(k1(printProgress=99), 1000.0)
    out = d.on_push({"printProgress": 100, "targetBedTemp0": 0}, 1010.0)
    out += d.on_push({"autohome": "X:0 Y:0 Z:0"}, 1011.0)
    out += d.on_push({"state": 2, "deviceState": 0}, 1012.0)
    out += d.tick(1030.0)
    assert kinds(out) == [("state", INFO)] and "COMPLETE" in out[0].text


def test_k1_cancel_macro_below_end_progress_is_silent_when_the_job_ends() -> None:
    d = CrealityDetector("k1se")
    d.on_push(k1(), 1000.0)  # printing at 37 %
    out = d.on_push({"targetBedTemp0": 0, "autohome": "X:0 Y:0 Z:0"}, 1010.0)
    out += d.on_push({"state": 4, "deviceState": 0}, 1014.0)
    out += d.tick(1030.0)
    assert [k for k, _ in kinds(out)] == ["state"]


def test_k1_bed_off_mid_print_still_fires() -> None:
    d = CrealityDetector("k1se")
    d.on_push(k1(), 1000.0)
    assert d.on_push({"targetBedTemp0": 0}, 1010.0) == []
    assert kinds(d.tick(1021.0)) == [("bed_off", WARNING)]


def k1_starting() -> CrealityDetector:
    """A K1 that this watcher saw go idle -> printing (START_PRINT running, 0 %)."""
    d = CrealityDetector("k1se")
    d.on_push(k1_state(0, 0), 1000.0)
    ev = d.on_push({"state": 1, "deviceState": 1, "printFileName": "/usr/data/printer_data/gcodes/k1_ovl1_v4.gcode",
                    "printProgress": 0}, 1010.0)
    assert "started printing" in ev[0].text
    return d


def test_k1_start_print_zeroing_targets_for_30s_is_silent() -> None:
    """K1 stock START_PRINT zeroes bed and nozzle targets while probing, ~30 s later 60/225 again."""
    d = k1_starting()
    out = d.on_push({"targetBedTemp0": 0, "targetNozzleTemp": 0}, 1020.0)
    out += d.tick(1035.0)
    out += d.on_push({"targetBedTemp0": 60, "targetNozzleTemp": 225}, 1050.0)
    out += d.tick(1200.0)
    assert out == []


def test_k1_bed_off_held_through_the_start_phase_still_fires() -> None:
    d = k1_starting()
    assert d.on_push({"targetBedTemp0": 0}, 1020.0) == []
    assert d.tick(1080.0) == []                       # 60 s: still inside the start hold
    assert kinds(d.tick(1111.0)) == [("bed_off", WARNING)]


def test_start_phase_by_time_since_started_even_above_start_progress() -> None:
    d = MoonrakerDetector("u1")
    d.on_status(u1_ws_status("standby"), 1000.0)
    d.on_status({"print_stats": {"state": "printing"}, "virtual_sdcard": {"progress": 0.05}}, 1010.0)
    assert d.on_status({"heater_bed": {"target": 0.0}}, 1100.0) == []
    assert d.tick(1120.0) == []                       # a mid-print hold (10 s) would have fired
    assert kinds(d.tick(1191.0)) == [("bed_off", WARNING)]


def test_held_bed_off_dropped_when_progress_reaches_the_end_before_release() -> None:
    d, _ = started()
    d.on_status({"virtual_sdcard": {"progress": 0.98, "file_position": 1}}, 1000.5)
    assert d.on_status({"heater_bed": {"target": 0.0}}, 1001.0) == []      # held at 98 %
    d.on_status({"virtual_sdcard": {"progress": 1.0, "file_position": 2}}, 1003.0)
    assert d.tick(1020.0) == []


def test_unknown_progress_mid_job_uses_the_short_hold() -> None:
    d = MoonrakerDetector("u1")
    status = u1_ws_status("printing")
    del status["virtual_sdcard"], status["display_status"]
    d.on_status(status, 1000.0)  # attached mid-job, firmware reports no progress
    assert d.on_status({"heater_bed": {"target": 0.0}}, 1010.0) == []
    assert kinds(d.tick(1021.0)) == [("bed_off", WARNING)]


def test_paused_at_end_progress_end_macro_is_silent() -> None:
    d, _ = started()
    d.on_status({"print_stats": {"state": "paused"}, "virtual_sdcard": {"progress": 0.995, "file_position": 3}},
                1001.0)
    out = d.on_status({"heater_bed": {"target": 0.0}, "toolhead": {"homed_axes": ""}}, 1002.0)
    out += d.tick(1030.0)
    assert out == []


def test_watch_started_during_the_end_macro_reports_no_anomaly() -> None:
    _, ev = started(virtual_sdcard={"progress": 1.0}, toolhead={"homed_axes": ""}, heater_bed={"target": 0.0})
    assert kinds(ev) == [("snapshot", INFO)]


def test_homing_lost_re_homed_within_the_hold_is_dropped() -> None:
    d, _ = started()
    assert d.on_status({"toolhead": {"homed_axes": ""}}, 1010.0) == []
    assert d.on_status({"toolhead": {"homed_axes": "xyz"}}, 1015.0) == []
    assert d.tick(1030.0) == []


def test_held_bed_off_dropped_when_a_full_status_reset_loses_the_heater() -> None:
    d, _ = started()
    d.on_status({"heater_bed": {"target": 0.0}}, 1010.0)
    status = u1_ws_status("printing")
    del status["heater_bed"]
    d.on_status(status, 1012.0, full=True)
    assert d.tick(1030.0) == []


def test_homing_lost_found_at_watch_start_while_printing_is_held() -> None:
    d, ev = started(toolhead={"homed_axes": ""})  # e.g. a start macro re-homing
    assert kinds(ev) == [("snapshot", INFO)]
    d.on_status({"toolhead": {"homed_axes": "xyz"}}, 1005.0)
    assert d.tick(1030.0) == []


def test_k1_runout_mid_print_is_critical_after_confirmation() -> None:
    d = CrealityDetector("k1se")
    d.on_push(k1(), 1000.0)  # printing at 37 %, filament present
    assert d.on_push({"materialStatus": 1}, 1010.0) == []
    ev = d.on_push({"layer": 41}, 1021.0)
    assert kinds(ev) == [("filament", CRITICAL)] and "no filament" in ev[0].text
    assert d.on_push({"layer": 42}, 1040.0) == []             # reported once


def test_k1_runout_flicker_mid_print_is_dropped() -> None:
    d = CrealityDetector("k1se")
    d.on_push(k1(), 1000.0)
    d.on_push({"materialStatus": 1}, 1010.0)
    d.on_push({"materialStatus": 0}, 1013.0)
    assert d.tick(1030.0) == []


def test_k1_sensor_flip_during_start_macro_is_silent() -> None:
    """k1_slate, 2026-10-15: materialStatus 0->1 at 0 % about 90 s after start, print went on."""
    d = k1_starting()
    d.on_push({"materialDetect": 1, "materialStatus": 0}, 1011.0)
    out = d.on_push({"materialStatus": 1}, 1100.0)
    out += d.tick(1250.0)
    out += d.on_push({"materialStatus": 0}, 1300.0)
    out += d.on_push({"printProgress": 5, "layer": 3}, 2000.0) + d.tick(2100.0)
    assert [e for e in out if e.kind == "filament"] == []


def test_k1_runout_that_outlasts_the_start_phase_fires() -> None:
    d = k1_starting()
    d.on_push({"materialDetect": 1, "materialStatus": 1}, 1020.0)
    assert d.tick(1500.0) == []                                  # deferred while starting
    assert d.on_push({"printProgress": 5, "layer": 3}, 1700.0) == []   # start over: held
    assert kinds(d.on_push({"layer": 4}, 1711.0)) == [("filament", CRITICAL)]


def test_k1_deferred_runout_is_rechecked_by_ticks_without_pushes() -> None:
    d = k1_starting()
    d.on_push({"materialDetect": 1, "materialStatus": 1, "printProgress": 5, "layer": 3}, 1500.0)
    assert d.tick(1600.0) == []                                  # still within 10 min of the start
    assert d.tick(1615.0) == []                                  # start phase over: held now
    assert kinds(d.tick(1626.0)) == [("filament", CRITICAL)]


def test_k1_runout_pause_reports_the_reason_at_once() -> None:
    d = CrealityDetector("k1se")
    d.on_push(k1(), 1000.0)
    d.on_push({"materialStatus": 1}, 1010.0)
    ev = d.on_push({"state": 5, "deviceState": 0}, 1012.0)
    state = [e for e in ev if e.kind == "state"][0]
    assert state.severity == WARNING and "filament not detected" in state.text


def test_k1_runout_paused_at_watch_start_is_reported_at_once() -> None:
    d = CrealityDetector("k1se")
    ev = d.on_push(k1(state=5, deviceState=0, materialStatus=1), 1000.0)
    assert ("filament", WARNING) in kinds(ev)


def test_k1_material_status_ignored_when_the_sensor_is_off() -> None:
    d = CrealityDetector("k1se")
    d.on_push(k1(materialDetect=0), 1000.0)
    d.on_push({"materialStatus": 1}, 1010.0)
    assert d.tick(1100.0) == []


def test_k1_runout_persisting_into_the_next_job_is_reported_again() -> None:
    d = CrealityDetector("k1se")
    d.on_push(k1(), 1000.0)
    d.on_push({"materialStatus": 1}, 1010.0)
    assert kinds(d.tick(1021.0)) == [("filament", CRITICAL)]
    d.on_push({"state": 4, "deviceState": 0}, 1030.0)          # cancelled, sensor still 1
    d.on_push({"state": 1, "deviceState": 1, "printProgress": 37}, 1100.0)  # job B
    d.tick(1800.0)                                               # job B's start phase over: held
    assert kinds(d.tick(1811.0)) == [("filament", CRITICAL)]


def test_k1_recovered_start_flicker_does_not_explain_a_later_pause() -> None:
    d = k1_starting()
    d.on_push({"materialDetect": 1, "materialStatus": 1}, 1020.0)
    d.on_push({"materialStatus": 0}, 1050.0)
    ev = d.on_push({"state": 5, "deviceState": 0}, 1060.0)
    state = [e for e in ev if e.kind == "state"][0]
    assert "filament" not in state.text


def test_k1_runout_on_the_push_that_starts_the_job_waits_for_the_start_phase() -> None:
    d = CrealityDetector("k1se")
    d.on_push(k1_state(0, 0) | {"materialDetect": 1, "materialStatus": 0}, 1000.0)
    d.on_push({"state": 1, "deviceState": 1, "materialStatus": 1}, 1010.0)  # no printProgress in this delta
    assert d.tick(1030.0) == []
    assert d.on_push({"materialStatus": 0}, 1040.0) == []
    assert d.tick(1700.0) == []


def test_k1_runout_with_progress_stuck_at_zero_fires_after_the_start_phase() -> None:
    d = k1_starting()
    d.on_push({"materialDetect": 1, "materialStatus": 1}, 1020.0)
    assert d.tick(1600.0) == []                                  # 590 s after the start
    assert d.tick(1611.0) == []                                  # start phase over: held now
    assert kinds(d.tick(1622.0)) == [("filament", CRITICAL)]


def test_k1_runout_with_unknown_start_and_stuck_progress_is_not_hidden_forever() -> None:
    d = CrealityDetector("k1se")
    d.on_push(k1(printProgress=0), 1000.0)  # attached to a running job at 0 %
    d.on_push({"materialStatus": 1}, 1010.0)
    assert d.tick(1500.0) == []
    d.tick(1611.0)                                               # waited start_phase_s: held
    assert kinds(d.tick(1622.0)) == [("filament", CRITICAL)]


def test_k1_runout_while_paused_mid_job_is_a_warning() -> None:
    d = CrealityDetector("k1se")
    d.on_push(k1(), 1000.0)
    d.on_push({"state": 5, "deviceState": 0}, 1010.0)
    d.on_push({"materialStatus": 1}, 1020.0)
    assert kinds(d.tick(1031.0)) == [("filament", WARNING)]


def test_k1_cancel_and_complete() -> None:
    d = CrealityDetector("k1se")
    d.on_push(k1(), 1000.0)
    assert kinds(d.on_push({"state": 4, "deviceState": 0}, 1010.0)) == [("state", WARNING)]
    d.on_push({"state": 1, "deviceState": 1}, 1020.0)
    ev = d.on_push({"state": 2, "deviceState": 0}, 1030.0)
    assert "COMPLETE" in ev[0].text


# ---- sinks and dispatcher ----------------------------------------------------------------------

class Recorder(Sink):
    name = "rec"

    def __init__(self, rate: tuple[int, float] | None = None) -> None:
        super().__init__()
        self.rate = rate
        self.batches: list[tuple[list[Event], int]] = []

    def deliver(self, events: list[Event], suppressed: int = 0) -> None:
        self.batches.append((events, suppressed))


def ev(sev: str = WARNING, key: str = "", printer: str = "u1", kind: str = "state") -> Event:
    return Event(printer, kind, sev, f"{printer}: {kind} {sev}", {}, key)


def test_dispatcher_filters_severity_and_dedups() -> None:
    clock = [0.0]
    d = Dispatcher(dedup_s=60, clock=lambda: clock[0])
    rec = Recorder()
    d.add("u1", [rec], WARNING)
    d.process([ev(INFO, "a"), ev(WARNING, "b"), ev(WARNING, "b")])
    assert [len(b) for b, _ in rec.batches] == [1]
    clock[0] = 30.0
    d.process([ev(WARNING, "b")])
    assert len(rec.batches) == 1
    clock[0] = 100.0
    d.process([ev(WARNING, "b")])
    assert len(rec.batches) == 2


def test_dispatcher_routes_by_printer() -> None:
    d = Dispatcher()
    a, b = Recorder(), Recorder()
    a.name, b.name = "a", "b"
    d.add("u1", [a])
    d.add("k1se", [b])
    d.process([ev(key="1"), ev(key="2", printer="k1se")])
    assert [e.printer for e in a.batches[0][0]] == ["u1"]
    assert [e.printer for e in b.batches[0][0]] == ["k1se"]


def test_rate_limit_holds_back_but_never_critical() -> None:
    clock = [0.0]
    d = Dispatcher(clock=lambda: clock[0])
    rec = Recorder(rate=(2, 600.0))
    d.add("u1", [rec])
    for i in range(4):
        d.process([ev(WARNING, f"w{i}")])
    assert len(rec.batches) == 2 and rec.stats.suppressed == 2
    d.process([ev(CRITICAL, "c")])
    assert len(rec.batches) == 3 and rec.batches[-1][1] == 2  # reports what was held back
    clock[0] = 700.0
    d.process([ev(WARNING, "w9")])
    assert len(rec.batches) == 4 and rec.batches[-1][1] == 0


def test_threaded_dispatcher_coalesces_a_burst() -> None:
    d = Dispatcher(coalesce_s=0.3)
    rec = Recorder()
    d.add("u1", [rec])
    d.start()
    for i in range(3):
        d.submit(ev(key=f"k{i}"))
    d.close()
    assert [len(b) for b, _ in rec.batches] == [3]


def test_failing_sink_is_logged_not_fatal(capsys: pytest.CaptureFixture[str]) -> None:
    class Boom(Sink):
        name = "boom"

        def deliver(self, events: list[Event], suppressed: int = 0) -> None:
            raise RuntimeError("tg is down")

    d = Dispatcher()
    boom, rec = Boom(), Recorder()
    d.add("u1", [boom, rec])
    d.process([ev(key="x")])
    assert len(rec.batches) == 1 and boom.stats.failed == 1
    assert "tg is down" in capsys.readouterr().err


def test_batch_text_puts_the_worst_first() -> None:
    text = batch_text([ev(INFO, "a"), ev(CRITICAL, "b", kind="exception")], suppressed=3)
    lines = text.splitlines()
    assert lines[0].startswith("[CRITICAL] u1: exception")
    assert "+3" in lines[-1]


def test_stdout_sink_writes_jsonl() -> None:
    buf = io.StringIO()
    StdoutSink(buf).deliver([ev(key="k")])
    row = json.loads(buf.getvalue())
    assert row["printer"] == "u1" and row["severity"] == WARNING and row["key"] == "k" and "ts" in row


class FakeRun:
    def __init__(self, replies: dict[str, tuple[int, str]] | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.replies = replies or {}

    def __call__(self, cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((cmd, kw))
        code, out = self.replies.get(" ".join(cmd[:3]), (0, '{"ok":true}'))
        return subprocess.CompletedProcess(cmd, code, out, "")


SESSIONS = json.dumps([
    {"pid": 433553, "sessionId": "01a12cd6-7b39", "cwd": "/tmp", "title": "MODEL: Finish 3D model", "busy": False},
    {"pid": 14610, "sessionId": "01a11af0-c495", "cwd": "/tmp", "title": "FREEZE: Diagnose", "busy": True},
])


@pytest.mark.parametrize("target", ["01a12cd6", "433553", "finish 3d", "01a12cd6-7b39"])
def test_steer_resolves_one_session(target: str) -> None:
    run = FakeRun({"tg-ctl omp-live status": (0, SESSIONS)})
    SteerSink(target, runner=run).deliver([ev(CRITICAL, "x", kind="exception")])
    send = [c for c, _ in run.calls if c[2] == "send"]
    assert len(send) == 1
    assert send[0][3:7] == ["433553", "--request-id", send[0][5], "--session"] and send[0][7] == "01a12cd6-7b39"
    assert send[0][-1].startswith(f"[3d printer watch -> steer {target}] u1 ")
    assert "3d printer status u1" in send[0][-1]


def test_steer_refuses_ambiguous_and_missing_targets() -> None:
    run = FakeRun({"tg-ctl omp-live status": (0, SESSIONS)})
    with pytest.raises(RuntimeError, match="matches 2 sessions"):
        SteerSink("01a1", runner=run).deliver([ev()])
    with pytest.raises(RuntimeError, match="no live omp session"):
        SteerSink("nope", runner=run).deliver([ev()])
    assert not [c for c, _ in run.calls if c[2] == "send"]


def test_steer_reports_bridge_failure() -> None:
    run = FakeRun({"tg-ctl omp-live status": (0, SESSIONS), "tg-ctl omp-live send": (1, '{"ok":false,"error":"x"}')})
    with pytest.raises(RuntimeError, match="omp-live send"):
        SteerSink("01a12cd6", runner=run).deliver([ev()])


def test_tg_and_exec_sinks() -> None:
    run = FakeRun()
    TgSink(runner=run).deliver([ev(key="k")])
    assert run.calls[0][0][0] == "tg" and "u1: state warning" in run.calls[0][0][1]
    run = FakeRun()
    ExecSink("cat > /dev/null", runner=run).deliver([ev(key="k"), ev(key="j")])
    assert len(run.calls) == 2
    cmd, kw = run.calls[0]
    assert cmd == ["sh", "-c", "cat > /dev/null"]
    assert json.loads(kw["input"])["key"] == "k"
    assert kw["env"]["PRINTER_EVENT_SEVERITY"] == WARNING and kw["env"]["PRINTER_EVENT_PRINTER"] == "u1"


def test_exec_sink_really_runs_a_shell(tmp_path: Path) -> None:
    out = tmp_path / "ev.json"
    ExecSink(f'cat > "{out}"; echo "$PRINTER_EVENT_KIND" >> "{out}.kind"').deliver([ev(key="k")])
    assert json.loads(out.read_text())["kind"] == "state"
    assert (tmp_path / "ev.json.kind").read_text().strip() == "state"


def test_sinks_from_settings_and_for_devices_validates(env: Path) -> None:
    tmp_path = env
    spec = {"stdout": True, "exec": ["logger"], "tg_fallback": True, "steer": ["01a12cd6"]}
    assert [s.ident for s in sinks_for(spec)] == ["stdout", "exec:logger", "tg", "steer:01a12cd6"]
    dry = sinks_for({**spec, "stdout": False}, dry_run=True)
    assert [s.ident for s in dry] == ["exec:logger", "tg", "steer:01a12cd6"]
    assert all(type(s).__name__ == "DryRunSink" for s in dry)
    dev = devices.Device("u1", "moonraker", "h", 7125, None, tmp_path,
                         watch={"tg_fallback": True, "steer": "01a12cd6", "min_severity": "warning", "dedup_minutes": 1})
    d = Dispatcher.for_devices([dev], dry_run=True)
    assert sorted(d.routes) == ["printer-agent", "stdout", "steer:01a12cd6", "subscriptions", "tg"]
    assert d.min_severity["u1"] == WARNING and d.dedup_s["u1"] == 60
    bad = devices.Device("u1", "moonraker", "h", 7125, None, tmp_path, watch={"min_severity": "warnig"})
    with pytest.raises(InvalidArgument):
        Dispatcher.for_devices([bad])


def test_dedup_window_is_per_printer() -> None:
    clock = [0.0]
    d = Dispatcher(clock=lambda: clock[0])
    rec = Recorder()
    d.add("u1", [rec], dedup_s=3600)
    d.add("k1se", [rec], dedup_s=0)
    d.process([ev(key="u"), ev(key="k", printer="k1se")])
    clock[0] = 10.0
    d.process([ev(key="u"), ev(key="k", printer="k1se")])
    assert [[e.printer for e in b] for b, _ in rec.batches] == [["u1", "k1se"], ["k1se"]]


# ---- config and CLI ----------------------------------------------------------------------------

@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)

    def no_agent(self: object, printer: str, events: list[Event], on_unreached: object = None) -> str:
        raise RuntimeError("tests never start a real omp printer agent")

    from printer_ops import printer_agent

    monkeypatch.setattr(printer_agent.PrinterAgents, "deliver", no_agent)
    return cfg


def test_device_watch_key_is_parsed_and_validated(env: Path) -> None:
    p = env / "3d-cli" / "printer-devices.yaml"
    p.parent.mkdir(parents=True)
    p.write_text("u1: {adapter: moonraker, host: h, watch: {tg_fallback: true, min_severity: warning}}\n")
    assert devices.load_devices()["u1"].watch == {"tg_fallback": True, "min_severity": "warning"}
    p.write_text("u1: {adapter: moonraker, host: h, watch: yes}\n")
    with pytest.raises(InvalidArgument):
        devices.load_devices()


def test_settings_merge_yaml_and_flags(tmp_path: Path) -> None:
    dev = devices.Device("u1", "moonraker", "h", 7125, None, tmp_path,
                         watch={"tg_fallback": True, "stall_minutes": 20, "milestones": [50, 90]})
    _, opts = watch_run.parse_args(["u1", "--steer", "abc", "--min-severity", "warning", "--no-stdout"])
    s = watch_run.settings_for(dev, opts)
    assert s["sinks"] == {"exec": [], "steer": ["abc"], "tg_fallback": True, "stdout": False}
    assert s["min_severity"] == WARNING
    assert s["limits"].stall_s == 1200 and s["limits"].milestones == (0.5, 0.9)


def test_settings_reject_bad_values(tmp_path: Path) -> None:
    dev = devices.Device("u1", "moonraker", "h", 7125, None, tmp_path, watch={"bogus": 1})
    with pytest.raises(InvalidArgument):
        watch_run.settings_for(dev, watch_run.parse_args(["u1"])[1])
    for watch in ({"tg_fallback": "yes"},):
        dev = devices.Device("u1", "moonraker", "h", 7125, None, tmp_path, watch=watch)
        with pytest.raises(InvalidArgument):
            watch_run.settings_for(dev, {})
    dev = devices.Device("u1", "moonraker", "h", 7125, None, tmp_path)
    with pytest.raises(InvalidArgument):
        watch_run.settings_for(dev, watch_run.parse_args(["u1", "--min-severity", "loud"])[1])
    for bad in ("inf", "-inf", "nan"):
        with pytest.raises(InvalidArgument):
            watch_run.settings_for(dev, watch_run.parse_args(["u1", "--dedup-min", bad])[1])
    dev = devices.Device("u1", "moonraker", "h", 7125, None, tmp_path, watch={"stall_minutes": float("inf")})
    with pytest.raises(InvalidArgument):
        watch_run.settings_for(dev, {})


def test_parse_args_errors() -> None:
    with pytest.raises(UsageError):
        watch_run.parse_args([])
    with pytest.raises(UsageError):
        watch_run.parse_args(["u1", "--all"])
    with pytest.raises(UsageError):
        watch_run.parse_args(["u1", "--bogus"])
    with pytest.raises(UsageError):
        watch_run.parse_args(["u1", "--steer"])


def test_help() -> None:
    assert watch_run.run(["--help"]) == 0


def test_install_service_writes_unit_and_env(env: Path, monkeypatch: pytest.MonkeyPatch,
                                             capsys: pytest.CaptureFixture[str]) -> None:
    p = env / "3d-cli" / "printer-devices.yaml"
    p.parent.mkdir(parents=True)
    p.write_text("u1: {adapter: moonraker, host: h}\nk1se: {adapter: creality-stock, host: h}\n")
    monkeypatch.setattr(watch_run.shutil, "which", lambda name: "/opt/bin/3d" if name == "3d" else None)
    assert watch_run.run(["u1", "k1se", "--install-service", "--steer", "01a12cd6"]) == 0
    unit = (env / "systemd" / "user" / "3d-printer-watch@.service").read_text()
    assert "ExecStart=/opt/bin/3d printer watch %i $WATCH_ARGS" in unit and "Restart=always" in unit
    assert f"EnvironmentFile=-{env / '3d-cli' / 'watch'}/%i.env" in unit
    assert f"Environment=XDG_CONFIG_HOME={env}" in unit
    assert (env / "3d-cli" / "watch" / "u1.env").read_text().splitlines()[-1] == "WATCH_ARGS=--steer 01a12cd6"
    assert (env / "3d-cli" / "watch" / "k1se.env").is_file()
    assert "systemctl --user enable --now 3d-printer-watch@u1" in capsys.readouterr().out


def test_install_service_refuses_args_with_spaces(env: Path) -> None:
    p = env / "3d-cli" / "printer-devices.yaml"
    p.parent.mkdir(parents=True)
    p.write_text("u1: {adapter: moonraker, host: h}\n")
    with pytest.raises(InvalidArgument):
        watch_run.run(["u1", "--install-service", "--exec", "logger -t x"])


@pytest.mark.parametrize("flag", [["--duration", "60"], ["--dry-run"], ["--heartbeat", "x.json"]])
def test_install_service_refuses_one_shot_flags(env: Path, flag: list[str]) -> None:
    p = env / "3d-cli" / "printer-devices.yaml"
    p.parent.mkdir(parents=True)
    p.write_text("u1: {adapter: moonraker, host: h}\n")
    with pytest.raises(UsageError):
        watch_run.run(["u1", "--install-service", *flag])
    assert not (env / "systemd").exists()


def test_default_heartbeat_names_are_unambiguous(env: Path) -> None:
    assert watch_run.default_heartbeat(["a", "b"]) != watch_run.default_heartbeat(["a-b"])


# ---- streams against loopback fakes ------------------------------------------------------------

@pytest.fixture()
def fake_ws() -> Iterator[FakeMoonrakerWS]:
    exc = {"id": 532, "index": 0, "code": 2, "message": "detected noodle", "level": 2}
    fake = FakeMoonrakerWS(pushes=[
        {"jsonrpc": "2.0", "method": "notify_proc_stat_update", "params": [{}]},
        {"jsonrpc": "2.0", "method": "notify_status_update",
         "params": [{"print_stats": {"state": "paused", "exception": exc}}, 2.0]},
        {"jsonrpc": "2.0", "method": "notify_gcode_response", "params": ["!! Error evaluating 'gcode_macro X:gcode'"]},
        {"jsonrpc": "2.0", "method": "notify_status_update", "params": [{"toolhead": {"homed_axes": ""}}, 3.0]},
        {"jsonrpc": "2.0", "method": "notify_status_update", "params": [{"heater_bed": {"target": 0.0}}, 4.0]},
    ])
    with fake.serve():
        yield fake


def _wait_for(pred: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not pred():
        time.sleep(0.02)


def test_moonraker_stream_end_to_end_is_read_only(fake_ws: FakeMoonrakerWS, tmp_path: Path) -> None:
    dev = devices.Device("u1", "moonraker", "127.0.0.1", fake_ws.port, None, tmp_path)
    got: list[Event] = []
    stop = threading.Event()
    hb = watch_run.Heartbeat(tmp_path / "hb.json")
    st = watch_run.stream_for(dev, got.append, stop, hb, WatchLimits(confirm_s=0))
    st.start()
    _wait_for(lambda: any(e.kind == "bed_off" for e in got))
    stop.set()
    st.join(5)
    assert [e.kind for e in got] == ["snapshot", "exception", "state", "gcode_error", "homing_lost", "bed_off"]
    assert fake_ws.methods == ["server.info", "printer.objects.list", "printer.objects.subscribe"]
    assert "filament_motion_sensor e0_filament" in fake_ws.subscribed[0]
    hb.write({}, {})
    beat = json.loads((tmp_path / "hb.json").read_text())
    assert beat["printers"]["u1"]["connected"] is True and beat["printers"]["u1"]["state"] == "paused"


def _run_stream(fake: FakeMoonrakerWS, tmp_path: Path, until: Any) -> list[Event]:
    dev = devices.Device("u1", "moonraker", "127.0.0.1", fake.port, None, tmp_path)
    stop = threading.Event()
    got: list[Event] = []
    st = watch_run.stream_for(dev, got.append, stop, watch_run.Heartbeat(None), WatchLimits())
    st.start()
    _wait_for(until)
    time.sleep(0.2)
    stop.set()
    st.join(5)
    return got


def test_moonraker_stream_waits_for_klippy_ready(tmp_path: Path) -> None:
    fake = FakeMoonrakerWS(klippy_state="startup", pushes=[])
    with fake.serve():
        _run_stream(fake, tmp_path, lambda: "server.info" in fake.methods)
    assert fake.methods == ["server.info"]


def test_moonraker_stream_subscribes_once_klippy_is_ready(tmp_path: Path) -> None:
    fake = FakeMoonrakerWS(klippy_state="startup", pushes=[{"jsonrpc": "2.0", "method": "notify_klippy_ready"}])
    with fake.serve():
        got = _run_stream(fake, tmp_path, lambda: "printer.objects.subscribe" in fake.methods)
    assert fake.methods == ["server.info", "printer.objects.list", "printer.objects.subscribe"]
    assert "snapshot" in [e.kind for e in got]


def test_moonraker_stream_resubscribes_after_firmware_restart(tmp_path: Path) -> None:
    fake = FakeMoonrakerWS(pushes=[{"jsonrpc": "2.0", "method": "notify_klippy_ready"}])
    with fake.serve():
        _run_stream(fake, tmp_path, lambda: fake.methods.count("printer.objects.subscribe") == 2)
    assert fake.methods == ["server.info", "printer.objects.list", "printer.objects.subscribe",
                            "printer.objects.list", "printer.objects.subscribe"]


def test_stream_reports_unreachable_printer(tmp_path: Path) -> None:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here
    dev = devices.Device("u1", "moonraker", "127.0.0.1", port, None, tmp_path)
    got: list[Event] = []
    stop = threading.Event()
    st = watch_run.stream_for(dev, got.append, stop, watch_run.Heartbeat(None), WatchLimits())
    st.start()
    _wait_for(lambda: bool(got))
    stop.set()
    st.join(5)
    assert got[0].kind == "connection" and "cannot connect" in got[0].text


def test_creality_stream_answers_heartbeat_only(tmp_path: Path) -> None:
    fake = FakeCreality(state=k1_state(1, 1, "part.gcode"))
    with fake.serve():
        dev = devices.Device("k1se", "creality-stock", "127.0.0.1", fake.http_port, None, tmp_path, fake.ws_port)
        got: list[Event] = []
        stop = threading.Event()
        st = watch_run.stream_for(dev, got.append, stop, watch_run.Heartbeat(None), WatchLimits())
        st.start()
        _wait_for(lambda: bool(got) and bool(fake.ws_received))
        stop.set()
        st.join(5)
    assert got[0].kind == "snapshot" and got[0].data["state"] == "printing"
    assert fake.ws_received == ["ok"]
    assert fake.writes() == []


def test_cli_watch_duration_prints_jsonl(fake_ws: FakeMoonrakerWS, env: Path,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    p = env / "3d-cli" / "printer-devices.yaml"
    p.parent.mkdir(parents=True)
    p.write_text(f"u1: {{adapter: moonraker, host: 127.0.0.1, port: {fake_ws.port}}}\n")
    assert watch_run.run(["u1", "--duration", "1.5", "--min-severity", "warning"]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    # homing_lost / bed_off are held 10 s (end-macro confirmation, #190): a 1.5 s run does not reach them
    assert [r["kind"] for r in rows] == ["exception", "state", "gcode_error"]
    beat = json.loads((env.parent / "state" / "3d-cli" / "watch" / "u1.json").read_text())
    assert beat["sinks"]["stdout"]["delivered"] == 3


def test_write_devices_fixture_still_loads(env: Path) -> None:
    fake = FakeCreality()
    with fake.serve():
        write_devices(env, k1=fake)
        assert devices.load_devices()["k1se"].watch == {}
