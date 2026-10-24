"""Unit tests for lib/gcode_mix_gate.py + lib/gcode_scan.py (issue #551): the multi-colour G-code gates and their teeth.

Every test prints a tiny synthetic G-code (tests/mix_gate_fixtures.py) through the real gate and asserts the verdict;
the 'wrong print' tests are the teeth: each one MUST make its gate FAIL."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import gcode_mix_gate as mg
from errors import InputNotFound, InvalidArgument
from gcode_scan import scan_gcode
from mix_gate_fixtures import (
    LAYERS,
    POCKET,
    POCKET_SHIFT,
    layer_top,
    pocket_gcode,
    stripe_gcode,
    write_config,
    write_parts,
)


@pytest.fixture(scope="module")
def stls(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_parts(tmp_path_factory.mktemp("mixparts"))


def _gate(
    tmp_path: Path,
    stls: Path,
    gcode: str,
    options: mg.GateOptions | None = None,
    /,
    **cfg_extra: Any,
) -> mg.GateResult:
    g = tmp_path / "plate.gcode"
    g.write_text(gcode, encoding="ascii")
    cfg = mg.load_config(write_config(tmp_path / "gates.json", **cfg_extra))
    opt = options or mg.GateOptions(parts_dir=stls)
    return mg.run_gates(g, cfg, opt)


def _row(res: mg.GateResult, prefix: str) -> mg.Row:
    rows = [r for r in res.report.rows if r.name.startswith(prefix)]
    assert len(rows) == 1, [r.name for r in res.report.rows]
    return rows[0]


# ---------------------------------------------------------------- scanner


def test_scan_streams_layers_pauses_and_header(tmp_path: Path) -> None:
    g = tmp_path / "p.gcode"
    g.write_text(stripe_gcode(pause_at=7), encoding="ascii")
    seen: list[tuple[int, float | None, int, list[int]]] = []
    scan = scan_gcode(
        g, [lambda lay: seen.append((lay.index, lay.z, len(lay.runs), lay.tools))]
    )
    assert scan.layers == LAYERS and [s[0] for s in seen] == list(range(LAYERS))
    assert seen[3][1] == pytest.approx(0.4) and seen[3][3] == [
        2,
        1,
        0,
    ]  # T2 (base), mix tool, tower
    assert [(p.layer, p.runs_before) for p in scan.pauses] == [(7, 0)]
    assert (
        scan.header["wall_loops"] == "4"
        and scan.filament_g == [1.0, 1.0, 1.0]
        and scan.time == "1h 2m 3s"
    )
    assert scan.types["Prime tower"] == LAYERS
    assert scan.stats()["prime_tower_g"] > 0


# ---------------------------------------------------------------- colour volumes


def test_correct_print_passes_every_colour_gate(tmp_path: Path, stls: Path) -> None:
    res = _gate(tmp_path, stls, stripe_gcode())
    assert res.ok, res.render()
    assert "OVERALL PASS" in res.render()
    assert "follows the pattern" in _row(res, "colour volume stripe: mix AB").name
    assert _row(res, "colour volume base: pure slot 3 (T2)").status == "PASS"


def test_pattern_shifted_by_one_layer_fails_the_mix_gate(
    tmp_path: Path, stls: Path
) -> None:
    res = _gate(tmp_path, stls, stripe_gcode(shift=1))
    row = _row(res, "colour volume stripe: mix AB")
    assert row.status == "FAIL" and f"{LAYERS} off the pattern" in row.detail
    assert not res.ok


def test_one_solid_layer_with_a_wrong_tool_fails_although_it_is_one_layer_in_a_hundred(
    tmp_path: Path, stls: Path
) -> None:
    """1 of 100 layers is inside the 5 % tolerance of fringe layers; a layer with >= 1 mm of its own outer wall gets none."""
    res = _gate(tmp_path, stls, stripe_gcode(wrong_tool={20: 2}))
    row = _row(res, "colour volume stripe: mix AB")
    assert row.status == "FAIL" and "(2.1, 20, [2])" in row.detail


def test_fringe_layer_with_under_one_mm_of_own_wall_is_not_judged(
    tmp_path: Path, stls: Path
) -> None:
    """The iris-top false positive: a layer whose own outer wall is 0.5 mm is no evidence, so a wrong tool there is reported, not failed."""
    res = _gate(tmp_path, stls, stripe_gcode(wrong_tool={20: 2}, short_wall={20: 0.5}))
    row = _row(res, "colour volume stripe: mix AB")
    assert row.status == "PASS" and "1 layers not judged" in row.detail
    strict = _gate(
        tmp_path,
        stls,
        stripe_gcode(wrong_tool={20: 2}, short_wall={20: 0.5}),
        mg.GateOptions(parts_dir=stls, limits=mg.Limits(min_evidence_mm=0.4)),
    )
    assert _row(strict, "colour volume stripe: mix AB").status == "FAIL"


def test_pure_volume_printed_with_another_tool_fails(
    tmp_path: Path, stls: Path
) -> None:
    g = stripe_gcode().replace("T2\nG1 X1 Y0", "T1\nG1 X1 Y0")
    res = _gate(tmp_path, stls, g)
    assert _row(res, "colour volume base: pure").status == "FAIL"


def test_stripe_thicker_than_one_layer_fails(tmp_path: Path, stls: Path) -> None:
    """Pattern '11' = one tool on every layer: the pattern itself matches, the stripe gate must refuse the 0.2 mm bands."""
    cfg = {
        "parts": [
            {"name": "base", "assign": {"slot": 3}},
            {"name": "stripe", "assign": {"mix": "11", "mix_src": "AA"}},
        ]
    }
    res = _gate(tmp_path, stls, stripe_gcode(pattern="11"), **cfg)
    assert _row(res, "colour volume stripe: mix AA follows").status == "PASS"
    assert _row(res, "colour volume stripe: no stripe thicker").status == "FAIL"


def test_stl_names_and_sorted_directory_are_both_accepted(
    tmp_path: Path, stls: Path
) -> None:
    named = {
        "parts": [
            {"name": "base", "stl": "1_base.stl", "assign": {"slot": 3}},
            {"name": "stripe", "stl": "2_stripe.stl", "assign": {"mix": "12"}},
        ]
    }
    assert _gate(tmp_path, stls, stripe_gcode(), **named).ok
    swapped = {
        "parts": [
            {"name": "base", "stl": "2_stripe.stl", "assign": {"slot": 3}},
            {"name": "stripe", "stl": "1_base.stl", "assign": {"mix": "12"}},
        ]
    }
    assert not _gate(tmp_path, stls, stripe_gcode(), **swapped).ok


def test_part_count_must_match_the_directory(tmp_path: Path, stls: Path) -> None:
    one = {"parts": [{"name": "base", "assign": {"slot": 3}}]}
    with pytest.raises(InvalidArgument, match="--parts"):
        _gate(tmp_path, stls, stripe_gcode(), **one)


# ---------------------------------------------------------------- teeth


def test_mix_teeth_catch_shift_and_wrong_tool_on_a_correct_print(
    tmp_path: Path, stls: Path
) -> None:
    res = _gate(
        tmp_path, stls, stripe_gcode(), mg.GateOptions(parts_dir=stls, mix_teeth=True)
    )
    labels = {t.label: t for t in res.teeth}
    assert res.ok, res.render()
    assert labels["stripe real print passes"].got == "PASS"
    assert labels["stripe pattern shifted by one layer"].got == "FAIL"
    wrong = [t for t in res.teeth if "printed with" in t.label]
    assert len(wrong) == 2 and all(
        t.caught for t in wrong
    )  # T0 and T1 are both expected somewhere
    assert "CAUGHT" in res.render() and "NOT CAUGHT" not in res.render()


def test_mix_teeth_on_a_wrong_print_fail_the_real_print_control(
    tmp_path: Path, stls: Path
) -> None:
    res = _gate(
        tmp_path,
        stls,
        stripe_gcode(shift=1),
        mg.GateOptions(parts_dir=stls, mix_teeth=True),
    )
    real = next(t for t in res.teeth if t.label.endswith("real print passes"))
    assert real.got == "FAIL" and not real.caught and not res.ok


def test_vacuous_teeth_fail_loudly(tmp_path: Path, stls: Path) -> None:
    """Pattern '11' shifted by one layer is the same pattern, so the shifted control passes where it must fail:
    the gate cannot tell a shifted print from a right one and the verdict must say so."""
    cfg = {
        "parts": [
            {"name": "base", "assign": {"slot": 3}},
            {"name": "stripe", "assign": {"mix": "11", "mix_src": "AA"}},
        ]
    }
    res = _gate(
        tmp_path,
        stls,
        stripe_gcode(pattern="11"),
        mg.GateOptions(parts_dir=stls, mix_teeth=True),
        **cfg,
    )
    shifted = next(t for t in res.teeth if "shifted" in t.label)
    assert shifted.got == "PASS" and not shifted.caught
    assert not res.ok and "TEETH FAILED" in res.render()


def test_teeth_without_solid_layers_cannot_prove_the_gate(
    tmp_path: Path, stls: Path
) -> None:
    opt = mg.GateOptions(
        parts_dir=stls, mix_teeth=True, limits=mg.Limits(min_evidence_mm=1000.0)
    )
    res = _gate(tmp_path, stls, stripe_gcode(), opt)
    assert not res.ok
    assert any("impossible" in t.detail and not t.caught for t in res.teeth)


def test_teeth_volume_filter_names_only_known_volumes(
    tmp_path: Path, stls: Path
) -> None:
    opt = mg.GateOptions(parts_dir=stls, mix_teeth=True, teeth_volumes=("nope",))
    res = _gate(tmp_path, stls, stripe_gcode(), opt)
    assert not res.ok and any(t.label == "nope" for t in res.teeth)


# ---------------------------------------------------------------- pause and tools


PAUSE = {"print_z": layer_top(30), "last_layer_before": {"top": layer_top(29)}}


def test_pause_in_the_configured_layer_before_any_extrusion_passes(
    tmp_path: Path, stls: Path
) -> None:
    res = _gate(tmp_path, stls, stripe_gcode(pause_at=30), pause=PAUSE)
    assert _row(res, "pause").status == "PASS", res.render()


@pytest.mark.parametrize(
    ("gcode_kwargs", "why"),
    [
        ({"pause_at": 31}, "wrong layer"),
        (
            {"pause_at": 30, "pause_after_extrusion": True},
            "after the layer already extruded",
        ),
        ({}, "no pause at all"),
    ],
)
def test_pause_in_the_wrong_place_fails(
    tmp_path: Path, stls: Path, gcode_kwargs: dict[str, Any], why: str
) -> None:
    res = _gate(tmp_path, stls, stripe_gcode(**gcode_kwargs), pause=PAUSE)
    assert _row(res, "pause").status == "FAIL", why


def test_unrequested_pause_fails(tmp_path: Path, stls: Path) -> None:
    res = _gate(tmp_path, stls, stripe_gcode(pause_at=5))
    row = _row(res, "pause: none requested")
    assert row.status == "FAIL" and "1 M600" in row.detail


def test_head_that_never_printed_fails_the_tools_gate(
    tmp_path: Path, stls: Path
) -> None:
    res = _gate(tmp_path, stls, stripe_gcode(used="1.0, 0.0, 1.0"))
    assert _row(res, "tools: heads used").status == "FAIL"


def test_missing_prime_tower_fails_with_several_heads(
    tmp_path: Path, stls: Path
) -> None:
    assert (
        _row(_gate(tmp_path, stls, stripe_gcode(tower=False)), "prime tower").status
        == "FAIL"
    )
    assert (
        _row(
            _gate(tmp_path, stls, stripe_gcode(tower=False), prime_tower=False),
            "prime tower",
        ).status
        == "PASS"
    )


def test_layer_and_settings_gates(tmp_path: Path, stls: Path) -> None:
    opt = mg.GateOptions(parts_dir=stls, layer_heights=(0.1, 0.3))
    ok = _gate(
        tmp_path, stls, stripe_gcode(), opt, expect_settings={"wall_loops": "4"}
    )
    assert (
        _row(ok, "layers: within").status == "PASS"
        and _row(ok, "settings: wall_loops").status == "PASS"
    )
    bad = _gate(
        tmp_path,
        stls,
        stripe_gcode(),
        mg.GateOptions(parts_dir=stls, layer_heights=(0.3, 0.4)),
        expect_settings={"wall_loops": "2"},
    )
    assert (
        _row(bad, "layers: within").status == "FAIL"
        and _row(bad, "settings: wall_loops").status == "FAIL"
    )


# ---------------------------------------------------------------- cavity


POCKET_CFG = {
    "shift": list(POCKET_SHIFT),
    "pause": {"print_z": 12.1, "last_layer_before": {"top": 12.0}},
}


def _pocket(tmp_path: Path, gcode: str, **opt: Any) -> mg.GateResult:
    cav = tmp_path / "cavity.json"
    cav.write_text(json.dumps(POCKET), encoding="utf-8")
    g = tmp_path / "pocket.gcode"
    g.write_text(gcode, encoding="ascii")
    cfg_p = tmp_path / "pocket-gates.json"
    cfg_p.write_text(json.dumps(POCKET_CFG), encoding="utf-8")
    return mg.run_gates(g, mg.load_config(cfg_p), mg.GateOptions(cavity=cav, **opt))


def test_pocket_stays_void_and_closes_above_the_ridge(tmp_path: Path) -> None:
    res = _pocket(tmp_path, pocket_gcode())
    assert res.ok, res.render()
    assert (
        _row(res, "cavity: void").status == "PASS"
        and _row(res, "pause").status == "PASS"
    )


def test_extrusion_inside_the_pocket_fails_the_void_gate(tmp_path: Path) -> None:
    res = _pocket(tmp_path, pocket_gcode(fill_at=40))
    assert _row(res, "cavity: void").status == "FAIL"


def test_pocket_left_open_above_the_ridge_fails(tmp_path: Path) -> None:
    res = _pocket(tmp_path, pocket_gcode(close_roof=False))
    assert _row(res, "cavity: pocket stays open").status == "FAIL"


def test_cavity_teeth_catch_a_moved_outline_and_a_misplaced_pause(
    tmp_path: Path,
) -> None:
    res = _pocket(tmp_path, pocket_gcode(), cavity_teeth=True)
    assert res.ok, res.render()
    assert [t.got for t in res.teeth] == ["FAIL", "FAIL"] and all(
        t.caught for t in res.teeth
    )


def test_filled_pocket_fails_the_report_even_though_the_moved_outline_control_trips(
    tmp_path: Path,
) -> None:
    """The moved outline is caught on any print with a wall ring; the real outline's void row is what fails the verdict."""
    res = _pocket(tmp_path, pocket_gcode(fill_at=40), cavity_teeth=True)
    assert not res.ok and _row(res, "cavity: void").status == "FAIL"


def test_cavity_teeth_need_a_cavity(tmp_path: Path, stls: Path) -> None:
    with pytest.raises(InvalidArgument, match="--cavity-teeth"):
        _gate(
            tmp_path,
            stls,
            stripe_gcode(),
            mg.GateOptions(parts_dir=stls, cavity_teeth=True),
        )


# ---------------------------------------------------------------- input errors


def test_bad_inputs_are_structured_errors(tmp_path: Path, stls: Path) -> None:
    with pytest.raises(InputNotFound):
        mg.run_gates(tmp_path / "missing.gcode", mg.parse_config({}), mg.GateOptions())
    empty = tmp_path / "empty.gcode"
    empty.write_text("G90\n", encoding="ascii")
    with pytest.raises(InvalidArgument, match="G-code"):
        mg.run_gates(empty, mg.parse_config({"heads_slots": [1]}), mg.GateOptions())
    with pytest.raises(InvalidArgument, match="assign"):
        mg.parse_config({"parts": [{"name": "x", "assign": {}}]})
    with pytest.raises(InvalidArgument, match="mix"):
        mg.parse_config({"parts": [{"name": "x", "assign": {"mix": "a1"}}]})
    with pytest.raises(InvalidArgument, match="pause"):
        mg.parse_config({"pause": {"print_z": 1.0}})


def test_unconfigured_gates_are_named_as_not_verified(tmp_path: Path, stls: Path) -> None:
    g = tmp_path / "plate.gcode"
    g.write_text(stripe_gcode(), encoding="ascii")
    res = mg.run_gates(g, mg.parse_config({"heads_slots": [1, 2, 3]}), mg.GateOptions())
    assert {"cavity", "colour volumes"} <= set(res.skipped)
    assert res.ok and "not verified, no input: cavity, colour volumes" in res.render()
    assert res.to_json()["skipped"] == res.skipped


def test_a_config_that_declares_no_check_is_refused_not_passed(tmp_path: Path) -> None:
    g = tmp_path / "plate.gcode"
    g.write_text(stripe_gcode(), encoding="ascii")
    with pytest.raises(InvalidArgument, match="no checks declared"):
        mg.run_gates(g, mg.parse_config({}), mg.GateOptions())


@pytest.mark.parametrize(
    "bad",
    [
        {"parts": {"a": 1}},
        {"parts": ["iris"]},
        {"shift": [1, "x", 3]},
        {"prime_tower": "false"},
        {"heads_slots": [True]},
        {"parts_dir": 3},
    ],
)
def test_malformed_config_entries_are_structured_errors(bad: dict[str, Any]) -> None:
    with pytest.raises(InvalidArgument):
        mg.parse_config(bad)


def test_layer_with_no_strict_margin_point_is_counted_as_not_judged(tmp_path: Path, stls: Path) -> None:
    """A layer with no unambiguous outer-wall point at the strict margin (only fringe points near a colour boundary) is not
    judged on those points; it must be counted in the row's `not judged`, not vanish without a trace."""
    g = tmp_path / "plate.gcode"
    g.write_text(stripe_gcode(wrong_tool={20: 2}), encoding="ascii")
    cfg = mg.load_config(write_config(tmp_path / "gates.json"))
    observer = mg.RegionObserver(cfg, stls, margin=mg.DEFAULT_MARGIN_MM, bed_tol=mg.DEFAULT_BED_TOL_MM)
    scan = scan_gcode(g, [observer])
    for margin in (mg.DEFAULT_MARGIN_MM, mg.LOOSE_MARGIN):
        observer.obs[20].seen[margin].pop(1)
        observer.obs[20].evid[margin].pop(1)
    report = mg.Report()
    mg.evaluate_regions(observer.obs, scan, cfg, observer, report, limits=mg.Limits())
    row = next(r for r in report.rows if r.key == "mix:stripe")
    assert row.detail.startswith("99 layers with outer wall, 0 off the pattern")
    assert "1 layers not judged" in row.detail and row.status == "PASS"


def test_arcs_extrude_and_move_the_position(tmp_path: Path) -> None:
    g = tmp_path / "arc.gcode"
    g.write_text(
        ";LAYER_CHANGE\n;Z:0.2\n;HEIGHT:0.2\nT0\n;TYPE:Outer wall\nG1 X0 Y0\nG3 X10 Y0 I5 J0 E1\nG1 X10 Y10 E1\n",
        encoding="ascii",
    )
    runs: list[list[tuple[float, float]]] = []
    scan_gcode(g, [lambda lay: runs.extend(r.xy.tolist() for r in lay.runs)])
    assert runs == [[[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]]]


def test_mix_volume_on_the_bed_layer_is_not_a_thick_stripe(tmp_path: Path, stls: Path) -> None:
    """Layer 0 height is the slicer's: a mix starting on the bed must not fail the stripe gate with the first layer's height."""
    g = stripe_gcode().replace(";HEIGHT:0.1\n", ";HEIGHT:0.2\n", 1).replace(";Z:0.100", ";Z:0.200", 1)
    assert "FAIL" not in _row(_gate(tmp_path, stls, g, mg.GateOptions(parts_dir=stls)), "colour volume stripe: no stripe").status


def test_partly_named_stl_list_is_refused(tmp_path: Path, stls: Path) -> None:
    cfg = {"parts": [{"name": "base", "stl": "1_base.stl", "assign": {"slot": 3}}, {"name": "stripe", "assign": {"mix": "12"}}]}
    with pytest.raises(InvalidArgument, match="parts"):
        _gate(tmp_path, stls, stripe_gcode(), **cfg)


def test_pause_before_the_first_layer_is_not_attributed_to_layer_zero(tmp_path: Path) -> None:
    g = tmp_path / "early.gcode"
    g.write_text("M600\n" + stripe_gcode(), encoding="ascii")
    assert scan_gcode(g).pauses[0].layer == -1


# ---------------------------------------------------------------- perceptual stripe contrast

G_K_R_W = ["#1B5E20", "#080A0D", "#E72F1D", "#FFFFFF"]  # slots 1..4: green, black, red, white


def _stripe_de(
    tmp_path: Path,
    stls: Path,
    pattern: str,
    options: mg.GateOptions | None = None,
    **gcode: Any,
) -> mg.Row:
    cfg: dict[str, Any] = {
        "parts": [
            {"name": "base", "assign": {"slot": 3}},
            {"name": "stripe", "assign": {"mix": pattern, "mix_src": pattern}},
        ],
        "heads_slots": [1, 2, 3, 4],
        "filament_colours": G_K_R_W,
    }
    cfg.update(gcode.pop("cfg", {}))
    res = _gate(tmp_path, stls, stripe_gcode(pattern=pattern, **gcode), options, **cfg)
    return _row(res, "colour volume stripe: stripe contrast")


def _worst_de(row: mg.Row) -> float:
    return float(row.detail.split("worst ")[1].split(" at")[0])


def test_gkgkwk_fails_the_stripe_contrast_gate_although_no_layer_repeats(tmp_path: Path, stls: Path) -> None:
    """The Chekhov iris: green/black/green/black/white/black passes the pattern and thickness gates, banding is the failure."""
    row = _stripe_de(tmp_path, stls, "121242")
    assert row.status == "FAIL", row.detail
    assert _worst_de(row) > 20


def test_gw_passes_the_stripe_contrast_gate(tmp_path: Path, stls: Path) -> None:
    row = _stripe_de(tmp_path, stls, "14")
    assert row.status == "PASS", row.detail
    assert _worst_de(row) < 1


def test_between_the_two_limits_is_a_warning_that_does_not_fail_the_gate(tmp_path: Path, stls: Path) -> None:
    opt = mg.GateOptions(parts_dir=stls, limits=mg.Limits(max_stripe_de=100.0, warn_stripe_de=1.0))
    row = _stripe_de(tmp_path, stls, "124", opt)
    assert row.status == "WARN" and row.ok


def test_max_stripe_de_option_moves_the_verdict(tmp_path: Path, stls: Path) -> None:
    opt = mg.GateOptions(parts_dir=stls, limits=mg.Limits(max_stripe_de=40.0, warn_stripe_de=30.0))
    assert _stripe_de(tmp_path, stls, "121242", opt).status == "PASS"


def test_far_viewing_distance_hides_the_bands(tmp_path: Path, stls: Path) -> None:
    opt = mg.GateOptions(parts_dir=stls, limits=mg.Limits(view_cm=100.0))
    assert _stripe_de(tmp_path, stls, "121242", opt).status == "PASS"


def test_colours_come_from_the_gcode_footer_when_the_config_has_none(tmp_path: Path, stls: Path) -> None:
    row = _stripe_de(tmp_path, stls, "121242", colours=";".join(G_K_R_W), cfg={"filament_colours": None})
    assert row.status == "FAIL"


def test_without_any_filament_colour_the_stripe_check_is_a_named_skip(tmp_path: Path, stls: Path) -> None:
    row = _stripe_de(tmp_path, stls, "121242", cfg={"filament_colours": None})
    assert row.status == "SKIP" and "filament colour" in row.detail


def test_a_tool_without_a_colour_is_a_skip_not_a_pass(tmp_path: Path, stls: Path) -> None:
    row = _stripe_de(tmp_path, stls, "121242", cfg={"filament_colours": G_K_R_W[:3]})
    assert row.status == "SKIP"


def test_bad_filament_colours_are_a_structured_error() -> None:
    with pytest.raises(InvalidArgument):
        mg.parse_config({"filament_colours": ["green"]})


def test_warn_limit_above_the_fail_limit_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert mg.main(["x.gcode", "--config", "x.json", "--max-stripe-de", "3", "--warn-stripe-de", "5"]) == 2
    assert "--warn-stripe-de" in capsys.readouterr().err


def test_an_unreadable_gcode_colour_keeps_its_slot_so_later_tools_do_not_shift(tmp_path: Path, stls: Path) -> None:
    """`#1B5E20;bogus;#E72F1D;#FFFFFF`: T1 has no colour, T3 stays white. The pattern uses T1, so the check is a SKIP,
    not a verdict computed on the wrong colours."""
    row = _stripe_de(tmp_path, stls, "121242", colours="#1B5E20;bogus;#E72F1D;#FFFFFF", cfg={"filament_colours": None})
    assert row.status == "SKIP"
