"""Tests for slicing.calibration_plate (the plate engine) and calibration_plates.filament (plate 1),
with a fake Orca CLI that writes Orca-shaped G-code (START_PRINT, EXCLUDE_OBJECT, ;Z: markers)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from printops.calibration_catalog import load_catalog
from slicing import calibration_gen as cg
from slicing import calibration_plate as cp
from slicing.calibration_plates import PLATES, filament

STOCK = cg.PresetChoice(
    "Creality K1 SE 0.4 nozzle",
    "0.20mm Standard @Creality K1 SE",
    "Creality Generic PLA @K1-all",
    (("machine", "test"), ("process", "test"), ("filament", "test")),
)

PRESETS: dict[tuple[str, str], dict[str, Any]] = {
    ("machine", "Creality K1 SE 0.4 nozzle"): {
        "bed_exclude_area": ["0x0"],
        "retraction_length": ["0.8"],
        "retraction_speed": ["40"],
        "extruder_clearance_height_to_rod": "45",
        "extruder_clearance_radius": "45",
    },
    ("process", "0.20mm Standard @Creality K1 SE"): {
        "default_acceleration": "12000",
        "default_jerk": "9",
        "initial_layer_print_height": "0.2",
        "layer_height": "0.2",
    },
    ("filament", "Creality Generic PLA @K1-all"): {
        "filament_flow_ratio": ["0.98"],
        "filament_colour": ["#FFFFFF"],
        "filament_diameter": ["1.75"],
        "nozzle_temperature": ["220"],
        "nozzle_temperature_initial_layer": ["220"],
    },
}

# The fake slices every STL into layers of `layer_height` (process preset written by
# slice_models) up to the STL's max Z; per layer and object: START, an extruding move, a
# retraction with a wipe, END. print_sequence by object prints each object's layers in a row.
FAKE = r"""#!/usr/bin/env python3
import json, pathlib, re, sys
a = sys.argv[1:]
out = pathlib.Path(a[a.index("--outputdir") + 1])
proc = json.loads(pathlib.Path(a[a.index("--load-settings") + 1].split(";")[1]).read_text())
h = float(proc.get("layer_height", "0.2"))
seq = proc.get("print_sequence") == "by object"
objs = []
for s in (x for x in a if x.endswith(".stl")):
    txt = pathlib.Path(s).read_text()
    zs = [float(m) for m in re.findall(r"vertex \S+ \S+ (\S+)", txt)]
    xs = [float(m) for m in re.findall(r"vertex (\S+) ", txt)]
    ys = [float(m) for m in re.findall(r"vertex \S+ (\S+) ", txt)]
    objs.append((pathlib.Path(s).stem, max(zs), (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2))
L = ["; thumbnails = 16x16/PNG"]
L += [f"EXCLUDE_OBJECT_DEFINE NAME={n}.stl_id_{i}_copy_0" for i, (n, *_r) in enumerate(objs)]
L += ["START_PRINT EXTRUDER_TEMP=220 BED_TEMP=55", "G90", "M83", "G1 E-.8 F2400"]
def layer(i, n, cx, cy, z):
    return [f"EXCLUDE_OBJECT_START NAME={n}.stl_id_{i}_copy_0", f"G1 X{cx} Y{cy} F30000", f"G1 Z{z:.2f}",
            "G1 E.8 F2400", f"G1 X{cx + 1} Y{cy} E.05", "G1 E-.56 F2400", f"G1 X{cx} Y{cy} E-.24",
            f"EXCLUDE_OBJECT_END NAME={n}.stl_id_{i}_copy_0"]
if seq:
    for i, (n, top, cx, cy) in enumerate(objs):
        z = h
        while z <= top + 1e-6:
            L += [";LAYER_CHANGE", f";Z:{z:.2f}"] + layer(i, n, cx, cy, z)
            z += h
else:
    z = h
    while z <= max(o[1] for o in objs) + 1e-6:
        L += [";LAYER_CHANGE", f";Z:{z:.2f}"]
        for i, (n, top, cx, cy) in enumerate(objs):
            if z <= top + 1e-6:
                L += layer(i, n, cx, cy, z)
        z += h
L += ["END_PRINT", "; estimated printing time (normal mode) = 10m 0s", "; use_relative_e_distances = 1"]
# footer like Orca's: the settings and the preset names it sliced with
mach = json.loads(pathlib.Path(a[a.index("--load-settings") + 1].split(";")[0]).read_text())
fil = json.loads(pathlib.Path(a[a.index("--load-filaments") + 1]).read_text())
first = lambda v: v[0] if isinstance(v, list) else v
L += [f"; default_acceleration = {proc.get('default_acceleration')}",
      f"; filament_flow_ratio = {first(fil.get('filament_flow_ratio'))}",
      f"; pressure_advance = {first(fil.get('pressure_advance', '0'))}",
      f"; printer_settings_id = {mach['name']}", f"; print_settings_id = {proc['name']}",
      f'; filament_settings_id = "{fil["name"]}"',
      f"; compatible_printers = {proc.get('compatible_printers')}"]
(out / "plate_1.gcode").write_text("\n".join(L) + "\n")
"""


@pytest.fixture
def orca(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    root = tmp_path / "profiles"
    for (kind, name), data in PRESETS.items():
        d = root / "Creality" / kind
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.json").write_text(json.dumps({"name": name, **data}))
    monkeypatch.setenv("ORCA_PROFILES", str(root))
    (tmp_path / "user").mkdir(exist_ok=True)
    monkeypatch.setenv("ORCA_USER_PRESETS", str(tmp_path / "user"))
    script = tmp_path / "orca"
    script.write_text(FAKE)
    script.chmod(0o755)
    return str(script)


def _ctx(orca: str) -> cp.PlateContext:
    return cp.make_context(
        "Creality K1 SE", orca, "PLA", "#E8732A", None, presets=STOCK
    )


def _object_lines(text: str, name: str) -> list[list[str]]:
    """Per START…END section of `name`: its lines, START and END included."""
    out, cur = [], None
    for line in text.splitlines():
        if line.startswith(f"EXCLUDE_OBJECT_START NAME={name}.stl"):
            cur = [line]
        elif cur is not None:
            cur.append(line)
            if line.startswith(f"EXCLUDE_OBJECT_END NAME={name}.stl"):
                out.append(cur)
                cur = None
    return out


def _box_obj(name: str, top: float, **kw: object) -> cp.PlateObject:
    return cp.PlateObject(name, "test", ((0.0, 0.0, 0.0, 5.0, 5.0, top),), **kw)  # type: ignore[arg-type]


def test_hooks_enter_band_leave_before_after() -> None:
    text = "\n".join(
        [
            ";Z:0.2",
            "EXCLUDE_OBJECT_START NAME=a.stl_id_0_copy_0",
            "G1 X1 E1",
            "G1 E-.8 F2400",
            "EXCLUDE_OBJECT_END NAME=a.stl_id_0_copy_0",
            "G1 E-.5 F2400",
            ";Z:0.4",
            "EXCLUDE_OBJECT_START NAME=a.stl_id_0_copy_0",
            "G1 E-.8 F2400",
            ";Z:0.6",
            "G1 E-.8 F2400",
            "EXCLUDE_OBJECT_END NAME=a.stl_id_0_copy_0",
        ]
    )
    obj = _box_obj(
        "a",
        1.0,
        before=("B",),
        enter=("IN",),
        leave=("OUT",),
        after=("A",),
        bands=(
            cp.Band(0.0, 0.3, ("BAND1",)),
            cp.Band(0.3, 0.5, ("BAND2",), transform=cp.scale_retractions(0.5)),
            cp.Band(0.5, 9.0, ("BAND3",), transform=cp.scale_retractions(2.0)),
        ),
    )
    lines = cp.apply_hooks(text, (obj,)).splitlines()
    assert lines[:4] == [
        ";Z:0.2",
        "B",
        "EXCLUDE_OBJECT_START NAME=a.stl_id_0_copy_0",
        "IN",
    ]
    assert lines[4] == "BAND1"
    assert "OUT" in lines and lines.index("OUT") + 1 == lines.index(
        "EXCLUDE_OBJECT_END NAME=a.stl_id_0_copy_0"
    )
    assert lines.count("B") == 1 and lines[-1] == "A" and lines.count("A") == 1
    assert (
        "G1 E-.5 F2400" in lines
    )  # a retraction outside the object is never rewritten
    assert "G1 E-0.40000 F2400" in lines  # band 2 inside the object
    i3 = lines.index("BAND3")  # a ;Z: inside the object switches the band mid-object
    assert lines[i3 - 1] == ";Z:0.6" and lines[i3 + 1] == "G1 E-1.60000 F2400"


def test_hooks_need_every_object_in_the_slice() -> None:
    with pytest.raises(cg.GenError, match="not found"):
        cp.apply_hooks("G1 X1", (_box_obj("ghost", 1.0),))


def test_scale_retractions_leaves_extrusion_and_travel() -> None:
    tf = cp.scale_retractions(1.5)
    assert tf("G1 X1 Y1 E.05") == "G1 X1 Y1 E.05"  # extrusion
    assert tf("G1 E-.56 F2400") == "G1 E-0.84000 F2400"  # retraction
    assert tf("G1 X2 Y2 E-.24") == "G1 X2 Y2 E-0.36000"  # retraction during a wipe
    assert tf("G1 E.8 F2400 ; deretract") == "G1 E1.20000 F2400 ; deretract"
    assert tf("G1 X5 Y5 F30000") == "G1 X5 Y5 F30000"


def test_plate1_builds_splices_and_maps(orca: str, tmp_path: Path) -> None:
    ctx = _ctx(orca)
    plate = filament.build(ctx)
    out = tmp_path / "plate1.gcode"
    res = cp.build_plate(ctx, plate, orca, out)
    text = out.read_text()
    lines = text.splitlines()
    assert res.files == [out, out.with_suffix(".json")]
    # one START_PRINT/END_PRINT; hand-written blocks right after START_PRINT, in order
    assert sum(ln.startswith("START_PRINT") for ln in lines) == 1
    assert sum(ln.startswith("END_PRINT") for ln in lines) == 1
    start = next(i for i, ln in enumerate(lines) if ln.startswith("START_PRINT"))
    assert lines[start + 1].startswith("; 3d calibrate gen combo: PA lines")
    blocks = [
        ln
        for ln in lines
        if ln.startswith("; 3d calibrate plate: ")
        and "end of" not in ln
        and ln != cp.SPLICE_MARK
        and "undo" not in ln
    ]
    assert blocks == [
        "; 3d calibrate plate: max volumetric speed strip",
        "; 3d calibrate plate: object labels",
    ]
    # the towers' object definitions joined the layer phase's, before START_PRINT
    defines = [
        i
        for i, ln in enumerate(lines)
        if ln.startswith("EXCLUDE_OBJECT_DEFINE NAME=temp_tower")
    ]
    assert defines and defines[0] < start
    # layer phase first, then the lift, then the temperature tower alone
    mark = lines.index(cp.SPLICE_MARK)
    assert all(
        "temp_tower" not in ln
        for ln in lines[:mark]
        if ln.startswith("EXCLUDE_OBJECT_START")
    )
    assert all(
        "temp_tower" in ln
        for ln in lines[mark:]
        if ln.startswith("EXCLUDE_OBJECT_START")
    )
    # flow tiles: own M221 on entry, 100 % on exit
    for name, pct in (("flow_m10", 90), ("flow_p10", 110)):
        for sec in _object_lines(text, name):
            assert sec[1] == f"M221 S{pct}" and sec[-2] == "M221 S100"
    # retraction tower: each block's retractions scaled to its length (preset 0.8 → 0.56 + 0.24 wipe)
    shape = filament.RETRACT_SHAPE
    for i, length in enumerate(filament.RETRACT_LENGTHS):
        z = round(shape.block_floor(i) + 0.2, 2)
        sec = _section_at(text, "retraction_tower", z)
        assert f"G1 E-{0.56 * length / 0.8:.5f} F2400" in sec
    # temperature tower: M109 hot start, one M104 per block, M109 back to the base temp
    tt = [ln for ln in lines[mark:] if re.match(r"^M10[49] S", ln)]
    steps = cg.temp_steps("PLA")
    assert tt[0] == f"M109 S{steps[0]}" and tt[-1] == "M109 S220"
    assert list(dict.fromkeys(tt[1:-1])) == [f"M104 S{t}" for t in steps]
    # the layer phase's last retraction is undone on the tower's first layer
    assert any("undo the layer phase's last retraction" in ln for ln in lines[mark:])
    # restore before END_PRINT
    end = next(i for i, ln in enumerate(lines) if ln.startswith("END_PRINT"))
    assert lines[end - 4 : end] == [
        "M221 S100",
        "M220 S100",
        "SET_PRESSURE_ADVANCE ADVANCE=0.04",
        "SET_VELOCITY_LIMIT ACCEL=12000 ACCEL_TO_DECEL=6000",
    ]
    assert not any(ln.startswith("M73 ") for ln in lines)
    # the JSON map
    data = json.loads(out.with_suffix(".json").read_text())
    assert data["gcode"] == out.name and data["plate"] == "plate1"
    names = [o["name"] for o in data["objects"]]
    assert names[:3] == ["pa_lines", "mvs_strip", "labels"]
    assert data["print_order"][-1] == "sequential: temp_tower"
    pa = next(o for o in data["objects"] if o["name"] == "pa_lines")
    assert [it["params"]["pressure_advance"] for it in pa["items"]] == list(
        cg.COMBO_PA_VALUES
    )
    mvs = next(o for o in data["objects"] if o["name"] == "mvs_strip")
    assert [it["label"] for it in mvs["items"]] == [str(v) for v in filament.MVS_VALUES]
    temp = next(o for o in data["objects"] if o["name"] == "temp_tower")
    assert [b["params"]["nozzle_temp"] for b in temp["bands"]] == steps


def _section_at(text: str, name: str, z: float) -> list[str]:
    for sec_z, sec in _sections_with_z(text, name):
        if abs(sec_z - z) < 1e-6:
            return sec
    raise AssertionError(f"no {name} section at z={z}")


def _sections_with_z(text: str, name: str) -> list[tuple[float, list[str]]]:
    out, z, cur = [], 0.0, None
    for line in text.splitlines():
        if m := re.match(r";Z:([\d.]+)", line):
            z = float(m.group(1))
        if line.startswith(f"EXCLUDE_OBJECT_START NAME={name}.stl"):
            cur = [line]
        elif cur is not None:
            cur.append(line)
            if line.startswith(f"EXCLUDE_OBJECT_END NAME={name}.stl"):
                out.append((z, cur))
                cur = None
    return out


def test_mvs_strip_speeds_give_the_labelled_flow(orca: str) -> None:
    ctx = _ctx(orca)
    block = next(c for c in filament.build(ctx).custom if c.name == "mvs_strip")
    epm = ctx.e_per_mm(filament.MVS_W, filament.MVS_H)
    seen: set[float] = set()
    for line in block.lines:
        m = re.match(r"^G1 X[\d.]+ E([\d.]+) F(\d+)$", line)
        if m and abs(float(m.group(1)) / filament.MVS_SEG_L - epm) < 1e-4:
            vol = float(m.group(2)) / 60 * filament.MVS_W * filament.MVS_H
            seen.add(round(vol))
    assert seen == set(filament.MVS_VALUES)


def test_plate_rules(orca: str, tmp_path: Path) -> None:
    ctx = _ctx(orca)
    tall = _box_obj("tall", 50.0, phase="sequential")
    with pytest.raises(cg.GenError, match="gantry rod"):
        cp.build_plate(
            ctx,
            cp.Plate("t", "t", (_box_obj("low", 1.0), tall)),
            orca,
            tmp_path / "x.gcode",
        )
    near = cp.PlateObject(
        "near", "test", ((0.0, 20.0, 0.0, 5.0, 25.0, 10.0),), phase="sequential"
    )
    with pytest.raises(cg.GenError, match="extruder_clearance_radius"):
        cp.build_plate(
            ctx,
            cp.Plate("t", "t", (_box_obj("low", 1.0), near)),
            orca,
            tmp_path / "x.gcode",
        )
    with pytest.raises(cg.GenError, match="unique"):
        cp.build_plate(
            ctx,
            cp.Plate("t", "t", (_box_obj("a", 1.0), _box_obj("a", 1.0))),
            orca,
            tmp_path / "x.gcode",
        )
    with pytest.raises(cg.GenError, match="at least one layer object"):
        cp.build_plate(ctx, cp.Plate("t", "t", ()), orca, tmp_path / "x.gcode")


def test_plate_record_hints_are_catalog_methods(orca: str) -> None:
    kinds = load_catalog().kinds
    plate = filament.build(_ctx(orca))
    records = [o.record for o in plate.objects] + [c.record for c in plate.custom]
    hints = [re.search(r"record (\w+) .*--method (\w+)", r) for r in records if r]
    assert {m.group(1) for m in hints if m} == {
        "pressure_advance",
        "max_volumetric_speed",
        "flow_ratio",
        "retraction",
        "temp_tower",
    }
    for m in hints:
        assert m and m.group(2) in kinds[m.group(1)].methods, m


def test_plates_are_cli_kinds() -> None:
    assert "plate1" in PLATES and "plate1" in cg.all_kinds()


def test_only_skip_select_tests_and_pack_without_holes(
    orca: str, tmp_path: Path
) -> None:
    from slicing.calibration_plates import resolve_tests

    assert resolve_tests("plate1", frozenset(), frozenset()) is None
    chosen = resolve_tests("plate1", frozenset(), frozenset({"pa", "flow"}))
    assert chosen == frozenset(filament.TESTS) - {"pa", "flow"}
    assert resolve_tests(
        "plate1", frozenset({"temp", "mvs"}), frozenset({"mvs"})
    ) == frozenset({"temp"})
    with pytest.raises(cg.GenError, match="no test ringing"):
        resolve_tests("plate1", frozenset({"ringing"}), frozenset())
    with pytest.raises(cg.GenError, match="leave no test"):
        resolve_tests("plate1", frozenset({"pa"}), frozenset({"pa"}))

    ctx = cp.make_context(
        "Creality K1 SE", orca, "PLA", "#E8732A", None, chosen, presets=STOCK
    )
    plate = filament.build(ctx)
    kinds = {o.kind for o in plate.objects} | {c.kind for c in plate.custom}
    assert "pressure_advance" not in kinds and "flow_ratio" not in kinds
    # without the PA block the strip takes the front-left corner: the skipped test left no hole
    mvs = next(c for c in plate.custom if c.name == "mvs_strip")
    assert mvs.bbox()[0] == pytest.approx(filament.MARGIN)
    # the sequential tower sits extruder_clearance_radius behind every front object
    front = max(o.bbox()[4] for o in plate.objects if o.phase == "layer")
    tower = next(o for o in plate.objects if o.phase == "sequential")
    assert tower.bbox()[1] >= front + 45 - 1e-6
    out = tmp_path / "p.gcode"
    cp.build_plate(ctx, plate, orca, out)
    assert "flow_m10" not in out.read_text()


def test_pack_places_sections_without_overlap() -> None:
    def sec(name: str, w: float, d: float, seq: bool = False) -> filament.Section:
        return filament.Section(name, w, d, seq, lambda x, y: ((), ()))

    placed = filament.pack(
        [sec("a", 80, 70), sec("b", 100, 10), sec("c", 90, 20), sec("t", 20, 10, True)],
        (220.0, 220.0),
        45.0,
    )
    rects = {s.test: (x, y, x + s.width, y + s.depth) for s, x, y in placed}
    assert rects["a"][:2] == (filament.MARGIN, filament.MARGIN)
    assert rects["b"][1] == filament.MARGIN  # next to a, not above it
    for p, q in ((p, q) for p in rects for q in rects if p < q):
        a, b = rects[p], rects[q]
        assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1], (p, q)
    assert rects["t"][1] >= max(r[3] for k, r in rects.items() if k != "t") + 45
    with pytest.raises(cg.GenError, match="no room"):
        filament.pack([sec("huge", 300, 10)], (220.0, 220.0), 45.0)


def _user_presets(tmp_path: Path) -> None:
    user = tmp_path / "user"
    for kind, data in (
        ("machine", {"name": "K1 SE - Copy", "inherits": "Creality K1 SE 0.4 nozzle"}),
        (
            "process",
            {
                "name": "Std accel 8900",
                "inherits": "0.20mm Standard @Creality K1 SE",
                "default_acceleration": "8000",
            },
        ),
        (
            "filament",
            {
                "name": "Orange PLA @K1SE",
                "inherits": "Creality Generic PLA @K1-all",
                "filament_flow_ratio": ["0.931"],
                "pressure_advance": ["0.05"],
                "filament_type": ["PLA"],
            },
        ),
    ):
        (user / kind).mkdir(parents=True, exist_ok=True)
        (user / kind / f"{data['name']}.json").write_text(json.dumps(data))


def test_user_presets_reach_the_gcode_and_the_map(orca: str, tmp_path: Path) -> None:
    _user_presets(tmp_path)
    choice = cg.PresetChoice(
        "K1 SE - Copy",
        "Std accel 8900",
        "Orange PLA @K1SE",
        (
            ("machine", "fleet k1se.slicer"),
            ("process", "fleet k1se.slicer"),
            ("filament", "--filament-preset"),
        ),
    )
    out = tmp_path / "u.gcode"
    cg.generate(
        "plate1",
        "Creality K1 SE",
        orca,
        out,
        presets=choice,
        only=frozenset({"bridge"}),
    )
    text = out.read_text()
    for line in (
        "; default_acceleration = 8000",
        "; filament_flow_ratio = 0.931",
        "; pressure_advance = 0.05",
        "; printer_settings_id = K1 SE - Copy",
        "; print_settings_id = Std accel 8900",
        '; filament_settings_id = "Orange PLA @K1SE"',
        "; compatible_printers = ['K1 SE - Copy']",
    ):  # Orca's CLI matches by the machine's name
        assert line in text.splitlines(), line
    assert (
        "; 3d calibrate presets: machine=K1 SE - Copy (fleet k1se.slicer); process=Std accel 8900 "
        "(fleet k1se.slicer); filament=Orange PLA @K1SE (--filament-preset)"
    ) in text.splitlines()
    data = json.loads(out.with_suffix(".json").read_text())
    assert data["presets"] == {
        "machine": "K1 SE - Copy",
        "process": "Std accel 8900",
        "filament": "Orange PLA @K1SE",
        "chosen_by": {
            "machine": "fleet k1se.slicer",
            "process": "fleet k1se.slicer",
            "filament": "--filament-preset",
        },
    }


def test_no_presets_is_refused_not_stock(orca: str, tmp_path: Path) -> None:
    with pytest.raises(cg.GenError, match="no slicer presets chosen"):
        cg.generate("flow", "Creality K1 SE", orca, tmp_path / "f.gcode")
    missing = cg.PresetChoice(
        "Creality K1 SE 0.4 nozzle", "0.20mm Standard @Creality K1 SE", "No such PLA"
    )
    with pytest.raises(cg.GenError, match="'No such PLA' not found"):
        cg.generate(
            "flow", "Creality K1 SE", orca, tmp_path / "f.gcode", presets=missing
        )
    _user_presets(tmp_path)
    wrong = cg.PresetChoice(
        "Creality K1 SE 0.4 nozzle",
        "0.20mm Standard @Creality K1 SE",
        "Orange PLA @K1SE",
    )
    with pytest.raises(cg.GenError, match="is PLA, not PETG"):
        cg.generate(
            "flow",
            "Creality K1 SE",
            orca,
            tmp_path / "f.gcode",
            material="PETG",
            presets=wrong,
        )


def test_cli_presets_from_flags_then_fleet(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from commands.calibrate import _preset_choice
    from errors import UsageError

    entry: Any = SimpleNamespace(  # stands in for a FleetPrinter: only .name and .slicer are read
        name="k1se", slicer={"machine": "M - Copy", "process": "P 8900"}
    )
    choice = _preset_choice({"--filament-preset": "Orange PLA @K1SE"}, entry)
    assert (choice.machine, choice.process, choice.filament) == (
        "M - Copy",
        "P 8900",
        "Orange PLA @K1SE",
    )
    assert dict(choice.sources) == {
        "machine": "fleet k1se.slicer",
        "process": "fleet k1se.slicer",
        "filament": "--filament-preset",
    }
    flagged = _preset_choice(
        {"--filament-preset": "F", "--process-preset": "Other"}, entry
    )
    assert (
        flagged.process == "Other"
        and dict(flagged.sources)["process"] == "--process-preset"
    )
    with pytest.raises(UsageError, match="no filament preset for k1se"):
        _preset_choice({}, entry)
    bare: Any = SimpleNamespace(name="u1", slicer={})
    with pytest.raises(UsageError, match="no machine, process, filament preset for u1"):
        _preset_choice({}, bare)


def test_fleet_slicer_key(tmp_path: Path) -> None:
    from printops.fleet import load_fleet
    from printops.fleet import FleetError

    base = "k1:\n  model: Creality K1 SE\n  owner: own\n  access: offline\n  heads: [{label: T1, index: 0}]\n"
    good = tmp_path / "good.yaml"
    good.write_text(base + "  slicer: {machine: M - Copy, process: P 8900}\n")
    fleet = load_fleet(good)
    assert fleet is not None and fleet.printers["k1"].slicer == {
        "machine": "M - Copy",
        "process": "P 8900",
    }
    bad = tmp_path / "bad.yaml"
    bad.write_text(base + "  slicer: {machine: M, filament: F}\n")
    with pytest.raises(FleetError) as err:
        load_fleet(bad)
    assert any("slicer.filament: unknown key" in r for r in err.value.remediation)


def test_user_preset_roots_follow_the_profiles_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repo profiles first, then the slicer's logged-in preset folder (XDG config, preset_folder)."""
    monkeypatch.delenv("ORCA_USER_PRESETS", raising=False)
    repo = tmp_path / "profiles" / "snapmaker-orca"
    (repo / "filament").mkdir(parents=True)
    monkeypatch.setenv("THREED_PROFILES_DIR", str(tmp_path / "profiles"))
    xdg = tmp_path / "xdg"
    data = xdg / "Snapmaker_Orca"
    (data / "user" / "12345" / "filament").mkdir(parents=True)
    (data / "Snapmaker_Orca.conf").write_text(
        json.dumps({"app": {"preset_folder": "12345"}})
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert cg.user_preset_roots() == [repo, data / "user" / "12345"]


@pytest.mark.parametrize("only", [{"pa"}, {"mvs"}, {"temp"}, {"pa", "mvs", "temp"}])
def test_plate1_needs_a_test_sliced_by_layer(orca: str, only: set[str]) -> None:
    ctx = cp.make_context(
        "Creality K1 SE", orca, "PLA", "#E8732A", None, frozenset(only), presets=STOCK
    )
    with pytest.raises(
        cg.GenError, match="must keep one of flow, bridge, overhang, retraction"
    ):
        filament.build(ctx)


def test_plate1_single_sliced_tests_build(orca: str, tmp_path: Path) -> None:
    for test in filament.LAYER_TESTS:
        ctx = cp.make_context(
            "Creality K1 SE",
            orca,
            "PLA",
            "#E8732A",
            None,
            frozenset({test}),
            presets=STOCK,
        )
        out = tmp_path / f"{test}.gcode"
        cp.build_plate(ctx, filament.build(ctx), orca, out)
        assert out.is_file()
