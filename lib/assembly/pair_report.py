"""pair_report.py — the text of a `3d assembly pair` report (the `--json` document is the source of truth).

WHAT: a pure function from the `pair.run_pair` document to the lines printed without `--json`. No geometry, no I/O.
"""

from __future__ import annotations

from typing import Any

from assembly.report import MARK


def _g(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.3f}"


def _printed_lines(doc: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key, info in doc.get("printed", {}).items():
        pl, dev = info["placement"], info["deviation"]
        how = "registered" if pl["registered"] else "given"
        resid = (
            f", residual {_g(pl['residual_rms_mm'])} mm"
            if pl["residual_rms_mm"] is not None
            else ""
        )
        out.append(
            f"as printed {key.upper()}: {info['gcode']} (object {info['object'] or 'all'}), placement {how} "
            f"(shift {pl['shift_applied_mm']}{resid})"
        )
        if info["overhang_wall_left_out_mm"] > 0.05 * max(info["wall_mm"], 1.0):
            out.append(
                f"    note: {info['overhang_wall_left_out_mm']} mm of `Overhang wall` (vs {info['wall_mm']} mm outer wall) "
                "was left out; thread flanks that overhang are thinly measured (see coverage)"
            )
        if info["z_lift_mm"]:
            out.append(f"    note: the object starts {info['z_lift_mm']} mm above the bed (raft); the model is lifted by it")
        if dev.get("layers"):
            first = dev.get("first_layer_max_mm")
            out.append(
                f"    wall vs model: max {dev['max_mm']:.3f} mm at z {dev['max_at_z_mm']:.2f}, mean of layer maxima "
                f"{dev['mean_of_layer_max_mm']:.3f} mm over {dev['layers']} layers"
                + (f"; first layer {first:.3f} mm" if first is not None else "")
            )
    return out


def _source_lines(label: str, res: dict[str, Any], feat: dict[str, Any]) -> list[str]:
    tag = MARK[res["status"]]
    out = [f"  [{label}] {tag}  required radial gap >= {res['required_gap_mm']:.3f} mm"]
    for key in ("a", "b"):
        p = res["profiles"][key]
        hand = p["handedness"]["hand"]
        out.append(
            f"      {key.upper()} {p['side']:<5} r {p['r_min_mm']:.3f}..{p['r_max_mm']:.3f} mm, coverage {p['coverage']:.0%}, "
            f"coherence {p['coherence_mm']:.3f} mm"
            + ("" if hand == "none" else f", {hand}-handed")
        )
    cl = res.get("clearance")
    if cl is not None:
        play = cl["free_play"]
        move = cl["best_phase_move_b"]
        out.append(
            f"      radial gap at the posed phase: min {_g(cl['at_pose_min_gap_mm'])} mm (mean {_g(cl['at_pose_mean_gap_mm'])})"
        )
        out.append(
            f"      best phase: tightest radial gap {_g(cl['best_phase_min_gap_mm'])} mm (move B {move}); "
            f"free play {_g(play['value'])} {play['unit']} ({_g(play['fraction_of_turn'])} of a turn)"
        )
        out.append(
            f"      male crest -> female root {_g(cl['male_crest_to_female_root_mm'])} mm, "
            f"female crest -> male root {_g(cl['female_crest_to_male_root_mm'])} mm"
        )
    out.extend(f"      ! {reason}" for reason in res["reasons"])
    return out


def _feature_lines(feat: dict[str, Any]) -> list[str]:
    pitch = f", pitch {feat['pitch_mm']:.3f} mm" if feat["pitch_mm"] else ""
    out = [f"feature {feat['name']} ({feat['kind']}{pitch}): {MARK[feat['status']]}"]
    out += _source_lines("model", feat["model"], feat)
    if "as_printed" in feat:
        parts = "+".join(p.upper() for p in feat["as_printed_parts"])
        out += _source_lines(
            f"as printed ({parts}, rest model)", feat["as_printed"], feat
        )
        for key, d in feat.get("printed_vs_model", {}).items():
            diff = d["radius_printed_minus_model_mm"]
            tail = (
                ""
                if diff is None
                else f"; printed - model radius mean {diff['mean']:+.3f}, min {diff['min']:+.3f}, max {diff['max']:+.3f} mm"
            )
            wd = d["wall_vs_model_in_window"]
            if wd.get("layers"):
                out.append(
                    f"      printed {key.upper()} wall vs model inside the feature window: max {wd['max_mm']:.3f} mm "
                    f"(p95 {wd['p95_of_layer_max_mm']:.3f}) over {wd['layers']} layers"
                )
            out.append(
                f"      printed {key.upper()} r {d['printed_r_min_mm']:.3f}..{d['printed_r_max_mm']:.3f} vs model "
                f"{d['model_r_min_mm']:.3f}..{d['model_r_max_mm']:.3f} mm{tail}"
            )
    return out


def pair_lines(doc: dict[str, Any]) -> list[str]:
    a, b = doc["parts"]["a"], doc["parts"]["b"]
    inter, clr = doc["interpenetration"], doc["clearance"]
    lines = [
        f"assembly pair: A {a['mesh']} (x{a['scale']})  B {b['mesh']} (x{b['scale']})  -> {'OK' if doc['ok'] else 'FAIL'}",
        f"axis through {doc['axis']['point']} along {doc['axis']['direction']}",
    ]
    if "screw_to_best" in doc:
        s = doc["screw_to_best"]
        lines.append(
            f"screw-to-best: B moved by {s['applied_to_b']} to the best phase of feature '{s['feature']}'"
        )
    lines.append(
        f"interpenetration: {MARK[inter['status']]}  {inter['volume_mm3']:.4f} mm3 in {inter['count']} region(s) "
        f"(allowed {inter['allowed_mm3']} mm3)"
    )
    for i, r in enumerate(inter["regions"][:5], 1):
        c = r["center"]
        lines.append(
            f"    [{i}] {r['volume_mm3']:.4f} mm3, depth {r['depth_mm']:.3f} mm at ({c[0]:.2f}, {c[1]:.2f}, {c[2]:.2f})"
        )
    ct = inter["contacts"]
    if ct["count"]:
        lines.append(
            f"    contact (thinner than {ct['seat_tol_mm']} mm, a seat): {ct['count']} region(s), "
            f"{ct['volume_mm3']:.4f} mm3, depth <= {ct['max_depth_mm']:.4f} mm"
        )
    req = (
        f" (required >= {clr['required_mm']} mm)"
        if clr["required_mm"] is not None
        else ""
    )
    cap = f" (search capped at {clr['capped_at_mm']} mm)" if clr["capped_at_mm"] else ""
    lines.append(
        f"minimum clearance: {MARK[clr['status']]}  {clr['min_gap_mm']:.4f} mm{cap}{req}"
    )
    if "zone" in clr:
        z = clr["zone"]
        lines.append(
            f"    inside the zone {z['box']}: {z['min_gap_mm']:.4f} mm "
            f"(surface samples every {z['sample_spacing_mm']} mm)"
        )
    if "fit" in doc:
        f = doc["fit"]
        lines.append(
            f"fit {f['fit_type']}/{f['shape']}: clearance {f['clearance_mm']:+.3f} mm, tol {f['tol_mm']:.3f} -> "
            f"required radial gap {f['required_gap_mm']:.3f} mm ({f['source']})"
        )
    lines += _printed_lines(doc)
    for feat in doc["features"]:
        lines += _feature_lines(feat)
    if doc["summary"]["fail"]:
        lines.append("FAILED: " + ", ".join(doc["summary"]["fail"]))
    if doc["summary"]["warn"]:
        lines.append("warnings: " + ", ".join(doc["summary"]["warn"]))
    return lines
