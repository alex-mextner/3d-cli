"""Unit tests for the parametric blockout generator (lib/ai/blockout.py)."""
from __future__ import annotations

from pathlib import Path

from ai.blockout import (
    CONTINUOUS_TUNABLES,
    BlockoutParams,
    default_params,
    params_from_dict,
    render_scad,
    with_values,
    write_scad,
)
from match_loop import derive_tunables


def test_render_scad_emits_top_level_numeric_constants() -> None:
    scad = render_scad(BlockoutParams(n_columns=5, span=72.0, column_radius=2.8))
    assert "n_columns = 5;" in scad
    assert "span = 72;" in scad
    assert "column_radius = 2.8;" in scad
    assert "module temple()" in scad
    assert "temple();" in scad


def test_generated_constants_are_match_loop_tunable(tmp_path: Path) -> None:
    """The whole point: match_loop can tune the generated blockout into existence."""
    scad = tmp_path / "temple.scad"
    write_scad(default_params(), str(scad))
    tunables = derive_tunables(str(scad), None)
    for name in (*CONTINUOUS_TUNABLES, "n_columns"):
        assert name in tunables, f"{name} not seen by match_loop.derive_tunables"


def test_params_from_dict_coerces_n_columns_to_positive_int() -> None:
    params = params_from_dict({"n_columns": 4.7, "span": 55.0})
    assert params.n_columns == 5
    assert isinstance(params.n_columns, int)
    assert params.span == 55.0


def test_with_values_keeps_n_columns_integral_and_copies() -> None:
    base = BlockoutParams(n_columns=6)
    changed = with_values(base, span=80.0, n_columns=3.2)
    assert changed.span == 80.0
    assert changed.n_columns == 3
    assert base.span == 60.0  # original untouched (frozen dataclass copy)


def test_dome_is_disabled_by_default_but_emitted_when_positive() -> None:
    assert "sphere" not in render_scad(BlockoutParams(dome_radius=0.0)).split("if (dome")[0]
    assert "sphere(r = dome_radius)" in render_scad(BlockoutParams(dome_radius=12.0))


def test_continuous_tunables_are_all_real_fields() -> None:
    fields = default_params().to_dict()
    for name in CONTINUOUS_TUNABLES:
        assert name in fields
    assert "n_columns" not in CONTINUOUS_TUNABLES  # the veto owns the discrete count
