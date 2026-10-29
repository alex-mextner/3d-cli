"""A simulated tester readout for the fit/record tests (#417): true wall colours differ from the
nominal (Spoolman) ones, the pure swatches read a little off the walls, the mixes read as the
model says from the TRUE colours. Built on a real tester manifest (tests/mix_tester_fixtures.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mix_tester_fixtures import build_tester

from printops.color import hex_delta_e, hex_from_linear, linear_rgb

# slot -> the colour the filament really shows on a wall; nominal: black, green, white (LOADED)
TRUTH = {1: "#1A1A1C", 3: "#2E7D32", 4: "#E8E4D8"}
# what the flat pure swatch reads: off the wall colour by a gloss/translucency shift
PURE = {1: "#1E1E21", 3: "#2B7A30", 4: "#EFEBE1"}


def mean_hex(layers: list[str]) -> str:
    n = len(layers)
    lin = [linear_rgb(h) for h in layers]
    r, g, b = (sum(c[i] for c in lin) / n for i in range(3))
    return hex_from_linear((r, g, b))


def simulated_readout(
    tmp_path: Path,
    *,
    truth: dict[int, str] = TRUTH,
    pure: dict[int, str] = PURE,
    target: str = "#808080",
    count: int = 4,
    sha: str = "a" * 64,
    filament_ids: tuple[int, int, int] = (21, 23, 24),
) -> dict[str, Any]:
    """A `mix-readout/1` record for a real tester of 3 filaments (`filament_ids` on heads 1, 3, 4)."""
    m = build_tester(tmp_path, target=target, count=count, filament_ids=filament_ids)
    rows: list[dict[str, Any]] = []
    for c in m["candidates"]:
        measured = mean_hex([truth[int(d)] for d in c["pattern"]])
        rows.append(
            {
                "number": c["number"],
                "kind": "candidate",
                "pattern": c["pattern"],
                "slot": None,
                "predicted_hex": c["hex"],
                "measured_hex": measured,
                "delta_e_predicted": round(hex_delta_e(measured, c["hex"]), 2),
                "delta_e_target": round(hex_delta_e(measured, m["target"]), 2),
                "calibration": c["calibration"],
            }
        )
    rows.extend(
        {
            "number": r["number"],
            "kind": "reference",
            "pattern": None,
            "slot": r["slot"],
            "predicted_hex": r["hex"],
            "measured_hex": pure[r["slot"]],
            "delta_e_predicted": round(hex_delta_e(pure[r["slot"]], r["hex"]), 2),
            "delta_e_target": round(hex_delta_e(pure[r["slot"]], m["target"]), 2),
            "calibration": r["calibration"],
        }
        for r in m["references"]
    )
    return {
        "schema": "mix-readout/1",
        "manifest": {
            "id": m["id"],
            "schema": m["schema"],
            "file": "tester.mix.json",
            "target": m["target"],
        },
        "photo": {"file": "phone.jpg", "sha256": sha, "size": [100, 100]},
        "swatches": rows,
        "summary": {},
        "warnings": [],
    }
