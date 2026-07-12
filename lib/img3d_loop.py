#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# img3d_loop.py — CLOSED parametric-recovery loop for image→3D (diagnosis §Closed-loop).
#
# WHAT IT DOES
#   Turns a reference silhouette into tuned parameters of a BLOCKOUT template family, as
#   a staged, honestly-gated loop:
#     1. perceive : a VLM veto reads the critical discrete feature (column count).
#     2. generate : ai.blockout emits a temple with that column count + default dims.
#     3. contour-fit : a LOCKED-FRAMING view-bank pose fit (fixed working distance +
#        centroid look-at, azimuth/elevation grid) — ported from
#        tools/spatial_fit_experiment.evaluate_view_bank_retrieval. Locking the framing
#        stops the free-pan/scale "cheat" that lets a wrong-sized model fake a match, so
#        the silhouette scale stays faithful to the dimensions being recovered.
#     4. monotonic-refine : coordinate descent over the continuous dims, accept a step
#        IFF the boundary SDF loss strictly improves AND the semantic veto still passes
#        (a dispose-gate: an edit that merges/erases columns is reverted). Reuses
#        match_loop.strictly_better + the changelog discipline.
#     5. proof-status gate : status is `ok` only when fit_status==ok AND the veto passes
#        AND the 6-artifact proof panel is emitted; otherwise `warning`/`failed`.
#
# HONESTY CONTRACT (see AGENTS.md proof rules)
#   - `--synthetic` is the ACCEPTANCE milestone: hidden params + a hidden azimuth/
#     elevation are drawn from the SAME family and rendered to a reference. The hidden
#     values are used ONLY for post-hoc scoring — NEVER passed to the pose fit or the
#     refine. The mock veto is configured with the hidden column count, standing in for a
#     VLM reading the reference (the diagnosis sanctions MockBackend with a canned count).
#     A fixed working distance D is a shared rendering convention, not hidden data.
#   - Real-photo input NEVER reports success: `recovery_status` is capped at `diagnostic`.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from typing import Any, Sequence

import numpy as np
from PIL import Image, ImageDraw

from ai.backends import Backend, MockBackend, resolve_backend
from ai.blockout import CONTINUOUS_TUNABLES, BlockoutParams, params_from_dict, render_scad, with_values
from ai.veto import FEATURE_SPECS, CriticalFeature, VetoResult, evaluate, perceive, run_veto
from fit_camera import OPENSCAD, array_to_mask
from fit_camera_math import cam_from_params, fit_status_from_spatial_metrics
from match_loop import changelog_append, changelog_init, strictly_better
from spatial_fit_metrics import binary_contour, spatial_fit_metrics

# Fixed camera working distance (rendering convention shared by reference + fit; NOT a
# hidden parameter). Locks silhouette scale to the model dimensions so absolute dims are
# recoverable rather than degenerate with a free zoom.
WORKING_DISTANCE = 200.0
DEFAULT_SIZE = (240, 200)
GRID_AZ_STEP = 30
GRID_ELEVATIONS = (0.0, 10.0, 20.0, 30.0)


def log(msg: str) -> None:
    print(msg, flush=True)


# ── rendering + geometry ─────────────────────────────────────────────────────
def _unlink_quiet(path: str) -> None:
    """Remove `path` if present; a lock/permission/race error is swallowed (best-effort
    cleanup must never crash the render loop)."""
    try:
        os.remove(path)
    except OSError:
        pass


def model_centroid(scad_path: str, tmp: str) -> list[float]:
    """Return the bbox centroid of a model via a temp binary-STL export."""
    stl = os.path.join(tmp, "centroid.stl")
    try:
        subprocess.run(
            [OPENSCAD, "--export-format", "binstl", "-o", stl, scad_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return [0.0, 0.0, 0.0]
    if not os.path.exists(stl) or os.path.getsize(stl) < 84:
        return [0.0, 0.0, 0.0]
    with open(stl, "rb") as handle:
        handle.read(80)
        (ntri,) = struct.unpack("<I", handle.read(4))
        if ntri == 0:
            return [0.0, 0.0, 0.0]
        data = handle.read(ntri * 50)
    rec = np.frombuffer(data, dtype=np.uint8).reshape(ntri, 50)
    floats = np.ascontiguousarray(rec[:, :48]).view("<f4")
    verts = floats[:, 3:12].reshape(-1, 3)
    lo, hi = verts.min(axis=0), verts.max(axis=0)
    return ((lo + hi) / 2.0).tolist()


def render_blockout(
    params: BlockoutParams, az: float, el: float, size: tuple[int, int], tmp: str,
    name: str = "cand", center: list[float] | None = None,
) -> Any:
    """Render one blockout instance at (az, el) with LOCKED framing; return RGB array.

    Pass a precomputed `center` (bbox centroid) to skip the STL export when the dims —
    hence the centroid — are constant across a call batch (e.g. a pose-fit grid)."""
    scad = os.path.join(tmp, f"{name}.scad")
    with open(scad, "w", encoding="utf-8") as handle:
        handle.write(render_scad(params))
    if center is None:
        center = model_centroid(scad, tmp)
    cam = cam_from_params([az, el, WORKING_DISTANCE, 0.0, 0.0], center)
    out = os.path.join(tmp, f"{name}.png")
    # Remove any stale output FIRST (fixed name is reused across candidates): a failed
    # render must NOT fall through to the previous candidate's PNG and be scored as this
    # one. Mirrors the safe pattern in fit_camera.render_to_array.
    _unlink_quiet(out)
    cam_arg = ",".join(f"{v:.3f}" for v in cam)
    try:
        proc = subprocess.run(
            [OPENSCAD, "--render", "-o", out, f"--camera={cam_arg}",
             f"--imgsize={size[0]},{size[1]}", scad],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
        )
    except subprocess.TimeoutExpired:
        _unlink_quiet(out)
        return None
    # A render FAILS on a nonzero rc, an absent output, or a 0-byte stub. Remove the output
    # so no partial/stale PNG survives on disk for a path-based reader (e.g. _veto_candidate
    # reads veto_cand.png by path) to score as a successful render.
    if proc.returncode != 0 or not _nonempty_file(out):
        _unlink_quiet(out)
        return None
    return np.asarray(Image.open(out).convert("RGB").resize(size), dtype=np.int16)


def _nonempty_file(path: str) -> bool:
    """True iff `path` exists and is non-empty; a vanished/unstattable path is False
    (fail-closed), never raising even under a TOCTOU race."""
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def boundary_loss(
    params: BlockoutParams, az: float, el: float, refm: Any, size: tuple[int, int], tmp: str,
    center: list[float] | None = None,
) -> float:
    arr = render_blockout(params, az, el, size, tmp, center=center)
    if arr is None:
        return 1e6
    from spatial_fit_metrics import boundary_sdf_loss

    return boundary_sdf_loss(array_to_mask(arr), refm)


# ── stage 3: view-bank pose fit (ported from spatial_fit_experiment) ──────────
def viewbank_pose_fit(
    params: BlockoutParams, refm: Any, size: tuple[int, int], tmp: str,
    *, az_step: int = GRID_AZ_STEP, elevations: Sequence[float] = GRID_ELEVATIONS,
) -> tuple[float, float, float]:
    """Coarse azimuth/elevation retrieval ranked by boundary SDF loss.

    This is the view-bank retrieval from tools/spatial_fit_experiment.py
    (evaluate_view_bank_retrieval): render a grid of poses at a fixed framing and pick
    the lowest boundary loss. Locked framing is what makes it a faithful pose retrieval
    rather than a scale/pan search. Returns (azimuth, elevation, loss)."""
    scad = os.path.join(tmp, "pose_probe.scad")
    with open(scad, "w", encoding="utf-8") as handle:
        handle.write(render_scad(params))
    center = model_centroid(scad, tmp)  # constant across the grid; compute once
    best: tuple[float, float, float] | None = None
    for az in range(-180, 180, az_step):
        for el in elevations:
            loss_px = boundary_loss(params, float(az), float(el), refm, size, tmp, center=center)
            if best is None or loss_px < best[2]:
                best = (float(az), float(el), loss_px)
    assert best is not None
    return best


# ── stage 4: monotonic refine (reuses match_loop discipline + veto dispose-gate) ─
def _refine_steps(params: BlockoutParams) -> dict[str, float]:
    return {
        "column_radius": 0.6,
        "column_height": max(2.0, 0.12 * params.column_height),
        "span": max(3.0, 0.10 * params.span),
        "base_height": max(1.5, 0.20 * params.base_height),
        "pediment_height": max(2.0, 0.18 * params.pediment_height),
    }


def monotonic_refine(
    params: BlockoutParams, az: float, el: float, refm: Any, size: tuple[int, int], tmp: str,
    *, backend: Backend, expected: dict[str, float], features: Sequence[CriticalFeature],
    changelog: str, max_passes: int = 10,
) -> BlockoutParams:
    """Coordinate descent on the continuous dims: accept a step IFF the boundary SDF loss
    strictly improves AND the semantic veto still passes (dispose-gate). Monotonic."""
    steps = _refine_steps(params)
    cur = params
    best = boundary_loss(cur, az, el, refm, size, tmp)
    rnd = 0
    for _pass in range(max_passes):
        improved = False
        for key in CONTINUOUS_TUNABLES:
            for delta in (steps[key], -steps[key]):
                cand = with_values(cur, **{key: max(0.5, getattr(cur, key) + delta)})
                cand_loss = boundary_loss(cand, az, el, refm, size, tmp)
                if not strictly_better(cand_loss, best, "lower", 1e-3):
                    continue
                rnd += 1
                veto = _veto_candidate(cand, az, el, size, tmp, backend, expected, features)
                verdict = "ok" if veto.passed else "reverted"
                changelog_append(
                    changelog, rnd, key, round(getattr(cur, key), 3),
                    round(getattr(cand, key), 3), round(best, 4), round(cand_loss, 4),
                    "PASS" if veto.passed else "FAIL", verdict,
                )
                if veto.passed:
                    best, cur, improved = cand_loss, cand, True
        if not improved:
            steps = {k: v * 0.5 for k, v in steps.items()}
            if max(steps.values()) < 0.35:
                break
    return cur


def _veto_candidate(
    cand: BlockoutParams, az: float, el: float, size: tuple[int, int], tmp: str,
    backend: Backend, expected: dict[str, float], features: Sequence[CriticalFeature],
) -> VetoResult:
    arr = render_blockout(cand, az, el, size, tmp, name="veto_cand")
    if arr is None:
        # The candidate render FAILED. Fail closed directly — do NOT read veto_cand.png
        # back by path (a nonzero-rc partial file could otherwise be scored as a render).
        observed: dict[str, float | None] = {f.name: None for f in features}
        return evaluate(observed, expected, features)
    veto_png = os.path.join(tmp, "veto_cand.png")
    Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB").save(veto_png)
    observed = perceive(backend, veto_png, features)
    return evaluate(observed, expected, features)


# ── proof panel (6 named artifacts) ──────────────────────────────────────────
def _edge_overlay(model_mask: Any, refm: Any) -> Any:
    canvas = np.zeros((*refm.shape, 3), dtype=np.uint8)
    canvas[..., 0] = (binary_contour(refm) * 255).astype(np.uint8)          # ref edge red
    canvas[..., 1] = (binary_contour(model_mask) * 255).astype(np.uint8)    # model edge cyan
    canvas[..., 2] = (binary_contour(model_mask) * 255).astype(np.uint8)
    return canvas


def write_recovery_panel(
    panel_png: str, reference_rgb: Any, refm: Any, model_rgb: Any,
    metrics: dict[str, float | bool | str | None], status: str, veto_line: str,
) -> None:
    """Compose the 6-artifact proof panel: reference, mask, recovered render, contour
    error map, a metrics cell, and a status/veto cell."""
    h, w = refm.shape
    model_mask = array_to_mask(model_rgb) if model_rgb is not None else np.zeros_like(refm)
    ref_img = Image.fromarray(np.clip(reference_rgb, 0, 255).astype(np.uint8), "RGB")
    mask_img = Image.fromarray((refm * 255).astype(np.uint8), "L").convert("RGB")
    # A None model_rgb means the final render FAILED. Emit a diagnostic placeholder cell
    # instead of crashing (np.clip(None) throws) so the panel + result.json still land.
    if model_rgb is not None:
        model_img: Image.Image = Image.fromarray(np.clip(model_rgb, 0, 255).astype(np.uint8), "RGB")
    else:
        model_img = _text_cell(w, h, "recovered render", ["RENDER FAILED", "no model image"])
    edge_img = Image.fromarray(_edge_overlay(model_mask, refm), "RGB")
    metrics_img = _text_cell(w, h, "metrics", _metrics_lines(metrics))
    status_img = _text_cell(w, h, "result", [f"status: {status}", veto_line,
                                             "reference=red  model=cyan"])
    cells = [
        ("reference", ref_img), ("reference mask", mask_img), ("recovered render", model_img),
        ("contour error", edge_img), ("boundary metrics", metrics_img), ("proof status", status_img),
    ]
    header = 22
    panel = Image.new("RGB", (w * 3, (h + header) * 2), "white")
    draw = ImageDraw.Draw(panel)
    for idx, (label, img) in enumerate(cells):
        col, row = idx % 3, idx // 3
        x, y = col * w, row * (h + header)
        draw.text((x + 6, y + 5), label, fill=(20, 20, 20))
        panel.paste(img.resize((w, h)), (x, y + header))
    panel.save(panel_png)


def _metrics_lines(metrics: dict[str, float | bool | str | None]) -> list[str]:
    def g(name: str) -> float:
        value = metrics.get(name)
        return float(value) if isinstance(value, (int, float)) else 0.0

    return [
        f"edge_f1@4 = {g('edge_f1@4'):.3f}",
        f"area_iou  = {g('area_iou'):.3f}",
        f"bbox_iou  = {g('bbox_iou'):.3f}",
        f"chamfer   = {g('edge_chamfer_px'):.2f} px",
        f"sdf_loss  = {g('boundary_sdf_loss_px'):.2f} px",
        f"p95       = {g('hausdorff_p95_px'):.2f} px",
    ]


def _text_cell(w: int, h: int, title: str, lines: list[str]) -> Image.Image:
    img = Image.new("RGB", (w, h), "white")
    draw = ImageDraw.Draw(img)
    draw.text((8, 8), title, fill=(20, 20, 20))
    for i, line in enumerate(lines):
        draw.text((8, 30 + i * 18), line, fill=(40, 40, 40))
    return img


# ── orchestration ────────────────────────────────────────────────────────────
def recover(
    refm: Any, reference_rgb: Any, *, template: str, expected_columns: int,
    backend: Backend, out_dir: str, size: tuple[int, int], tmp: str,
    start_params: BlockoutParams | None = None,
) -> dict[str, Any]:
    """Run generate → contour-fit → refine → final veto → proof-status gate.

    `expected_columns` is the VLM-perceived column count; the boundary optimizer never
    sees hidden values. Returns a result dict; also writes the 6-artifact panel + JSON.
    """
    features = FEATURE_SPECS[template]
    expected: dict[str, float] = {"column_count": float(expected_columns)}
    os.makedirs(out_dir, exist_ok=True)
    changelog = os.path.join(out_dir, "changelog.md")
    changelog_init(changelog)

    start = start_params or BlockoutParams(n_columns=expected_columns)
    start = with_values(start, n_columns=expected_columns)
    log("[stage] view-bank pose fit (locked framing)")
    az, el, pose_loss = viewbank_pose_fit(start, refm, size, tmp)
    log(f"  pose az={az} el={el} loss={pose_loss:.3f}")

    log("[stage] monotonic refine (boundary SDF + veto dispose-gate)")
    refined = monotonic_refine(
        start, az, el, refm, size, tmp,
        backend=backend, expected=expected, features=features, changelog=changelog,
    )
    az, el, _ = viewbank_pose_fit(refined, refm, size, tmp)  # re-fit pose to refined dims

    model_rgb = render_blockout(refined, az, el, size, tmp, name="recovered")
    model_mask = array_to_mask(model_rgb) if model_rgb is not None else np.zeros_like(refm)
    metrics = spatial_fit_metrics(model_mask, refm).as_dict()
    fit_status, fit_warnings = fit_status_from_spatial_metrics(metrics)

    log("[stage] final semantic veto")
    final_png = os.path.join(out_dir, "recovered_render.png")
    # Remove any stale render from a prior run in a reused --out dir FIRST: if this run's
    # final render failed (model_rgb is None) the veto must fail closed on an ABSENT file,
    # not pass against last run's leftover PNG.
    _unlink_quiet(final_png)
    if model_rgb is not None:
        Image.fromarray(np.clip(model_rgb, 0, 255).astype(np.uint8), "RGB").save(final_png)
    veto = run_veto(backend, final_png, expected, features)
    veto_line = "veto PASS" if veto.passed else "veto FAIL: " + "; ".join(veto.failures)[:60]
    log(f"  {veto_line}")

    status = _proof_status(fit_status, veto.passed)
    panel_png = os.path.join(out_dir, "proof_panel.png")
    write_recovery_panel(panel_png, reference_rgb, refm, model_rgb, metrics, status, veto_line)
    _write_reference_artifacts(out_dir, reference_rgb, refm)

    result: dict[str, Any] = {
        "template": template,
        "recovered_params": refined.to_dict(),
        "expected_columns": expected_columns,
        "pose": {"azimuth_deg": az, "elevation_deg": el},
        "fit_status": fit_status,
        "fit_warnings": fit_warnings,
        "veto": veto.as_dict(),
        "spatial_metrics": metrics,
        "recovery_status": status,
        "proof_panel": panel_png,
        # Honest schema: null when the final render failed and no PNG was written, rather
        # than pointing at an absent path the docs promise exists.
        "recovered_render": final_png if os.path.exists(final_png) else None,
        "changelog": changelog,
    }
    return result


def _proof_status(fit_status: str, veto_passed: bool) -> str:
    if fit_status == "failed" or not veto_passed:
        return "failed" if fit_status == "failed" else "warning"
    if fit_status == "ok" and veto_passed:
        return "ok"
    return "warning"


def _write_reference_artifacts(out_dir: str, reference_rgb: Any, refm: Any) -> None:
    Image.fromarray(np.clip(reference_rgb, 0, 255).astype(np.uint8), "RGB").save(
        os.path.join(out_dir, "reference.png"))
    Image.fromarray((refm * 255).astype(np.uint8), "L").save(
        os.path.join(out_dir, "reference_mask.png"))


# ── synthetic acceptance milestone ───────────────────────────────────────────
HIDDEN_PARAMS = BlockoutParams(
    n_columns=5, span=72.0, column_radius=2.8, column_height=36.0,
    base_height=11.0, pediment_height=18.0,
)
HIDDEN_AZ, HIDDEN_EL = -90.0, 12.0


def run_synthetic(out_dir: str, size: tuple[int, int]) -> dict[str, Any]:
    """The ACCEPTANCE test: recover hidden params from a same-family reference.

    Hidden params + hidden az/el are used ONLY to render the reference and to score the
    recovery post-hoc — never passed to the pose fit or the refine. The mock veto is
    configured with the hidden column count (standing in for a VLM reading the ref)."""
    os.makedirs(out_dir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="img3d_syn_")
    try:
        return _run_synthetic(out_dir, size, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_synthetic(out_dir: str, size: tuple[int, int], tmp: str) -> dict[str, Any]:
    hidden = HIDDEN_PARAMS
    reference_rgb = render_blockout(hidden, HIDDEN_AZ, HIDDEN_EL, size, tmp, name="reference")
    if reference_rgb is None:
        raise SystemExit("img3d-loop: synthetic reference render failed (openscad?)")
    refm = array_to_mask(reference_rgb)

    # Mock VLM veto: returns the hidden column count as a VLM would read it from the ref.
    backend: Backend = MockBackend(response=json.dumps({"column_count": hidden.n_columns}))
    perceived = perceive(backend, os.path.join(tmp, "reference.png"), FEATURE_SPECS["temple"])
    expected_columns = int(round(float(perceived["column_count"] or 0)))

    start = BlockoutParams(n_columns=expected_columns)  # default dims, correct N
    result = recover(
        refm, reference_rgb, template="temple", expected_columns=expected_columns,
        backend=backend, out_dir=out_dir, size=size, tmp=tmp, start_params=start,
    )

    recovered = params_from_dict(result["recovered_params"])
    evaluation = _score_recovery(hidden, start, recovered)
    result["synthetic"] = {
        "hidden_params": hidden.to_dict(),
        "hidden_pose": {"azimuth_deg": HIDDEN_AZ, "elevation_deg": HIDDEN_EL},
        "start_params": start.to_dict(),
        **evaluation,
    }
    proven = (
        evaluation["within_tolerance"] and evaluation["error_reduced"]
        and result["fit_status"] == "ok" and result["veto"]["passed"]
        and os.path.exists(result["proof_panel"])
    )
    result["recovery_status"] = "ok" if proven else "failed"
    result["proven"] = proven
    with open(os.path.join(out_dir, "result.json"), "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
    return result


def _score_recovery(
    hidden: BlockoutParams, start: BlockoutParams, recovered: BlockoutParams,
) -> dict[str, Any]:
    per_param: dict[str, dict[str, float]] = {}
    within = True
    total_start_err = 0.0
    total_final_err = 0.0
    for key in CONTINUOUS_TUNABLES:
        truth = float(getattr(hidden, key))
        final_err = abs(float(getattr(recovered, key)) - truth)
        start_err = abs(float(getattr(start, key)) - truth)
        tol = max(1.5, 0.10 * abs(truth))
        per_param[key] = {
            "hidden": truth, "recovered": float(getattr(recovered, key)),
            "abs_error": round(final_err, 3), "tolerance": round(tol, 3),
            "start_error": round(start_err, 3),
        }
        within = within and final_err <= tol
        total_start_err += start_err
        total_final_err += final_err
    return {
        "per_param": per_param,
        "total_start_error": round(total_start_err, 3),
        "total_final_error": round(total_final_err, 3),
        "within_tolerance": within,
        "error_reduced": total_final_err <= 0.5 * total_start_err,
    }


# ── real-image (diagnostic-only) mode ────────────────────────────────────────
def run_reference(
    reference: str, out_dir: str, template: str, size: tuple[int, int],
    backend_name: str | None,
) -> dict[str, Any]:
    """Run the loop on a real reference image. NEVER reports success: real-photo output
    is capped at `diagnostic` per the honesty contract."""
    tmp = tempfile.mkdtemp(prefix="img3d_ref_")
    try:
        return _run_reference(reference, out_dir, template, size, backend_name, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_reference(
    reference: str, out_dir: str, template: str, size: tuple[int, int],
    backend_name: str | None, tmp: str,
) -> dict[str, Any]:
    reference_rgb = np.asarray(Image.open(reference).convert("RGB").resize(size), dtype=np.int16)
    refm = _reference_mask(reference, size)
    backend = resolve_backend(backend_name)
    perceived = perceive(backend, reference, FEATURE_SPECS[template])
    if perceived["column_count"] is None:
        raise SystemExit("img3d-loop: veto backend could not read column_count from reference")
    expected_columns = int(round(float(perceived["column_count"])))
    result = recover(
        refm, reference_rgb, template=template, expected_columns=expected_columns,
        backend=backend, out_dir=out_dir, size=size, tmp=tmp,
    )
    result["input_kind"] = "photo"
    result["recovery_status"] = "diagnostic"  # real photos never claim success
    result["proven"] = False
    with open(os.path.join(out_dir, "result.json"), "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
    return result


def _reference_mask(reference: str, size: tuple[int, int]) -> Any:
    """Light-on-dark mask (white subject) or dark-on-light photo → subject mask."""
    from fit_camera import image_looks_like_binary_mask, ref_mask

    polarity = "light" if image_looks_like_binary_mask(reference) else "dark"
    return ref_mask(reference, size[0], size[1], 150, polarity)


def _parse_size(text: str) -> tuple[int, int]:
    w, h = text.lower().split("x", 1)
    return int(w), int(h)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="3d recover-blockout", description=__doc__)
    ap.add_argument("reference", nargs="?", help="reference image (real-photo, diagnostic mode)")
    ap.add_argument("--synthetic", action="store_true",
                    help="run the synthetic parametric-recovery acceptance milestone")
    ap.add_argument("--template", default="temple", choices=sorted(FEATURE_SPECS))
    ap.add_argument("--out", default="recover_out", help="output directory")
    ap.add_argument("--size", default="240x200", help="render size WxH")
    ap.add_argument("--backend", default=None, help="veto AI backend (claude|codex|mock|…)")
    args = ap.parse_args(argv)
    size = _parse_size(args.size)

    if args.synthetic:
        result = run_synthetic(args.out, size)
    elif args.reference:
        if not os.path.isfile(args.reference):
            raise SystemExit(f"img3d-loop: reference not found: {args.reference}")
        result = run_reference(args.reference, args.out, args.template, size, args.backend)
    else:
        ap.print_help()
        return 1

    print(json.dumps(result, indent=2))
    log(f"STATUS={result['recovery_status']}")
    return 0 if result["recovery_status"] in ("ok", "diagnostic") else 1


if __name__ == "__main__":
    sys.exit(main())
