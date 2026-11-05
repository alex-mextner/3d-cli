"""convert_plan.py — the route and collision policy of `3d convert` (pure stdlib).

WHAT: the arg-independent decisions every `3d convert` route shares:
  - which format an input path is, and which format an output path names (an
    extensionless output is FILLED from the conversion: a single stl/3mf input keeps
    its own format, anything else becomes the 3mf hub format);
  - which input→output routes exist (png renders exactly one model; the retarget
    options need one .3mf in and .3mf out);
  - the collision contract: an output that would overwrite one of its OWN inputs is
    never a failure — it auto-appends a free suffix (`a.stl` → `a-2.stl`); any other
    existing target refuses (exit 2) with the exact ready-to-run command that uses a
    free name, unless `--force`;
  - staging + `commit`: the result is written to a hidden staging name beside the
    target that KEEPS the target's extension (every tool infers its output format from
    the extension), then moved on only after re-checking that the target still does
    not exist — the file may have appeared since planning.

WHY: this is policy, not mesh work — it must be unit-testable without openscad or
trimesh, and shared by every route (the openscad export, the mesh tool, the retarget
delegation and the render route) so the collision contract is identical everywhere.

INVARIANTS: `commit` never overwrites an existing target without `force` (on POSIX it
hardlinks the staged file, which refuses atomically; filesystems without hardlinks fall
back to `os.replace` right after the existence re-check). `resolve_output` never
returns a path that would clobber an input without `force`.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Sequence

from errors import GateFailure, UsageError

# What `3d convert` reads: dimension_model (scad through OpenSCAD's colour-preserving
# 3MF export, 3mf/obj natively, stl via trimesh) plus a pxr (usd-core) reader for usdz.
READ_FORMATS: tuple[str, ...] = ("scad", "stl", "3mf", "usdz", "obj")
# What it writes: the four formats the repo's libs emit (png is a render).
WRITE_FORMATS: tuple[str, ...] = ("stl", "3mf", "usdz", "png")

# Retarget flags `3d convert` passes through to `3d retarget` on a 3mf→3mf conversion.
RETARGET_FLAGS: tuple[str, ...] = (
    "--keep",
    "--margin",
    "--process",
    "--filament",
    "--preset",
    "--set",
    "--preset-dir",
    "--include-non-printable",
    "-v",
    "--verbose",
)


def extension_of(path: str) -> str:
    """The lower-cased extension of `path` without its dot ("" when there is none)."""
    return os.path.splitext(path)[1].lower().lstrip(".")


def input_format(path: str) -> str:
    """The read format `path` names; a structured error when it is not readable."""
    fmt = extension_of(path)
    if fmt not in READ_FORMATS:
        raise UsageError(
            f"cannot read '{path}': unknown input format '{fmt or '(no extension)'}'",
            command="convert",
            remediation=[
                "accepted input formats: " + ", ".join(f".{f}" for f in READ_FORMATS),
                "3d import <model> builds an OpenSCAD wrapper for other mesh formats",
            ],
        )
    return fmt


def output_format(out: str, input_formats: Sequence[str]) -> str:
    """The write format `out` names, filled from the conversion when it has no extension."""
    fmt = extension_of(out)
    if fmt in WRITE_FORMATS:
        return fmt
    if fmt:
        raise UsageError(
            f"cannot write '{out}': unknown output format '.{fmt}'",
            command="convert",
            remediation=[
                "accepted output formats: " + ", ".join(f".{f}" for f in WRITE_FORMATS),
                "for a .scad wrapper use: 3d import <model>",
            ],
        )
    # No extension: keep the input's own format when converting into it is meaningful
    # (an stl merge/copy, a 3mf repackage/retarget); everything else takes the 3mf hub.
    single = input_formats[0] if len(set(input_formats)) == 1 else ""
    return single if single in ("stl", "3mf") else "3mf"


def check_route(
    input_formats: Sequence[str], out_format: str, *, retarget_opts: bool
) -> None:
    """Refuse routes the libs cannot serve, with the accepted shape in the remediation."""
    if retarget_opts:
        ok = (
            len(input_formats) == 1
            and input_formats[0] == "3mf"
            and out_format == "3mf"
        )
        if not ok:
            raise UsageError(
                "the retarget options (--to/--set/--preset/…) need exactly one .3mf "
                f"input and a .3mf output (got {len(input_formats)} input(s), "
                f"output .{out_format})",
                command="convert",
                remediation=[
                    "3d convert project.3mf project-u1.3mf --to u1",
                    "3d retarget project.3mf --to u1",
                ],
            )
    if out_format == "png" and len(input_formats) != 1:
        raise UsageError(
            f"a png output renders ONE model, got {len(input_formats)} inputs",
            command="convert",
            remediation=[
                "3d convert model.scad shot.png   # render one model",
                "3d convert a.stl b.stl merged.3mf # merge several inputs into one file",
            ],
        )


def _suffixed(path: str, n: int) -> str:
    base, ext = os.path.splitext(path)
    return f"{base}-{n}{ext}"


def free_output(path: str) -> str:
    """`path`, or the first `stem-2`, `stem-3`, … that does not exist."""
    n = 2
    while os.path.exists(_suffixed(path, n)):
        n += 1
    return _suffixed(path, n)


def command_line(inputs: Sequence[str], out: str) -> str:
    """The exact ready-to-run command that converts `inputs` into `out`."""
    return " ".join(shlex.quote(a) for a in ("3d", "convert", *inputs, out))


def refusal(inputs: Sequence[str], out: str) -> UsageError:
    """The error for an existing target without --force: a free-name command to run."""
    return UsageError(
        f"output '{out}' already exists (use --force to overwrite)",
        command="convert",
        remediation=[
            command_line(inputs, free_output(out)),
            f"or overwrite it explicitly: {command_line(inputs, out)} --force",
        ],
    )


def _same_path(out: str, path: str) -> bool:
    """True when `out` and `path` name the same file (realpath when it exists)."""
    try:
        return os.path.samefile(out, path)
    except OSError:  # one of them does not exist yet: compare the normalized paths
        return os.path.abspath(out) == os.path.abspath(path)


def _same_file(out: str, inputs: Sequence[str]) -> bool:
    return any(_same_path(out, path) for path in inputs)


def resolve_output(inputs: Sequence[str], out: str, *, force: bool) -> tuple[str, str]:
    """(final path, note). Never clobbers an input; an existing target needs `force`."""
    target = os.path.abspath(out)
    if _same_file(target, inputs) and not force:
        alt = free_output(target)
        return alt, f"note: output would overwrite the input; writing {alt} instead"
    if os.path.exists(target) and not force:
        raise refusal(inputs, target)
    return target, ""


def staging_path(final: str) -> str:
    """A hidden staging name beside `final` that KEEPS its extension (format inference)."""
    directory, name = os.path.split(final)
    stem = os.path.splitext(name)[0]
    staged = f".{stem}.stage-{os.getpid()}{os.path.splitext(name)[1]}"
    return os.path.join(directory, staged)


def commit(staged: str, final: str, *, force: bool, inputs: Sequence[str]) -> None:
    """Move the staged result onto `final`, re-checking that it may overwrite.

    The target may have appeared since planning, so existence is checked again here.
    Without `force` the move refuses atomically (hardlink first on POSIX, which fails
    with FileExistsError when the target exists); with `force` it replaces.
    """
    if not os.path.isfile(staged):
        raise GateFailure(
            f"conversion produced no output at {staged}", command="convert"
        )
    if not force and os.path.exists(final):
        os.unlink(staged)
        raise refusal(inputs, final)
    if force:
        os.replace(staged, final)
        return
    try:
        os.link(staged, final)  # atomic; refuses an existing target
    except FileExistsError:
        os.unlink(staged)
        raise refusal(inputs, final) from None
    except OSError:
        os.replace(staged, final)  # no hardlinks here: re-checked just above
    else:
        os.unlink(staged)
