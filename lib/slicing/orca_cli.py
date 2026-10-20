"""orca_cli.py — the one place that knows how to drive the Snapmaker Orca / OrcaSlicer CLI headlessly.

WHAT: find the CLI, make a project the CLI can open (`slicing_copy`), run a headless slice
  (`slice_project`) and turn every way that can fail into a named failure with its remediation,
  read the vendor system presets the CLI ships (`SystemPresets`) and export the reference
  `project_settings.config` the CLI itself would write (`reference_project_settings`).
  `KNOWN_QUIRKS` is the catalogue of CLI behaviour that cost hours once; docs/printers/u1-orca-cli.md
  is written from it and a test fails when a quirk id is missing from that page.
WHY: the same knowledge lived in three throw-away scripts (Chekhov u1/verify_gcode.py, babushkin-dom
  gen/verify_slices.py and gen/build_orca_3mf.py) and each re-discovered the same crashes. The
  Chekhov print preparation lost 2h22m partly to them (ticket #431).
INVARIANTS:
  * stdlib + `slicing.slice_warnings`/`errors`/`cli.heavy` only; importable without numpy.
  * A heavy slice runs under `cli.heavy.heavy_prefix()`; this module never builds its own flock, so it
    cannot nest one (a nested flock deadlocked a 25-minute run). `heavy_prefix()` is `[]` inside a lock.
  * `slice_project` never raises for a slice that merely fails: the outcome carries the failure class,
    the reason and the remediation. It raises `MissingDependency` only when no CLI exists at all.
ASSUMPTIONS:
  * CLI 01.10.01.50 (Snapmaker Orca 2.4.0 line). Option spelling is the one the AppImage's `--help` prints.
  * The CLI writes `plate_N.gcode`, `result.json` and the `--export-3mf` file into `--outputdir`.
  * Without GNU `timeout` (macOS) the deadline is enforced by this module and includes the lock wait.
PAST BUGS: see KNOWN_QUIRKS (each entry names its evidence).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from errors import MissingDependency, ThreeDError
from prep.contracts import Verdict
from slicing import slice_warnings

CLI_FALLBACK_VERSION = "01.10.01.50"
INSTALL_HINT = (
    "extract the Snapmaker Orca AppImage to ~/Applications/snapmaker_orca/ "
    "(so that ~/Applications/snapmaker_orca/AppRun exists) or set ORCA=/path/to/the/CLI"
)
_RESOURCES_HINT = (
    "~/Applications/snapmaker_orca/resources/profiles (set ORCA_RES=/path/to/resources/profiles "
    "when the slicer lives elsewhere)"
)
EXIT_TIMEOUT = 124  # GNU `timeout`
_SEGFAULT_CODES = (
    -11,
    139,
)  # killed by SIGSEGV directly / seen through a shell or `timeout`
_PLATE_BLOCK = re.compile(rb"[ \t]*<plate>.*?</plate>[ \t]*\r?\n?", re.DOTALL)
_PLATE_JSON = re.compile(r"Metadata/plate_\d+\.json")
_APP_VERSION = re.compile(r"Snapmaker_Orca-([0-9.]+)")


# --------------------------------------------------------------------------- quirks


@dataclass(frozen=True)
class Quirk:
    """One behaviour of the slicer CLI that is not obvious and cost real time."""

    id: str
    symptom: str
    cause: str
    fix: str
    evidence: str


KNOWN_QUIRKS: tuple[Quirk, ...] = (
    Quirk(
        "plate-block-segfault",
        "The CLI segfaults (rc -11 / 139) while slicing a project it was handed, before any G-code.",
        "A <plate> block in Metadata/model_settings.config makes PartPlate::generate_plate_name_texture() crash in "
        "01.10.01.50 — even for projects the CLI exported itself.",
        "Slice a copy without the <plate> blocks and Metadata/plate_N.json (`slicing_copy`); objects stay on the "
        "single default plate at the same coordinates. Keep the original for the GUI.",
        "babushkin-dom gen/verify_slices.py cli_copy() docstring; Chekhov u1/verify_gcode.py module docstring "
        "'Past bugs'; tests/test_orca_cli.py::test_slicing_copy_drops_only_the_plate_block",
    ),
    Quirk(
        "layer-range-segfault",
        "The CLI segfaults on a project that carries Metadata/layer_config_ranges.xml.",
        "Layer-range metadata (per-height-range settings) crashes the 01.10.01.50 CLI.",
        "Use a per-object Metadata/layer_heights_profile.txt (`object_id=N|z;h;z;h;...`) for variable layer heights.",
        "Chekhov u1/RECIPE.md section 'Что НЕ работает' (line ~101); u1/build_3mf.py docstring 'Past bugs'",
    ),
    Quirk(
        "exclude-triangles",
        "No output at all; the log ends with `calc_exclude_triangles:Unable to create exclude triangles`.",
        "Seen when the CLI ran with `--datadir dd --slice 0` but without `--arrange 0` on a project whose machine "
        "preset it could not find (log: `can not find system preset file .../BBL/machine_full/...`). With "
        "`--datadir` and `--arrange 0` the same line is only a warning: the U1 bed has no exclude area.",
        "Always pass `--datadir WORK/dd --arrange 0 --slice 0` (slice_project does). If the failure persists, the "
        "project lacks a usable machine preset: rebuild it with project_settings.config from the vendor presets.",
        "~/.cache/prep-speedup/tx/U1MixPipeline.txt (smoke slice of u1_mix_tester.3mf, 2026-10-20 17:22); "
        "Chekhov u1/RECIPE.md line ~176 (accepted warning)",
    ),
    Quirk(
        "prime-tower-single-object",
        "Slicing stops with an error about layer heights when a prime tower and variable layer heights are used "
        "with more than one object.",
        "Print.cpp:1745 validates that every object has the same layer-height profile for the prime tower; a "
        "variable layer-height profile is stored per object.",
        "Build a multi-colour model as ONE object made of colour parts (volumes); never one object per colour.",
        "~/xp/snap-orca/src/libslic3r/Print.cpp:1695-1790 (read in U1MixPipeline session); Chekhov u1/RECIPE.md "
        "'Башня + переменные слои'; u1/layer_profile.py docstring",
    ),
    Quirk(
        "mixed-row-convention",
        "A Full Spectrum pattern prints on the wrong heads, or a gate reads another filament than the slicer.",
        "In `mixed_filament_definitions` the pattern tokens 1 and 2 mean the row's component_a / component_b and "
        "3..9 a slot directly (MixedFilament.cpp); the row's a/b are cosmetic for a manual pattern.",
        "Write every row with component_a=1, component_b=2 so each pattern token IS the slot number; "
        "`3d print-preflight`'s head-sequence gate reads it the same way.",
        "Chekhov u1/build_3mf.py mix_row() docstring; u1/RECIPE.md line ~64; u1/colours.py docstring",
    ),
    Quirk(
        "tool-index-offset",
        "G-code changes to T1 although the recipe says slot 2 (or a gate expects the wrong head).",
        "G-code `Tn` is the 0-based filament index of the project, so slot n+1 prints with tool Tn "
        "(slot 1 = T0); the printer screen shows `T N` 1-based.",
        "Convert with tool = slot - 1 whenever G-code and recipes are compared.",
        "Chekhov u1/RECIPE.md 'Подводные камни' (line ~204); u1/verify_gcode.py module docstring 'Assumptions'",
    ),
    Quirk(
        "layer-profile-top-mismatch",
        "A layer_heights_profile.txt is accepted and silently ignored: the layers come out uniform.",
        "Slicing.cpp generate_object_layers drops a profile whose last z does not equal the object height "
        "(tolerance 1e-3 mm).",
        "End the profile at exactly the object's height after placement (`build_project` checks it and refuses).",
        "Chekhov u1/RECIPE.md line ~98; u1/build_3mf.py (assert 'profile top != object height')",
    ),
    Quirk(
        "local-z-dithering",
        "A custom layer-height profile has no effect.",
        "Local-Z dithering (`dithering_local_z_mode`) replaces the object's layer-height profile (PrintObject.cpp).",
        "Set `dithering_local_z_mode` to 0 in the project's process settings.",
        "Chekhov u1/RECIPE.md line ~101; u1/build_3mf.py project_settings() comment",
    ),
    Quirk(
        "arrange-moves-objects",
        "Objects overlap or the slice fails with error -101 after the CLI re-arranged the plate.",
        "Without `--arrange 0` the CLI arranges the objects itself.",
        "Pass `--arrange 0` to keep the layout of the project.",
        "babushkin-dom gen/verify_slices.py (comment at the slice command: the CLI re-arranged the P1S roofs "
        "into each other, error -101)",
    ),
    Quirk(
        "inherits-one-level",
        "A preset written for the CLI loses settings of its grandparent presets.",
        "`--load-settings` resolves one `inherits` level only.",
        "Flatten the whole `inherits` chain of the vendor presets (`SystemPresets.flat`) before handing them to "
        "the CLI.",
        "babushkin-dom gen/build_orca_3mf.py module docstring and flat_profile()",
    ),
    Quirk(
        "application-tag-newer",
        "The CLI refuses to open a project written by us: result.json return_code -24 'Unsupported 3MF version'.",
        "It rejects a project whose `Application` tag carries a version newer than its own build number — even "
        "the `Snapmaker_Orca-2.4.0` tag it writes itself.",
        "Stamp projects with the CLI's build number (`orca_version`, e.g. 01.10.01.50), not the product version.",
        "babushkin-dom gen/build_orca_3mf.py orca_version() docstring",
    ),
    Quirk(
        "no-thumbnails",
        "The sliced G-code has no thumbnail, so the printer screen shows no preview.",
        "The CLI's export_gcode gets a null thumbnail callback.",
        "Add thumbnails afterwards with `3d gcode-thumbnails add FILE`.",
        "babushkin-dom gen/verify_slices.py (comment before add_thumbnails)",
    ),
    Quirk(
        "host-noise-warnings",
        "A clean slice reports warnings: version banner, OpenGL/GLFW/Wayland errors.",
        "The CLI logs its version banner as a warning and, without a display, the thumbnail renderer's GL "
        "initialisation as errors.",
        "Ignore them (`slice_warnings.HOST_NOISE`); `SliceOutcome.warnings` already leaves them out.",
        "lib/slicing/slice_warnings.py HOST_NOISE comment (real 01.10.01.50 `--slice` log)",
    ),
)

QUIRK_BY_ID: dict[str, Quirk] = {q.id: q for q in KNOWN_QUIRKS}


# --------------------------------------------------------------------------- finding the CLI


@dataclass(frozen=True)
class OrcaCli:
    """A usable CLI executable and where it was found (`source`: the env var or location)."""

    path: Path
    source: str
    version: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {"path": str(self.path), "source": self.source, "version": self.version}


def _executable(candidate: str) -> Path | None:
    if not candidate:
        return None
    p = Path(candidate).expanduser()
    return p if p.is_file() and os.access(p, os.X_OK) else None


def orca_version(cli: Path, *, timeout_s: float = 120.0) -> str:
    """The CLI's build number from its `--help` banner. Costs ~7 s on the AppImage (it starts the whole
    runtime), so `find_orca` only does it on request."""
    try:
        r = subprocess.run(
            [str(cli), "--help"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )  # noqa: S603 — the user's own slicer binary
    except (OSError, subprocess.SubprocessError):
        return CLI_FALLBACK_VERSION
    m = _APP_VERSION.search(r.stdout + r.stderr)
    return m.group(1) if m else CLI_FALLBACK_VERSION


def find_orca(
    env: Mapping[str, str] | None = None, *, probe_version: bool = False
) -> OrcaCli | None:
    """The first existing executable of: $ORCA, $SLICER, ~/Applications/snapmaker_orca/AppRun, the macOS
    app, ~/.local/bin/snapmaker-orca, `snapmaker-orca` / `orca-slicer` on PATH. An env var that names a
    missing file is skipped (the next candidate wins), not an error."""
    e = os.environ if env is None else env
    home = Path(e.get("HOME") or Path.home())
    candidates: list[tuple[str, str]] = [
        ("env ORCA", e.get("ORCA", "")),
        ("env SLICER", e.get("SLICER", "")),
        (
            "~/Applications/snapmaker_orca/AppRun",
            str(home / "Applications/snapmaker_orca/AppRun"),
        ),
        (
            "Snapmaker Orca.app",
            "/Applications/Snapmaker Orca.app/Contents/MacOS/Snapmaker Orca",
        ),
        ("~/.local/bin/snapmaker-orca", str(home / ".local/bin/snapmaker-orca")),
    ]
    search_path = e.get("PATH")
    for name in ("snapmaker-orca", "orca-slicer"):
        found = shutil.which(name, path=search_path)
        if found:
            candidates.append((f"PATH {name}", found))
    for source, candidate in candidates:
        path = _executable(candidate)
        if path is not None:
            version = orca_version(path) if probe_version else None
            return OrcaCli(path, source, version)
    return None


def find_resources(
    env: Mapping[str, str] | None = None, cli: OrcaCli | None = None
) -> Path | None:
    """The slicer's `resources/profiles` dir (vendor folders inside): $ORCA_RES, next to the CLI, then the
    AppImage / macOS app locations."""
    e = os.environ if env is None else env
    home = Path(e.get("HOME") or Path.home())
    candidates = [Path(e["ORCA_RES"])] if e.get("ORCA_RES") else []
    if cli is not None:
        candidates.append(cli.path.resolve().parent / "resources" / "profiles")
    candidates += [
        home / "Applications/snapmaker_orca/resources/profiles",
        Path("/Applications/Snapmaker Orca.app/Contents/Resources/profiles"),
    ]
    return next((c for c in candidates if c.is_dir()), None)


# --------------------------------------------------------------------------- slicing copy


def slicing_copy(
    src: str | Path,
    dst: str | Path,
    *,
    set_cfg: Mapping[str, str | list[str]] | None = None,
    drop_layer_profile: bool = False,
) -> None:
    """Write a copy of project `src` that the CLI can slice (quirks `plate-block-segfault`).

    Dropped: every `<plate>` block of Metadata/model_settings.config and every Metadata/plate_N.json (and
    Metadata/layer_heights_profile.txt with `drop_layer_profile`). `set_cfg` overrides keys of
    Metadata/project_settings.config. Every other member keeps its bytes and its zip metadata."""
    with (
        zipfile.ZipFile(src) as zi,
        zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zo,
    ):
        names = zi.namelist()
        if set_cfg and "Metadata/project_settings.config" not in names:
            raise MissingDependency(
                f"Metadata/project_settings.config in {src}",
                install="rebuild the project with `3d prep` / project3mf.build_project",
                degrades="set_cfg overrides cannot be applied",
                command="prep",
            )
        for item in zi.infolist():
            name = item.filename
            data = zi.read(name)
            if name == "Metadata/model_settings.config":
                data = _PLATE_BLOCK.sub(b"", data)
            elif _PLATE_JSON.fullmatch(name) or (
                name == "Metadata/layer_heights_profile.txt" and drop_layer_profile
            ):
                continue
            elif name == "Metadata/project_settings.config" and set_cfg:
                cfg = json.loads(data)
                cfg.update(set_cfg)
                data = json.dumps(cfg, indent=4, ensure_ascii=False).encode()
            zo.writestr(item, data)


# --------------------------------------------------------------------------- failures

FailureClass = Literal[
    "segfault", "timeout", "lock_busy", "exclude_triangles", "return_code", "no_gcode"
]


@dataclass(frozen=True)
class Failure:
    kind: FailureClass
    reason: str
    remediation: tuple[str, ...]
    quirk_ids: tuple[str, ...] = ()


def classify_failure(
    returncode: int,
    log: str,
    result: Mapping[str, Any] | None,
    *,
    has_gcode: bool,
    heavy: bool = False,
    lock_busy_code: int | None = None,
    timed_out: bool = False,
) -> Failure | None:
    """Name why a slice failed, or None when it produced G-code and exited cleanly.

    `result` is the parsed result.json (None when the CLI wrote none). Order matters: a segfault or a
    timeout explains the missing files, so they are tested before the generic classes."""
    res = result or {}
    res_rc = res.get("return_code")
    if returncode in _SEGFAULT_CODES:
        return Failure(
            "segfault",
            f"the CLI crashed (exit {returncode}, SIGSEGV)",
            (
                "Slice a copy without the <plate> block (`slicing_copy`) — never hand the CLI a project that "
                "has one.",
                "If the project has Metadata/layer_config_ranges.xml, remove it and use "
                "Metadata/layer_heights_profile.txt instead.",
            ),
            ("plate-block-segfault", "layer-range-segfault"),
        )
    if timed_out or returncode == EXIT_TIMEOUT:
        return Failure(
            "timeout",
            "the slice did not finish within the time limit",
            (
                "Raise timeout_s, or slice a smaller model; check the slice.log tail for the stage it stuck in.",
            ),
        )
    if (
        heavy
        and lock_busy_code is not None
        and returncode == lock_busy_code
        and not res
    ):
        return Failure(
            "lock_busy",
            "the machine-wide heavy lock stayed busy for the whole bounded wait",
            ("Wait for the running heavy job, or inspect it with `3d heavy status`.",),
        )
    if not has_gcode and "calc_exclude_triangles" in log:
        return Failure(
            "exclude_triangles",
            "the CLI could not create the bed's exclude triangles and wrote nothing",
            (
                "Run the CLI with `--datadir WORK/dd --arrange 0 --slice 0` (slice_project does).",
                "Check the project carries its machine preset (project_settings.config from the vendor presets).",
            ),
            ("exclude-triangles", "arrange-moves-objects"),
        )
    bad_rc = res_rc not in (0, None)
    if bad_rc or returncode != 0:
        code = res_rc if bad_rc else returncode
        detail = str(res.get("error_string") or "").strip()
        tail = "\n".join(log.strip().splitlines()[-5:])
        return Failure(
            "return_code",
            f"the slicer reported failure (return code {code})"
            + (f": {detail}" if detail else (f"; log tail: {tail}" if tail else "")),
            (
                "Read slice.log in the work dir; the last [error] line names the cause.",
                "Error -101 is the plate arrangement: keep `--arrange 0`.",
                "Error -24 'Unsupported 3MF version': the project's Application tag is newer than the CLI's "
                "build number; stamp it with `orca_version`.",
            ),
            ("arrange-moves-objects", "application-tag-newer"),
        )
    if not has_gcode:
        return Failure(
            "no_gcode",
            "the CLI exited 0 but wrote no plate_N.gcode",
            (
                "Check slice.log: the project may have no printable object on the plate (objects outside the "
                "bed or not assigned to a plate).",
            ),
        )
    return None


# --------------------------------------------------------------------------- slice outcome


@dataclass(frozen=True)
class SliceWarning:
    level: str
    message: str
    error_code: str
    source: str  # log | result.json | slice_info

    def to_dict(self) -> dict[str, str]:
        return {
            "level": self.level,
            "message": self.message,
            "error_code": self.error_code,
            "source": self.source,
        }


@dataclass
class SliceOutcome:
    """Result of `slice_project`; satisfies `prep.contracts.StepResult`."""

    verdict: Verdict
    workdir: Path
    gcode: Path | None
    sliced_3mf: Path | None
    log_path: Path
    returncode: int
    failure: Failure | None
    warnings: list[SliceWarning]
    seconds: float
    command: list[str] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.failure is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "ok": self.ok,
            "workdir": str(self.workdir),
            "gcode": str(self.gcode) if self.gcode else None,
            "sliced_3mf": str(self.sliced_3mf) if self.sliced_3mf else None,
            "log": str(self.log_path),
            "returncode": self.returncode,
            "failure": None
            if self.failure is None
            else {
                "kind": self.failure.kind,
                "reason": self.failure.reason,
                "remediation": list(self.failure.remediation),
                "quirks": list(self.failure.quirk_ids),
            },
            "warnings": [w.to_dict() for w in self.warnings],
            "seconds": round(self.seconds, 2),
            "command": list(self.command),
            "result": self.result,
        }

    def summary(self) -> str:
        if self.failure is not None:
            return f"slice FAILED ({self.failure.kind}): {self.failure.reason}"
        n = len(self.warnings)
        warn = f", {n} slicer warning(s)" if n else ""
        name = self.gcode.name if self.gcode else "?"
        return f"sliced {name} in {self.seconds:.0f}s{warn}"


def _is_host_noise(message: str) -> bool:
    return any(p.search(message) for _, p in slice_warnings.HOST_NOISE)


def collect_warnings(workdir: Path, log: str) -> list[SliceWarning]:
    """Warnings from the CLI log, result.json and the sliced project's slice_info, host noise left out,
    each message once (first source wins)."""
    found: dict[str, SliceWarning] = {}

    def add(level: str, message: str, code: str, source: str) -> None:
        if not message.strip() or _is_host_noise(message):
            return
        key = slice_warnings.normalise(message)
        found.setdefault(
            key, SliceWarning(level, " ".join(message.split()), code, source)
        )

    for level, message in slice_warnings.parse_log(log):
        add(level, message, "", "log")
    for source, path, parse in (
        ("result.json", workdir / "result.json", slice_warnings.parse_result_json),
        ("slice_info", workdir / "sliced.3mf", slice_warnings.parse_slice_info),
    ):
        if not path.exists():
            continue
        try:
            entries = parse(path)
        except slice_warnings.SidecarError:
            continue
        for level, message, code in entries:
            add(level, message, code, source)
    return list(found.values())


def _read_result(workdir: Path) -> dict[str, Any] | None:
    path = workdir / "result.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _run(
    argv: list[str], timeout_s: float, *, python_deadline: bool
) -> tuple[int, str, bool]:
    """Run `argv`, return (returncode, merged output, timed_out). With `python_deadline` the whole process
    group is killed after `timeout_s`; otherwise GNU `timeout` in argv enforces it."""
    proc = subprocess.Popen(  # noqa: S603 — argv is built here
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=timeout_s if python_deadline else None)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        return EXIT_TIMEOUT, out or "", True
    return proc.returncode, out or "", False


def slice_project(
    project: str | Path,
    workdir: str | Path,
    *,
    orca: OrcaCli | None = None,
    heavy: bool = True,
    timeout_s: int = 3600,
    set_cfg: Mapping[str, str | list[str]] | None = None,
    drop_layer_profile: bool = False,
) -> SliceOutcome:
    """Slice `project` headlessly in `workdir` (created; stale outputs removed).

    Runs `ORCA --debug 2 --datadir WORK/dd --arrange 0 --slice 0 --outputdir WORK --export-3mf sliced.3mf
    in.3mf` on a `slicing_copy`. `heavy=True` puts the call under the machine-wide heavy lock/cap
    (`cli.heavy.heavy_prefix`, `[]` when the caller already holds it). Writes WORK/slice.log."""
    cli = orca or find_orca()
    if cli is None:
        raise MissingDependency(
            "Snapmaker Orca CLI",
            install=INSTALL_HINT,
            degrades="headless slicing",
            command="prep",
        )
    work = Path(workdir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    for stale in (
        *work.glob("plate_*.gcode"),
        work / "result.json",
        work / "sliced.3mf",
    ):
        stale.unlink(missing_ok=True)
    copy = work / "in.3mf"
    slicing_copy(project, copy, set_cfg=set_cfg, drop_layer_profile=drop_layer_profile)

    prefix: list[str] = []
    lock_busy_code: int | None = None
    if heavy:
        from cli.heavy import EXIT_BUSY, heavy_prefix

        prefix = heavy_prefix()
        lock_busy_code = EXIT_BUSY
    timeout_bin = shutil.which("timeout")
    guard = [timeout_bin, str(timeout_s)] if timeout_bin else []
    command = [
        *prefix,
        *guard,
        str(cli.path),
        "--debug",
        "2",
        "--datadir",
        str(work / "dd"),
        "--arrange",
        "0",
        "--slice",
        "0",
        "--outputdir",
        str(work),
        "--export-3mf",
        "sliced.3mf",
        str(copy),
    ]
    started = time.monotonic()
    returncode, log, timed_out = _run(
        command, timeout_s, python_deadline=timeout_bin is None
    )
    seconds = time.monotonic() - started
    log_path = work / "slice.log"
    log_path.write_text(log, encoding="utf-8")

    result = _read_result(work)
    gcodes = sorted(work.glob("plate_*.gcode"))
    gcode = gcodes[0] if gcodes and gcodes[0].stat().st_size > 0 else None
    failure = classify_failure(
        returncode,
        log,
        result,
        has_gcode=gcode is not None,
        heavy=heavy,
        lock_busy_code=lock_busy_code,
        timed_out=timed_out,
    )
    warnings = collect_warnings(work, log)
    verdict: Verdict = "FAIL" if failure else ("WARN" if warnings else "PASS")
    sliced = work / "sliced.3mf"
    return SliceOutcome(
        verdict=verdict,
        workdir=work,
        gcode=gcode,
        sliced_3mf=sliced if sliced.exists() else None,
        log_path=log_path,
        returncode=returncode,
        failure=failure,
        warnings=warnings,
        seconds=seconds,
        command=command,
        result=result or {},
    )


# --------------------------------------------------------------------------- vendor system presets


PRESET_KINDS = ("machine", "process", "filament")


class SystemPresets:
    """The vendor presets under `resources/profiles/<Vendor>/{machine,process,filament}/*.json`.

    A preset is found by file stem first (the vendor files are named after their preset), by the `name`
    inside the files when no stem matches (4 of 547 Snapmaker files differ). A parent is looked up in the
    child's own vendor folder before the others, so two vendors' `fdm_process_common` do not mix."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._stems: dict[str, dict[str, list[Path]]] = {}
        self._names: dict[str, dict[str, list[Path]]] = {}

    def _kind_dirs(self, kind: str) -> Iterator[Path]:
        for vendor in sorted(self.root.iterdir()):
            d = vendor / kind
            if d.is_dir():
                yield d

    def _stem_index(self, kind: str) -> dict[str, list[Path]]:
        if kind not in self._stems:
            idx: dict[str, list[Path]] = {}
            for d in self._kind_dirs(kind):
                for p in sorted(d.glob("*.json")):
                    idx.setdefault(p.stem, []).append(p)
            self._stems[kind] = idx
        return self._stems[kind]

    def _name_index(self, kind: str) -> dict[str, list[Path]]:
        if kind not in self._names:
            idx: dict[str, list[Path]] = {}
            for paths in self._stem_index(kind).values():
                for p in paths:
                    try:
                        name = json.loads(p.read_text(encoding="utf-8")).get("name")
                    except (OSError, ValueError, AttributeError):
                        continue
                    if isinstance(name, str) and name != p.stem:
                        idx.setdefault(name, []).append(p)
            self._names[kind] = idx
        return self._names[kind]

    def path(self, kind: str, name: str, near: Path | None = None) -> Path | None:
        """File of the preset `name`, preferring the vendor folder of `near`."""
        hits = (
            self._stem_index(kind).get(name) or self._name_index(kind).get(name) or []
        )
        if not hits:
            return None
        if near is not None:
            same = [p for p in hits if p.parent == near.parent]
            if same:
                return same[0]
        return hits[0]

    def load(
        self, kind: str, name: str, near: Path | None = None
    ) -> dict[str, Any] | None:
        p = self.path(kind, name, near)
        if p is None:
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None

    def chain(self, kind: str, name: str) -> list[tuple[Path, dict[str, Any]]]:
        """`name` and its ancestors, child first. KeyError names the first missing preset; ValueError a cycle."""
        out: list[tuple[Path, dict[str, Any]]] = []
        near: Path | None = None
        current: str | None = name
        while current:
            if any(p.stem == current or d.get("name") == current for p, d in out):
                raise ValueError(
                    f"{kind} preset {name!r}: inherits cycle at {current!r}"
                )
            path = self.path(kind, current, near)
            if path is None:
                raise KeyError(current)
            data = json.loads(path.read_text(encoding="utf-8"))
            out.append((path, data))
            near = path
            parent = data.get("inherits")
            current = parent if isinstance(parent, str) and parent else None
        return out

    def flat(self, kind: str, name: str) -> dict[str, Any]:
        """The preset with its whole `inherits` chain merged (child wins)."""
        merged: dict[str, Any] = {}
        for _, data in reversed(self.chain(kind, name)):
            merged.update({k: v for k, v in data.items() if k != "inherits"})
        return merged


def _write_probe_stl(path: Path, cx: float, cy: float) -> None:
    """A 10 x 10 x 2 mm box centred on (cx, cy): the smallest model the CLI will export a project for."""
    x0, x1, y0, y1, z0, z1 = cx - 5, cx + 5, cy - 5, cy + 5, 0.0, 2.0
    v = [(x, y, z) for z in (z0, z1) for y in (y0, y1) for x in (x0, x1)]
    quads = [
        (0, 1, 3, 2),
        (4, 6, 7, 5),
        (0, 4, 5, 1),
        (2, 3, 7, 6),
        (0, 2, 6, 4),
        (1, 5, 7, 3),
    ]
    rows = ["solid probe"]
    for a, b, c, d in quads:
        for tri in ((a, b, c), (a, c, d)):
            rows += ["facet normal 0 0 0", " outer loop"]
            rows += [f"  vertex {v[i][0]:.3f} {v[i][1]:.3f} {v[i][2]:.3f}" for i in tri]
            rows += [" endloop", "endfacet"]
    rows.append("endsolid probe")
    path.write_text("\n".join(rows) + "\n", encoding="ascii")


def reference_project_settings(
    cli: OrcaCli,
    presets: SystemPresets,
    *,
    vendor: str,
    machine: str,
    process: str,
    filaments: Sequence[str],
    bed_centre: tuple[float, float],
    timeout_s: int = 600,
    heavy: bool = True,
) -> dict[str, Any]:
    """`Metadata/project_settings.config` exactly as the CLI writes it for this machine / process / filaments:
    the flattened vendor presets go through `--load-settings` / `--load-filaments` and a probe model is
    exported as a project, whose settings are returned (quirk `inherits-one-level`).

    Raises `MissingDependency` when a named preset is not in `presets`, `ThreeDError` when the CLI wrote
    no project."""
    wanted = [
        ("machine", machine),
        ("process", process),
        *(("filament", f) for f in filaments),
    ]
    with tempfile.TemporaryDirectory(prefix="orca-ref-") as tmp_s:
        tmp = Path(tmp_s)
        files: dict[tuple[str, str], Path] = {}
        for kind, name in wanted:
            if (kind, name) in files:
                continue
            try:
                flat = presets.flat(kind, name)
            except KeyError as exc:
                raise MissingDependency(
                    f"system {kind} preset {name!r} (missing: {exc.args[0]!r})",
                    install=f"install Snapmaker Orca and expose its presets at {_RESOURCES_HINT}",
                    degrades="project_settings.config cannot be generated",
                    command="prep",
                ) from exc
            flat.update({"name": name, "from": "system", "instantiation": "true"})
            f = tmp / f"{kind}_{name}".replace("/", "_")
            f = f.with_suffix(".json")
            f.write_text(json.dumps(flat, indent=1), encoding="utf-8")
            files[kind, name] = f
        probe = tmp / "probe.stl"
        _write_probe_stl(probe, *bed_centre)
        prefix: list[str] = []
        if heavy:
            from cli.heavy import heavy_prefix

            prefix = heavy_prefix()
        command = [
            *prefix,
            str(cli.path),
            "--debug",
            "1",
            "--datadir",
            str(tmp / "dd"),
            "--load-settings",
            f"{files['machine', machine]};{files['process', process]}",
            "--load-filaments",
            ";".join(str(files["filament", f]) for f in filaments),
            "--outputdir",
            str(tmp),
            "--export-3mf",
            "probe.3mf",
            str(probe),
        ]
        returncode, log, timed_out = _run(command, timeout_s, python_deadline=True)
        out = tmp / "probe.3mf"
        if not out.exists():
            why = "timed out" if timed_out else f"exit {returncode}"
            if "libOpenGL" in log or "libGLU" in log:
                raise MissingDependency(
                    "host OpenGL runtime libraries (libOpenGL.so.0, libGLU)",
                    install="sudo apt-get install libopengl0 libglu1-mesa; on a box without sudo point ORCA at a wrapper that sets LD_LIBRARY_PATH (e.g. ORCA=~/.local/bin/snapmaker-orca)",
                    degrades="headless slicing and project_settings generation",
                    command="prep",
                )
            raise ThreeDError(
                f"the Orca CLI wrote no reference project ({why})",
                command="prep",
                remediation=[log[-1500:].strip() or "no output"],
            )
        with zipfile.ZipFile(out) as z:
            cfg = json.loads(z.read("Metadata/project_settings.config"))
    if not isinstance(cfg, dict):
        raise ThreeDError(
            "the Orca CLI's project_settings.config is not a JSON object", command="prep"
        )
    return cfg
