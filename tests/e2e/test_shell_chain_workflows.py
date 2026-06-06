from __future__ import annotations

import json
from pathlib import Path

from .workflow_helper import require_binary, run_shell, write_pgm


def test_shell_chain_scores_masks_then_builds_a_markdown_report(tmp_path: Path) -> None:
    """A user turns CLI score output and generated overlay artifacts into a report."""
    require_binary("magick")
    write_pgm(tmp_path / "candidate.pgm", ["00000", "01110", "01110", "00000"])
    write_pgm(tmp_path / "reference.pgm", ["00000", "00111", "00111", "00000"])

    result = run_shell(
        "\n".join(
            [
                "set -eu",
                '"$PYTHON" "$THREED" score candidate.pgm reference.pgm --masks -o score > score.env',
                "\"$PYTHON\" -c 'import pathlib; "
                "pairs=dict(line.split(\"=\", 1) for line in pathlib.Path(\"score.env\").read_text().splitlines()); "
                "print(\"# Match Report\"); "
                "print(\"IoU: \" + pairs[\"IoU\"]); "
                "print(\"AE: \" + pairs[\"AE\"]); "
                "print(\"Overlay: \" + pairs[\"OVERLAY\"])' "
                "> report.md",
            ]
        ),
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "score" / "overlay.png").exists()
    assert (tmp_path / "report.md").read_text(encoding="utf-8").splitlines() == [
        "# Match Report",
        "IoU: 0.5000",
        "AE: 4",
        "Overlay: score/overlay.png",
    ]


def test_shell_chain_combines_params_pack_and_procurement_json(tmp_path: Path) -> None:
    """A user derives layout and purchasing context from CLI JSON artifacts."""
    result = run_shell(
        "\n".join(
            [
                "set -eu",
                '"$PYTHON" "$THREED" params "$CUBE" --json > params.json',
                "\"$PYTHON\" -c 'import json, pathlib; "
                "rows={row[\"name\"]: row[\"value\"] for row in "
                "json.loads(pathlib.Path(\"params.json\").read_text())}; "
                "print(\"cube=\" + rows[\"width\"] + \"x\" + rows[\"depth\"] + \":4\")' "
                "> parts.txt",
                '"$PYTHON" "$THREED" pack --bed 100x80 --gap 5 --part "$(cat parts.txt)" --json > pack.json',
                "cat > bom.json <<'JSON'\n"
                '{"items":[{"sku":"m3-bolt","description":"M3 bolt","quantity":24,'
                '"unit":"each","supplier":"BoltCo","package_qty":50}]}\nJSON',
                "cat > inventory.json <<'JSON'\n"
                '{"items":{"m3-bolt":6}}\nJSON',
                '"$PYTHON" "$THREED" procurement plan --bom bom.json --inventory inventory.json --format json > plan.json',
                "\"$PYTHON\" -c 'import json, pathlib; "
                "pack=json.loads(pathlib.Path(\"pack.json\").read_text()); "
                "plan=json.loads(pathlib.Path(\"plan.json\").read_text()); "
                "print(str(len(pack[\"placements\"])) + \" placements\"); "
                "print(plan[\"items\"][0][\"sku\"] + \" shortage=\" + str(plan[\"items\"][0][\"short_qty\"]))' "
                "> summary.txt",
            ]
        ),
        tmp_path,
    )

    assert result.returncode == 0, result.stderr
    pack = json.loads((tmp_path / "pack.json").read_text(encoding="utf-8"))
    assert len(pack["placements"]) == 4
    assert pack["placements"][0]["name"] == "cube"
    assert (tmp_path / "summary.txt").read_text(encoding="utf-8").splitlines() == [
        "4 placements",
        "m3-bolt shortage=18.0",
    ]
