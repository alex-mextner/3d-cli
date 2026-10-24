"""E2E: a user ranks recipes through bin/3d and a visibly striped 12-layer cycle loses (#116)."""

from __future__ import annotations

import json
from pathlib import Path

from .workflow_helper import run_cli

ARGS = (
    "mix", "recipes", "--target", "#B0B0B0", "--slots", "1=#000000,4=#FFFFFF",
    "--max-cycle", "12", "--max-run-mm", "0.72", "--count", "20",
)  # fmt: skip


def test_a_high_contrast_12_layer_cycle_with_lower_de_ranks_below_the_short_cycle(
    tmp_path: Path,
) -> None:
    """The 12-layer cycle 141411414141 is ΔE 2+ closer to #B0B0B0 than the 2-layer `14`, but its
    stripes at 0.12 mm layers are visible at 30 cm, so the short cycle is the best recipe."""
    result = run_cli(tmp_path, *ARGS, "--json")
    assert result.returncode == 0, result.stderr
    cands = json.loads(result.stdout)["candidates"]
    best = cands[0]
    long_cycle = next(c for c in cands if sum(c["weights"].values()) == 12)
    assert best["role"] == "best" and sum(best["weights"].values()) == 2
    assert long_cycle["delta_e"] <= best["delta_e"] - 2
    assert long_cycle["stripe_delta_e_30cm"] > best["stripe_delta_e_30cm"] + 10
    assert long_cycle["score"] > best["score"]
    assert cands.index(long_cycle) > 0


def test_json_equals_the_text_table_for_every_candidate(tmp_path: Path) -> None:
    """A user ranks 20 recipes once as a text table and once as JSON; both list the same rows."""
    text = run_cli(tmp_path, *ARGS)
    as_json = run_cli(tmp_path, *ARGS, "--json")
    assert text.returncode == 0 and as_json.returncode == 0
    cands = json.loads(as_json.stdout)["candidates"]
    rows = [ln for ln in text.stdout.splitlines() if ln.startswith("  ")]
    assert len(rows) == len(cands) == 20
    for row, c in zip(rows, cands, strict=True):
        stripes = c["stripe_delta_e_30cm"]
        expected = (
            f"  {c['number']:>2} {c['role']:<8} {c['pattern']:<8} {c['hex']}  ΔE {c['delta_e']:.2f}"
            + ("" if stripes is None else f"  stripes {stripes:.2f} @30cm")
        )
        assert row == expected
