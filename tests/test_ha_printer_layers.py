"""Render the actual HA package templates against captured printer telemetry."""

from __future__ import annotations

import datetime as dt
import unittest
from pathlib import Path
from types import SimpleNamespace

import yaml
from jinja2.nativetypes import NativeEnvironment


PACKAGE = Path(__file__).resolve().parents[1] / "integrations/homeassistant/packages/3d_printers.yaml"
CURRENT = "/usr/data/printer_data/gcodes/M6-M16x25-cilinder.gcode"
RAW = "sensor.k1se_85b6_print_status"
CACHE = "sensor.printer_k1_se_file_layers"


class StateLookup:
    def __init__(self, values: dict[str, str], status: SimpleNamespace) -> None:
        self.values = values
        self.sensor = SimpleNamespace(k1se_85b6_print_status=status)

    def __call__(self, entity: str) -> str:
        return self.values.get(entity, "unknown")


class PrinterLayersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.reset_scenario()

    def reset_scenario(self) -> None:
        self.package = yaml.safe_load(PACKAGE.read_text(encoding="utf-8"))
        self.cache = next(
            section
            for section in self.package["template"]
            if any(sensor.get("unique_id") == "printer_k1_se_file_layers"
                   for sensor in section.get("sensor", []))
        )
        self.sensor = next(
            sensor for sensor in self.cache["sensor"]
            if sensor.get("unique_id") == "printer_k1_se_file_layers"
        )
        self.total = next(
            sensor
            for section in self.package["template"]
            for sensor in section.get("sensor", [])
            if sensor.get("unique_id") == "printer_k1_se_total_layers"
        )
        self.status = SimpleNamespace(last_changed=dt.datetime.fromtimestamp(100, dt.timezone.utc))
        self.states = {"sensor.k1se_85b6_total_layers": "0", CACHE: "104"}
        self.attrs: dict[str, dict[str, object]] = {
            RAW: {"file": CURRENT},
            CACHE: {"file": CURRENT, "revision": self.status.last_changed.isoformat(),
                    "attempts": 1, "attempted_at": 100, "completed_at": 101},
        }
        self.lookup = StateLookup(self.states, self.status)
        self.env = NativeEnvironment()
        self.env.globals.update(
            states=self.lookup,
            state_attr=lambda entity, key: self.attrs.get(entity, {}).get(key),
            now=lambda: dt.datetime.fromtimestamp(200, dt.timezone.utc),
            as_timestamp=lambda value: value.timestamp(),
        )
        self.variables: dict[str, object] = {}
        self.refresh_variables()
        self.start_request()
        self.variables["metadata_response"] = self.response()

    def refresh_variables(
        self, previous: str | None = "printing", current: str = "printing",
        platform: str = "state",
    ) -> None:
        self.variables = {
            "trigger": {
                "platform": platform,
                "from_state": {"state": previous} if previous is not None else None,
                "to_state": {"state": current},
            },
        }
        for name, template in self.cache["variables"].items():
            self.variables[name] = self.render(template)

    def start_request(self) -> None:
        for name, value in self.cache["actions"][0]["variables"].items():
            self.variables[name] = self.render(value) if isinstance(value, str) else value

    @staticmethod
    def response(filename: str = "M6-M16x25-cilinder.gcode", layers: int = 104) -> dict[str, object]:
        return {"status": 200, "content": {"result": {"filename": filename, "layer_count": layers}}}

    def render(self, template: str) -> object:
        return self.env.from_string(template).render(self.variables)

    def value(self) -> object:
        return self.render(self.total["state"])

    def should_fetch(self) -> object:
        return self.render(self.cache["conditions"][0]["value_template"])

    def finish_request(self) -> None:
        for name, template in self.cache["actions"][-1]["variables"].items():
            self.variables[name] = self.render(template)

    def response_value(self) -> object:
        self.finish_request()
        return self.render(self.sensor["state"])

    def rendered_attributes(self) -> dict[str, object]:
        return {name: self.render(template) for name, template in self.sensor["attributes"].items()}

    def test_captured_orca_job_uses_104_metadata_layers(self) -> None:
        self.assertEqual(self.value(), 104)
        self.assertEqual(self.response_value(), 104)

    def test_positive_live_count_takes_priority(self) -> None:
        self.states["sensor.k1se_85b6_total_layers"] = "120"
        self.assertEqual(self.value(), 120)

    def test_missing_or_zero_metadata_stays_unknown(self) -> None:
        for value in ("0", "unknown", "unavailable"):
            self.states[CACHE] = value
            self.assertIsNone(self.value())

    def test_previous_file_is_never_used(self) -> None:
        self.attrs[CACHE]["file"] = CURRENT.replace("gcodes/", "gcodes/previous/")
        self.refresh_variables()
        self.assertIsNone(self.value())
        self.assertTrue(self.should_fetch())

    def test_metadata_response_filename_must_match_exact_relative_path(self) -> None:
        self.attrs[RAW]["file"] = CURRENT.replace("gcodes/", "gcodes/subdir/")
        self.refresh_variables()
        self.start_request()
        self.variables["metadata_response"] = self.response()
        self.assertEqual(self.variables["metadata_filename"], "subdir/M6-M16x25-cilinder.gcode")
        self.assertIsNone(self.response_value())

    def test_late_response_preserves_cache_but_is_not_used_for_a_new_file(self) -> None:
        self.attrs[RAW]["file"] = CURRENT.replace("cilinder", "next")
        self.assertEqual(self.response_value(), 104)
        self.assertEqual(self.rendered_attributes()["file"], CURRENT)
        self.assertIsNone(self.value())

    def test_removed_raw_entity_short_circuits_post_request_revision_check(self) -> None:
        self.attrs[RAW]["file"] = None
        self.lookup.sensor.k1se_85b6_print_status = None
        self.assertEqual(self.response_value(), 104)
        self.assertFalse(self.variables["request_current"])
        self.assertEqual(self.rendered_attributes(), {**self.attrs[CACHE], "completed_at": 200})
        self.assertIsNone(self.value())

    def test_failed_or_malformed_metadata_does_not_publish_zero(self) -> None:
        for response in ({}, {"status": 404}, {"status": 200, "content": "bad"},
                         {"status": 200, "content": {"result": None}},
                         self.response(layers=0)):
            self.variables["metadata_response"] = response
            self.assertIsNone(self.response_value())

    def test_cached_metadata_stops_requests_from_regular_telemetry(self) -> None:
        self.refresh_variables()
        self.assertFalse(self.should_fetch())

    def test_status_changes_including_pause_resume_refetch_same_filename(self) -> None:
        for previous, current in (("idle", "printing"), ("complete", "printing"),
                                  ("paused", "printing"), ("printing", "paused"),
                                  ("printing", "printing")):
            with self.subTest(previous=previous, current=current):
                self.reset_scenario()
                self.attrs[CACHE]["attempts"] = 3
                changed = previous != current
                if changed:
                    self.status.last_changed = dt.datetime.fromtimestamp(190, dt.timezone.utc)
                self.refresh_variables(previous=previous, current=current)
                self.assertFalse(self.variables["refresh"])
                self.assertEqual(self.variables["cache_invalidated"], changed)
                self.assertEqual(self.variables["attempts"], 0 if changed else 3)
                self.assertEqual(self.should_fetch(), changed)
                if changed:
                    self.assertIsNone(self.value())

    def test_start_and_reload_refresh_restored_cache(self) -> None:
        for platform in ("homeassistant", "event"):
            self.refresh_variables(platform=platform)
            self.assertTrue(self.variables["refresh"])
            self.assertEqual(self.variables["attempts"], 0)
            self.assertTrue(self.should_fetch())

    def test_failed_lookup_retries_are_bounded_and_spaced(self) -> None:
        self.states[CACHE] = "unknown"
        self.refresh_variables()
        self.assertTrue(self.should_fetch())
        self.attrs[CACHE]["attempted_at"] = 180
        self.refresh_variables()
        self.assertFalse(self.should_fetch())
        self.attrs[CACHE]["attempted_at"] = 100
        for attempts, fetch in ((1, True), (2, True), (3, False)):
            self.attrs[CACHE]["attempts"] = attempts
            self.refresh_variables()
            self.assertEqual(self.variables["attempts"], attempts)
            self.assertEqual(self.should_fetch(), fetch)

    def test_skipped_concurrent_trigger_preserves_cache_and_retry_budget(self) -> None:
        self.refresh_variables()
        self.assertEqual(self.render(self.sensor["state"]), 104)
        self.assertEqual(self.rendered_attributes(), self.attrs[CACHE])

    def test_missing_startup_entity_waits_for_telemetry(self) -> None:
        self.attrs[RAW]["file"] = None
        self.lookup.sensor.k1se_85b6_print_status = None
        self.refresh_variables(previous=None, platform="homeassistant")
        self.assertEqual(self.variables["filename"], "")
        self.assertEqual(self.variables["revision"], "")
        self.assertFalse(self.should_fetch())

    def test_first_raw_state_refetches_a_restored_same_filename(self) -> None:
        self.status.last_changed = dt.datetime.fromtimestamp(190, dt.timezone.utc)
        self.refresh_variables(previous=None)
        self.assertTrue(self.variables["cache_invalidated"])
        self.assertEqual(self.variables["attempts"], 0)
        self.assertTrue(self.should_fetch())
        self.assertIsNone(self.value())

    def test_metadata_filename_preserves_relative_subdirectories(self) -> None:
        for filename in ("subdir/part.gcode", "/usr/data/printer_data/gcodes/subdir/part.gcode"):
            with self.subTest(filename=filename):
                self.attrs[RAW]["file"] = filename
                self.refresh_variables()
                self.assertEqual(self.variables["metadata_filename"], "subdir/part.gcode")

    def test_same_filename_reconnect_invalidates_ambiguous_old_cache(self) -> None:
        self.status.last_changed = dt.datetime.fromtimestamp(190, dt.timezone.utc)
        self.refresh_variables(previous="unavailable")
        self.assertTrue(self.should_fetch())
        self.assertIsNone(self.value())
        self.start_request()
        self.variables["metadata_response"] = {}
        self.assertIsNone(self.response_value())

    def test_completion_retries_a_skipped_different_or_same_filename_job(self) -> None:
        for filename in (CURRENT, CURRENT.replace("cilinder", "next")):
            with self.subTest(filename=filename):
                self.reset_scenario()
                old_request = self.variables.copy()
                old_cache = self.attrs[CACHE].copy()
                self.attrs[RAW]["file"] = filename
                self.status.last_changed = dt.datetime.fromtimestamp(190, dt.timezone.utc)
                self.refresh_variables(previous="idle")
                self.assertTrue(self.should_fetch())
                self.assertEqual(self.render(self.sensor["state"]), 104)
                self.assertEqual(self.rendered_attributes(), old_cache)
                self.assertIsNone(self.value())

                self.variables = old_request
                self.assertEqual(self.response_value(), 104)
                completed_cache = self.rendered_attributes()
                self.assertEqual(completed_cache, {**old_cache, "completed_at": 200})
                self.attrs[CACHE] = completed_cache

                self.refresh_variables(previous="104", current="104")
                self.assertTrue(self.variables["cache_invalidated"])
                self.assertEqual(self.variables["attempts"], 0)
                self.assertTrue(self.should_fetch())
                self.start_request()
                self.assertEqual(self.variables["attempts"], 1)
                relative = filename.removeprefix("/usr/data/printer_data/gcodes/")
                self.variables["metadata_response"] = self.response(relative, layers=120)
                self.states[CACHE] = str(self.response_value())
                self.attrs[CACHE] = self.rendered_attributes()
                self.assertEqual(self.value(), 120)

    def test_no_added_timer_or_printer_mutation(self) -> None:
        command = self.package["rest_command"]["printer_k1_se_file_metadata"]
        self.assertEqual(command["method"], "get")
        self.assertIn("/server/files/metadata?", command["url"])
        self.assertNotIn("time_pattern", {trigger["trigger"] for trigger in self.cache["triggers"]})
        self.assertIn(
            {"trigger": "state", "entity_id": CACHE, "attribute": "completed_at"},
            self.cache["triggers"],
        )


if __name__ == "__main__":
    unittest.main()
