"""report.py — tables from the runs of experiment.py (pulled with remote.py pull).

    report.py RUN_DIR [--dataset DATASET]

RUN_DIR holds OUT/experiment.json (+ oof_scores.json) and one sub-directory per `--tag` run. Prints,
for every run, the per-method/class/domain metrics; across the runs that differ only by the
fine-tune seed, the spread of AUROC and TPR@1%FPR; and (with --dataset) the score of every frame of
the own `part_detached` event under each method next to the distribution of own healthy frames,
which is the only look at the original incident that is available.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def runs(root: Path) -> dict[str, dict[str, Any]]:
    found = {}
    if (root / "experiment.json").is_file():
        found["base"] = json.loads((root / "experiment.json").read_text())
    for sub in sorted(
        p for p in root.iterdir() if p.is_dir() and (p / "experiment.json").is_file()
    ):
        found[sub.name] = json.loads((sub / "experiment.json").read_text())
    return found


def table(result: dict[str, Any]) -> list[str]:
    head = "| method | class | frames | n+/n- | groups+ | AUROC (95% CI) | TPR@1%FPR | TPR@5%FPR |"
    lines = [head, "|---|---|---|---:|---:|---|---:|---:|"]
    for key, m in result["metrics"].items():
        method, cls, domain = key.split("/")
        ci = m["auroc_ci95_group_bootstrap"]
        ci_s = "n/a" if ci is None else f"{ci[0]:.2f}-{ci[1]:.2f}"
        lines.append(
            f"| {method} | {cls} | {domain} | {m['n_pos']}/{m['n_neg']} | {m['groups_pos']} | "
            f"{m['auroc']:.3f} ({ci_s}) | {m['tpr_at_1pct_fpr']:.3f} | {m['tpr_at_5pct_fpr']:.3f} |"
        )
    return lines


def seed_spread(all_runs: dict[str, dict[str, Any]]) -> list[str]:
    """Runs that differ only by --seed (same blocks/lr/setup-clusters): AUROC and TPR@1% per seed."""
    group = {
        name: r
        for name, r in all_runs.items()
        if r["args"]["blocks"] == 2
        and r["args"]["lr"] == 1e-5
        and not r["args"].get("setup_clusters")
    }
    if len(group) < 2:
        return []
    lines = [
        "",
        "Fine-tune seed spread (same data, blocks 2, lr 1e-5):",
        "",
        "| key | "
        + " | ".join(f"seed {r['args'].get('seed', 0)}" for r in group.values())
        + " | min-max |",
        "|---|" + "---|" * (len(group) + 1),
    ]
    keys = [
        k
        for k in next(iter(group.values()))["metrics"]
        if k.startswith("finetune/") and k.endswith("/all")
    ]
    for key in keys:
        vals = [r["metrics"].get(key) for r in group.values()]
        if any(v is None for v in vals):
            continue
        a = [v["auroc"] for v in vals]
        t = [v["tpr_at_1pct_fpr"] for v in vals]
        lines.append(
            f"| {key} AUROC / TPR@1% | "
            + " | ".join(f"{x:.3f} / {y:.3f}" for x, y in zip(a, t, strict=True))
            + f" | {min(a):.3f}-{max(a):.3f} / {min(t):.3f}-{max(t):.3f} |"
        )
    return lines


def matched_tables(root: Path, dataset: Path) -> list[str]:
    """Metrics on the `matched` subset (healthy frames only from the sources that have positives),
    recomputed from each run's out-of-fold scores: runs made before that subset existed get it too."""
    import numpy as np
    import experiment

    rows = []
    for line in (dataset / "labels.jsonl").read_text().splitlines():
        r = json.loads(line)
        r["domain"] = "own" if r["provenance"].startswith("own") else "third_party"
        rows.append(r)
    by_id = {r["id"]: r for r in rows}
    lines = ["", "Source-matched negatives (healthy frames of the same sources as the positives only):", "",
             "| run | method | class | n+/n- | AUROC | TPR@1%FPR | TPR@5%FPR |", "|---|---|---|---:|---:|---:|---:|"]
    for sub in [root, *sorted(p for p in root.iterdir() if p.is_dir() and (p / "oof_scores.json").is_file())]:
        if not (sub / "oof_scores.json").is_file():
            continue
        oof = json.loads((sub / "oof_scores.json").read_text())
        sel = [by_id[i] for i in oof["ids"]]
        for method, per in oof["scores"].items():
            for cls in ("spaghetti", "part_detached"):
                if cls not in per:
                    continue
                s = np.array([np.nan if v is None else v for v in per[cls]])
                m = experiment.class_metrics(s, sel, oof["folds"], cls, "matched", boot=0)
                if m:
                    name = sub.name if sub != root else "base"
                    lines.append(f"| {name} | {method} | {cls} | {m['n_pos']}/{m['n_neg']} | {m['auroc']:.3f} | {m['tpr_at_1pct_fpr']:.3f} | {m['tpr_at_5pct_fpr']:.3f} |")
    return lines


def event_frames(root: Path, dataset: Path) -> list[str]:
    import numpy as np

    oof = json.loads((root / "oof_scores.json").read_text())
    rows = {}
    for line in (dataset / "labels.jsonl").read_text().splitlines():
        r = json.loads(line)
        rows[r["id"]] = r
    ids = oof["ids"]
    event = [
        i
        for i, fid in enumerate(ids)
        if rows[fid]["label"] == "part_detached"
        and rows[fid]["provenance"].startswith("own")
    ]
    healthy = [
        i
        for i, fid in enumerate(ids)
        if rows[fid]["label"] == "healthy" and rows[fid]["provenance"].startswith("own")
    ]
    lines = [
        "",
        f"Own part_detached event ({len(event)} frames) vs own healthy frames ({len(healthy)}), `part_detached` score:",
        "",
        "| method | healthy median / p99 | event frames: score (percentile among own healthy) |",
        "|---|---|---|",
    ]
    for method, per in oof["scores"].items():
        if "part_detached" not in per:
            continue
        s = np.array([np.nan if v is None else v for v in per["part_detached"]])
        h = s[healthy]
        h = h[~np.isnan(h)]
        if not len(h):
            continue
        cells = []
        for i in event:
            pct = (
                100.0 * float((h < s[i]).mean()) if not np.isnan(s[i]) else float("nan")
            )
            name = rows[ids[i]]["source"].replace("u1-panel-", "")
            cells.append(f"{name} {s[i]:.2f} ({pct:.0f})")
        lines.append(
            f"| {method} | {np.median(h):.3f} / {np.quantile(h, 0.99):.3f} | "
            + "; ".join(cells)
            + " |"
        )
    return lines


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--dataset", type=Path)
    args = ap.parse_args(argv)
    all_runs = runs(args.run_dir)
    for name, r in all_runs.items():
        a = r["args"]
        print(
            f"\n### run `{name}`: blocks {a['blocks']}, lr {a['lr']}, epochs {a['epochs']}, seed {a.get('seed', 0)}, setup clusters {a.get('setup_clusters', 0)}, "
            f"{r['gpu']}; {r['summary']['folds']} folds; frames {r['summary']['frames']}; groups {r['summary']['groups']}"
        )
        print(
            f"timings {r['timings_s']}; VRAM peak (torch allocator) {r['vram_mib']['peak_all_phases']}, cap {r['vram_mib']['cap']} MiB\n"
        )
        print("\n".join(table(r)))
    print("\n".join(seed_spread(all_runs)))
    if args.dataset:
        print("\n".join(matched_tables(args.run_dir, args.dataset)))
        print("\n".join(event_frames(args.run_dir, args.dataset)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
