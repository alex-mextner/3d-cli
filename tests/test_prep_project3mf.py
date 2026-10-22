"""Behavioural tests for lib/prep/project3mf.py — the multi-colour project 3MF writer and its manifest.

The 3MF is written, then read back with the repo's own loader (`standing_props.load_3mf`) and by raw zip
access, so every assertion is about what a slicer would find in the file."""

from __future__ import annotations

import json
import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest
import trimesh

from errors import InvalidArgument, MissingDependency
from prep import project3mf
from prep.contracts import Assign, Filament, Part, StepResult
from prep.project3mf import (
    ProcessOverrides,
    ProjectSpec,
    build_project,
    check_mix_pattern,
    free_spot,
)
from slicing import orca_cli
from standing_props import load_3mf

FILAMENTS = [
    Filament(1, "K", "black", "#111111"),
    Filament(2, "W", "white", "#F2F2F2"),
    Filament(3, "R", "red", "#CC1111"),
    Filament(4, "G", "green", "#116622"),
]
BASE = {
    "inherits_group": [""] * 6,
    "different_settings_to_system": [""] * 6,
    "min_layer_height": ["0.08"] * 4,
    "max_layer_height": ["0.32"] * 4,
    "retraction_length": ["0.8"] * 4,
    "prime_tower_width": "30",
    "curr_bed_type": "Cool Plate",
}


@dataclass
class Plan:
    profile_text: str


def box(
    extents: tuple[float, float, float], at: tuple[float, float, float]
) -> trimesh.Trimesh:
    m = trimesh.creation.box(extents=extents)
    m.apply_translation(at)
    return m


def two_part_spec(**kw: object) -> ProjectSpec:
    """A 10 mm black block with a 4 mm cap that prints a white/green mix; 14 mm high in total."""
    return ProjectSpec(
        name="cat",
        parts=[
            Part("body", box((20, 20, 10), (0, 0, 5)), Assign(key="K")),
            Part("cap", box((20, 20, 4), (0, 0, 12)), Assign(mix="WG")),
        ],
        filaments=list(FILAMENTS),
        process_preset="0.20mm Standard @Snapmaker U1 (0.4 nozzle)",
        **kw,  # type: ignore[arg-type]
    )


def members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n) for n in z.namelist()}


def settings(path: Path) -> dict[str, object]:
    return json.loads(members(path)["Metadata/project_settings.config"])


# ------------------------------------------------------------------ the written project


def test_written_3mf_is_a_valid_package_with_the_expected_parts(tmp_path: Path) -> None:
    out = tmp_path / "cat.3mf"
    man = build_project(two_part_spec(), out, base_settings=BASE)
    names = set(members(out))
    assert {
        "[Content_Types].xml",
        "_rels/.rels",
        "3D/3dmodel.model",
        "3D/_rels/3dmodel.model.rels",
        "3D/Objects/object_1.model",
        "Metadata/model_settings.config",
        "Metadata/project_settings.config",
        "Metadata/plate_1.json",
    } <= names
    assert zipfile.ZipFile(out).testzip() is None
    assert man.path == str(out)
    assert (tmp_path / "cat.manifest.json").exists()


def test_reading_it_back_gives_one_object_with_the_parts_and_their_extruders(
    tmp_path: Path,
) -> None:
    out = tmp_path / "cat.3mf"
    build_project(two_part_spec(), out, base_settings=BASE)
    objs = load_3mf(str(out))
    assert len(objs) == 1  # ONE object: the prime tower needs one layer-height profile
    bodies = {b.name.split(" · ")[0]: b for b in objs[0].bodies}
    assert set(bodies) == {"body", "cap"}
    assert bodies["body"].extruder == 1  # key K -> slot 1
    assert bodies["cap"].extruder == 5  # first mixed row = n_physical + 1
    allv = np.vstack([b.vertices for b in objs[0].bodies])
    assert allv.min(0).round(3).tolist() == [
        125.0,
        125.0,
        0.0,
    ]  # on the bed, centred on the 270 mm bed
    assert allv.max(0).round(3).tolist() == [145.0, 145.0, 14.0]


def test_mix_row_uses_component_1_2_and_the_pattern_is_the_slot_digits(
    tmp_path: Path,
) -> None:
    out = tmp_path / "cat.3mf"
    build_project(two_part_spec(), out, base_settings=BASE)
    cfg = settings(out)
    # letters W,G resolve through the filament keys to slots 2,4; tokens equal slots
    assert (
        cfg["mixed_filament_definitions"]
        == "1,2,1,1,50,0,g,w,m2,z0,xa0,xb0,d0,o0,u701,cm1,24"
    )
    assert cfg["filament_colour"] == [f.hex for f in FILAMENTS]


def test_reordering_the_spools_changes_the_digits_not_the_recipe(
    tmp_path: Path,
) -> None:
    swapped = [
        FILAMENTS[0],
        Filament(2, "G", "green", "#116622"),
        FILAMENTS[2],
        Filament(4, "W", "white", "#F2F2F2"),
    ]
    spec = two_part_spec()
    spec.filaments = swapped
    out = tmp_path / "cat.3mf"
    build_project(spec, out, base_settings=BASE)
    assert str(settings(out)["mixed_filament_definitions"]).endswith(
        ",cm1,42"
    )  # same letters, other digits


def test_layer_profile_is_written_verbatim(tmp_path: Path) -> None:
    text = "object_id=1|0.000000;0.200000;7.000000;0.080000;14.000000;0.320000\n"
    out = tmp_path / "cat.3mf"
    man = build_project(two_part_spec(layer_plan=Plan(text)), out, base_settings=BASE)
    assert members(out)["Metadata/layer_heights_profile.txt"].decode() == text
    assert man.data["layer_profile"] == {
        "points": 3,
        "top_mm": 14.0,
        "min_h": 0.08,
        "max_h": 0.32,
    }


def test_no_layer_plan_writes_no_profile(tmp_path: Path) -> None:
    out = tmp_path / "cat.3mf"
    build_project(two_part_spec(), out, base_settings=BASE)
    assert "Metadata/layer_heights_profile.txt" not in members(out)


def test_process_overrides_brim_and_machine_overlay_reach_the_settings(
    tmp_path: Path,
) -> None:
    out = tmp_path / "cat.3mf"
    build_project(two_part_spec(brim_mm=3.0), out, base_settings=BASE)
    cfg = settings(out)
    assert cfg["wall_loops"] == "4" and cfg["sparse_infill_density"] == "15%"
    assert cfg["enable_support"] == "0" and cfg["dithering_local_z_mode"] == "0"
    assert cfg["brim_type"] == "outer_only" and cfg["brim_width"] == "3"
    assert cfg["z_hop"] == "0.6"
    assert cfg["retraction_length"] == ["1"] * 4  # a per-extruder list stays a list
    assert cfg["curr_bed_type"] == "Textured PEI Plate"
    ds = cfg["different_settings_to_system"]
    assert isinstance(ds, list) and "wall_loops" in ds[0] and "filament_colour" in ds[1]


def test_without_brim_the_slicer_is_told_no_brim(tmp_path: Path) -> None:
    out = tmp_path / "cat.3mf"
    build_project(two_part_spec(), out, base_settings=BASE)
    assert settings(out)["brim_type"] == "no_brim"


def test_prime_tower_is_placed_clear_of_the_model_when_several_heads_print(
    tmp_path: Path,
) -> None:
    out = tmp_path / "cat.3mf"
    man = build_project(two_part_spec(), out, base_settings=BASE)
    cfg = settings(out)
    assert man.data["heads_slots"] == [1, 2, 4]
    assert cfg["enable_prime_tower"] == "1" and man.data["prime_tower"] is True
    tx, ty = float(cfg["wipe_tower_x"][0]), float(cfg["wipe_tower_y"][0])  # type: ignore[index]
    assert man.data["tower_xy"] == [tx, ty]
    inside = 125 - 6 <= tx <= 145 + 6 and 125 - 6 <= ty <= 145 + 6
    assert not inside  # the model sits at 125..145 on both axes


def test_a_single_colour_model_gets_no_tower_and_no_mixed_rows(tmp_path: Path) -> None:
    spec = ProjectSpec(
        name="mono",
        parts=[Part("all", box((20, 20, 10), (0, 0, 5)), Assign(slot=3))],
        filaments=list(FILAMENTS),
        process_preset="p",
    )
    out = tmp_path / "mono.3mf"
    man = build_project(spec, out, base_settings=BASE)
    cfg = settings(out)
    assert cfg["enable_prime_tower"] == "0" and "mixed_filament_definitions" not in cfg
    assert man.data["mix_rows"] == [] and man.data["heads_slots"] == [3]
    assert load_3mf(str(out))[0].bodies[0].extruder == 3


def test_the_shipped_project_keeps_its_plate_block_and_the_slicing_copy_drops_it(
    tmp_path: Path,
) -> None:
    out = tmp_path / "cat.3mf"
    build_project(two_part_spec(), out, base_settings=BASE)
    assert b"<plate>" in members(out)["Metadata/model_settings.config"]
    copy = tmp_path / "copy.3mf"
    orca_cli.slicing_copy(out, copy)
    assert b"<plate>" not in members(copy)["Metadata/model_settings.config"]
    assert "Metadata/plate_1.json" not in members(copy)


def test_rebuilding_the_same_input_gives_identical_bytes(tmp_path: Path) -> None:
    a, b = tmp_path / "a" / "cat.3mf", tmp_path / "b" / "cat.3mf"
    build_project(two_part_spec(), a, base_settings=BASE)
    build_project(two_part_spec(), b, base_settings=BASE)
    assert members(a) == members(b)


# ------------------------------------------------------------------ manifest


def test_manifest_records_parts_z_ranges_filaments_shift_process_and_gates(
    tmp_path: Path,
) -> None:
    out = tmp_path / "cat.3mf"
    man = build_project(
        two_part_spec(layer_plan=Plan("object_id=1|0;0.2;14;0.2\n")),
        out,
        base_settings=BASE,
    )
    on_disk = json.loads((tmp_path / "cat.manifest.json").read_text())
    assert on_disk == man.to_dict()
    d = on_disk
    parts = {p["name"]: p for p in d["parts"]}
    assert parts["body"]["z_range"] == [0.0, 10.0] and parts["cap"]["z_range"] == [
        10.0,
        14.0,
    ]
    assert parts["body"]["tools"] == [0]  # slot 1 = T0
    assert parts["cap"]["tools"] == [1, 3]  # white (slot 2) and green (slot 4)
    assert parts["cap"]["assign"] == {"mix": "24", "mix_src": "WG"}
    assert d["mix_rows"] == [{"pattern": "24", "extruder": 5, "source": "WG"}]
    assert d["shift"] == [
        135.0,
        135.0,
        0.0,
    ]  # (bed centre) - (bbox centre); z lifted by 0 (already on the bed)
    assert [f["key"] for f in d["filaments"]] == ["K", "W", "R", "G"]
    assert [f["tool"] for f in d["filaments"]] == [0, 1, 2, 3]
    assert d["process"]["wall_loops"] == "4"
    assert d["process_preset"] == "0.20mm Standard @Snapmaker U1 (0.4 nozzle)"
    g = d["gates"]
    assert (
        g["wall_loops"] == 4
        and g["supports"] is False
        and g["layer_height_band"] == [0.08, 0.32]
    )
    assert g["bed"] == [270.0, 270.0, 270.0] and g["prime_tower"] is True
    json.dumps(d)


def test_shift_lifts_a_floating_model_onto_the_bed(tmp_path: Path) -> None:
    spec = ProjectSpec(
        name="float",
        parts=[Part("all", box((10, 10, 10), (0, 0, 30)), Assign(slot=1))],
        filaments=list(FILAMENTS),
        process_preset="p",
    )
    man = build_project(spec, tmp_path / "f.3mf", base_settings=BASE)
    assert man.data["shift"][2] == -25.0
    assert man.data["object_bbox"][0][2] == 0.0


def test_result_satisfies_the_step_result_contract(tmp_path: Path) -> None:
    man = build_project(two_part_spec(), tmp_path / "cat.3mf", base_settings=BASE)
    assert isinstance(man, StepResult) and man.verdict == "PASS"
    assert "2 parts" in man.summary() and "24" in man.summary()


# ------------------------------------------------------------------ refusals


def test_a_mix_letter_that_is_not_a_filament_key_raises(tmp_path: Path) -> None:
    spec = two_part_spec()
    spec.parts[1] = Part("cap", spec.parts[1].mesh, Assign(mix="WX"))
    with pytest.raises(InvalidArgument) as exc:
        build_project(spec, tmp_path / "x.3mf", base_settings=BASE)
    assert "'X'" in exc.value.message
    assert not (tmp_path / "x.3mf").exists()


def test_a_pure_key_that_is_not_a_filament_key_raises(tmp_path: Path) -> None:
    spec = two_part_spec()
    spec.parts[0] = Part("body", spec.parts[0].mesh, Assign(key="Q"))
    with pytest.raises(InvalidArgument):
        build_project(spec, tmp_path / "x.3mf", base_settings=BASE)


@pytest.mark.parametrize("mix", ["WWG", "WW", "WGG"])
def test_illegal_mix_patterns_are_refused(tmp_path: Path, mix: str) -> None:
    spec = two_part_spec()
    spec.parts[1] = Part("cap", spec.parts[1].mesh, Assign(mix=mix))
    with pytest.raises(InvalidArgument):
        build_project(spec, tmp_path / "x.3mf", base_settings=BASE)


def test_a_mix_layer_above_the_cap_is_refused(tmp_path: Path) -> None:
    with pytest.raises(InvalidArgument):
        build_project(
            two_part_spec(mix_layer_mm=0.2), tmp_path / "x.3mf", base_settings=BASE
        )


@pytest.mark.parametrize(
    "text",
    [
        "object_id=1|0;0.2;13.0;0.2\n",  # top 13 != object height 14: the slicer would drop it silently
        "object_id=2|0;0.2;14;0.2\n",  # not object 1
        "garbage",
        "object_id=1|0;0.2\n",
    ],
)
def test_a_layer_profile_the_slicer_would_ignore_is_refused(
    tmp_path: Path, text: str
) -> None:
    with pytest.raises(InvalidArgument):
        build_project(
            two_part_spec(layer_plan=Plan(text)), tmp_path / "x.3mf", base_settings=BASE
        )


def test_a_model_that_does_not_fit_the_bed_is_refused(tmp_path: Path) -> None:
    spec = ProjectSpec(
        name="big",
        parts=[Part("all", box((300, 10, 10), (0, 0, 5)), Assign(slot=1))],
        filaments=list(FILAMENTS),
        process_preset="p",
    )
    with pytest.raises(InvalidArgument):
        build_project(spec, tmp_path / "big.3mf", base_settings=BASE)


def test_filaments_must_be_the_printers_slots_in_order(tmp_path: Path) -> None:
    spec = two_part_spec()
    spec.filaments = FILAMENTS[:3]
    with pytest.raises(InvalidArgument):
        build_project(spec, tmp_path / "x.3mf", base_settings=BASE)


def test_duplicate_filament_keys_are_refused(tmp_path: Path) -> None:
    spec = two_part_spec()
    spec.filaments = [
        FILAMENTS[0],
        Filament(2, "K", "w", "#fff"),
        FILAMENTS[2],
        FILAMENTS[3],
    ]
    with pytest.raises(InvalidArgument):
        build_project(spec, tmp_path / "x.3mf", base_settings=BASE)


def test_bad_name_and_printer_are_refused(tmp_path: Path) -> None:
    for kw in ({"name": "has space"}, {"printer": "nope"}):
        spec = two_part_spec()
        for k, v in kw.items():
            setattr(spec, k, v)
        with pytest.raises(InvalidArgument):
            build_project(spec, tmp_path / "x.3mf", base_settings=BASE)


def test_without_the_orca_install_the_error_names_the_install_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orca_cli, "find_orca", lambda *a, **k: None)
    with pytest.raises(MissingDependency) as exc:
        build_project(two_part_spec(), tmp_path / "x.3mf")
    assert "snapmaker_orca" in " ".join(exc.value.remediation)


def test_missing_resources_name_the_profiles_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = tmp_path / "orca"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(
        orca_cli, "find_orca", lambda *a, **k: orca_cli.OrcaCli(exe, "t")
    )
    monkeypatch.setattr(orca_cli, "find_resources", lambda *a, **k: None)
    with pytest.raises(MissingDependency) as exc:
        build_project(two_part_spec(), tmp_path / "x.3mf")
    assert "resources" in exc.value.message
    assert "snapmaker_orca" in " ".join(exc.value.remediation)


# ------------------------------------------------------------------ helpers


def test_process_overrides_always_switch_supports_raft_and_dithering_off() -> None:
    cfg = ProcessOverrides(
        wall_loops=None, extra={"enable_support": "1", "x": "y"}
    ).to_config()
    assert (
        cfg["enable_support"] == "0"
        and cfg["raft_layers"] == "0"
        and cfg["dithering_local_z_mode"] == "0"
    )
    assert cfg["x"] == "y" and "wall_loops" not in cfg


def test_process_overrides_from_a_user_preset_drop_identity_keys(
    tmp_path: Path,
) -> None:
    f = tmp_path / "p.json"
    f.write_text(
        json.dumps(
            {
                "name": "n",
                "inherits": "base",
                "from": "User",
                "version": "1",
                "wall_loops": "4",
                "gap": "0.1",
            }
        )
    )
    o = ProcessOverrides.from_user_preset(f)
    assert o.extra == {"wall_loops": "4", "gap": "0.1"}
    assert o.to_config()["wall_loops"] == "4"


def test_adaptive_layer_height_flag_is_written() -> None:
    assert (
        ProcessOverrides(adaptive_layer_height=True).to_config()[
            "adaptive_layer_height"
        ]
        == "1"
    )
    assert (
        ProcessOverrides(adaptive_layer_height=False).to_config()[
            "adaptive_layer_height"
        ]
        == "0"
    )
    assert "adaptive_layer_height" not in ProcessOverrides().to_config()


def test_free_spot_clears_every_footprint_and_reports_a_full_bed() -> None:
    rects = [(100.0, 100.0, 170.0, 170.0)]
    x, y = free_spot(rects, 40, 50, 270) or (0.0, 0.0)
    assert x + 40 + 6 <= 100 or x >= 176 or y + 50 + 6 <= 100 or y >= 176
    assert free_spot([(0.0, 0.0, 270.0, 270.0)], 40, 50, 270) is None


@pytest.mark.parametrize(
    ("pattern", "layer", "legal"),
    [
        ("24", 0.10, True),
        ("242", 0.10, False),
        ("24", 0.20, False),
        ("1213", 0.10, True),
    ],
)
def test_mix_pattern_rule(pattern: str, layer: float, legal: bool) -> None:
    assert (check_mix_pattern(pattern, layer) == []) is legal


def test_the_printer_table_has_the_270_mm_u1() -> None:
    u1 = project3mf.PRINTERS["u1"]
    assert u1.bed == 270.0 and u1.extruders == 4


@pytest.mark.skipif(
    orca_cli.find_orca() is None or os.environ.get("THREED_REAL_ORCA") != "1",
    reason="real slice: needs the Snapmaker Orca CLI and THREED_REAL_ORCA=1 (run under the heavy lock)",
)
def test_real_cli_slices_the_built_project_on_the_planned_heads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "THREED_HEAVY_HELD", "1"
    )  # the caller holds the lock; never nest one
    spec = two_part_spec(mix_layer_mm=0.10)
    spec.process_preset = "0.20mm Standard @Snapmaker U1 (0.4 nozzle)"
    out = tmp_path / "cat.3mf"
    man = build_project(spec, out)  # real vendor presets through the CLI
    outcome = orca_cli.slice_project(out, tmp_path / "work", timeout_s=900)
    assert outcome.ok, outcome.summary()
    assert outcome.gcode is not None
    tools = {
        int(t)
        for t in re.findall(
            r"^T(\d+)\s*$", outcome.gcode.read_text(errors="replace"), re.M
        )
    }
    assert {0, 1, 3} <= tools  # slot 1 (body), slots 2 and 4 (the mix) = T0, T1, T3
    assert 2 not in tools  # slot 3 (red) is not used
    assert man.data["heads_slots"] == [1, 2, 4]
