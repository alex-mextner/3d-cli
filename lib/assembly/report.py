"""report.py — the human-readable text of every `3d assembly` report (the `--json` document is the source of truth).

WHAT: pure functions from a report dict (`assembly.check.run_check`, `assembly.sweep.sweep_mate` / `plan_order`,
  `assembly.ergonomics.run_ergonomics`, the position plan) to the lines printed without `--json`. No geometry, no I/O.
"""

from __future__ import annotations

from typing import Any

MARK = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}


def _xyz(c: list[float]) -> str:
    return f"({c[0]:.2f}, {c[1]:.2f}, {c[2]:.2f})"


def legend_lines(rows: list[dict[str, Any]]) -> list[str]:
    out = ["parts (colour legend):"]
    for r in rows:
        src = ""
        if r.get("reference_source"):
            rs = r["reference_source"]
            src = f"  source {rs['url']} ({rs['license']})"
        ghost = ", ghost" if r.get("style") == "ghost" else ""
        out.append(f"  {r['color']}  {r['name']:<16} {r['role']}{ghost}{src}")
    return out


def check_lines(rep: dict[str, Any]) -> list[str]:
    ctx = rep["context"]
    fallback = (
        " (fit table default, not calibrated)" if ctx.get("fits_fallback") else ""
    )
    lines = [
        f"assembly check: {rep['assembly']['name']}  "
        f"printer {ctx.get('printer') or '-'}, material {ctx.get('material') or '-'}{fallback}",
        *legend_lines(rep["legend"]),
    ]
    s = rep["summary"]
    lines.append(
        f"pairs checked {s['pairs_checked']}, overlapping {s['pairs_overlapping']}: "
        f"{s['blocking']} blocking ({s['blocking_volume_mm3']} mm3), {s['allowed']} allowed "
        f"({s['allowed_volume_mm3']} mm3), {s['contacts']} contact(s)"
    )
    if rep["blocking"]:
        lines.append("BLOCKING intersections:")
        for i, r in enumerate(rep["blocking"], 1):
            lines.append(
                f"  [{i}] {r['a']} x {r['b']}: depth {r['depth_mm']:.3f} mm, {r['volume_mm3']:.3f} mm3 at "
                f"{_xyz(r['center'])}  box {_xyz(r['bbox']['min'])}..{_xyz(r['bbox']['max'])}"
            )
            for part, face in r.get("faces", {}).items():
                lines.append(
                    f"      {part}: face normal {face['normal']}, {face['area_mm2']} mm2 of surface"
                )
            lines.append(f"      why: {r['reason']}")
            lines.append(f"      fix: {r['suggestion']}")
            if r.get("image"):
                lines.append(f"      image: {r['image']}")
    if rep["allowed"]:
        lines.append("ALLOWED (designed) interference:")
        for r in rep["allowed"]:
            lines.append(
                f"  {r['a']} x {r['b']} [{r['mate']}]: depth {r['depth_mm']:.3f} mm (limit {r['allowed_depth_mm']:.3f}), "
                f"{r['volume_mm3']:.3f} mm3 at {_xyz(r['center'])}"
            )
            if r.get("material_note"):
                lines.append(f"      note: {r['material_note']}")
            if r.get("image"):
                lines.append(f"      image: {r['image']}")
    if rep["mates"]:
        lines.append("MATES:")
        for m in rep["mates"]:
            fit, cl, v = m["fit"], m["clearance"], m["verdict"]
            fb = " [default row]" if fit["fallback"] else ""
            gap = (
                "-"
                if cl["measured_min_gap_mm"] is None
                else f"{cl['measured_min_gap_mm']:.3f}"
            )
            lines.append(
                f"  {MARK[v['status']]}  {m['name']} ({m['a']} - {m['b']}, {m['kind']}, fit {fit['fit_type']} "
                f"{fit['clearance_mm']:+.2f} +/- {fit['tol_mm']:.2f} mm, {fit['source']}{fb}): min gap {gap} mm "
                f"(need >= {cl['required_min_mm']:.3f}); {'; '.join(v['reasons'])}"
            )
    if rep.get("drift"):
        lines.append("3MF DRIFT (file no longer matches its recorded layout):")
        lines.extend(f"  {d['part']}: {d['reason']}" for d in rep["drift"])
    lines.append(
        f">>> ASSEMBLY CHECK: {'PASS' if rep['ok'] else 'FAIL'}  "
        f"({rep['elapsed_s']} s, peak {rep.get('resources', {}).get('peak_rss_mb', '?')} MB)"
    )
    return lines


def sweep_lines(rep: dict[str, Any]) -> list[str]:
    m = rep["motion"]
    lines = [
        f"assembly sweep: mate {rep['mate']} ({rep['a']} - {rep['b']}), {rep['direction']} {rep['mover']}"
        + (f" (+ {', '.join(rep['carried'])})" if rep["carried"] else "")
        + f" along {m['axis_label']} over {m['extent']:g} {m['unit']}, step {rep['step']:g}, {rep['samples']} samples",
        f"  obstacles: {', '.join(rep['obstacles']) or '-'}",
    ]
    fc, fb = rep["first_contact"], rep["first_blocking"]
    if fc is None:
        lines.append("  first contact: none: the path is free")
    else:
        lines.append(
            f"  first contact: {fc['travelled']:g} {fc['unit']} along the path ({fc['pair'][0]} x {fc['pair'][1]}, "
            f"{fc['classification']}, depth {fc['depth_mm']:.3f} mm at {_xyz(fc['center'])})"
        )
    if fb is None:
        lines.append("  first BLOCKING: none")
    else:
        lines.append(
            f"  first BLOCKING: {fb['travelled']:g} {fb['unit']} along the path: {fb['reason']} at {_xyz(fb['center'])}"
        )
    if rep.get("snap"):
        sv = rep["snap"]
        lines.append(f"  snap hooks: {MARK[sv['verdict']['status']]}")
        for h in sv["hooks"]:
            lim = (
                "unknown"
                if h["flex_limit_mm"] is None
                else f"{h['flex_limit_mm']:.3f} mm"
            )
            lines.append(
                f"    {h['hook']}: max interference {h['max_interference_mm']:.3f} mm, implied tip deflection "
                f"{h['implied_deflection_mm']:.3f} mm, flex limit {lim} ({h['flex_limit_source']})"
            )
            f = h.get("forces")
            if f:
                lines.append(
                    "      forces: "
                    + (
                        f"flex {f['deflection_force_n']} N, push-in {f['push_in_force_n']} N, pull-off {f['pull_off_force_n']} N"
                        if f.get("available")
                        else f"unavailable: {f.get('reason')}"
                    )
                )
        lines.extend(f"    {r}" for r in sv["verdict"]["reasons"])
    lines.append(f"  note: {rep['resolution_note']}")
    lines.append(f">>> ASSEMBLY SWEEP: {'PASS' if rep['ok'] else 'FAIL'}")
    return lines


def order_lines(rep: dict[str, Any]) -> list[str]:
    lines = [
        f"assembly order: base {rep['base']}, {len(rep['parts'])} parts, step {rep['step_mm']:g} mm"
    ]
    if rep["feasible"]:
        lines.append("  assembly order:")
        for s in rep["order"]:
            if s["action"] == "place":
                lines.append(
                    f"    {s['step']}. place {', '.join(s['parts'])} ({s['note']})"
                )
            else:
                passes = "".join(
                    f"; passes {p['class']} {p['pair'][0]}x{p['pair'][1]} depth {p['depth_mm']:.2f}"
                    for p in s["passes"]
                )
                lines.append(
                    f"    {s['step']}. move {', '.join(s['parts'])} along {s['move_along_label']} "
                    f"(straight path free for {s['clear_travel_mm']:g} mm){passes}"
                )
        lines.append(
            "  disassembly = the same steps in reverse, each part leaving along the opposite direction"
        )
    else:
        st = rep["stuck"]
        lines.append(
            f"  NOT FEASIBLE by straight-line moves: stuck with {', '.join(st['remaining'])} assembled"
        )
        for b in st["blocked"]:
            for blk in b["blockers"][:3]:
                lines.append(  # noqa: PERF401 — a formatted row per item reads better than a comprehension
                    f"    {b['part']} blocked along {blk['removal_direction']} at {blk['at_mm']} mm by {blk['blocker']}"
                )
    lines.append(f"  note: {rep['resolution_note']}")
    lines.append(
        f">>> ASSEMBLY ORDER: {'FEASIBLE' if rep['feasible'] else 'INFEASIBLE'}"
    )
    return lines


def ergonomics_lines(rep: dict[str, Any]) -> list[str]:
    p = rep["probes"]
    lines = [
        f"assembly ergonomics: finger pad {p['finger_pad_mm']:g} mm, nail {p['finger_nail_mm']:g} mm, "
        f"tool {p['tool_mm']:g} mm, reach {p['reach_mm']:g} mm"
    ]
    for c in rep["checks"]:
        lines.append(  # noqa: PERF401 — a formatted row per item reads better than a comprehension
            f"  {MARK[c['status']]}  {c['id']} [{c['check']}]: {'; '.join(c['reasons'])}"
        )
    s = rep["summary"]
    lines.append(f"  approximations: {'; '.join(rep['approximations'])}")
    lines.append(
        f">>> ASSEMBLY ERGONOMICS: {'PASS' if rep['ok'] else 'FAIL'}  ({s['pass']} pass, {s['warn']} warn, {s['fail']} fail)"
    )
    return lines


def position_lines(rep: dict[str, Any]) -> list[str]:
    lay = rep["layout"]
    lines = [
        f"assembly position: {rep['assembly']['name']} -> {rep['out']}  "
        f"({'written' if rep['written'] else 'DRY RUN, nothing written: pass --yes'})",
        f"  {len(lay['parts'])} printed part(s) on {lay['plates']} plate(s), bed {lay['bed_mm']:g} mm",
    ]
    for p in lay["parts"]:
        o = p["orientation"]
        warn = f"  WARNING: {o['warning']}" if o.get("warning") else ""
        lines.append(
            f"    plate {p['plate']}: {p['part']} ({p['role']}) {p['size_mm'][0]} x {p['size_mm'][1]} x {p['size_mm'][2]} mm, "
            f"face down {o.get('face', o.get('rotate_deg'))} ({o['how']}){warn}"
        )
    for d in lay["dropped_non_printable"]:
        lines.append(f"    NOT on any plate: {d['name']} ({d['role']}, non-printable)")  # noqa: PERF401 — a formatted row per item reads better than a comprehension
    for d in lay["override"]:
        lines.append(  # noqa: PERF401 — a formatted row per item reads better than a comprehension
            f"    OVERRIDE (logged): {d['name']} ({d['role']}) placed on a plate"
        )
    s = rep.get("slicer", {})
    if s:
        lines.append(f"  slicer settings: {s['note']}")
    if rep.get("verification"):
        v = rep["verification"]
        lines.append(
            f"  verified: {v['objects']} objects, tags ok, non-printable on plates: {v['non_printable_on_plates'] or 'none'}"
        )
    return lines
