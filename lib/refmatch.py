"""refmatch.py -- headless core for RELIABLE model<->reference comparison.

ACCESSED VIA: `3d compare <model.scad|render.png> <reference.jpg>`
(lib/commands/compare.py is the thin CLI wrapper; this module does the work and
NEVER prints -- it returns values / raises, so it stays unit-testable).

WHY THIS EXISTS (the bug it fixes):
  The old fit-camera/score flow compared the model silhouette against the RAW
  reference photo thresholded into a mask. For a cluttered photo (sky, ground,
  neighbouring buildings) that threshold mask is garbage, so the reported IoU is
  meaningless -- 0.7 while the building plainly doesn't match. The fix is to
  segment the reference into a clean SUBJECT mask FIRST (OpenCV grabCut via the
  existing preprocessor), then fit the camera against THAT mask, then score and
  collage against the masked reference.

INVARIANTS:
  - Import-light: stdlib only at module top level. Heavy deps (cv2/numpy) and
    binaries (openscad/magick) are reached LAZILY via subprocess inside functions.
  - No printing here. The command module owns all user-facing output.
  - Every metric is derived from the SEGMENTED subject mask, never the raw photo.
  - A degenerate camera fit (silhouette collapses or fills the frame) is REJECTED
    and we fall back to a plain whole-model framing render.

PIPELINE (see compare_pipeline()):
  1. segment_reference()    -> clean subject mask PNG (grabCut)
  2. matched_render()       -> model render from a fitted-or-fallback camera
  3. silhouette_iou()       -> IoU of render silhouette vs subject mask
     ssim_dssim()           -> SSIM/DSSIM of render vs masked reference
  4. build_collage()        -> render | diff | reference labelled montage PNG
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Optional

# --------------------------------------------------------------------------- #
# Tuning constants (degenerate-fit rejection thresholds).
# --------------------------------------------------------------------------- #
# A fitted silhouette covering < MIN_FRAC or > MAX_FRAC of the frame is almost
# certainly degenerate: the optimizer either let the camera distance collapse
# (subject explodes past the frame) or pushed the model to a speck. Either way
# the IoU is untrustworthy, so we fall back to a plain whole-model view.
MIN_SILHOUETTE_FRAC = 0.15
MAX_SILHOUETTE_FRAC = 0.95

# IoU below this means the comparison is unreliable / the tool is misapplied.
UNRELIABLE_IOU = 0.50

# OpenSCAD default render background (srgb 255,255,229) -- used to mask the render.
BG = "#ffffe5"
BG_FUZZ = "10%"

# Default render size for the fallback whole-model view (matches score.py).
FALLBACK_SIZE = (1200, 900)


# --------------------------------------------------------------------------- #
# Small result containers.
# --------------------------------------------------------------------------- #
@dataclass
class RenderResult:
    """Outcome of matched_render()."""

    render_png: str
    used_fallback: bool
    reason: str  # human note: why fallback was taken (or "" if fit was used)
    silhouette_frac: float  # fraction of frame the render silhouette covers


@dataclass
class CompareResult:
    """Everything compare.py needs to print + the paths it must report."""

    iou: float
    ssim: float
    dssim: float
    mask_png: str
    matched_render_png: str
    diff_png: str
    collage_png: str
    used_fallback: bool
    fallback_reason: str
    reliable: bool  # iou >= UNRELIABLE_IOU


# --------------------------------------------------------------------------- #
# Binary resolution (lazy; no heavy imports).
# --------------------------------------------------------------------------- #
def _repo_root() -> str:
    # lib/refmatch.py -> repo root is two levels up.
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bin_3d() -> str:
    return os.path.join(_repo_root(), "bin", "3d")


def _find_magick() -> Optional[str]:
    """Resolve the ImageMagick driver without importing cli.env (keeps refmatch
    standalone-importable). Mirrors cli.env.find_magick."""
    import shutil

    if shutil.which("magick"):
        return "magick"
    for p in ("/opt/homebrew/bin/magick", "/usr/local/bin/magick"):
        if os.access(p, os.X_OK):
            return p
    if shutil.which("convert"):
        return "convert"
    return None


def _magick_compare(magick: str) -> list[str]:
    # IM7: `<magick> compare` works whether `magick` is the literal on PATH or a resolved
    # absolute path (e.g. /opt/homebrew/bin/magick). Only fall back to the legacy standalone
    # `compare` binary if we somehow have no magick at all (defensive; _find_magick gates this).
    return [magick, "compare"] if magick else ["compare"]


def _find_openscad() -> Optional[str]:
    import shutil

    env = os.environ.get("OPENSCAD")
    if env and shutil.which(env):
        return env
    w = shutil.which("openscad")
    if w:
        return w
    for p in (
        "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD",
        "/opt/homebrew/bin/openscad",
        "/usr/local/bin/openscad",
    ):
        if os.access(p, os.X_OK):
            return p
    return None


# --------------------------------------------------------------------------- #
# ImageMagick helpers (subprocess only).
# --------------------------------------------------------------------------- #
class MagickError(RuntimeError):
    """A `magick` invocation failed; carries the stderr for the caller to surface."""


def _magick(args: list[str], *, what: str) -> str:
    mgk = _find_magick()
    if mgk is None:
        raise MagickError(f"{what}: ImageMagick (magick) not found")
    r = subprocess.run([mgk, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise MagickError(f"{what} failed: {(r.stderr or r.stdout).strip()}")
    return r.stdout


def _identify_int(path: str, fmt: str) -> int:
    mgk = _find_magick()
    assert mgk is not None
    if mgk == "magick":
        cmd = ["magick", "identify", "-format", fmt, path]
    else:
        cmd = ["identify", "-format", fmt, path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return int(r.stdout.strip())


def _is_valid_png(path: str) -> bool:
    """True if `path` is a non-empty image ImageMagick can read. Used to accept
    outputs from tools (montage/compare) that return nonzero on non-fatal warnings
    yet still write a valid file."""
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return False
    mgk = _find_magick()
    if mgk is None:
        return False
    if mgk == "magick":
        cmd = ["magick", "identify", path]
    else:
        cmd = ["identify", path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode == 0


# --------------------------------------------------------------------------- #
# (1) SEGMENT the reference into a clean subject mask.
# --------------------------------------------------------------------------- #
def segment_reference(ref: str, outdir: str, *, mask_name: str = "mask.png") -> str:
    """Run the existing preprocessor's OpenCV grabCut to get a clean subject mask.

    Shells out to `3d preprocess <ref> -o <outdir> --force-fallback` (the
    --force-fallback flag skips heavy model tiers and uses the always-available
    grabCut path, so this works offline). Returns the absolute path to the mask
    PNG the preprocessor wrote.

    Raises RuntimeError if the preprocessor fails or the mask is not produced.
    """
    os.makedirs(outdir, exist_ok=True)
    cmd = [_bin_3d(), "preprocess", ref, "-o", outdir, "--force-fallback"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    mask_path = os.path.join(outdir, mask_name)
    if r.returncode != 0 or not os.path.isfile(mask_path):
        raise RuntimeError(
            "reference segmentation failed: "
            + (r.stderr or r.stdout).strip()
            + (f" (no mask at {mask_path})" if not os.path.isfile(mask_path) else "")
        )
    return os.path.abspath(mask_path)


# --------------------------------------------------------------------------- #
# (2) MATCHED RENDER: fit a camera against the MASK, reject degeneracy, fall back.
# --------------------------------------------------------------------------- #
def _silhouette_frac(render_png: str) -> float:
    """Fraction of the frame the render's non-background silhouette covers (0..1)."""
    frac = _magick(
        [
            render_png,
            "-fuzz",
            BG_FUZZ,
            "-fill",
            "black",
            "-opaque",
            BG,
            "-fill",
            "white",
            "+opaque",
            "black",
            "-colorspace",
            "Gray",
            "-format",
            "%[fx:mean]",
            "info:",
        ],
        what="silhouette-frac",
    )
    return float(frac.strip())


def _fallback_render(model: str, render_png: str, size: tuple[int, int]) -> None:
    """Plain whole-model framing render via OpenSCAD --autocenter --viewall.

    No camera fitting -- this just frames the entire model so the silhouette is
    always sane, the safety net for when the fit is degenerate or unavailable.
    """
    osc = _find_openscad()
    if osc is None:
        raise RuntimeError("OpenSCAD not found; cannot render the model")
    w, h = size
    cmd = [
        osc,
        "--render",
        "--autocenter",
        "--viewall",
        f"--imgsize={w},{h}",
        "-o",
        render_png,
        model,
    ]
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0 or not os.path.isfile(render_png):
        raise RuntimeError(f"fallback render failed: {(r.stderr or '').strip()}")


def _is_scad(path: str) -> bool:
    return path.rsplit(".", 1)[-1].lower() == "scad" if "." in path else False


def matched_render(
    model: str,
    mask_png: str,
    outdir: str,
    *,
    fit_rand: int = 80,
    fit_refine: int = 40,
    out_name: str = "matched_render.png",
) -> RenderResult:
    """Produce a render of the model aligned to the subject MASK.

    If `model` is already a PNG render, it is used as-is (no fitting). If it is a
    .scad, we run `3d fit-camera <model> <mask>` (fitting against the SEGMENTED
    mask, not the raw photo, so the optimizer aligns the building silhouette),
    then REJECT a degenerate solution: if the rendered silhouette covers less
    than MIN_SILHOUETTE_FRAC or more than MAX_SILHOUETTE_FRAC of the frame, we
    discard the fit and fall back to a plain whole-model framing render.
    """
    os.makedirs(outdir, exist_ok=True)
    out_png = os.path.join(outdir, out_name)

    # Case A: the "model" is already a rendered image -- nothing to fit.
    if not _is_scad(model):
        # Normalize into outdir so downstream paths are stable.
        _magick([model, out_png], what="copy-render")
        frac = _silhouette_frac(out_png)
        return RenderResult(out_png, used_fallback=False, reason="", silhouette_frac=frac)

    # Case B: a .scad -- try fit-camera against the MASK.
    cam_json = os.path.join(outdir, "camera.json")
    fit_ok = False
    fit_reason = ""
    try:
        cmd = [
            _bin_3d(),
            "fit-camera",
            model,
            mask_png,
            "--out",
            cam_json,
            "--rand",
            str(fit_rand),
            "--refine",
            str(fit_refine),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0 and os.path.isfile(cam_json):
            fit_ok = True
        else:
            fit_reason = "fit-camera failed: " + (r.stderr or r.stdout).strip()[:200]
    except Exception as exc:  # subprocess / OS error -- degrade, don't crash.
        fit_reason = f"fit-camera errored: {exc}"

    if fit_ok:
        with open(cam_json) as f:
            cam = json.load(f)
        fit_png = cam.get("fit_render")
        # Use the fit render directly if it exists; else re-render at the cam.
        if fit_png and os.path.isfile(fit_png):
            _magick([fit_png, out_png], what="copy-fit")
        else:
            osc = _find_openscad()
            if osc is None:
                raise RuntimeError("OpenSCAD not found; cannot render the model")
            cam_arg = cam.get("camera_arg")
            w, h = FALLBACK_SIZE
            cmd2 = [
                osc, "--render", f"--camera={cam_arg}",
                f"--imgsize={w},{h}", "-o", out_png, model,
            ]
            rr = subprocess.run(cmd2, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            if rr.returncode != 0 or not os.path.isfile(out_png):
                fit_ok = False
                fit_reason = f"fit render failed: {(rr.stderr or '').strip()[:200]}"

    if fit_ok:
        frac = _silhouette_frac(out_png)
        if not is_degenerate(frac):
            return RenderResult(out_png, used_fallback=False, reason="", silhouette_frac=frac)
        fit_reason = (
            f"fitted silhouette covers {frac * 100:.1f}% of frame "
            f"(outside {MIN_SILHOUETTE_FRAC * 100:.0f}-{MAX_SILHOUETTE_FRAC * 100:.0f}%); degenerate"
        )

    # Fallback: plain whole-model framing.
    _fallback_render(model, out_png, FALLBACK_SIZE)
    frac = _silhouette_frac(out_png)
    return RenderResult(out_png, used_fallback=True, reason=fit_reason or "no usable fit", silhouette_frac=frac)


def is_degenerate(frac: float) -> bool:
    """Pure: True if a silhouette fraction is outside the sane [MIN, MAX] band."""
    return frac < MIN_SILHOUETTE_FRAC or frac > MAX_SILHOUETTE_FRAC


# --------------------------------------------------------------------------- #
# (3) METRICS: segmented silhouette IoU + SSIM/DSSIM.
# --------------------------------------------------------------------------- #
def silhouette_iou(render_png: str, mask_png: str, outdir: str) -> float:
    """IoU between the render's silhouette and the subject MASK.

    Both are reduced to binary masks at the render's resolution, then
    intersection/union are measured with ImageMagick composites. Delegates the
    final arithmetic to cli.imaging.score_metrics so the math stays shared.
    """
    from cli.imaging import score_metrics  # lazy: keeps refmatch standalone-importable

    os.makedirs(outdir, exist_ok=True)
    mr = os.path.join(outdir, "_mask_render.png")
    _magick(
        [render_png, "-fuzz", BG_FUZZ, "-fill", "black", "-opaque", BG,
         "-fill", "white", "+opaque", "black", mr],
        what="render-mask",
    )
    w = _identify_int(mr, "%w")
    h = _identify_int(mr, "%h")
    # Resize the subject mask to the render frame so the composite lines up.
    mf = os.path.join(outdir, "_mask_ref.png")
    _magick([mask_png, "-resize", f"{w}x{h}!", "-colorspace", "Gray", "-threshold", "50%", mf],
            what="ref-mask")

    area = w * h
    if area <= 0:
        return 0.0
    inter = float(_magick(
        [mr, "-threshold", "50%", mf, "-threshold", "50%",
         "-compose", "multiply", "-composite", "-format", "%[fx:mean]", "info:"],
        what="inter",
    ).strip())
    union = float(_magick(
        [mr, "-threshold", "50%", mf, "-threshold", "50%",
         "-compose", "lighten", "-composite", "-format", "%[fx:mean]", "info:"],
        what="union",
    ).strip())
    return score_metrics(inter, union, 0.0, area)["IoU"]


def _parse_metric(raw: str) -> float:
    """Extract the NORMALIZED [0,1] value from `magick compare -metric ...` output.

    ImageMagick writes the metric to stderr (and exits nonzero when images differ)
    as `RAW (NORMALIZED)`, e.g. `6756.61 (0.103099)`. The first token is the raw
    quantum-scaled sum -- meaningless to us; the parenthesized number is the [0,1]
    value we want. When there are no parentheses (a build that prints a lone
    number), fall back to the first token.
    """
    import re

    s = raw.strip()
    if not s:
        raise MagickError("compare produced no metric output")
    # IM6 sometimes prefixes the line with the tool name, e.g. "convert-im6.q16: (0.12)".
    # Strip any leading "tool:" prefix before parsing.
    if ":" in s and not s.startswith("("):
        s = s.split(":", 1)[-1].strip()
    m = re.search(r"\(([-+0-9.eE]+)\)", s)
    if m:
        return float(m.group(1))
    return float(s.split()[0])


def ssim_dssim(render_png: str, masked_ref_png: str, outdir: str) -> tuple[float, float]:
    """SSIM and DSSIM between the matched render and the MASKED reference.

    Both images are first brought to the render's geometry so `magick compare`
    doesn't bail on a size mismatch.

    WHY WE DON'T TRUST native `-metric SSIM`: on the IM 7.1.2 build we run, the
    SSIM metric is broken -- it returns 0 for IDENTICAL images (should be 1) and
    reports the same value as DSSIM. DSSIM is correct (identical -> 0, different ->
    a [0,1] dissimilarity). So we take DSSIM from `-metric DSSIM` (the parenthesized
    normalized value) and derive SSIM = 1 - DSSIM. For identical images this gives
    the right SSIM=1 / DSSIM=0; don't "fix" this back to native SSIM or you reinstate
    the broken metric.
    """
    mgk = _find_magick()
    if mgk is None:
        raise MagickError("ssim: ImageMagick (magick) not found")
    os.makedirs(outdir, exist_ok=True)
    w = _identify_int(render_png, "%w")
    h = _identify_int(render_png, "%h")
    ref_rs = os.path.join(outdir, "_ref_for_ssim.png")
    _magick([masked_ref_png, "-resize", f"{w}x{h}!", ref_rs], what="ref-resize")

    cmp_cmd = _magick_compare(mgk)
    r = subprocess.run(
        [*cmp_cmd, "-metric", "DSSIM", render_png, ref_rs, "null:"],
        capture_output=True, text=True,
    )
    # compare returns 1 (images differ) or 0 (identical); both are fine.
    # returncode >= 2 is a real error.
    if r.returncode not in (0, 1):
        raise MagickError(f"compare -metric DSSIM failed: {(r.stderr or r.stdout).strip()}")
    dssim = _parse_metric(r.stderr or r.stdout)
    ssim = 1.0 - dssim
    return ssim, dssim


def masked_reference(ref: str, mask_png: str, outdir: str, *, out_name: str = "_masked_ref.png") -> str:
    """Apply the subject mask to the reference photo so only the subject remains
    (background -> the OpenSCAD render background, so SSIM/diff are apples-to-apples)."""
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, out_name)
    w = _identify_int(ref, "%w")
    h = _identify_int(ref, "%h")
    m_rs = os.path.join(outdir, "_mask_rs.png")
    _magick([mask_png, "-resize", f"{w}x{h}!", "-colorspace", "Gray", "-threshold", "50%", m_rs],
            what="mask-resize")
    # Composite: subject pixels from ref, background -> BG colour.
    _magick(
        ["-size", f"{w}x{h}", f"xc:{BG}", ref, m_rs,
         "-compose", "over", "-composite", out],
        what="apply-mask",
    )
    return out


# --------------------------------------------------------------------------- #
# (4) COLLAGE: render | diff | reference, each labelled.
# --------------------------------------------------------------------------- #
def build_diff(render_png: str, masked_ref_png: str, outdir: str, *, out_name: str = "diff.png") -> str:
    """Visual diff (red = mismatch) of render vs masked reference via `magick compare`."""
    mgk = _find_magick()
    if mgk is None:
        raise MagickError("diff: ImageMagick (magick) not found")
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, out_name)
    w = _identify_int(render_png, "%w")
    h = _identify_int(render_png, "%h")
    ref_rs = os.path.join(outdir, "_ref_for_diff.png")
    _magick([masked_ref_png, "-resize", f"{w}x{h}!", ref_rs], what="ref-resize-diff")
    cmp_cmd = _magick_compare(mgk)
    r = subprocess.run(
        [*cmp_cmd, "-metric", "AE", "-highlight-color", "red", render_png, ref_rs, out],
        capture_output=True, text=True,
    )
    # compare exits nonzero when images differ; the diff file is still written.
    if not _is_valid_png(out):
        raise MagickError(f"diff failed: {(r.stderr or r.stdout).strip()}")
    return out


def build_collage(
    render_png: str,
    diff_png: str,
    reference_png: str,
    outdir: str,
    *,
    out_name: str = "collage.png",
) -> str:
    """render | diff | reference 3-in-a-row labelled montage via `magick montage`.

    Each panel is labelled. On hosts whose ImageMagick has no usable font (no
    Freetype/Ghostscript font config -- `magick montage -label` then fails),
    we retry WITHOUT labels so the collage is still produced (panels stay in the
    render|diff|reference order, just unlabelled)."""
    mgk = _find_magick()
    if mgk is None:
        raise MagickError("collage: ImageMagick (magick) not found")
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, out_name)
    montage = ["magick", "montage"] if mgk == "magick" else ["montage"]
    base_tail = ["-tile", "3x1", "-geometry", "+8+8", "-background", "white", out]

    labelled = [
        *montage,
        "-label", "render", render_png,
        "-label", "diff", diff_png,
        "-label", "reference", reference_png,
        *base_tail,
    ]
    # NOTE: `magick montage` returns nonzero for non-fatal warnings too (e.g. a
    # missing font on a host with no Freetype config) yet still writes a valid
    # collage with the labels simply blank. So we accept the output when the file
    # is a valid non-empty PNG, regardless of the exit code.
    r = subprocess.run(labelled, capture_output=True, text=True)
    if _is_valid_png(out):
        return out

    # Truly failed (no file): retry without labels in case a label option is the
    # cause, then surface both errors.
    if os.path.exists(out):
        os.remove(out)
    plain = [*montage, render_png, diff_png, reference_png, *base_tail]
    r2 = subprocess.run(plain, capture_output=True, text=True)
    if _is_valid_png(out):
        return out

    # Fallback: `montage` may not be installed on minimal Ubuntu packages.
    # Use `convert` (+append) for a horizontal concatenation without labels.
    if os.path.exists(out):
        os.remove(out)
    convert_fb = [mgk, render_png, diff_png, reference_png, "+append", out]
    r3 = subprocess.run(convert_fb, capture_output=True, text=True)
    if _is_valid_png(out):
        return out
    raise MagickError(
        "montage failed: "
        + (r.stderr or r.stdout).strip()
        + " | retry-without-labels: "
        + (r2.stderr or r2.stdout).strip()
        + " | convert-fallback: "
        + (r3.stderr or r3.stdout).strip()
    )


# --------------------------------------------------------------------------- #
# Top-level pipeline (the command module calls this and just prints the result).
# --------------------------------------------------------------------------- #
def compare_pipeline(
    model: str,
    reference: str,
    outdir: str,
    *,
    fit_rand: int = 80,
    fit_refine: int = 40,
) -> CompareResult:
    """Full reliable comparison. Writes mask/matched_render/diff/collage into
    `outdir` and returns the metrics. Raises RuntimeError / MagickError on
    unrecoverable failure; the caller maps those to structured CLI errors."""
    os.makedirs(outdir, exist_ok=True)

    mask_png = segment_reference(reference, outdir)
    rr = matched_render(model, mask_png, outdir, fit_rand=fit_rand, fit_refine=fit_refine)

    iou = silhouette_iou(rr.render_png, mask_png, outdir)
    masked_ref = masked_reference(reference, mask_png, outdir)
    ssim, dssim = ssim_dssim(rr.render_png, masked_ref, outdir)

    diff_png = build_diff(rr.render_png, masked_ref, outdir)
    collage_png = build_collage(rr.render_png, diff_png, masked_ref, outdir)

    return CompareResult(
        iou=iou,
        ssim=ssim,
        dssim=dssim,
        mask_png=mask_png,
        matched_render_png=rr.render_png,
        diff_png=diff_png,
        collage_png=collage_png,
        used_fallback=rr.used_fallback,
        fallback_reason=rr.reason,
        reliable=iou >= UNRELIABLE_IOU,
    )
