"""flat_profiles: fleet presets folded flat, overrides in a fixed order, typos caught, requested speeds derived."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from k1se_fixtures import ELEGOO, FILAMENT, MACHINE, PROCESS, isolate_presets

from errors import InputNotFound, InvalidArgument
from prep import flat_profiles
from prep.flat_profiles import (
    Overlay,
    build_profiles,
    load_overlay,
    parse_assignments,
    speed_limits,
)

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def presets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return isolate_presets(monkeypatch, tmp_path)


def test_fleet_presets_are_folded_flat_and_made_compatible() -> None:
    p = build_profiles("k1se")
    assert p.names == {"machine": MACHINE, "process": PROCESS, "filament": FILAMENT}
    assert not p.renamed
    # the user copy's accelerations win over the system machine, the parent's keys are still there
    assert p.machine["machine_max_acceleration_extruding"] == ["8900", "8900"]
    assert p.machine["printable_area"][1] == "220x0"
    assert (
        p.process["compatible_printers"]
        == [MACHINE]
        == p.filament["compatible_printers"]
    )
    assert "inherits" not in p.process
    assert (p.machine["type"], p.process["type"], p.filament["type"]) == (
        "machine",
        "process",
        "filament",
    )
    assert (
        p.machine["printer_settings_id"] == MACHINE
        and p.process["print_settings_id"] == PROCESS
    )
    assert p.filament["filament_settings_id"] == [FILAMENT]


def test_elegoo_rapid_pla_plus_is_the_measured_filament() -> None:
    p = build_profiles("k1se", filament_preset=ELEGOO)
    f = p.filament
    assert p.names["filament"] == ELEGOO and f["filament_settings_id"] == [ELEGOO]
    assert f["nozzle_temperature"] == ["235"] and f["hot_plate_temp"] == ["60"]
    assert f["filament_flow_ratio"] == ["0.98"] and f[
        "filament_max_volumetric_speed"
    ] == ["30"]
    assert f["pressure_advance"] == ["0.04"]


def test_the_repo_ships_the_elegoo_preset_with_its_info_file() -> None:
    d = REPO / "profiles" / "snapmaker-orca" / "filament"
    data = json.loads((d / f"{ELEGOO}.json").read_text(encoding="utf-8"))
    assert data["inherits"] == FILAMENT and data["name"] == ELEGOO
    assert data["filament_max_volumetric_speed"] == ["30"] and data[
        "nozzle_temperature"
    ] == ["235"]
    assert (d / f"{ELEGOO}.info").read_text(encoding="utf-8").startswith("base_id = ")


def test_overrides_apply_preset_then_flags_then_overlay_then_set() -> None:
    overlay = Overlay(
        process={"outer_wall_speed": 250, "inner_wall_speed": 400},
        filament={"pressure_advance": 0.03},
    )
    overlay = overlay.merged(
        {"inner_wall_speed": "500"}, {"filament_max_volumetric_speed": "30"}
    )
    p = build_profiles(
        "k1se",
        process_flags={"wall_loops": "3", "outer_wall_speed": "100"},
        overlay=overlay,
    )
    assert p.process["wall_loops"] == "3"  # prep's own flag over the preset (2)
    assert p.process["outer_wall_speed"] == "250"  # overlay over the flag (100)
    assert p.process["inner_wall_speed"] == "500"  # --set over the overlay (400)
    assert p.filament["pressure_advance"] == ["0.03"] and p.filament[
        "filament_max_volumetric_speed"
    ] == ["30"]


def test_a_scalar_takes_the_shape_of_the_presets_own_value() -> None:
    p = build_profiles(
        "k1se", overlay=Overlay(machine={"machine_max_acceleration_extruding": 12000})
    )
    assert p.machine["machine_max_acceleration_extruding"] == ["12000", "12000"]
    assert flat_profiles.coerce("x", True) == "1" and flat_profiles.coerce(
        ["1"], [2, 3]
    ) == ["2", "3"]
    assert flat_profiles.coerce("x", 250.0) == "250"


def test_a_filament_identity_override_is_one_name_or_an_error() -> None:
    named = build_profiles(
        "k1se", overlay=Overlay(filament={"filament_settings_id": "Custom PLA"})
    )
    assert named.names["filament"] == "Custom PLA"
    assert named.filament["filament_settings_id"] == ["Custom PLA"]
    with pytest.raises(InvalidArgument):
        build_profiles("k1se", overlay=Overlay(filament={"filament_settings_id": []}))
    with pytest.raises(InvalidArgument):
        build_profiles(
            "k1se", overlay=Overlay(filament={"filament_settings_id": ["a", "b"]})
        )


def test_requested_settings_are_what_the_gcode_must_echo() -> None:
    overlay = Overlay(
        process={"outer_wall_speed": 250, "small_perimeter_speed": 120},
        filament={"filament_max_volumetric_speed": 30},
        machine={"machine_max_acceleration_extruding": 12000},
    )
    p = build_profiles(
        "k1se",
        process_flags={"wall_loops": "3", "sparse_infill_density": "15%"},
        overlay=overlay,
    )
    assert (
        p.requested
        == {
            "wall_loops": "3",
            "sparse_infill_density": "15%",
            "enable_support": "0",
            "outer_wall_speed": "250",
            "small_perimeter_speed": "120",  # not in the preset chain, still a real Orca setting: accepted
            "filament_max_volumetric_speed": "30",
            "machine_max_acceleration_extruding": "12000,12000",
            "printer_settings_id": p.names["machine"],
            "print_settings_id": p.names["process"],
            "filament_settings_id": FILAMENT,
        }
    )


def test_supports_none_forces_them_off_and_auto_is_the_tear_off_recipe() -> None:
    assert build_profiles("k1se").process["enable_support"] == "0"  # the preset had 1
    auto = build_profiles("k1se", supports="auto").process
    assert auto["enable_support"] == "1" and auto["support_type"] == "normal(auto)"
    assert (
        auto["support_threshold_angle"] == "45"
        and auto["support_on_build_plate_only"] == "1"
    )
    with pytest.raises(InvalidArgument):
        build_profiles("k1se", supports="tree")


def test_fleet_names_stay_unless_a_compared_acceleration_changes_value() -> None:
    same = build_profiles(
        "k1se",
        overlay=Overlay(
            process={"default_acceleration": 8000, "outer_wall_speed": 250}
        ),
    )
    assert not same.renamed and same.names["process"] == PROCESS
    changed = build_profiles(
        "k1se", overlay=Overlay(process={"inner_wall_acceleration": 16000})
    )
    assert changed.renamed and changed.names["process"] == PROCESS + " - prep overrides"
    assert changed.names["machine"] == MACHINE  # only the process changed
    assert changed.process["print_settings_id"] == changed.names["process"]
    both = build_profiles(
        "k1se", overlay=Overlay(machine={"machine_max_acceleration_x": 16000})
    )
    assert both.names["machine"].endswith("prep overrides") and both.names[
        "process"
    ].endswith("prep overrides")
    assert both.process["compatible_printers"] == [both.names["machine"]]


def test_unknown_printer_lists_the_known_ones() -> None:
    with pytest.raises(InvalidArgument) as exc:
        build_profiles("u1")
    assert "k1se" in str(exc.value)


def test_files_are_written_per_kind(tmp_path: Path) -> None:
    paths = build_profiles("k1se").write(tmp_path / "out")
    assert sorted(paths) == ["filament", "machine", "process"]
    assert json.loads(paths["process"].read_text(encoding="utf-8"))["name"] == PROCESS


def test_overlay_file_sections_and_errors(tmp_path: Path) -> None:
    f = tmp_path / "o.json"
    f.write_text(
        json.dumps(
            {"process": {"travel_speed": 600}, "filament": {"pressure_advance": 0.04}}
        )
    )
    o = load_overlay(f)
    assert o.process == {"travel_speed": 600} and o.machine == {}
    f.write_text(json.dumps({"processes": {}}))
    with pytest.raises(InvalidArgument):
        load_overlay(f)
    f.write_text(json.dumps({"process": ["a"]}))
    with pytest.raises(InvalidArgument):
        load_overlay(f)
    falsy_sections: list[object] = [[], False, 0, ""]
    for falsy in falsy_sections:
        f.write_text(json.dumps({"process": falsy}))
        with pytest.raises(InvalidArgument):
            load_overlay(f)
    bad_values: list[object] = [None, {"a": 1}, [[1]], [None]]
    for bad in bad_values:
        f.write_text(json.dumps({"process": {"wall_loops": bad}}))
        with pytest.raises(InvalidArgument):
            load_overlay(f)
    f.write_text(
        json.dumps({"machine": {"machine_max_acceleration_extruding": [8000, 9000]}})
    )
    assert load_overlay(f).machine == {
        "machine_max_acceleration_extruding": [8000, 9000]
    }
    f.write_text("{not json")
    with pytest.raises(InvalidArgument):
        load_overlay(f)
    with pytest.raises(InputNotFound):
        load_overlay(tmp_path / "missing.json")


def test_parse_assignments() -> None:
    assert parse_assignments(["a=1", "b = x=y"], "--set") == {"a": "1", "b": " x=y"}
    with pytest.raises(InvalidArgument):
        parse_assignments(["nokey"], "--set")
    with pytest.raises(InvalidArgument):
        parse_assignments(["=1"], "--set")


def test_speed_limits_are_the_largest_setting_that_can_set_a_feature() -> None:
    p = build_profiles(
        "k1se",
        overlay=Overlay(
            process={
                "outer_wall_speed": 250,
                "inner_wall_speed": 500,
                "sparse_infill_speed": 550,
                "travel_speed": 600,
                "overhang_2_4_speed": 200,
                "overhang_4_4_speed": 60,
                "initial_layer_speed": 50,
                "initial_layer_infill_speed": 80,
            }
        ),
    )
    limits = speed_limits(p.process)
    assert limits["Sparse infill"] == 550 and limits["travel"] == 600
    assert limits["Inner wall"] == 500
    assert (
        limits["Outer wall"] == 250
    )  # `small_perimeter_speed` is "50%" of it in the preset
    assert (
        limits["Overhang wall"] == 500
    )  # the largest of the overhang steps and the wall speeds
    assert (
        limits["Bridge"] == 80
    )  # the first layer runs bridges at the first-layer speeds: never below 80
    assert limits["Top surface"] == 200


def test_a_percentage_other_than_the_small_perimeter_one_gives_no_limit() -> None:
    limits = speed_limits(
        {"outer_wall_speed": "200", "bridge_speed": "50%", "travel_speed": "500"}
    )
    assert limits == {"Outer wall": 200.0, "Overhang wall": 200.0, "travel": 500.0}


def test_a_zero_speed_means_auto_and_gives_no_limit() -> None:
    assert "Bridge" not in speed_limits(
        {"bridge_speed": "0", "initial_layer_speed": "0"}
    )
    assert speed_limits({}) == {}
