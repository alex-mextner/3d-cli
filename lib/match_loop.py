#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# match_loop.py — FORCED-MONOTONIC acceptance orchestrator for a silhouette-match
#                 loop (research report §3.2 + §7.4). GENERIC: works on any
#                 assembly.scad + a reference image; no project-specific names.
#
# WHAT IT DOES
#   Drives a render -> critique -> edit -> verify -> accept/revert loop that can ONLY
#   improve. The LLM critic (codex, optional) PROPOSES one numeric parameter delta; an
#   image-space metric (IoU/AE) + the manifold gate DISPOSE. A change is kept iff the
#   score STRICTLY improves AND the model still renders as a clean manifold; otherwise
#   reverted. Every decision is appended to a changelog. The loop stops on the critic's
#   CONVERGED token or after N no-improvement rounds.
#
#   One parameter per round (keeps the monotone test unambiguous). The changelog is fed
#   back to the critic to kill the FlipFlop oscillation (report §3.1).
#
# GENERALIZATION (vs the loco-specific original):
#   - assembly + reference are positional args.
#   - the CONSTANTS file defaults to the assembly itself (--constants overrides).
#   - the TUNABLE parameter set is DERIVED from the constants file via the params
#     extractor (numeric scalars), not a hardcoded list. --params restricts it.
#   - rendering + scoring go through the `3d` CLI (render / score), so there is no
#     dependency on any project's sibling scripts.
#
# USAGE
#   match_loop.py <assembly.scad> <reference> [--rounds N] [--dry-run]
#                 [--constants FILE] [--metric iou|ae] [--params a,b,c]
#                 [--no-improve N] [--margin F] [--cam C] [--size WxH] [--ortho]
#
#   --dry-run   SKIP codex; synthesise a deterministic edit each round so the
#               render/score/accept/revert/changelog machinery is exercised.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import re
import subprocess
import sys
from typing import Any

from ai.backends import Backend, resolve_backend
from ai import load_backend_config
from errors import ThreeDError

REPO_ROOT = os.environ.get("REPO_ROOT") or os.path.abspath(
    os.path.join(os.path.dirname(__file__), ".."))
THREED = os.path.join(REPO_ROOT, "bin", "3d")


def log(msg: str) -> None:
    print(msg, flush=True)


def run(
    cmd: list[str], timeout: float | None = None, stdin: str | None = None
) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT after {timeout}s"
    except FileNotFoundError as e:
        return 127, f"NOT FOUND: {e}"


# ── constants.scad read / write ──────────────────────────────────────────────
_CONST_RE = (
    r"(?m)^(\s*{name}\s*=\s*)"     # 1: prefix incl '='
    r"(-?\d+(?:\.\d+)?)"           # 2: numeric value
    r"(\s*;.*)$"                   # 3: ';' + trailing comment
)


def read_const(constants: str, name: str) -> float | None:
    with open(constants) as f:
        src = f.read()
    m = re.search(_CONST_RE.format(name=re.escape(name)), src)
    return float(m.group(2)) if m else None


def write_const(constants: str, name: str, value: float) -> float | None:
    with open(constants) as f:
        src = f.read()
    m = re.search(_CONST_RE.format(name=re.escape(name)), src)
    if not m:
        return None
    old = float(m.group(2))
    if "." not in m.group(2) and float(value).is_integer():
        vstr = str(int(round(value)))
    else:
        vstr = f"{value:.4f}".rstrip("0").rstrip(".")
    new_src = src[:m.start()] + m.group(1) + vstr + m.group(3) + src[m.end():]
    with open(constants, "w") as f:
        f.write(new_src)
    return old


def derive_tunables(constants: str, restrict: str | None) -> list[str]:
    """Numeric scalar parameters editable by the loop. Derived from the constants
    file (single-line `name = number;`), optionally restricted to --params."""
    names: list[str] = []
    with open(constants) as f:
        in_block = 0
        for line in f:
            in_block += line.count("{") - line.count("}")
            if in_block > 0:
                continue
            m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(-?\d+(?:\.\d+)?)\s*;", line)
            if m:
                names.append(m.group(1))
    if restrict:
        want = {x.strip() for x in restrict.split(",") if x.strip()}
        names = [n for n in names if n in want]
    return names


# ── changelog ────────────────────────────────────────────────────────────────
def changelog_init(path: str) -> None:
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write("# match loop — changelog\n\n")
            f.write("Anti-oscillation log. One line per round; fed back to the "
                    "critic so it never re-proposes a reverted edit.\n\n")
            f.write("| # | param | old->new | score old->new | manifold | verdict |\n")
            f.write("|---|-------|----------|----------------|----------|---------|\n")


def changelog_append(
    path: str, rnd: int, param: str, old: Any, new: Any,
    s_old: Any, s_new: Any, manifold: str, verdict: str,
) -> None:
    def fmt(x: Any) -> str:
        if x is None:
            return "—"
        if isinstance(x, float):
            return "inf" if math.isinf(x) else f"{x:.4f}".rstrip("0").rstrip(".")
        return str(x)
    line = (f"| {rnd} | `{param}` | {fmt(old)}->{fmt(new)} | "
            f"{fmt(s_old)}->{fmt(s_new)} | {manifold} | **{verdict}** |\n")
    with open(path, "a") as f:
        f.write(line)


def changelog_text(path: str) -> str:
    if not os.path.exists(path):
        return "(empty)"
    with open(path) as f:
        return f.read()


# ── metric handling ──────────────────────────────────────────────────────────
def parse_score(
    out: str, prefer: str
) -> tuple[float | None, str | None, str | None]:
    iou: float | None = None
    ae: float | None = None
    for m in re.finditer(r"(?im)\bIoU\s*=\s*([0-9.eE+-]+)", out):
        try:
            iou = float(m.group(1))
        except ValueError:
            pass
    for m in re.finditer(r"(?im)\bAE\s*=\s*([0-9.eE+-]+)", out):
        try:
            ae = float(m.group(1))
        except ValueError:
            pass
    if prefer == "iou" and iou is not None:
        return iou, "IoU", "higher"
    if prefer == "ae" and ae is not None:
        return ae, "AE", "lower"
    if iou is not None:
        return iou, "IoU", "higher"
    if ae is not None:
        return ae, "AE", "lower"
    return None, None, None


def strictly_better(
    new: float | None, best: float | None, better: str | None, margin: float
) -> bool:
    if new is None:
        return False
    if best is None:
        return True
    if better == "higher":
        if math.isinf(best) and best < 0:
            return True
        return new > best + margin
    if math.isinf(best) and best > 0:
        return True
    return new < best - margin


def init_best(better: str) -> float:
    return (-math.inf) if better == "higher" else math.inf


# ── VERIFY: render (silhouette) + score + manifold ───────────────────────────
def verify(
    assembly: str, ref: str, prefer: str, work: str,
    cam: str | None, size: str, ortho: bool,
) -> tuple[float | None, str | None, str | None, bool, str]:
    """render+score the assembly vs ref via `3d score <scad> <ref>`, plus a
    manifold gate via `3d check`. Returns (score, metric, better, m_ok, m_detail)."""
    os.makedirs(work, exist_ok=True)
    args = ["bash", THREED, "score", assembly, ref, "-o", work, "--size", size]
    if cam:
        args += ["--cam", cam]
    if ortho:
        args += ["--ortho"]
    rc, out = run(args, timeout=600)
    score, metric, better = parse_score(out, prefer)
    log(f"    score rc={rc}: " + " ".join(
        line for line in out.splitlines() if "=" in line and line.split("=")[0] in
        ("AE", "IoU", "CLOSENESS"))[:160])

    # manifold gate
    rc2, out2 = run(["bash", THREED, "check", assembly], timeout=600)
    m_ok = (">>> CHECK: PASS" in out2) or ("MANIFOLD: PASS" in out2)
    if not m_ok and rc2 == 0:
        m_ok = True  # check passed by exit code
    detail = "clean" if m_ok else (
        next((line for line in out2.splitlines() if re.search(r"(?i)(ERROR|WARNING|FAIL):", line)),
             "manifold gate failed")[:120])
    return score, metric, better, m_ok, detail


# ── CRITIC: codex (real) or deterministic synthesiser (dry-run) ──────────────
CRITIC_PROMPT = """\
You are the vision CRITIC in a forced-monotonic CAD silhouette-match loop.
Attached: overlay.png (reference=RED, render=CYAN, matched=GREY).
You are matching a 3D OpenSCAD model to a reference photo.

Current best score: {best} (metric {metric}, {better} is better).

Changelog of edits already tried (DO NOT re-propose a reverted edit, do not oscillate):
{changelog}

Tunable parameters in the constants file (name : current value):
{params}

TASK: emit EXACTLY ONE edit that will most reduce the silhouette mismatch, as STRICT
JSON on a single line:  {{"param":"NAME","current":<num>,"target":<num>}}
- NAME must be one of the listed parameters.
- target is the new numeric value (one parameter, one change). Conservative deltas;
  the loop reverts overshoots.
If the residual is small and you have no high-confidence edit, output the single token:
CONVERGED
Output ONLY the JSON object or the token CONVERGED. No prose, no code fences.
"""


def params_block(constants: str, tunables: list[str]) -> str:
    rows: list[str] = []
    for n in tunables:
        v = read_const(constants, n)
        if v is not None:
            rows.append(f"  {n} : {v:g}")
    return "\n".join(rows)


def build_critic_prompt(
    constants: str, tunables: list[str], best: float | None,
    metric: str | None, better: str | None, changelog: str,
) -> str:
    return CRITIC_PROMPT.format(
        best=("inf" if best is None or math.isinf(best) else f"{best:.4f}"),
        metric=metric or "IoU", better=better or "higher",
        changelog=changelog_text(changelog), params=params_block(constants, tunables))


def critic_backend(
    backend: Backend,
    constants: str, tunables: list[str], best: float | None,
    metric: str | None, better: str | None, work: str, changelog: str,
    timeout: float = 1200,
) -> str | dict[str, Any] | None:
    """Ask the selected AI backend for ONE parameter edit (or CONVERGED).

    Backend-agnostic replacement for the old codex-only critic. When `backend` is the
    CodexBackend the invocation is identical to before (prompt on stdin, `-i overlay`,
    same output parsing), so codex behavior is preserved; any other backend is now a
    first-class alternative. A missing binary / timeout / transport failure is caught
    and reported as a no-improve round (matching the old codex-absent degrade)."""
    overlay = os.path.join(work, "overlay.png")
    prompt = build_critic_prompt(constants, tunables, best, metric, better, changelog)
    images = [pathlib.Path(overlay)] if os.path.exists(overlay) else None
    if images and not backend.supports_images:
        log(f"    CRITIC: WARNING — backend '{backend.name}' cannot see images; the "
            "overlay is NOT sent. The critique is text-only (metrics + changelog); pick "
            "an image-capable backend (codex, ollama) for vision-guided edits.")
    log(f"    CRITIC: {backend.name} …")
    try:
        out = backend.complete("", prompt, images=images, timeout=timeout)
    except ThreeDError as e:
        log(f"    CRITIC: {backend.name} unusable ({e.message}) — treating as no-improve")
        return None
    return parse_critic_output(out)


def parse_critic_output(out: str) -> str | dict[str, Any] | None:
    if re.search(r"(?m)^\s*CONVERGED\s*$", out) and not re.search(r"\{", out):
        return "CONVERGED"
    for raw in reversed(re.findall(r"\{[^{}]*\}", out)):
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if "param" in d and "target" in d:
            return {"param": str(d["param"]), "current": d.get("current"),
                    "target": float(d["target"])}
    if "CONVERGED" in out:
        return "CONVERGED"
    return None


def critic_dry(
    constants: str, tunables: list[str], rnd: int
) -> str | dict[str, Any]:
    """Deterministic stand-in for codex: cycle params with alternating small/large
    bumps — exercises both the accept and revert paths."""
    if not tunables or rnd >= len(tunables) * 2:
        return "CONVERGED"
    param = tunables[rnd % len(tunables)]
    cur = read_const(constants, param)
    if cur is None:
        return "CONVERGED"
    delta = 1.0 if rnd % 2 == 0 else 12.0
    return {"param": param, "current": cur, "target": cur + delta}


# ── MAIN LOOP ────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="forced-monotonic silhouette match loop")
    ap.add_argument("assembly")
    ap.add_argument("reference")
    ap.add_argument("--rounds", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true",
                    help="skip the AI critic; synth edits to exercise the machinery")
    ap.add_argument("--backend", default=None,
                    help="AI critic backend (claude|codex|opencode|ollama|mock); "
                         "default: ai.json, else first available")
    ap.add_argument("--constants", default=None,
                    help="file holding the tunable constants (default: the assembly)")
    ap.add_argument("--metric", choices=["iou", "ae"], default="iou")
    ap.add_argument("--params", default=None,
                    help="comma list restricting which constants the critic may tune")
    ap.add_argument("--no-improve", type=int, default=4)
    ap.add_argument("--margin", type=float, default=1e-4)
    ap.add_argument("--cam", default=None, help="6-param vector camera for renders")
    ap.add_argument("--size", default="1200,900")
    ap.add_argument("--ortho", action="store_true")
    ap.add_argument("--work", default=None, help="work dir (default: alongside assembly)")
    args = ap.parse_args()

    assembly = os.path.abspath(args.assembly)
    if not os.path.isfile(assembly):
        log(f"match: assembly not found: {assembly}")
        return 2
    ref = os.path.abspath(args.reference)
    if not os.path.isfile(ref):
        log(f"match: reference not found: {ref}")
        return 2
    constants = os.path.abspath(args.constants) if args.constants else assembly
    if not os.path.isfile(constants):
        log(f"match: constants file not found: {constants}")
        return 2

    work = args.work or os.path.join(os.path.dirname(assembly), "match_work")
    os.makedirs(work, exist_ok=True)
    changelog = os.path.join(work, "changelog.md")
    changelog_init(changelog)
    size = args.size.replace("x", ",")

    tunables = derive_tunables(constants, args.params)
    if not tunables:
        log("match: no numeric scalar constants found to tune "
            "(check --constants / --params).")
        return 2

    log("=" * 70)
    log("FORCED-MONOTONIC MATCH LOOP")
    log(f"  assembly={assembly}")
    log(f"  reference={ref}")
    log(f"  constants={constants}")
    log(f"  tunables ({len(tunables)}): {', '.join(tunables[:12])}"
        + (" …" if len(tunables) > 12 else ""))
    log(f"  rounds={args.rounds} metric={args.metric} margin={args.margin} "
        f"dry_run={args.dry_run}")

    backend: Backend | None = None
    if not args.dry_run:
        try:
            backend = resolve_backend(args.backend, config=load_backend_config())
        except ThreeDError as e:
            log(e.render(color=False))
            log("  (or run with --dry-run to exercise the loop without an AI backend)")
            return e.exit_code
        log(f"  backend={backend.name}")
    log("=" * 70)

    log("[round 0] baseline VERIFY")
    score, metric, better, m_ok, m_detail = verify(
        assembly, ref, args.metric, work, args.cam, size, args.ortho)
    if better is None:
        better = "higher" if args.metric == "iou" else "lower"
        metric = args.metric.upper()
    best = score if score is not None else init_best(better)
    log(f"  baseline {metric}={score} manifold={'PASS' if m_ok else 'FAIL'} ({m_detail})")

    no_improve = 0
    rnd = 0
    verdict_final = "STOPPED"

    while rnd < args.rounds:
        rnd += 1
        log("-" * 70)
        log(f"[round {rnd}/{args.rounds}] best {metric}="
            f"{'inf' if math.isinf(best) else f'{best:.4f}'} no_improve={no_improve}")

        edit: str | dict[str, Any] | None
        if args.dry_run:
            edit = critic_dry(constants, tunables, rnd - 1)
        else:
            assert backend is not None  # set above for the non-dry-run path
            edit = critic_backend(
                backend, constants, tunables, best, metric, better, work, changelog)

        if edit == "CONVERGED":
            log("  CRITIC -> CONVERGED")
            verdict_final = "CONVERGED"
            break
        if not edit or not isinstance(edit, dict):
            log("  CRITIC -> no parseable edit; counting as no-improve")
            no_improve += 1
            if no_improve >= args.no_improve:
                break
            continue

        param = str(edit["param"])
        target = float(edit["target"])
        if param not in tunables:
            log(f"  CRITIC proposed non-tunable '{param}'; skip")
            no_improve += 1
            if no_improve >= args.no_improve:
                break
            continue
        log(f"  CRITIC -> edit {param} -> {target}")

        old = write_const(constants, param, target)
        if old is None:
            log(f"  AUTHOR -> '{param}' not found; skip")
            changelog_append(changelog, rnd, param, "?", target, best, None, "—", "SKIP")
            no_improve += 1
            if no_improve >= args.no_improve:
                break
            continue
        log(f"  AUTHOR -> {param}: {old} -> {target}")

        score, vmetric, vbetter, m_ok, m_detail = verify(
            assembly, ref, args.metric, work, args.cam, size, args.ortho)
        if vmetric:
            metric, better = vmetric, vbetter
        log(f"  VERIFY -> {metric}={score} manifold={'PASS' if m_ok else 'FAIL'} ({m_detail})")

        improved = strictly_better(score, best, better, args.margin)
        if improved and m_ok and score is not None:  # strictly_better => score not None
            log(f"  ACCEPT -> {metric} {'inf' if math.isinf(best) else best} -> {score}")
            changelog_append(changelog, rnd, param, old, target, best, score, "PASS", "ok")
            best = score
            no_improve = 0
        else:
            why = []
            if not improved:
                why.append("no score gain")
            if not m_ok:
                why.append(f"manifold FAIL ({m_detail})")
            log(f"  REVERT -> {param} {target} -> {old} [{', '.join(why)}]")
            write_const(constants, param, old)
            changelog_append(changelog, rnd, param, old, target, best, score,
                             "PASS" if m_ok else "FAIL", "reverted")
            no_improve += 1

        if no_improve >= args.no_improve:
            log(f"  stop: {no_improve} consecutive no-improvement rounds")
            break

    log("=" * 70)
    log(f">>> MATCH-LOOP: {verdict_final}  final best {metric}="
        f"{'inf' if math.isinf(best) else f'{best:.4f}'}  rounds_used={rnd}")
    log(f">>> changelog: {changelog}")
    log("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
