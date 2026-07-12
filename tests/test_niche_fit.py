"""Unit tests for the cavity-insert clearance math and `.scad` emission (`3d fit-niche`)."""
from __future__ import annotations

import json
import math

import pytest

from commands import fit_niche as cmd
from errors import InvalidArgument, UsageError
from niche_fit import (
    DEFAULT_CLEARANCE,
    FIT_CLEARANCES,
    emit_scad,
    make_spec,
    resolve_clearance,
    spec_from_json,
)


# --------------------------------------------------------------------------- clearance
def test_fit_presets_map_to_documented_clearances() -> None:
    assert FIT_CLEARANCES == {"snug": 0.10, "normal": 0.20, "loose": 0.35}
    assert DEFAULT_CLEARANCE == 0.20


def test_resolve_clearance_defaults_to_normal_preset() -> None:
    assert resolve_clearance(None, None) == (0.20, "normal")


def test_resolve_clearance_explicit_override_wins_and_labels_custom() -> None:
    assert resolve_clearance(None, 0.15) == (0.15, "custom")
    # a named fit alongside an explicit clearance keeps the fit label
    assert resolve_clearance("loose", 0.5) == (0.5, "loose")


def test_resolve_clearance_rejects_unknown_fit() -> None:
    with pytest.raises(InvalidArgument):
        resolve_clearance("tight", None)


def test_resolve_clearance_rejects_unknown_fit_even_with_explicit_clearance() -> None:
    # An explicit --clearance must NOT let an invalid --fit slip past the closed enum.
    with pytest.raises(InvalidArgument):
        resolve_clearance("bogus", 0.2)


def test_resolve_clearance_rejects_negative_override() -> None:
    with pytest.raises(InvalidArgument):
        resolve_clearance(None, -0.1)


# --------------------------------------------------------------------------- insert dims
def test_rect_insert_shrinks_by_two_clearances_per_axis() -> None:
    spec = make_spec(shape="rect", width=20, depth=16, height=12, clearance=0.2)
    assert math.isclose(spec.insert_width, 19.6)
    assert math.isclose(spec.insert_depth, 15.6)
    # height is a full seat -> no Z clearance
    assert spec.height == 12


def test_round_insert_shrinks_diameter_by_two_radial_clearances() -> None:
    spec = make_spec(shape="round", diameter=20, height=14, clearance=0.25)
    assert math.isclose(spec.insert_diameter, 19.5)


def test_summary_reports_convention_and_derived_insert() -> None:
    spec = make_spec(shape="rect", width=20, depth=16, height=12, fit="snug")
    summary = spec.summary()
    assert summary["clearance"] == 0.10
    assert summary["clearance_convention"] == "per mating face"
    assert summary["insert"] == {"width": 19.8, "depth": 15.8, "height": 12}

    round_summary = make_spec(shape="round", diameter=20, height=10).summary()
    assert round_summary["clearance_convention"] == "radial (uniform gap)"


# --------------------------------------------------------------------------- validation
def test_positive_dimensions_are_required() -> None:
    with pytest.raises(InvalidArgument):
        make_spec(shape="rect", width=0, depth=16, height=12)
    with pytest.raises(InvalidArgument):
        make_spec(shape="round", diameter=20, height=0)


def test_unknown_shape_is_rejected() -> None:
    with pytest.raises(InvalidArgument):
        make_spec(shape="hex", width=10, depth=10, height=10)


def test_clearance_larger_than_cavity_is_a_usage_error() -> None:
    with pytest.raises(UsageError):
        make_spec(shape="rect", width=1.0, depth=1.0, height=10, clearance=0.8)


def test_groove_on_a_tiny_cavity_is_rejected_before_emitting_negative_scad() -> None:
    # insert width/depth would be ~1.0 mm, below the 2*groove_depth (1.6 mm) floor.
    with pytest.raises(UsageError):
        make_spec(shape="rect", width=1.4, depth=1.4, height=10, clearance=0.2, groove=True)
    # The same cavity WITHOUT a groove is fine.
    assert make_spec(shape="rect", width=1.4, depth=1.4, height=10, clearance=0.2).groove is False


def test_lead_in_on_a_small_cavity_still_emits_valid_scad() -> None:
    # lead-in clamps its own geometry, so it must survive a small (but positive) insert.
    scad = emit_scad(make_spec(shape="round", diameter=3, height=6, lead_in=True))
    assert "cylinder(h = lead, d1 = dia, d2 = max(dia - 2 * lead, 0.2));" in scad


# --------------------------------------------------------------------------- scad emission
def test_emit_scad_carries_parametric_header_and_derived_dims() -> None:
    scad = emit_scad(make_spec(shape="rect", width=20, depth=16, height=12, clearance=0.2))
    assert "cavity_width = 20;" in scad
    assert "cavity_depth = 16;" in scad
    assert "clearance = 0.2;" in scad
    # derived dims are expressed parametrically, not baked to numbers
    assert "insert_width = cavity_width - 2 * clearance;" in scad
    assert "module insert()" in scad
    assert "module cavity_block()" in scad
    assert "show_cavity = false;" in scad


def test_emit_scad_toggles_features_from_the_spec() -> None:
    plain = emit_scad(make_spec(shape="rect", width=20, depth=16, height=12))
    assert "lead_in = false;" in plain
    assert "groove = false;" in plain
    assert "snap_tab = false;" in plain

    loaded = emit_scad(
        make_spec(shape="rect", width=20, depth=16, height=12,
                  lead_in=True, groove=True, snap_tab=True)
    )
    assert "lead_in = true;" in loaded
    assert "groove = true;" in loaded
    assert "snap_tab = true;" in loaded


def test_round_emit_uses_diameter_and_shape_constant() -> None:
    scad = emit_scad(make_spec(shape="round", diameter=20, height=14))
    assert 'shape = "round";' in scad
    assert "cavity_diameter = 20;" in scad


def test_emit_fills_inactive_shape_dimension_so_runtime_flip_stays_valid() -> None:
    # A rect spec must still emit a POSITIVE cavity_diameter, so `-D shape=round` at
    # OpenSCAD runtime yields valid (not negative) geometry.
    rect = emit_scad(make_spec(shape="rect", width=20, depth=16, height=12))
    assert "cavity_diameter = 16;" in rect  # min(width, depth)
    # A round spec fills width/depth from the diameter for the reverse flip.
    rnd = emit_scad(make_spec(shape="round", diameter=18, height=12))
    assert "cavity_width = 18;" in rnd
    assert "cavity_depth = 18;" in rnd


def test_groove_z_is_a_runtime_expression_not_a_baked_literal() -> None:
    # groove_z must follow a -D cavity_height override, so it is emitted as an expression.
    scad = emit_scad(make_spec(shape="rect", width=20, depth=16, height=12))
    assert "groove_z = max(groove_width / 2 + 0.5," in scad


# --------------------------------------------------------------------------- spec_from_json
def test_spec_from_json_round_trips_flag_field_names() -> None:
    spec = spec_from_json(
        {"shape": "rect", "width": 24, "depth": 18, "height": 12, "fit": "loose", "groove": True}
    )
    assert spec.fit == "loose"
    assert spec.clearance == 0.35
    assert spec.groove is True
    assert math.isclose(spec.insert_width, 24 - 2 * 0.35)


def test_spec_from_json_rejects_unknown_fields() -> None:
    with pytest.raises(UsageError):
        spec_from_json({"shape": "rect", "width": 20, "depth": 16, "height": 12, "bogus": 1})


# --------------------------------------------------------------------------- command wiring
def test_command_no_args_prints_usage_and_returns_one(capsys: pytest.CaptureFixture[str]) -> None:
    assert cmd.run([]) == 1
    assert "3d fit-niche" in capsys.readouterr().out


def test_command_help_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert cmd.run(["--help"]) == 0
    assert "fit-niche" in capsys.readouterr().out


def test_command_writes_scad_and_prints_summary(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    out = tmp_path / "insert.scad"
    rc = cmd.run(["--width", "20", "--depth", "16", "--height", "12", "-o", str(out)])
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert "module insert()" in text
    stdout = capsys.readouterr().out
    assert "insert: width=19.6mm" in stdout


def test_command_json_reports_resolved_spec(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    out = tmp_path / "plug.scad"
    rc = cmd.run(
        ["--shape", "round", "--diameter", "20", "--height", "14", "--json", "-o", str(out)]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["output"] == str(out)
    assert payload["insert"]["diameter"] == 19.6
    assert payload["clearance_convention"] == "radial (uniform gap)"


def test_command_spec_file_flags_override_json(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    spec_file = tmp_path / "cavity.json"
    spec_file.write_text(
        json.dumps({"shape": "rect", "width": 20, "depth": 16, "height": 12, "fit": "snug"}),
        encoding="utf-8",
    )
    out = tmp_path / "insert.scad"
    rc = cmd.run(["--spec", str(spec_file), "--fit", "loose", "--json", "-o", str(out)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["fit"] == "loose"  # flag overrode the file's "snug"
    assert payload["clearance"] == 0.35


def test_command_unknown_option_raises_usage_error() -> None:
    with pytest.raises(UsageError):
        cmd.run(["--width", "20", "--bogus"])


def test_command_missing_spec_file_raises() -> None:
    from errors import InputNotFound

    with pytest.raises(InputNotFound):
        cmd.run(["--spec", "/no/such/spec.json"])


def test_command_missing_required_dimension_reports_missing_not_zero() -> None:
    with pytest.raises(UsageError, match="missing required --height"):
        cmd.run(["--width", "20", "--depth", "16"])
    with pytest.raises(UsageError, match="missing required --diameter"):
        cmd.run(["--shape", "round", "--height", "10"])
