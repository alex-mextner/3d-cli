# `3d report` - deterministic artifact summaries

Composes existing gate logs, score output, metrics JSONL, and previous report JSON into a
stable text or JSON summary. It only reads files; it does not run OpenSCAD, render, mesh
checks, collision checks, or scoring.

## Usage

```bash
3d report <artifact.log|metrics.jsonl|report.json> [...] [options]
```

## Options

| Option | What |
|---|---|
| `--format text\|json` | Output format; default is `text`. |
| `--json` | Shortcut for `--format json`. |
| `--title TITLE` | Report title; default is `3d report`. |
| `-o, --out FILE` | Write the summary to a file instead of stdout. |

## Examples

```bash
3d report check.log score.log
3d report --json metrics.jsonl -o report.json
3d check model.scad > check.log
3d score render-mask.png ref-mask.png > score.log
3d report --title "Reference pass" check.log score.log
```

Text artifacts can contain gate rows such as `MANIFOLD PASS ...`, marker lines such as
`>>> CHECK: FAIL`, and key/value metrics such as `IoU=0.875`. JSON and JSONL artifacts can
contain `gates`, `metrics`, `values`, or single `status`/`gate` records.
