"""3d fits — the shared fit/tolerance table: list, show, resolve, apply and manage its corrections.

WHAT: fit types (snap, sliding, tight, press, glue) with a per-face clearance, interference and tolerance, plus
  correction rows per printer x material x nozzle x layer. `resolve` answers "what clearance for a SNAP fit on a
  ROUND feature printed in PETG on k1se?" with the provenance chain behind the number; `apply` puts it into a
  mate (OpenSCAD snippet / JSON) or re-corrects the `@fit`-marked clearances of an existing model;
  `corrections` manages the manual rows (calibration rows are written by `3d calibrate fit`).

WHY: tolerances differ per fit type and per machine/plastic; they were typed by hand as `--gap 0.2` literals.
  One table (lib/data/fits.yaml, every value cited) and one resolver (lib/fits.py) make every number explainable.

ACCESSED VIA: `3d fits <sub>`; `3d fit-check --fit` and `3d fit-niche --fit` call the same resolver
  (`fits_sources.resolve`). Thin CLI: argv parsing and printing only; the logic is in lib/fits.py,
  lib/fits_sources/, lib/fits_registry.py and lib/fits_apply.py (lazy imports: command discovery stays stdlib-only).
"""

from __future__ import annotations

import argparse
import json
from typing import TYPE_CHECKING, Any, NoReturn

from cli.registry import Command
from errors import InputNotFound, InvalidArgument, UsageError

if TYPE_CHECKING:
    from fits import Correction, FitTable, Resolution

USAGE = """3d fits <list|show|resolve|apply|corrections> [options]
  Fit tolerances as a table: the clearance of a snap / sliding / tight / press / glue fit, corrected for the
  printer and plastic. Every command takes --json (schema `3d.fits/1`).

Subcommands:
  list [--fit TYPE] [--shape SHAPE]
      the fit types with clearance per face, interference, tolerance, shapes and aliases.
      Example: 3d fits list --shape round
  show TYPE [--printer P --material M --nozzle MM --layer MM]
      one fit type in full (notes, features) and its clearance for every shape on that printer/material.
      Example: 3d fits show snap --printer k1se --material PETG
  resolve --fit TYPE [--shape SHAPE] [--printer P] [--material M] [--nozzle MM] [--layer MM]
      the signed clearance per face, tolerance and the provenance chain (table row, alias, printer/material
      registry lookup, the ONE correction row applied, material shrink). Unknown printer/material = table
      default, flagged `fallback`. --shape defaults to planar.
      Example: 3d fits resolve --fit snap --shape round --printer k1se --material petg --nozzle 0.4 --layer 0.2
  apply --fit TYPE [--shape SHAPE] [--printer P] [--material M] [--var NAME]
      emit the clearance of a mate as an OpenSCAD snippet (`NAME = 0.35; // @fit snap shape=round ...`),
      or JSON with --json. Read-only.
      Example: 3d fits apply --fit sliding --shape round --printer k1se --material PETG --var pin_clearance
  apply FILE.scad... [--printer P] [--material M] [--nozzle MM] [--layer MM] [--yes]
      correct the declared clearances of existing models: every line `NAME = NUMBER; // @fit TYPE [shape=S]`
      gets the table value for that printer/material (the absolute value, so a re-run changes nothing). Dry
      run (diff) by default; --yes rewrites the files. `3d fit-niche --fit` writes such lines.
      Example: 3d fits apply insert.scad --printer k1se --material PETG --yes
  corrections list [--origin table|manual|calibration]
      every correction row the resolver can use: the table's own, the manual ones and the calibration ones.
      Example: 3d fits corrections list --json
  corrections add --printer P --material M --delta MM [--nozzle MM] [--layer MM] [--shape S] [--fit TYPE]
                  [--note TEXT] [--measured-at ISO] [--yes]
      a manual row (signed mm added to the per-face clearance; `*` = any printer/material). One row per
      scope: the same scope replaces the old row. Dry run (shows before -> after) unless --yes.
      Example: 3d fits corrections add --printer k1se --material PETG --nozzle 0.4 --delta 0.05 --yes
  corrections remove --printer P --material M [--nozzle MM] [--layer MM] [--shape S] [--fit TYPE] [--yes]
      delete the manual row of that exact scope. Dry run unless --yes.
      Example: 3d fits corrections remove --printer k1se --material PETG --nozzle 0.4 --yes

Options:
  --fit TYPE      snap | sliding | tight | press | glue, or an alias (free_running, tight_running, snug, normal,
                  loose). Example: 3d fits resolve --fit press --shape pin
  --shape SHAPE   planar | round | slot | pin | hinge. Example: 3d fits list --shape slot
  --printer NAME  fleet key (k1se) or printer model (`Creality K1 SE`). Example: 3d fits resolve --fit snap --printer k1se
  --material NAME material (`PETG`) or a product name containing one (`Elegoo Rapid PLA+`).
                  Example: 3d fits resolve --fit snap --material "Rapid PLA+"
  --nozzle MM     nozzle diameter. Example: 3d fits resolve --fit snap --printer k1se --nozzle 0.4
  --layer MM      layer height. Example: 3d fits resolve --fit snap --printer k1se --layer 0.2
  --var NAME      variable name of the `apply --fit` snippet (default clearance).
                  Example: 3d fits apply --fit snap --var latch_gap
  --delta MM      signed mm added to the per-face clearance by a manual row (positive = bigger gap).
                  Example: 3d fits corrections add --printer k1se --material PLA --delta 0.05
  --note TEXT     free text stored with a manual row. Example: 3d fits corrections add --printer k1se --material PLA --delta 0.05 --note "caliper"
  --measured-at ISO  timestamp of a manual row (default now, UTC).
                  Example: 3d fits corrections add --printer k1se --material PLA --delta 0.05 --measured-at 2026-10-24T10:00:00Z
  --origin ORIGIN filter of `corrections list`: table | manual | calibration.
                  Example: 3d fits corrections list --origin manual
  --json          machine-readable output, schema `3d.fits/1`. Example: 3d fits resolve --fit snap --json
  --yes           perform the write of `apply FILE` / `corrections add|remove` (dry run without it).
                  Example: 3d fits corrections remove --printer k1se --material PETG --yes
  -h, --help      this help

Manual rows live in <config dir>/fits-corrections.yaml ($THREED_FITS_CORRECTIONS overrides).

Exit: 0 ok, 2 unknown fit/shape/subcommand/missing input (the accepted values are listed), 127 missing dependency.

Examples:
  3d fits list
  3d fits resolve --fit snap --shape round --printer k1se --material petg --nozzle 0.4 --layer 0.2 --json
  3d fits apply insert.scad --printer k1se --material PETG
  3d fits corrections add --printer k1se --material PETG --delta 0.05 --yes"""

_SUBCOMMANDS = ("list", "show", "resolve", "apply", "corrections")
_CORRECTION_SUBS = ("list", "add", "remove")


class _Parser(argparse.ArgumentParser):
    """argparse that reports a bad flag as a structured UsageError (exit 2) instead of exiting itself."""

    def __init__(self, sub: str) -> None:
        super().__init__(prog=f"3d fits {sub}", add_help=False)
        self._sub = sub

    def error(self, message: str) -> NoReturn:
        raise UsageError(
            message,
            command="fits",
            remediation=[f"See `3d fits --help` for `{self._sub}`."],
        )


def _query_flags(p: argparse.ArgumentParser, *, fit: bool = True) -> None:
    if fit:
        p.add_argument("--fit")
    p.add_argument("--shape")
    p.add_argument("--printer")
    p.add_argument("--material")
    p.add_argument("--nozzle", type=float)
    p.add_argument("--layer", type=float)
    p.add_argument("--json", action="store_true")


def _emit(doc: dict[str, Any]) -> None:
    import fits

    print(json.dumps({"schema": fits.SCHEMA, **doc}, indent=2, sort_keys=False))


def _check(flag: str, value: str, accepted: list[str]) -> None:
    if value not in accepted:
        raise InvalidArgument(flag, value, accepted, command="fits")


def _mm(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def _describe(p: dict[str, Any]) -> str:
    """One text line of a provenance entry."""
    kind = p["kind"]
    if kind == "fit_type":
        return (
            f"fit type     {p['id']}: clearance {_mm(p['clearance_mm_per_face'])}, interference "
            f"{_mm(p['interference_mm'])}, tol ±{_mm(p['tol_mm'])} mm  [{p['source']}]"
        )
    if kind == "alias":
        return f"alias        {p['id']} = {p['fit_type']} + {_mm(p['extra_mm'])} mm  [{p['source']}]"
    if kind == "note":
        return f"note         {p['message']}"
    if kind in ("printer", "material"):
        if not p["known"]:
            return f"{kind:<12} {p['message']}"
        found = (
            f"fleet key {p['key']}, model {p['model']}"
            if kind == "printer" and p["key"]
            else (
                f"model {p['model']}"
                if kind == "printer"
                else f"{p['name']} (via {p['via']})"
            )
        )
        return f"{kind:<12} {p['input']!r}: {found}  [{p['source']}]"
    if kind in ("correction", "correction_superseded"):
        scope = (
            ", ".join(
                f"{k} {p[key]}"
                for k, key in (
                    ("printer", "printer"),
                    ("material", "material"),
                    ("nozzle", "nozzle_mm"),
                    ("layer", "layer_height_mm"),
                    ("shape", "shape"),
                    ("fit", "fit_type"),
                )
                if p[key] not in (None, "*")
            )
            or "any printer/material"
        )
        label = "correction" if kind == "correction" else "superseded"
        status = p.get("status") or p["source"]
        extra = (
            f"; fallback: no row for {', '.join(p['fallback_reason'])}"
            if p.get("fallback")
            else ""
        )
        note = f"  ({p['note']})" if p["note"] else ""
        return f"{label:<12} {p['delta_mm']:+g} mm [{status}] {scope}{extra}{note}"
    if kind == "material_correction":
        if p["id"] == "none":
            return f"material     {p['message']}"
        if p["id"] == "shrink_xy":
            return f"material     shrink {p['shrink_xy_pct']:g}% -> scale XY by {p['xy_scale']:g}  [{p['source']}]"
        return f"material     Z shrink {p['shrink_z_pct']:g}% -> scale Z by {p['z_scale']:g}  [{p['source']}]"
    return f"{kind} {p}"


def _print_resolution(res: Resolution) -> None:
    who = " · ".join(
        part
        for part in (
            f"printer {res.printer}" if res.printer else "",
            f"material {res.material}" if res.material else "",
            f"nozzle {_mm(res.nozzle_mm)}" if res.nozzle_mm else "",
            f"layer {_mm(res.layer_height_mm)}" if res.layer_height_mm else "",
        )
        if part
    )
    label = (
        res.fit_type
        if res.requested == res.fit_type
        else f"{res.fit_type} (as {res.requested})"
    )
    print(
        f"fit {label} / {res.shape}" + (f"  ·  {who}" if who else "  ·  table defaults")
    )
    print(
        f"  clearance     {res.clearance_mm:+.3f} mm per face (radial for round); negative = interference"
    )
    print(f"  tolerance     ±{_mm(res.tol_mm)} mm")
    print(
        f"  interference  {_mm(res.interference_mm)} mm  (already included in the clearance)"
    )
    corr = next(p for p in res.provenance if p["kind"] == "correction")
    print(
        f"  status        {corr['status']}" + ("  (fallback)" if res.fallback else "")
    )
    if res.xy_scale is not None:
        print(
            f"  xy scale      {res.xy_scale:g}  (slicer shrinkage compensation; not part of the clearance)"
        )
    print("provenance:")
    for entry in res.provenance:
        print(f"  {_describe(entry)}")


# --------------------------------------------------------------------------- list / show
def _cmd_list(argv: list[str]) -> int:
    import fits

    p = _Parser("list")
    p.add_argument("--fit")
    p.add_argument("--shape")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    table = fits.load_table()
    if a.fit is not None:
        _check("--fit", a.fit, table.names())
    if a.shape is not None:
        _check("--shape", a.shape, list(table.shapes))
    wanted = table.aliases[a.fit].fit_type if a.fit in table.aliases else a.fit
    types = [
        ft
        for ft in table.fit_types.values()
        if (wanted is None or ft.name == wanted)
        and (a.shape is None or a.shape in ft.applies_to)
    ]
    aliases = [
        al
        for al in table.aliases.values()
        if any(al.fit_type == ft.name for ft in types)
    ]
    if a.fit is not None:
        aliases = (
            [al for al in aliases if al.name == a.fit]
            if a.fit in table.aliases
            else aliases
        )
    if a.json:
        _emit(
            {
                "command": "list",
                "shapes": list(table.shapes),
                "fit_types": [
                    {
                        "name": ft.name,
                        "clearance_mm_per_face": ft.clearance_mm_per_face,
                        "interference_mm": ft.interference_mm,
                        "tol_mm": ft.tol_mm,
                        "applies_to": list(ft.applies_to),
                        "features": list(ft.features),
                        "notes": ft.notes,
                    }
                    for ft in types
                ],
                "aliases": [
                    {
                        "name": al.name,
                        "fit_type": al.fit_type,
                        "extra_mm": al.extra_mm,
                        "clearance_mm": round(
                            table.fit_types[al.fit_type].clearance_mm_per_face
                            + al.extra_mm,
                            4,
                        ),
                    }
                    for al in aliases
                ],
            }
        )
        return 0
    if not types:
        print(f"no fit type applies to shape {a.shape!r}")
        return 0
    print(f"{'FIT':<9} {'CLEARANCE':>9} {'INTERF.':>8} {'TOL':>7}  {'SHAPES':<28} USE")
    for ft in types:
        print(
            f"{ft.name:<9} {_mm(ft.clearance_mm_per_face):>9} {_mm(ft.interference_mm):>8} "
            f"{'±' + _mm(ft.tol_mm):>7}  {','.join(ft.applies_to):<28} {', '.join(ft.features)}"
        )
    for al in aliases:
        base = table.fit_types[al.fit_type]
        extra = f" + {_mm(al.extra_mm)}" if al.extra_mm else ""
        print(
            f"  alias {al.name} = {al.fit_type}{extra} = {_mm(base.clearance_mm_per_face + al.extra_mm)} mm"
        )
    print(
        "\nmm per mating face (radial for round); `3d fits show TYPE` for notes, `3d fits resolve` for a printer."
    )
    return 0


def _cmd_show(argv: list[str]) -> int:
    import fits
    import fits_sources

    p = _Parser("show")
    p.add_argument("fit", nargs="?")
    _query_flags(p, fit=False)
    a = p.parse_args(argv)
    if a.fit is None:
        raise UsageError(
            "`show` needs a fit type",
            command="fits",
            remediation=["Example: 3d fits show snap  (`3d fits list` has the names)."],
        )
    table = fits.load_table()
    _check("fit", a.fit, table.names())
    shapes = [a.shape] if a.shape else list(table.shapes)
    for shape in shapes:
        _check("--shape", shape, list(table.shapes))
    resolved = [
        fits_sources.resolve(
            a.fit, s, a.printer, a.material, a.nozzle, a.layer, table=table
        )
        for s in shapes
    ]
    base = table.fit_types[resolved[0].fit_type]
    if a.json:
        _emit(
            {
                "command": "show",
                "fit_type": base.name,
                "requested": a.fit,
                "notes": base.notes,
                "features": list(base.features),
                "applies_to": list(base.applies_to),
                "shapes": [r.to_dict() for r in resolved],
            }
        )
        return 0
    print(f"{base.name}: {base.notes}")
    print(f"features: {', '.join(base.features)}")
    print(f"applies to: {', '.join(base.applies_to)}")
    first = resolved[0]
    who = " ".join(x for x in (first.printer, first.material) if x) or "table defaults"
    print(f"\nclearance per face for {who}:")
    print(f"  {'SHAPE':<8} {'CLEARANCE':>9} {'TOL':>7}  NOTE")
    for r in resolved:
        corr = next(x for x in r.provenance if x["kind"] == "correction")
        meant = "" if r.shape in base.applies_to else "outside applies_to; "
        print(
            f"  {r.shape:<8} {r.clearance_mm:>+9.3f} {'±' + _mm(r.tol_mm):>7}  {meant}{corr['status']}"
        )
    return 0


def _cmd_resolve(argv: list[str]) -> int:
    import fits_sources

    p = _Parser("resolve")
    _query_flags(p)
    a = p.parse_args(argv)
    if a.fit is None:
        raise UsageError(
            "`resolve` needs --fit TYPE",
            command="fits",
            remediation=[
                "Example: 3d fits resolve --fit snap --shape round --printer k1se --material PETG"
            ],
        )
    res = fits_sources.resolve(
        a.fit, a.shape or "planar", a.printer, a.material, a.nozzle, a.layer
    )
    if a.json:
        _emit({"command": "resolve", **res.to_dict()})
    else:
        _print_resolution(res)
    return 0


# --------------------------------------------------------------------------- apply
def _cmd_apply(argv: list[str]) -> int:
    p = _Parser("apply")
    p.add_argument("files", nargs="*")
    _query_flags(p)
    p.add_argument("--var", default="clearance")
    p.add_argument("--yes", action="store_true")
    a = p.parse_args(argv)
    if a.files and a.fit is not None:
        raise UsageError(
            "give FILE.scad to correct, or --fit TYPE to emit a snippet, not both",
            command="fits",
            remediation=[
                "Files are corrected from their own `@fit` markers; --fit goes with a snippet."
            ],
        )
    if not a.files and a.fit is None:
        raise UsageError(
            "`apply` needs --fit TYPE (emit a snippet) or FILE.scad (correct its @fit clearances)",
            command="fits",
            remediation=[
                "Example: 3d fits apply --fit snap --shape round --printer k1se --material PETG",
                "Example: 3d fits apply insert.scad --printer k1se --material PETG --yes",
            ],
        )
    if a.files:
        return _apply_files(a)
    import fits_apply
    import fits_sources

    res = fits_sources.resolve(
        a.fit, a.shape or "planar", a.printer, a.material, a.nozzle, a.layer
    )
    snippet = fits_apply.scad_snippet(res, a.var)
    if a.json:
        _emit(
            {
                "command": "apply",
                "mode": "snippet",
                **res.to_dict(),
                "var": a.var,
                "snippet": snippet,
            }
        )
    else:
        print(snippet, end="")
    return 0


def _apply_files(a: argparse.Namespace) -> int:
    import os
    import tempfile
    from pathlib import Path

    import fits_apply
    import fits_sources

    cache: dict[tuple[str, str], Resolution] = {}

    def resolver(fit: str, shape: str) -> Resolution:
        if (fit, shape) not in cache:
            cache[(fit, shape)] = fits_sources.resolve(
                fit, shape, a.printer, a.material, a.nozzle, a.layer
            )
        return cache[(fit, shape)]

    results: list[tuple[Path, str, list[Any]]] = []
    for name in a.files:
        path = Path(name)
        if not path.is_file():
            raise InputNotFound(name, command="fits")
        text = path.read_text(encoding="utf-8")
        new_text, changes = fits_apply.correct_text(text, resolver)
        if not changes:
            raise UsageError(
                f"{name} declares no fit clearance: no line `NAME = NUMBER; // @fit TYPE [shape=SHAPE]`",
                command="fits",
                remediation=[
                    "Mark the clearance: `clearance = 0.2; // @fit sliding shape=planar` "
                    "(`3d fit-niche --fit` writes it), or emit one with `3d fits apply --fit TYPE`."
                ],
            )
        results.append((path, new_text, changes))
        if a.yes and new_text != text:
            fd, tmp = tempfile.mkstemp(
                dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(new_text)
                os.chmod(tmp, path.stat().st_mode & 0o7777)
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
    if a.json:
        _emit(
            {
                "command": "apply",
                "mode": "correct",
                "applied": bool(a.yes),
                "printer": a.printer,
                "material": a.material,
                "files": [
                    {
                        "path": str(path),
                        "written": bool(a.yes) and any(c.changed for c in changes),
                        "changes": [c.to_dict() for c in changes],
                    }
                    for path, _, changes in results
                ],
            }
        )
        return 0
    for path, _, changes in results:
        for c in changes:
            arrow = (
                f"{_mm(c.before)} -> {_mm(c.after)} mm"
                if c.changed
                else f"{_mm(c.after)} mm (unchanged)"
            )
            print(f"{path}:{c.line}: {c.name} ({c.fit}/{c.shape}): {arrow}")
        scales = {
            c.resolution.xy_scale for c in changes if c.resolution.xy_scale is not None
        }
        for scale in sorted(scales):
            print(
                f"  note: slicer shrink compensation for {a.material}: scale XY by {scale:g}"
            )
    changed = sum(c.changed for _, _, changes in results for c in changes)
    if a.yes:
        print(f"{changed} clearance(s) rewritten")
    else:
        print(
            f"{changed} clearance(s) would change; dry run, add --yes to rewrite the file(s)"
        )
    return 0


# --------------------------------------------------------------------------- corrections
def _origin_rows(table: FitTable) -> list[tuple[str, Correction]]:
    import fits_sources

    rows: list[tuple[str, Correction]] = [("table", c) for c in table.corrections]
    for source in fits_sources.discover().values():
        rows.extend((source.name, c) for c in source.load(fits_sources.Query()))
    return rows


def _cmd_corrections(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if argv else 1
    sub, rest = argv[0], argv[1:]
    if any(t in ("-h", "--help") for t in rest):
        print(USAGE)
        return 0
    if sub not in _CORRECTION_SUBS:
        raise InvalidArgument(
            "corrections subcommand", sub, list(_CORRECTION_SUBS), command="fits"
        )
    if sub == "list":
        return _corrections_list(rest)
    return _corrections_write(sub, rest)


def _corrections_list(argv: list[str]) -> int:
    import fits
    import fits_sources
    from fits_sources import manual

    p = _Parser("corrections list")
    p.add_argument("--origin")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    table = fits.load_table()
    origins = ["table", *fits_sources.discover()]
    if a.origin is not None:
        _check("--origin", a.origin, origins)
    rows = [
        {"origin": origin, **c.to_dict(), "status": fits.CORRECTION_STATUS[c.source]}
        for origin, c in _origin_rows(table)
        if a.origin is None or origin == a.origin
    ]
    if a.json:
        _emit(
            {
                "command": "corrections list",
                "manual_store": str(manual.store_path()),
                "sources": {n: s.summary for n, s in fits_sources.discover().items()},
                "corrections": rows,
            }
        )
        return 0
    print(
        f"{'ORIGIN':<12} {'PRINTER':<10} {'MATERIAL':<9} {'NOZZLE':>6} {'LAYER':>5} {'SHAPE':<7} {'FIT':<8} {'DELTA':>7}  STATUS"
    )
    for r in rows:
        print(
            f"{r['origin']:<12} {r['printer']:<10} {r['material']:<9} {_mm(r['nozzle_mm']):>6} "
            f"{_mm(r['layer_height_mm']):>5} {r['shape'] or '-':<7} {r['fit_type'] or '-':<8} "
            f"{r['delta_mm']:>+7.3f}  {r['status']}"
            + (f"  ({r['note']})" if r["note"] else "")
        )
    print(f"\nmanual rows: {manual.store_path()}")
    return 0


def _corrections_write(sub: str, argv: list[str]) -> int:
    import datetime

    import fits
    from fits_sources import manual

    p = _Parser(f"corrections {sub}")
    p.add_argument("--printer")
    p.add_argument("--material")
    p.add_argument("--nozzle", type=float)
    p.add_argument("--layer", type=float)
    p.add_argument("--shape")
    p.add_argument("--fit")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--json", action="store_true")
    if sub == "add":
        p.add_argument("--delta", type=float)
        p.add_argument("--note", default="")
        p.add_argument("--measured-at")
    a = p.parse_args(argv)
    missing = [
        f
        for f, v in (("--printer", a.printer), ("--material", a.material))
        if v is None
    ]
    if sub == "add" and a.delta is None:
        missing.append("--delta")
    if missing:
        raise UsageError(
            f"`corrections {sub}` needs {', '.join(missing)}",
            command="fits",
            remediation=[
                "Use `*` for any printer/material. Example: "
                "3d fits corrections add --printer k1se --material PETG --delta 0.05 --yes"
            ],
        )
    table = fits.load_table()
    if a.shape is not None:
        _check("--shape", a.shape, list(table.shapes))
    if a.fit is not None:
        _check(
            "--fit", a.fit, list(table.fit_types)
        )  # rows match canonical names, not aliases
    measured_at = getattr(a, "measured_at", None) or datetime.datetime.now(
        datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        row = fits.correction_from_dict(
            {
                "printer": a.printer,
                "material": a.material,
                "nozzle_mm": a.nozzle,
                "layer_height_mm": a.layer,
                "shape": a.shape,
                "fit_type": a.fit,
                "delta_mm": a.delta if sub == "add" else 0.0,
                "source": "manual",
                "measured_at": measured_at,
                "note": getattr(a, "note", ""),
            },
            table,
        )
    except fits.FitTableError as exc:
        raise UsageError(str(exc), command="fits") from exc

    existing = next(
        (r for r in manual.load_rows() if manual.scope(r) == manual.scope(row)), None
    )
    if sub == "remove":
        if existing is None:
            raise UsageError(
                "no manual correction row with that scope",
                command="fits",
                remediation=[
                    "`3d fits corrections list --origin manual` shows the rows; remove needs the exact scope."
                ],
            )
        if a.yes:
            manual.remove(row)
        _report_write(a, "remove", existing, None, a.yes, [])
        return 0

    preview = _preview(table, row)
    if a.yes:
        manual.add(row)
    _report_write(a, "add", existing, row, a.yes, preview)
    return 0


def _preview(table: FitTable, row: Correction) -> list[dict[str, Any]]:
    """Clearance before -> after the new row for the fit types and shape it covers."""
    import fits
    import fits_sources
    from fits_sources import manual

    types = [row.fit_type] if row.fit_type else list(table.fit_types)
    shape = row.shape or "planar"
    printer = None if row.printer == "*" else row.printer
    material = None if row.material == "*" else row.material
    # the rows the resolver would use if `row` were stored: every source's rows, minus the manual row it replaces
    kept = [
        r
        for r in fits_sources.collect(fits_sources.Query(printer, material, row.nozzle_mm, row.layer_height_mm))
        if not (r.source == "manual" and manual.scope(r) == manual.scope(row))
    ]
    out = []
    for name in types:
        args = (name, shape, printer, material, row.nozzle_mm, row.layer_height_mm)
        before = fits.resolve(*args, extra_corrections=kept, table=table).clearance_mm
        after = fits.resolve(*args, extra_corrections=[*kept, row], table=table).clearance_mm
        out.append({"fit_type": name, "shape": shape, "before_mm": before, "after_mm": after})
    return out


def _report_write(
    a: argparse.Namespace,
    verb: str,
    old: Correction | None,
    new: Correction | None,
    applied: bool,
    preview: list[dict[str, Any]],
) -> None:
    from fits_sources import manual

    if a.json:
        _emit(
            {
                "command": f"corrections {verb}",
                "applied": applied,
                "store": str(manual.store_path()),
                "previous": old.to_dict() if old else None,
                "row": new.to_dict() if new else None,
                "removed": old.to_dict() if verb == "remove" and old else None,
                "preview": preview,
            }
        )
        return
    row = new or old
    assert row is not None
    scope = f"{row.printer} / {row.material} / nozzle {_mm(row.nozzle_mm)} / layer {_mm(row.layer_height_mm)}"
    if verb == "remove":
        print(
            f"{'removed' if applied else 'would remove'} manual row {scope}: {row.delta_mm:+g} mm"
        )
    else:
        replaced = f" (replaces {old.delta_mm:+g} mm)" if old else ""
        print(
            f"{'added' if applied else 'would add'} manual row {scope}: {row.delta_mm:+g} mm{replaced}"
        )
        for line in preview:
            print(
                f"  {line['fit_type']}/{line['shape']}: {line['before_mm']:+.3f} -> {line['after_mm']:+.3f} mm"
            )
    print(
        f"store: {manual.store_path()}"
        + ("" if applied else "  (dry run, add --yes to write)")
    )


def run(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 1
    if argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    sub, rest = argv[0], argv[1:]
    if sub not in _SUBCOMMANDS:
        raise InvalidArgument("subcommand", sub, list(_SUBCOMMANDS), command="fits")
    if sub != "corrections" and any(t in ("-h", "--help") for t in rest):
        print(USAGE)
        return 0
    return {
        "list": _cmd_list,
        "show": _cmd_show,
        "resolve": _cmd_resolve,
        "apply": _cmd_apply,
        "corrections": _cmd_corrections,
    }[sub](rest)


COMMAND = Command(
    name="fits",
    group="GEOMETRY & EXPORT",
    summary="fit tolerances per fit type (snap/sliding/tight/press/glue) corrected for printer x material",
    usage=USAGE,
    run=run,
)
