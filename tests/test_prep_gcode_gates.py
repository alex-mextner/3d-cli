"""Gates of `prep.gcode_gates`: each gate has a conforming G-code (PASS) and a deliberately broken one (FAIL)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from errors import InputNotFound, InvalidArgument
from prep.contracts import StepResult
from prep.gcode_gates import GateExpectations, GateSpec, Region, check_gcode, parse
from prep_gcode_fixtures import write_gcode

N = 24
FLAT = [(0.2, 0)] * N
Z_MIN, Z_MAX = 0.0, 999.0


def gate(rep, name):  # type: ignore[no-untyped-def]
    return rep.gate(name)


def run(
    tmp_path: Path,
    layers,
    spec: GateSpec | None = None,
    exp: GateExpectations | None = None,
    **kw,
):  # type: ignore[no-untyped-def]
    path = write_gcode(tmp_path / "t.gcode", layers, **kw)
    return check_gcode(path, spec or GateSpec(), exp)


def test_conforming_single_tool_file_passes_every_gate_with_input(
    tmp_path: Path,
) -> None:
    rep = run(tmp_path, FLAT)
    by = {g.name: g.verdict for g in rep.gates}
    assert (
        by["walls"] == by["infill"] == by["supports"] == by["layer-heights"] == "PASS"
    )
    assert by["prime-tower"] == by["bed-bounds"] == "PASS"
    # no manifest / tool list -> SKIP, never PASS
    assert (
        by["tools-allowed"]
        == by["region-tools"]
        == by["mix-layer-cap"]
        == by["mix-minor-adjacent"]
        == "SKIP"
    )
    assert rep.verdict == "PASS"


def test_missing_manifest_skips_mix_and_region_gates_with_reason(
    tmp_path: Path,
) -> None:
    rep = run(tmp_path, FLAT)
    for name in (
        "mix-layer-cap",
        "region-tools",
        "mix-minor-adjacent",
        "tools-allowed",
    ):
        g = gate(rep, name)
        assert g.verdict == "SKIP"
        assert "no " in g.detail and (
            "manifest" in g.detail or "allowed tools" in g.detail
        )


# ---- parser


def test_parser_reads_layers_tools_time_filament_exactly(tmp_path: Path) -> None:
    layers = [(0.2, 0), (0.1, 1), (0.1, 1), (0.12, 0)]
    p = parse(
        write_gcode(
            tmp_path / "p.gcode", layers, grams="1.5, 2.5", time_text="1d 2h 3m 4s"
        )
    )
    assert [round(la.z, 3) for la in p.layers] == [0.2, 0.3, 0.4, 0.52]
    assert [la.h for la in p.layers] == [0.2, 0.1, 0.1, 0.12]
    assert p.tool_sequence == [0, 0, 1, 1, 0]  # initial T0 + one Tn per layer
    assert p.n_tool_changes == 2
    assert p.filament_g == [1.5, 2.5]
    assert p.time == "1d 2h 3m 4s"
    assert p.time_s == 86400 + 2 * 3600 + 3 * 60 + 4
    assert p.tools_used() == {0, 1}
    # start-G-code purge (before the first layer) is not part of any layer's extents
    assert p.extents is not None and p.extents[0] > 100


def test_parser_handles_absolute_extrusion(tmp_path: Path) -> None:
    g = tmp_path / "abs.gcode"
    g.write_text(
        "M82\nG92 E0\n;LAYER_CHANGE\n;Z:0.2\n;HEIGHT:0.2\n;TYPE:Outer wall\nG0 X10 Y10\n"
        "G1 X20 Y10 E1.0\nG1 X20 Y20 E0.5\nG1 X30 Y20 E1.5\n"
    )
    p = parse(g)
    runs = p.layers[0].runs
    # the retraction (E 1.0 -> 0.5) splits the run; only increasing E extrudes
    assert [len(r.pts) for r in runs] == [2, 2]
    assert runs[0].pts[-1].tolist() == [20.0, 10.0]


def test_parse_200k_lines_is_fast(tmp_path: Path) -> None:
    path = write_gcode(tmp_path / "big.gcode", [(0.2, 0)] * 650)
    lines = path.read_text().count("\n")
    assert lines > 200_000
    t0 = time.perf_counter()
    p = parse(path)
    assert (
        time.perf_counter() - t0 < 15.0
    )  # ~1 s on a quiet machine; generous for a shared one
    assert len(p.layers) == 650


def test_parse_missing_file_raises_input_not_found(tmp_path: Path) -> None:
    with pytest.raises(InputNotFound):
        parse(tmp_path / "nope.gcode")


# ---- walls / infill


def test_walls_four_loops_pass_three_fail(tmp_path: Path) -> None:
    assert gate(run(tmp_path, FLAT, loops=4), "walls").verdict == "PASS"
    bad = gate(run(tmp_path, FLAT, loops=3), "walls")
    assert bad.verdict == "FAIL"
    assert (
        f"{N} layers with walls" in bad.detail
        and f"{N} have fewer than 4" in bad.detail
    )


def test_walls_tolerates_a_few_short_layers_but_not_many(tmp_path: Path) -> None:
    # 1 short layer in 60 = 1.7 % <= 2 % allowed; build by splicing a 2-loop layer into a 4-loop file
    good = tmp_path / "g.gcode"
    write_gcode(good, [(0.2, 0)] * 59)
    short = (
        write_gcode(tmp_path / "s.gcode", [(0.2, 0)], loops=2)
        .read_text()
        .split(";LAYER_CHANGE", 1)[1]
    )
    mixed = tmp_path / "m.gcode"
    mixed.write_text(
        good.read_text().replace(
            "; filament used",
            ";LAYER_CHANGE" + short.split("; filament used")[0] + "; filament used",
            1,
        )
    )
    rep = check_gcode(mixed, GateSpec())
    assert gate(rep, "walls").verdict == "PASS"
    assert "1 have fewer than 4" in gate(rep, "walls").detail
    assert (
        check_gcode(mixed, GateSpec(wall_deficit_fraction=0.0)).gate("walls").verdict
        == "FAIL"
    )


def test_infill_15_percent_pass_30_percent_fail(tmp_path: Path) -> None:
    ok = gate(run(tmp_path, FLAT, density=0.15), "infill")
    assert ok.verdict == "PASS"
    est = float(ok.detail.split("estimate ")[1].split(" ")[0])
    assert est == pytest.approx(0.15, rel=0.1)
    bad = gate(run(tmp_path, FLAT, density=0.30), "infill")
    assert bad.verdict == "FAIL"
    assert float(bad.detail.split("estimate ")[1].split(" ")[0]) == pytest.approx(
        0.30, rel=0.1
    )


def test_infill_without_estimable_layers_is_skip(tmp_path: Path) -> None:
    rep = run(tmp_path, FLAT[:2])
    assert (
        gate(rep, "infill").verdict == "SKIP" and "need 3" in gate(rep, "infill").detail
    )


def test_walls_and_infill_switched_off_are_skip(tmp_path: Path) -> None:
    rep = run(tmp_path, FLAT, GateSpec(walls=None, infill=None))
    assert gate(rep, "walls").verdict == gate(rep, "infill").verdict == "SKIP"


def test_zone_limits_walls_gate_to_the_structural_slab(tmp_path: Path) -> None:
    # bottom 12 layers 4 loops would pass, so put a 2-loop file and a zone with no layer: SKIP, not PASS
    rep = run(tmp_path, FLAT, GateSpec(zone=(50.0, 60.0)), loops=2)
    assert gate(rep, "walls").verdict == "SKIP"
    assert (
        gate(run(tmp_path, FLAT, GateSpec(zone=(0.0, 3.0)), loops=2), "walls").verdict
        == "FAIL"
    )


# ---- supports


def test_support_line_fails_supports_gate(tmp_path: Path) -> None:
    assert gate(run(tmp_path, FLAT), "supports").verdict == "PASS"
    bad = gate(run(tmp_path, FLAT, support_layer=5), "supports")
    assert bad.verdict == "FAIL" and "Support" in bad.detail
    assert (
        gate(
            run(tmp_path, FLAT, GateSpec(supports=True), support_layer=5), "supports"
        ).verdict
        == "PASS"
    )


# ---- layer heights


def test_layer_of_040_fails_layer_heights(tmp_path: Path) -> None:
    layers = [*FLAT[:10], (0.40, 0), *FLAT[:10]]
    bad = gate(run(tmp_path, layers), "layer-heights")
    assert (
        bad.verdict == "FAIL"
        and "max 0.400" in bad.detail
        and "outside 0.08..0.32" in bad.detail
    )


def test_sixty_percent_thin_layers_fail_rare_fraction(tmp_path: Path) -> None:
    layers = (
        [(0.2, 0)] + [(0.08, 0)] * 15 + [(0.2, 0)] * 10
    )  # 15 of 25 body layers = 60 %
    bad = gate(run(tmp_path, layers), "layer-heights")
    assert bad.verdict == "FAIL" and "60% of layers at 0.08" in bad.detail
    ok = gate(
        run(tmp_path, [(0.2, 0)] + [(0.08, 0)] * 2 + [(0.2, 0)] * 20), "layer-heights"
    )
    assert ok.verdict == "PASS"


def test_inconsistent_height_comment_fails_layer_heights(tmp_path: Path) -> None:
    path = write_gcode(tmp_path / "x.gcode", FLAT)
    path.write_text(
        path.read_text()
        .replace(";HEIGHT:0.200", ";HEIGHT:0.150", 1)
        .replace(";HEIGHT:0.200", ";HEIGHT:0.150", 1)
    )
    g = check_gcode(path, GateSpec()).gate("layer-heights")
    assert g.verdict == "FAIL" and "differ from ;HEIGHT" in g.detail


def test_no_layers_fails_layer_heights_and_skips_bed(tmp_path: Path) -> None:
    p = tmp_path / "empty.gcode"
    p.write_text("G28\n")
    rep = check_gcode(p, GateSpec())
    assert gate(rep, "layer-heights").verdict == "FAIL"
    assert gate(rep, "bed-bounds").verdict == "SKIP"
    assert rep.verdict == "FAIL"


# ---- tools


MIX = (0, 1, 0, 2)
MIX_LAYERS = [(0.1, MIX[k % 4]) for k in range(24)]
MIX_EXP = GateExpectations(
    regions=(Region("eyes", Z_MIN, Z_MAX, mix=MIX),), allowed_tools=frozenset({0, 1, 2})
)


def test_mix_region_conforming_passes_all_mix_gates(tmp_path: Path) -> None:
    rep = run(tmp_path, MIX_LAYERS, GateSpec(), MIX_EXP, tower=True)
    for name in (
        "mix-layer-cap",
        "region-tools",
        "mix-minor-adjacent",
        "tools-allowed",
        "prime-tower",
    ):
        assert gate(rep, name).verdict == "PASS", gate(rep, name).detail
    assert rep.verdict == "PASS"


def test_mix_layer_020_in_mix_zone_fails_cap_only(tmp_path: Path) -> None:
    layers = list(MIX_LAYERS)
    layers[10] = (0.20, layers[10][1])
    rep = run(tmp_path, layers, GateSpec(), MIX_EXP, tower=True)
    g = gate(rep, "mix-layer-cap")
    assert (
        g.verdict == "FAIL"
        and "eyes: 1 of 24 layers above 0.12" in g.detail
        and "0.200" in g.detail
    )
    assert (
        gate(rep, "layer-heights").verdict == "PASS"
    )  # 0.20 is a legal layer, just not in a mix zone
    # the same layer outside the mix slab is fine
    exp = GateExpectations(regions=(Region("eyes", z_min=0.0, z_max=0.8, mix=MIX),))
    assert (
        gate(
            run(tmp_path, layers, GateSpec(), exp, tower=True), "mix-layer-cap"
        ).verdict
        == "PASS"
    )


def test_tool_not_allowed_fails(tmp_path: Path) -> None:
    spec = GateSpec(tools=frozenset({0, 1}))
    rep = run(tmp_path, MIX_LAYERS, spec, tower=True)  # uses T2 too
    g = gate(rep, "tools-allowed")
    assert g.verdict == "FAIL" and "not allowed: [T2]" in g.detail
    assert (
        gate(
            run(tmp_path, MIX_LAYERS, GateSpec(tools=frozenset({0, 1, 2})), tower=True),
            "tools-allowed",
        ).verdict
        == "PASS"
    )


def test_wrong_mix_pattern_fails_region_tools(tmp_path: Path) -> None:
    layers = [(0.1, (0, 2, 0, 1)[k % 4]) for k in range(24)]  # 1 and 2 swapped
    g = gate(run(tmp_path, layers, GateSpec(), MIX_EXP, tower=True), "region-tools")
    assert g.verdict == "FAIL" and "eyes: mix 0102 not followed" in g.detail
    # a pure region printed with another head
    exp = GateExpectations(regions=(Region("base", Z_MIN, Z_MAX, tools=(0,)),))
    bad = gate(run(tmp_path, MIX_LAYERS, GateSpec(), exp, tower=True), "region-tools")
    assert bad.verdict == "FAIL" and "outside [T0]" in bad.detail
    assert gate(run(tmp_path, FLAT, GateSpec(), exp), "region-tools").verdict == "PASS"


def test_region_without_extrusion_fails(tmp_path: Path) -> None:
    exp = GateExpectations(regions=(Region("sky", z_min=500, z_max=600, tools=(0,)),))
    g = gate(run(tmp_path, FLAT, GateSpec(), exp), "region-tools")
    assert g.verdict == "FAIL" and "sky: no extrusion" in g.detail


def test_minor_colour_on_adjacent_layers_fails(tmp_path: Path) -> None:
    seq = (0, 1, 1, 0)  # tool 1 is minor (half the height) and prints twice in a row
    layers = [(0.1, seq[k % 4]) for k in range(24)]
    exp = GateExpectations(regions=(Region("eyes", Z_MIN, Z_MAX, mix=seq),))
    rep = run(tmp_path, layers, GateSpec(), exp, tower=True)
    g = gate(rep, "mix-minor-adjacent")
    assert (
        g.verdict == "FAIL" and "eyes:" in g.detail and "same minor colour" in g.detail
    )
    assert (
        gate(rep, "region-tools").verdict == "PASS"
    )  # the pattern is followed; the pattern itself is the defect
    # the fixed sequence passes (see test_mix_region_conforming_passes_all_mix_gates)


# ---- prime tower


def test_missing_prime_tower_with_two_tools_fails(tmp_path: Path) -> None:
    layers = [(0.2, k % 2) for k in range(N)]
    bad = gate(run(tmp_path, layers, tower=False), "prime-tower")
    assert bad.verdict == "FAIL" and "needs a prime tower" in bad.detail
    assert gate(run(tmp_path, layers, tower=True), "prime-tower").verdict == "PASS"


def test_manifest_prime_tower_false_rejects_a_tower(tmp_path: Path) -> None:
    exp = GateExpectations(prime_tower=False)
    assert (
        gate(run(tmp_path, FLAT, GateSpec(), exp, tower=True), "prime-tower").verdict
        == "FAIL"
    )
    assert gate(run(tmp_path, FLAT, GateSpec(), exp), "prime-tower").verdict == "PASS"


# ---- bed


def test_extrusion_outside_bed_fails(tmp_path: Path) -> None:
    bad = gate(run(tmp_path, FLAT, centre=(265.0, 135.0)), "bed-bounds")
    assert bad.verdict == "FAIL" and "leaves the 270x270 bed" in bad.detail
    assert (
        gate(
            run(
                tmp_path, FLAT, GateSpec(bed_mm=(300, 270, 270)), centre=(265.0, 135.0)
            ),
            "bed-bounds",
        ).verdict
        == "PASS"
    )


def test_build_height_is_checked(tmp_path: Path) -> None:
    g = gate(run(tmp_path, FLAT, GateSpec(bed_mm=(270, 270, 3.0))), "bed-bounds")
    assert g.verdict == "FAIL" and "above the 3 mm build height" in g.detail


def test_travel_outside_printable_area_is_caught_by_move_bounds(tmp_path: Path) -> None:
    area = "0x0,270x0,270x270,0x270"
    clean = gate(run(tmp_path, FLAT, printable_area=area), "bed-bounds")
    assert clean.verdict == "PASS"
    bad = gate(run(tmp_path, FLAT, printable_area=area, travel_out=300.0), "bed-bounds")
    assert (
        bad.verdict == "FAIL"
        and "outside the G-code's own printable_area" in bad.detail
    )


# ---- report / manifest


def test_report_is_a_step_result_with_json_dict_and_worst_gate_verdict(
    tmp_path: Path,
) -> None:
    rep = run(tmp_path, FLAT, loops=3)
    assert isinstance(rep, StepResult)
    assert rep.verdict == "FAIL"
    d = json.loads(json.dumps(rep.to_dict()))
    assert d["verdict"] == "FAIL" and {g["name"] for g in d["gates"]} >= {
        "walls",
        "bed-bounds",
    }
    assert d["stats"]["layers"] == N and d["spec"]["walls"] == 4
    assert "FAIL: walls" in rep.summary() and f"{N} layers" in rep.summary()


def test_from_manifest_reads_regions_tolerates_missing_keys_and_converts_slots(
    tmp_path: Path,
) -> None:
    m = tmp_path / "p.manifest.json"
    m.write_text(
        json.dumps(
            {
                "heads_slots": [1, 2, 3],
                "prime_tower": True,
                "structural": {"z_min": 1, "z_max": 9},
                "regions": [
                    {"name": "eyes", "z_min": 10, "z_max": 20, "mix_slots": "1213"},
                    {"name": "base", "z_min": 0, "z_max": 5, "tools": [0]},
                    {"z_min": 5, "z_max": 6},
                ],
            }
        )
    )
    e = GateExpectations.from_manifest(m)
    assert (
        e.allowed_tools == frozenset({0, 1, 2})
        and e.prime_tower is True
        and e.structural_zone == (1.0, 9.0)
    )
    assert e.regions[0].mix == (0, 1, 0, 2) and e.regions[1].tools == (0,)
    assert (
        e.regions[2].tools is None
        and e.regions[2].mix is None
        and e.regions[2].name == "z5-6"
    )
    empty = tmp_path / "e.manifest.json"
    empty.write_text("{}")
    assert GateExpectations.from_manifest(empty) == GateExpectations()


def test_from_manifest_rejects_malformed_and_missing(tmp_path: Path) -> None:
    with pytest.raises(InputNotFound):
        GateExpectations.from_manifest(tmp_path / "none.json")
    bad = tmp_path / "bad.json"
    bad.write_text('{"regions": [{"name": "x"}]}')
    with pytest.raises(InvalidArgument):
        GateExpectations.from_manifest(bad)
    bad.write_text('{"allowed_tools": "a,b"}')
    with pytest.raises(InvalidArgument):
        GateExpectations.from_manifest(bad)
    bad.write_text("[1]")
    with pytest.raises(InvalidArgument):
        GateExpectations.from_manifest(bad)


def test_manifest_structural_zone_drives_walls_gate(tmp_path: Path) -> None:
    exp = GateExpectations(structural_zone=(0.0, 3.0))
    assert (
        gate(run(tmp_path, FLAT, GateSpec(), exp, loops=2), "walls").verdict == "FAIL"
    )
