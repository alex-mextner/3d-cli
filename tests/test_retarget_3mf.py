"""retarget_3mf: moving an Orca-family project 3MF from a U1 to a K1-like printer."""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any

import pytest
from retarget_fixtures import TWO, Box, make_project, write_presets

from errors import GateFailure, InputNotFound, InvalidArgument
from slicing import orca_presets
from slicing import retarget_3mf as rt


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("THREED_PROFILES_DIR", str(tmp_path / "no-profiles"))
    return write_presets(tmp_path / "presets")


def source(
    env: Path, query: str = "Test K1 0.4 nozzle", kind: str = "machine"
) -> rt.PresetSource:
    catalog = orca_presets.PresetCatalog([env])
    ref = catalog.find(kind, query)
    return rt.PresetSource(ref.name, catalog.flatten(ref))


def check(
    parts: dict[str, bytes], report: rt.Report, *, margin: float = 5.0, nozzles: int = 1
) -> list[str]:
    return rt.validate_parts(parts, bed=report.bed, margin=margin, nozzles=nozzles)


def grab(pattern: str, text: str, flags: int = 0) -> str:
    """The first match of `pattern` in `text` (group 1 when it has one); fails the test if absent."""
    m = re.search(pattern, text, flags)
    assert m is not None, f"{pattern!r} not found"
    return m.group(1) if m.groups() else m.group(0)


def world_bounds(
    parts: dict[str, bytes],
) -> tuple[float, float, float, float, float, float]:
    """Independent of the production code: world AABB of every build item from raw XML + 3x3 math."""
    main = parts["3D/3dmodel.model"].decode()
    lo, hi = [1e9] * 3, [-1e9] * 3
    for item in re.findall(r"<item\b[^>]*/>", main):
        oid = grab(r'objectid="(\d+)"', item)
        m = [float(v) for v in grab(r'transform="([^"]*)"', item).split()]
        block = grab(rf'<object id="{oid}".*?</object>', main, re.DOTALL)
        sub = grab(r'p:path="/([^"]+)"', block)
        for x, y, z in re.findall(
            r'<vertex x="([^"]+)" y="([^"]+)" z="([^"]+)"', parts[sub].decode()
        ):
            v = (float(x), float(y), float(z))
            w = [
                sum(v[k] * m[3 * k + c] for k in range(3)) + m[9 + c] for c in range(3)
            ]
            lo = [min(a, b) for a, b in zip(lo, w, strict=True)]
            hi = [max(a, b) for a, b in zip(hi, w, strict=True)]
    return (lo[0], lo[1], lo[2], hi[0], hi[1], hi[2])


def settings_of(parts: dict[str, bytes]) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(parts["Metadata/project_settings.config"])
    return data


def build(
    tmp_path: Path,
    env: Path,
    boxes: list[Box],
    *,
    filaments: int = 1,
    keep: str = "",
    margin: float = 5.0,
    setting_sets: tuple[str, ...] = (),
    overrides: tuple[tuple[str, str], ...] = (),
) -> tuple[dict[str, bytes], rt.Report]:
    src = make_project(tmp_path / "in.3mf", boxes, filaments=filaments)
    return rt.retarget(
        src,
        machine=source(env),
        keep=keep,
        margin=margin,
        setting_sets=setting_sets,
        overrides=overrides,
    )


def test_printer_keys_come_from_the_target_and_u1_macros_do_not_survive(
    tmp_path: Path, env: Path
) -> None:
    parts, report = build(tmp_path, env, TWO, keep="1", filaments=2)
    s = settings_of(parts)
    assert s["printable_area"] == ["0x0", "220x0", "220x220", "0x220"]
    assert s["printable_height"] == "250"
    assert (
        s["printer_settings_id"] == "Test K1 0.4 nozzle"
        and s["printer_model"] == "Test K1"
    )
    assert s["machine_start_gcode"].startswith("START_PRINT")
    assert (
        s["machine_end_gcode"] == "END_PRINT"
    )  # inherited base says BASE_END; the child wins
    assert s["before_layer_change_gcode"] == ";K1 LAYER"
    # Not defined by the K1 preset: cleared rather than left as macros that do not exist on it.
    assert (
        s["time_lapse_gcode"] == ""
        and s["change_filament_gcode"] == ""
        and s["machine_pause_gcode"] == ""
    )
    text = json.dumps(s)
    assert (
        "DEFECT_DETECTION" not in text
        and "U1_ONLY" not in text
        and "PRINT_START" not in text
    )
    assert any("time_lapse_gcode" in n for n in report.notes)


def test_extruders_and_filaments_collapse_to_one(tmp_path: Path, env: Path) -> None:
    parts, _ = build(tmp_path, env, TWO, keep="2", filaments=2)
    s = settings_of(parts)
    assert s["nozzle_diameter"] == ["0.4"]
    assert (
        s["extruder_colour"] == ["#FCE94F"]
        and s["extruder_offset"] == ["0x0"]
        and s["retract_lift_above"] == ["0"]
    )
    assert (
        s["filament_settings_id"] == ["Snapmaker PLA @U1"]
        and len(s["filament_colour"]) == 1
    )
    assert s["nozzle_temperature"] == ["220"] and s["filament_flow_ratio"] == ["0.966"]
    assert s["support_filament"] == "1"  # was slot 2
    assert s["enable_prime_tower"] == "0" and s["flush_volumes_matrix"] == ["0"]
    ms = parts["Metadata/model_settings.config"].decode()
    assert set(re.findall(r'key="extruder" value="(\d+)"', ms)) == {
        "1"
    }  # the kept object was on extruder 2
    assert 'key="filament_maps" value="1"' in ms


def test_keep_drops_the_other_object_everywhere(tmp_path: Path, env: Path) -> None:
    parts, report = build(tmp_path, env, TWO, keep="2")
    assert report.kept == [2]
    main = parts["3D/3dmodel.model"].decode()
    assert main.count("<item ") == 1 and 'objectid="4"' in main and 'id="2"' not in main
    assert "3D/Objects/box_1.model" not in parts and "3D/Objects/box_2.model" in parts
    assert "box_1" not in parts["3D/_rels/3dmodel.model.rels"].decode()
    ms = parts["Metadata/model_settings.config"].decode()
    assert (
        'name" value="left"' not in ms
        and 'value="right"' in ms
        and 'object_id="2"' not in ms
    )
    assert ms.count("<assemble_item") == 1
    assert check(parts, report, margin=5) == []


def test_part_is_moved_onto_the_new_bed_with_margin(tmp_path: Path, env: Path) -> None:
    parts, report = build(
        tmp_path, env, TWO, keep="1"
    )  # centred at (200, 200): off a 220 bed
    x0, y0, z0, x1, y1, z1 = world_bounds(parts)
    assert x0 >= 5 - 1e-6 and y0 >= 5 - 1e-6 and x1 <= 215 + 1e-6 and y1 <= 215 + 1e-6
    assert (x0 + x1) / 2 == pytest.approx(110, abs=1e-3) and (
        y0 + y1
    ) / 2 == pytest.approx(110, abs=1e-3)
    assert z0 == pytest.approx(0, abs=1e-6) and z1 == pytest.approx(
        30
    )  # lifted/rotated only about Z
    assert report.placement.angle_deg == 0.0


def test_too_long_part_is_rotated_about_z_and_still_sits_on_the_bed(
    tmp_path: Path, env: Path
) -> None:
    parts, report = build(
        tmp_path, env, [Box("long", (240, 20, 10), center=(100, 100))], keep="1"
    )
    assert report.placement.angle_deg != 0.0
    x0, y0, z0, x1, y1, z1 = world_bounds(parts)
    assert x0 >= 5 - 1e-6 and x1 <= 215 + 1e-6 and y0 >= 5 - 1e-6 and y1 <= 215 + 1e-6
    assert (z0, z1) == (pytest.approx(0, abs=1e-6), pytest.approx(10))
    assert (x1 - x0) < 240  # no longer axis-aligned along X


def test_oversize_part_fails_loudly_with_footprint_and_bed(
    tmp_path: Path, env: Path
) -> None:
    with pytest.raises(GateFailure) as err:
        build(tmp_path, env, [Box("slab", (260, 250, 10))])
    text = err.value.render(color=False)
    assert "does not fit Test K1 0.4 nozzle" in text
    assert "usable 210 x 210" in text and "bed 220 x 220" in text
    assert "--keep" in text
    assert err.value.exit_code == 1


def test_margin_decides_between_fit_and_failure(tmp_path: Path, env: Path) -> None:
    boxes = [Box("snug", (214, 100, 10))]
    with pytest.raises(GateFailure) as err:
        build(tmp_path, env, boxes, margin=5.0)
    assert "--margin" in err.value.render(color=False)
    parts, report = build(tmp_path, env, boxes, margin=2.0)
    assert check(parts, report, margin=2.0) == []


def test_part_taller_than_the_target_fails(tmp_path: Path, env: Path) -> None:
    with pytest.raises(GateFailure) as err:
        build(tmp_path, env, [Box("tower", (20, 20, 260))])
    assert "printable_height" in err.value.render(color=False)


def test_set_overrides_apply_last_and_unknown_keys_are_refused(
    tmp_path: Path, env: Path
) -> None:
    parts, report = build(
        tmp_path,
        env,
        TWO,
        keep="1",
        overrides=(
            ("support_top_z_distance", "0.3"),
            ("nozzle_temperature", '["205"]'),
        ),
    )
    s = settings_of(parts)
    assert s["support_top_z_distance"] == "0.3" and s["nozzle_temperature"] == ["205"]
    assert {c.key: c.origin for c in report.changes}[
        "support_top_z_distance"
    ] == "--set"
    with pytest.raises(InvalidArgument) as err:
        build(tmp_path, env, TWO, overrides=(("support_top_z_distanc", "0.3"),))
    assert "support_top_z_distance" in err.value.render(
        color=False
    )  # a close match is offered


def test_easy_release_preset_changes_only_support_keys(
    tmp_path: Path, env: Path
) -> None:
    parts, report = build(
        tmp_path, env, TWO, keep="1", setting_sets=("easy-release-supports",)
    )
    s = settings_of(parts)
    assert (
        s["support_top_z_distance"] == "0.2" and s["support_bottom_z_distance"] == "0.2"
    )
    assert (
        s["support_interface_pattern"] == "rectilinear"
        and s["support_interface_bottom_layers"] == "0"
    )
    by_origin = {
        c.key for c in report.changes if c.origin == "preset easy-release-supports"
    }
    assert by_origin == {
        "support_top_z_distance",
        "support_bottom_z_distance",
        "support_interface_bottom_layers",
        "support_interface_pattern",
    }  # enable_support, top layers and spacing already had the preset's values


def test_process_and_filament_presets_replace_source_values(
    tmp_path: Path, env: Path
) -> None:
    src = make_project(tmp_path / "in.3mf", TWO)
    parts, report = rt.retarget(
        src,
        machine=source(env),
        process=source(env, "0.20mm Test Process", kind="process"),
        filament=source(env, "Test PLA @K1", kind="filament"),
        keep="1",
    )
    s = settings_of(parts)
    assert (
        s["layer_height"] == "0.2"
        and s["enable_support"] == "0"
        and s["print_settings_id"] == "0.20mm Test Process"
    )
    assert s["filament_settings_id"] == ["Test PLA @K1"] and s[
        "nozzle_temperature"
    ] == ["210"]
    assert s["enable_pressure_advance"] == ["1"] and s["filament_flow_ratio"] == [
        "0.93"
    ]
    assert not any("process settings kept" in n for n in report.notes)


def test_source_process_and_filament_kept_by_default_with_a_note(
    tmp_path: Path, env: Path
) -> None:
    parts, report = build(tmp_path, env, TWO, keep="1")
    s = settings_of(parts)
    assert s["layer_height"] == "0.08" and s["print_settings_id"].startswith(
        "0.12mm Standard @Snapmaker U1"
    )
    assert s["print_compatible_printers"] == []
    assert any("--process" in n for n in report.notes) and any(
        "--filament" in n for n in report.notes
    )


def test_diff_summary_lists_old_and_new_values(tmp_path: Path, env: Path) -> None:
    _, report = build(tmp_path, env, TWO, keep="1")
    changes = {c.key: c for c in report.changes}
    assert (
        changes["printable_height"].old == "270.05"
        and changes["printable_height"].new == "250"
    )
    assert changes["nozzle_diameter"].old == ["0.4"] * 4 and changes[
        "nozzle_diameter"
    ].new == ["0.4"]
    assert "layer_height" not in changes  # untouched keys are not reported


def test_variable_layer_profile_follows_its_object_and_is_called_out(
    tmp_path: Path, env: Path
) -> None:
    parts, report = build(tmp_path, env, TWO, keep="2")
    profile = parts["Metadata/layer_heights_profile.txt"].decode()
    assert (
        profile.startswith("object_id=1|")
        and "0.220000" in profile
        and profile.count("object_id=") == 1
    )
    assert any("variable layer-height profile" in n for n in report.notes)


def test_stale_previews_are_dropped_and_unreferenced(tmp_path: Path, env: Path) -> None:
    parts, _ = build(tmp_path, env, TWO, keep="1")
    assert not [
        n
        for n in parts
        if n.startswith("Metadata/plate_") or n.startswith("Metadata/top_")
    ]
    assert "plate_1.png" not in parts["_rels/.rels"].decode()
    assert "thumbnail_file" not in parts["Metadata/model_settings.config"].decode()


def test_colour_paint_marks_are_stripped_for_a_single_filament_target(
    tmp_path: Path, env: Path
) -> None:
    parts, report = build(
        tmp_path, env, [Box("painted", (30, 30, 10), painted=True)], filaments=2
    )
    assert b"paint_color" not in parts["3D/Objects/box_1.model"]
    assert any("paint marks" in n for n in report.notes)


def test_more_filaments_than_a_multi_extruder_target_has_is_refused(
    tmp_path: Path, env: Path
) -> None:
    src = make_project(tmp_path / "in.3mf", TWO, filaments=3)
    with pytest.raises(InvalidArgument) as err:
        rt.retarget(src, machine=source(env, "Test Dual 0.4 nozzle"), keep="1")
    assert "3 filaments" in err.value.render(color=False)


def test_retargeting_leaves_the_source_file_untouched(
    tmp_path: Path, env: Path
) -> None:
    src = make_project(tmp_path / "in.3mf", TWO)
    before = src.read_bytes()
    rt.retarget(src, machine=source(env), keep="1")
    assert src.read_bytes() == before


def test_written_package_round_trips_and_validates(tmp_path: Path, env: Path) -> None:
    parts, report = build(tmp_path, env, TWO, keep="1")
    out = tmp_path / "out" / "k1.3mf"
    rt.write_package(out, parts)
    assert zipfile.ZipFile(out).testzip() is None
    again = rt.read_package(out)
    assert check(again, report, margin=5) == []
    assert list(again)[0] == "[Content_Types].xml"


# ---- validation must be able to fail --------------------------------------------------------
def test_validation_flags_a_part_pushed_off_the_bed(tmp_path: Path, env: Path) -> None:
    parts, report = build(tmp_path, env, TWO, keep="1")
    bad = dict(parts)
    main = bad["3D/3dmodel.model"].decode()
    bad["3D/3dmodel.model"] = re.sub(
        r'(transform="(?:\S+ ){9})\S+', r"\g<1>400", main
    ).encode()
    problems = check(bad, report, margin=5)
    assert any("leaves the bed" in p for p in problems)


def test_validation_flags_wrong_bed_missing_mesh_and_bad_json(
    tmp_path: Path, env: Path
) -> None:
    parts, report = build(tmp_path, env, TWO, keep="1")
    wrong_bed = dict(parts)
    wrong_bed["Metadata/project_settings.config"] = json.dumps(
        {**settings_of(parts), "printable_area": ["0x0", "270x0", "270x270", "0x270"]}
    ).encode()
    assert any("printable_area" in p for p in check(wrong_bed, report, margin=5))

    no_mesh = {k: v for k, v in parts.items() if not k.startswith("3D/Objects/")}
    assert any(
        "missing from the archive" in p for p in check(no_mesh, report, margin=5)
    )

    broken = dict(parts)
    broken["Metadata/project_settings.config"] = b"{not json"
    assert any("not valid JSON" in p for p in check(broken, report, margin=5))

    missing_key = dict(parts)
    s = settings_of(parts)
    del s["machine_start_gcode"]
    missing_key["Metadata/project_settings.config"] = json.dumps(s).encode()
    assert any(
        "missing key machine_start_gcode" in p
        for p in check(missing_key, report, margin=5)
    )


def test_validation_flags_leftover_extruders_for_a_single_extruder_target(
    tmp_path: Path, env: Path
) -> None:
    parts, report = build(tmp_path, env, TWO, keep="1")
    bad = dict(parts)
    bad["Metadata/model_settings.config"] = parts[
        "Metadata/model_settings.config"
    ].replace(b'key="extruder" value="1"', b'key="extruder" value="3"')
    assert any("extruder > 1" in p for p in check(bad, report, margin=5))


# ---- input handling ------------------------------------------------------------------------
def test_missing_and_non_project_inputs_are_rejected(tmp_path: Path, env: Path) -> None:
    machine = source(env)
    with pytest.raises(InputNotFound):
        rt.retarget(tmp_path / "nope.3mf", machine=machine)
    (tmp_path / "text.3mf").write_text("not a zip", encoding="utf-8")
    with pytest.raises(InvalidArgument):
        rt.retarget(tmp_path / "text.3mf", machine=machine)
    plain = tmp_path / "plain.3mf"
    with zipfile.ZipFile(plain, "w") as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
    with pytest.raises(InvalidArgument) as err:
        rt.retarget(plain, machine=machine)
    assert "not a slicer project" in err.value.render(color=False)


def test_keep_must_name_existing_objects(tmp_path: Path, env: Path) -> None:
    for spec in ("0", "3", "x", ","):
        with pytest.raises(InvalidArgument):
            build(tmp_path, env, TWO, keep=spec)
    assert rt.parse_keep("2,1,2", 2) == [1, 2]


def test_assignment_parsing_and_coercion() -> None:
    assert rt.parse_assignment("a=b=c") == ("a", "b=c")
    with pytest.raises(InvalidArgument):
        rt.parse_assignment("novalue")
    assert rt.coerce_override(["1", "1"], "0.5") == ["0.5", "0.5"]
    assert rt.coerce_override("1", "0.5") == "0.5"
    assert rt.coerce_override(["1"], '["2","3"]') == ["2", "3"]
    with pytest.raises(InvalidArgument):
        rt.coerce_override("1", "[oops")


def test_round_bed_is_refused() -> None:
    with pytest.raises(InvalidArgument):
        rt.bed_from_settings(
            {"printable_area": ["0x0", "100x0", "100x100", "50x150", "0x100"]}
        )
    assert (
        rt.bed_from_settings(
            {"printable_area": ["0.5x1", "270.5x1", "270.5x271", "0.5x271"]}
        ).width
        == 270
    )


# ---- review findings: plates, instances, filaments on multi-extruder targets ----------------
def test_object_from_the_second_plate_is_moved_to_plate_one(
    tmp_path: Path, env: Path
) -> None:
    src = make_project(
        tmp_path / "in.3mf",
        [Box("a", (40, 40, 10)), Box("b", (50, 30, 10), center=(100, 100))],
        plates=((1,), (2,)),
    )
    parts, report = rt.retarget(src, machine=source(env), keep="2")
    ms = parts["Metadata/model_settings.config"].decode()
    assert ms.count("<plate>") == 1 and 'key="plater_id" value="1"' in ms
    assert 'object_id" value="4"' in ms and 'object_id" value="2"' not in ms
    x0, _y0, _z0, x1, _y1, _z1 = world_bounds(parts)
    assert (
        x0 >= 5 - 1e-6 and x1 <= 215 + 1e-6
    )  # centred on plate 1's bed, not 324 mm to the right
    assert check(parts, report) == []


def test_objects_on_different_plates_cannot_be_kept_together(
    tmp_path: Path, env: Path
) -> None:
    src = make_project(
        tmp_path / "in.3mf",
        [Box("a", (40, 40, 10)), Box("b", (50, 30, 10))],
        plates=((1,), (2,)),
    )
    with pytest.raises(InvalidArgument) as err:
        rt.retarget(src, machine=source(env))
    text = err.value.render(color=False)
    assert "plates 1, 2" in text and "one plate" in text


def test_keeping_the_second_instance_renumbers_it_to_instance_zero(
    tmp_path: Path, env: Path
) -> None:
    src = make_project(
        tmp_path / "in.3mf", [Box("twin", (30, 30, 10), center=(60, 60), copies=2)]
    )
    parts, report = rt.retarget(src, machine=source(env), keep="2")
    ms = parts["Metadata/model_settings.config"].decode()
    assert re.findall(r'key="instance_id" value="(\d+)"', ms) == ["0"]
    assert re.findall(r'<assemble_item[^>]*instance_id="(\d+)"', ms) == ["0"]
    assert parts["3D/3dmodel.model"].decode().count("<item ") == 1
    assert check(parts, report) == []


def test_filament_preset_fills_every_slot_of_a_multi_extruder_target(
    tmp_path: Path, env: Path
) -> None:
    src = make_project(tmp_path / "in.3mf", TWO, filaments=2)
    parts, report = rt.retarget(
        src,
        machine=source(env, "Test Dual 0.4 nozzle"),
        filament=source(env, "Test PLA @K1", kind="filament"),
        keep="1",
    )
    s = settings_of(parts)
    assert s["filament_settings_id"] == ["Test PLA @K1", "Test PLA @K1"]
    assert s["nozzle_temperature"] == ["210", "210"] and len(s["filament_colour"]) == 2
    assert check(parts, report, nozzles=2) == []


def test_kept_objects_on_different_filaments_are_called_out(
    tmp_path: Path, env: Path
) -> None:
    _, report = build(tmp_path, env, TWO, filaments=2)
    assert any("filament slots [1, 2]" in n and "slot 1" in n for n in report.notes)


def test_per_extruder_machine_keys_follow_the_kept_objects_extruder(
    tmp_path: Path, env: Path
) -> None:
    src = make_project(tmp_path / "in.3mf", TWO, filaments=2)
    ref = source(env)
    sparse = rt.PresetSource(
        ref.name, {k: v for k, v in ref.settings.items() if k != "retract_lift_above"}
    )
    parts, _ = rt.retarget(
        src, machine=sparse, keep="2"
    )  # object 2 prints on extruder 2
    assert settings_of(parts)["retract_lift_above"] == ["0"]
    src2 = tmp_path / "in2.3mf"
    with zipfile.ZipFile(src) as zf, zipfile.ZipFile(src2, "w") as out:
        for info in zf.infolist():
            data = zf.read(info)
            if info.filename == "Metadata/project_settings.config":
                s = json.loads(data)
                s["retract_lift_above"] = ["1", "2", "3", "4"]
                data = json.dumps(s).encode()
            out.writestr(info.filename, data)
    parts, _ = rt.retarget(src2, machine=sparse, keep="2")
    assert settings_of(parts)["retract_lift_above"] == ["2"]


def test_unreadable_mesh_is_a_validation_problem_not_a_crash(
    tmp_path: Path, env: Path
) -> None:
    parts, report = build(tmp_path, env, TWO, keep="1")
    bad = dict(parts)
    bad["3D/Objects/box_1.model"] = bad["3D/Objects/box_1.model"].replace(
        b'<object id="1"', b'<object id="77"'
    )
    problems = check(bad, report)
    assert problems and any("mesh part cannot be read" in p for p in problems)


def inline_bounds_xy(parts: dict[str, bytes]) -> tuple[float, float, float, float]:
    """XY AABB of the first build item whose meshes sit in the main model (identity components)."""
    main = parts["3D/3dmodel.model"].decode()
    m = [float(v) for v in grab(r'<item\b[^>]*transform="([^"]*)"', main).split()]
    verts = [
        (float(x), float(y))
        for x, y, _z in re.findall(r'<vertex x="([^"]+)" y="([^"]+)" z="([^"]+)"', main)
    ]
    xs = [x * m[0] + y * m[3] + m[9] for x, y in verts]
    ys = [x * m[1] + y * m[4] + m[10] for x, y in verts]
    return (min(xs), min(ys), max(xs), max(ys))


def test_meshes_stored_in_the_main_model_survive_a_keep(
    tmp_path: Path, env: Path
) -> None:
    src = make_project(tmp_path / "in.3mf", TWO, inline_meshes=True)
    assert not [n for n in rt.read_package(src) if n.startswith("3D/Objects/")]
    parts, report = rt.retarget(src, machine=source(env), keep="2")
    main = parts["3D/3dmodel.model"].decode()
    assert (
        main.count("<mesh>") == 1 and 'objectid="3"' in main and 'id="1"' not in main
    )  # the kept box's mesh stays
    assert check(parts, report) == []
    x0, y0, x1, y1 = inline_bounds_xy(parts)
    assert x0 >= 5 - 1e-6 and y0 >= 5 - 1e-6 and x1 <= 215 + 1e-6 and y1 <= 215 + 1e-6


def test_modifier_volumes_do_not_count_toward_footprint_or_height(
    tmp_path: Path, env: Path
) -> None:
    box = Box("with-blocker", (60, 40, 30), center=(80, 80), modifier=(400, 400, 400))
    parts, report = build(
        tmp_path, env, [box]
    )  # a 400 mm modifier would be "too tall" and "too big"
    assert report.zmax == pytest.approx(30)
    assert report.footprint == pytest.approx((60, 40), abs=1e-3)
    assert check(parts, report) == []


def test_a_sliced_projects_source_gcode_and_stale_references_are_dropped(
    tmp_path: Path, env: Path
) -> None:
    src = make_project(tmp_path / "in.3mf", TWO, sliced=True)
    assert "Metadata/plate_1.gcode" in rt.read_package(src)
    parts, report = rt.retarget(src, machine=source(env), keep="1")
    assert not [n for n in parts if "gcode" in n]
    ms = parts["Metadata/model_settings.config"].decode()
    assert "pattern_bbox_file" not in ms and "gcode_file" not in ms
    assert "gcode" not in parts["Metadata/_rels/model_settings.config.rels"].decode()
    assert any("G-code part" in n for n in report.notes)


def test_mode_count_machine_lists_are_not_mistaken_for_per_extruder_lists(
    tmp_path: Path, env: Path
) -> None:
    src = make_project(
        tmp_path / "in.3mf", TWO, extruders=2
    )  # two extruders: machine_max_* has 2 entries too
    ref = source(env)
    sparse = rt.PresetSource(
        ref.name,
        {
            k: v
            for k, v in ref.settings.items()
            if k not in {"retract_lift_above", "machine_max_speed_x"}
        },
    )
    parts, _ = rt.retarget(src, machine=sparse, keep="1")
    s = settings_of(parts)
    assert s["machine_max_speed_x"] == ["500", "200"]  # normal + silent limits survive
    assert s["extruder_offset"] == ["0x0"] and s["retract_lift_above"] == [
        "0"
    ]  # per-extruder lists collapse


def test_a_failed_write_leaves_no_partial_file(tmp_path: Path) -> None:
    out = tmp_path / "out" / "x.3mf"
    with pytest.raises(TypeError):
        rt.write_package(out, {"a.txt": b"ok", "b.txt": 123})  # type: ignore[dict-item]
    assert not out.exists() and not list(out.parent.glob("*.tmp"))


def test_project_only_per_extruder_and_per_filament_lists_collapse_too(
    tmp_path: Path, env: Path
) -> None:
    parts, _ = build(tmp_path, env, TWO, filaments=2, keep="1")
    s = settings_of(parts)
    assert s["extruder_type"] == [
        "Direct Drive"
    ]  # 4 -> 1, though no system preset names the key
    assert s["filament_map"] == [
        "1"
    ]  # was ["2", "2"]: the one filament sits on extruder 1


def test_a_malformed_bed_is_an_argument_error_not_a_crash() -> None:
    for bad in (
        ["220", "220x0", "220x220", "0x220"],
        ["0x0", "0x0", "0x0", "0x0"],
        "nope",
        None,
    ):
        with pytest.raises(InvalidArgument):
            rt.bed_from_settings({"printable_area": bad})


def test_a_build_item_with_a_missing_mesh_is_an_error(
    tmp_path: Path, env: Path
) -> None:
    src = make_project(tmp_path / "in.3mf", TWO)
    parts = rt.read_package(src)
    parts["3D/Objects/box_2.model"] = parts["3D/Objects/box_2.model"].replace(
        b'<object id="3"', b'<object id="9"'
    )
    broken = tmp_path / "broken.3mf"
    rt.write_package(broken, parts)
    with pytest.raises(InvalidArgument) as err:
        rt.retarget(
            broken, machine=source(env), keep="1,2"
        )  # item 2 points at a mesh that is gone
    assert "existing mesh" in err.value.render(color=False)


def test_exported_stl_is_the_placed_part_in_bed_coordinates(
    tmp_path: Path, env: Path
) -> None:
    import struct

    parts, report = build(
        tmp_path,
        env,
        [Box("blk", (60, 40, 30), center=(500, -300), rot_z=30)],
        keep="1",
    )
    stl = rt.export_stl(parts)
    n = struct.unpack("<I", stl[80:84])[0]
    assert n == 12 and len(stl) == 84 + 50 * 12  # a box = 12 triangles
    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []
    for i in range(n):
        v = struct.unpack("<12fH", stl[84 + 50 * i : 134 + 50 * i])
        xs += v[3:12:3]
        ys += v[4:12:3]
        zs += v[5:12:3]
    assert (
        min(xs) >= 5 - 1e-3
        and max(xs) <= 215 + 1e-3
        and min(ys) >= 5 - 1e-3
        and max(ys) <= 215 + 1e-3
    )
    assert min(zs) == pytest.approx(0, abs=1e-4) and max(zs) == pytest.approx(
        30, abs=1e-4
    )


def test_export_stl_skips_modifier_volumes(tmp_path: Path, env: Path) -> None:
    parts, _ = build(
        tmp_path, env, [Box("m", (30, 30, 10), center=(60, 60), modifier=(50, 50, 50))]
    )
    assert len(rt.export_stl(parts)) == 84 + 50 * 12  # only the 12-triangle body


def test_split_settings_name_the_machine_as_the_only_compatible_printer(
    tmp_path: Path, env: Path
) -> None:
    parts, _ = build(tmp_path, env, TWO, keep="1")
    split = rt.split_project_settings(settings_of(parts), "Test K1 0.4 nozzle")
    assert split["machine"]["printable_area"] == ["0x0", "220x0", "220x220", "0x220"]
    for kind in ("process", "filament"):
        assert split[kind]["compatible_printers"] == ["Test K1 0.4 nozzle"]
    assert (
        "layer_height" in split["process"] and "nozzle_temperature" in split["filament"]
    )
    assert (
        "printable_area" not in split["process"]
        and "layer_height" not in split["machine"]
    )


def test_split_settings_without_preset_names_is_an_error_with_a_remedy(
    tmp_path: Path, env: Path
) -> None:
    parts, _ = build(tmp_path, env, TWO, keep="1")
    settings = {
        k: v for k, v in settings_of(parts).items() if k != "filament_settings_id"
    }
    with pytest.raises(GateFailure) as err:
        rt.split_project_settings(settings, "Test K1 0.4 nozzle")
    assert "filament_settings_id" in str(err.value)


def test_split_settings_neutralise_keys_that_reach_the_host(
    tmp_path: Path, env: Path
) -> None:
    parts, _ = build(tmp_path, env, TWO, keep="1")
    settings = {
        **settings_of(parts),
        "post_process": ["/tmp/evil.sh"],
        "output_filename_format": "../../x.gcode",
    }
    process = rt.split_project_settings(settings, "Test K1 0.4 nozzle")["process"]
    assert process["post_process"] == []
    assert "/" not in process["output_filename_format"]
