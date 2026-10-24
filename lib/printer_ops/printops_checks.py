"""printops_checks.py — PrintOps material gates of `3d print-preflight` (issue #164, plan §9.1).

WHAT: the G-code's per-tool facts (slicer footer + body temperatures) checked against what
  PrintOps knows about the target printer — fleet topology (fleet/printers.yaml) and, through
  3d-api, the spool bound to each slot with its filament data, remaining-weight interval and
  drying state. One preflight line per check (per-tool breakdown under it):
    printer_profile_match  `; printer_model` is the target printer's model
    nozzle_compat          `; nozzle_diameter` = the head's nozzle; `; required_nozzle_HRC`
                           <= its material's nominal hardness; abrasive filament (Spoolman
                           `abrasive`, CF/GF/... markers) only on policy `abrasive_ok` materials
    filament_temp_range    `; nozzle_temperature[_initial_layer]`, the body's M104/M109 per tool
                           and the bed (plate-specific footer temps, M140/M190) inside the loaded
                           filament's nozzle/bed_temp_min..max
    loaded_filament_match  bound spool's material = `; filament_type` (FAIL), colour CIEDE2000
                           vs `; filament_colour` (WARN/FAIL by project), vendor = `; filament_vendor` (WARN).
                           A slice whose recorded slicer warnings say the project carries no
                           filament colour ("no filament colors found in projects", sidecar
                           `3d slice-warnings`) has only the profile's default colour in the footer:
                           the colour is UNVERIFIED, never graded against the spool
    slot_binding_known     every used slot has a spool in the ledger (WARN: usage not booked)
    filament_sufficient    `; filament used [g]` + purge + margin vs the ledger interval: FAIL
                           above its high end, WARN "weigh" inside it
    drying_due             the bound spool's drying assessment (drying.assess): wet = hard, due = WARN
    maintenance_due        the head is usable (fleet), open faults / due maintenance (when 3d-api
                           reports them — #166)
    enclosure_material     materials of policy `enclosure_material` on an open/partial printer
    plate_material         `; curr_bed_type` is a plate on hand; no bad material/plate pair
    preset_match           `; printer_settings_id` / `; print_settings_id` are the fleet entry's
                           `slicer: {machine, process}` user presets (#293: a G-code sliced with
                           Orca's stock presets prints settings nobody calibrated), and the
                           footer's default/outer wall acceleration equal the resolved process
                           preset when the presets are on this machine (Orca and user preset
                           folders, slicing.calibration_gen); when the spool bound to a used tool
                           names a filament preset (#294), `; filament_settings_id` equals it and
                           `; filament_flow_ratio` / `; pressure_advance` / `; enable_pressure_advance`
                           equal its resolved values (#506)
  calibration_current (#167) is added when the calibration engine lands.

LEVELS: PASS / WARN / FAIL / UNVERIFIED. A check whose input is missing (no 3d-api, no spool
  bound, filament without a temperature window) is UNVERIFIED and says what is missing — never
  PASS (plan §9.1). A hard finding is FAIL under policy level `block`, WARN under `warn`; `off`
  hides the check (fleet/policy.yaml `gates:`, lib/printops/gates_policy.py).

STATE SOURCES (printops.gates STATE): `--printops-api URL` / $PRINTOPS_API / printops.yaml →
  live `GET /v1/gates/state`; `--offline` → the snapshot file (`3d printops gates snapshot`);
  neither → fleet facts only (spool checks UNVERIFIED). A given in-process state (3d-api) skips
  loading.

INVARIANTS: read-only — never writes to a printer, Spoolman or 3d-api; stdlib + pyyaml (preset_match
  reads slicer preset JSON through slicing.calibration_gen, imported lazily).
"""

from __future__ import annotations

import datetime
import math
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from printer_ops.preflight import FAIL, PASS, UNVERIFIED, WARN, Check
from printops.color import hex_delta_e

RANK = {PASS: 0, UNVERIFIED: 1, WARN: 2, FAIL: 3}
# Orca compatibility values, not a measurement of the installed nozzle. Ruby has no
# documented value here and must remain unknown when a positive HRC minimum is required.
# https://github.com/OrcaSlicer/OrcaSlicer/wiki/printer_basic_information_accessory
NOZZLE_HARDNESS_HRC: dict[str, int] = {
    "brass": 2,
    "stainless": 20,
    "hardened": 55,
    "tungsten": 85,
}
_KEY_LINE = re.compile(r"^;\s*([A-Za-z_][A-Za-z0-9_ \[\]]*?)\s*=\s*(.*)$")
_TOOL_LINE = re.compile(r"^T(\d+)\s*(?:;.*)?$")
_TEMP_CMD = re.compile(r"^(M10[49]|M1[49]0)\b([^;]*)")
_PARAM = re.compile(r"\b([ST])(-?\d+(?:\.\d+)?)")
# Orca `curr_bed_type` → (footer temp key, first-layer key, fleet plate types that are this plate)
BED_TYPES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "Cool Plate": ("cool_plate_temp", "cool_plate_temp_initial_layer", ("cool_plate",)),
    "Engineering Plate": (
        "eng_plate_temp",
        "eng_plate_temp_initial_layer",
        ("engineering_plate",),
    ),
    "High Temp Plate": (
        "hot_plate_temp",
        "hot_plate_temp_initial_layer",
        ("high_temp_plate", "smooth_pei"),
    ),
    "Smooth PEI Plate": (
        "hot_plate_temp",
        "hot_plate_temp_initial_layer",
        ("smooth_pei", "high_temp_plate"),
    ),
    "Textured PEI Plate": (
        "textured_plate_temp",
        "textured_plate_temp_initial_layer",
        ("textured_pei",),
    ),
    "Textured Cool Plate": (
        "textured_cool_plate_temp",
        "textured_cool_plate_temp_initial_layer",
        (),
    ),
    "Supertack Plate": (
        "supertack_plate_temp",
        "supertack_plate_temp_initial_layer",
        (),
    ),
}
_STRING_LISTS = (
    "filament_type",
    "filament_colour",
    "filament_vendor",
    "filament_settings_id",
)


# -- G-code facts -----------------------------------------------------------------------------
@dataclass
class JobFacts:
    path: str
    config: dict[str, str]
    used_tools: list[int]
    body_nozzle_max: dict[
        int, float
    ]  # highest M104/M109 S per tool (explicit T or after `Tn`)
    body_bed_max: float | None  # highest M140/M190 S
    calibration_print: bool = (
        False  # `3d calibrate gen` wrote it (its presets_stamp line)
    )

    def str_list(self, key: str) -> list[str | None]:
        raw = self.config.get(key)
        if raw is None:
            return []
        return [v.strip().strip('"') or None for v in raw.split(";")]

    def num_list(self, key: str) -> list[float | None]:
        raw = self.config.get(key)
        if raw is None:
            return []
        out: list[float | None] = []
        for v in raw.replace(";", ",").split(","):
            try:
                n = float(v)
            except ValueError:  # noqa: PERF203 — a non-number entry is unknown, keep the rest
                out.append(None)
                continue
            # NaN/inf compare false both ways and would slip past every range check: unknown
            out.append(n if math.isfinite(n) else None)
        return out

    @staticmethod
    def per_tool(values: list[Any], tool: int) -> Any:
        """Entry `tool`; a one-value list applies to every tool (Orca collapses equal values)."""
        if tool < len(values):
            return values[tool]
        return values[0] if len(values) == 1 else None

    def text(self, key: str, tool: int) -> str | None:
        v: str | None = self.per_tool(self.str_list(key), tool)
        return v

    def num(self, key: str, tool: int) -> float | None:
        v: float | None = self.per_tool(self.num_list(key), tool)
        return v

    def grams(self, tool: int) -> float | None:
        """`; filament used [g]`, else mm × density × cross-section."""
        g = self.num("filament used [g]", tool)
        if g is not None:
            return g
        mm, rho, dia = (
            self.num(k, tool)
            for k in ("filament used [mm]", "filament_density", "filament_diameter")
        )
        if mm is None or rho is None or dia is None:
            return None
        return mm / 10 * rho * math.pi * (dia / 20) ** 2  # cm × g/cm³ × cm²


def read_job(path: Path) -> JobFacts:
    """One pass: slicer config comments (first occurrence wins), tool changes, heater commands."""
    config: dict[str, str] = {}
    tool_lines: set[int] = set()
    current: int | None = None
    nozzle: dict[int, float] = {}
    bed: float | None = None
    calibration = False
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            c = raw[:1]
            if c == ";":
                m = _KEY_LINE.match(raw.rstrip("\n"))
                if m:
                    config.setdefault(m.group(1).strip(), m.group(2).strip())
                elif raw.startswith(CALIBRATION_STAMP):
                    calibration = True
            elif c == "T":
                t = _TOOL_LINE.match(raw.strip())
                if t:
                    current = int(t.group(1))
                    tool_lines.add(current)
            elif c == "M":
                m = _TEMP_CMD.match(raw)
                if not m:
                    continue
                params = dict(_PARAM.findall(m.group(2)))
                if "S" not in params or float(params["S"]) <= 0:
                    continue
                s = float(params["S"])
                if m.group(1) in ("M140", "M190"):
                    bed = max(bed or 0.0, s)
                    continue
                tool = int(params["T"]) if "T" in params else current
                if (
                    tool is not None
                ):  # an untargeted heat-up before the first `Tn` is start-macro territory
                    nozzle[tool] = max(nozzle.get(tool, 0.0), s)
    job = JobFacts(str(path), config, [], nozzle, bed, calibration)
    amounts = job.num_list("filament used [mm]") or job.num_list("filament used [g]")
    # an all-zero/unreadable usage footer names no tool: fall back to the tool changes seen
    job.used_tools = (
        [i for i, a in enumerate(amounts) if a] or sorted(tool_lines) or [0]
    )
    return job


def _hex(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip().lstrip("#")
    return (
        "#" + v[:6].upper()
        if re.fullmatch(r"[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", v)
        else None
    )


# -- findings -> checks -----------------------------------------------------------------------
@dataclass(frozen=True)
class Finding:
    """One verdict of a check, before (`grade`) and after (`status`) the policy level.

    grade: hard (a violation: FAIL under `block`, WARN under `warn`) | soft (WARN) | ok | unknown.
    kind names the sub-check (type, colour, vendor, diameter, abrasive, nozzle_temp, bed_temp, ...)
    so a caller can single out serious ones (D5: loaded_filament_match/type)."""

    check: str
    grade: str
    text: str
    tool: int | None = None
    slot: str | None = None
    kind: str = ""
    status: str = ""


@dataclass
class Result:
    checks: list[Check] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    printer: str | None = None
    source: str = ""


def _status(grade: str, level: str) -> str:
    if grade == "hard":
        return FAIL if level == "block" else WARN
    return {"soft": WARN, "ok": PASS}.get(grade, UNVERIFIED)


def _summarise(name: str, findings: list[Finding]) -> Check:
    """One line per check: the worst finding (identical verdicts on several slots merged into
    "T1, T4: text"); every finding under it when they differ."""
    worst = max(findings, key=lambda f: RANK[f.status])
    groups: dict[tuple[str, str], list[Finding]] = {}
    for f in findings:
        groups.setdefault((f.status, f.text), []).append(f)
    merged = [
        (status, text, ", ".join(_where(f) for f in fs if _where(f)))
        for (status, text), fs in groups.items()
    ]
    shown = [m for m in merged if m[0] == worst.status]
    _, text, where = shown[0]
    detail = (
        (f"{where}: " if where else "")
        + text
        + (f" (+{len(shown) - 1} more)" if len(shown) > 1 else "")
    )
    lines = (
        [f"{s:<10} {w + ': ' if w else ''}{t}" for s, t, w in merged]
        if len(merged) > 1
        else []
    )
    return Check(name, worst.status, detail, lines)


def _where(f: Finding) -> str:
    if f.slot:
        return f.slot
    return f"tool {f.tool}" if f.tool is not None else ""


# -- state source -----------------------------------------------------------------------------
@dataclass(frozen=True)
class PrintopsContext:
    """How `run_checks` finds the PrintOps state. `printer` None → inferred from the G-code's
    `; printer_model` (the one fleet printer of that model). `state` given → used as is."""

    printer: str | None = None
    api: str | None = None
    token: str | None = None
    offline: bool = False
    snapshot: Path | None = None
    state: Mapping[str, Any] | None = None
    accept: frozenset[str] = frozenset()  # checks whose FAIL is accepted (ACCEPTABLE); needs `reason`
    reason: str = ""


# Checks `--accept NAME --reason TEXT` may downgrade from FAIL to WARN: a mismatch the user knowingly
# prints (other presets on purpose, #506). The reason is printed under the WARN.
ACCEPTABLE = frozenset({"preset_match"})


def context_from_options(
    printer: str | None,
    axis_limits: str | None,
    *,
    api: str | None,
    offline: bool,
    snapshot: str | None,
    command: str,
    accept: str | None = None,
    reason: str | None = None,
) -> PrintopsContext:
    """The context of a CLI run: --printer (else --axis-limits NAME; a numeric X0:X1,Y0:Y1 spec
    names no printer), --printops-api else $PRINTOPS_API / printops.yaml, --offline + snapshot."""
    from errors import UsageError
    from printops.client import ApiConfigError, load_config

    if offline and api:
        raise UsageError(
            "--offline reads the snapshot and never calls 3d-api: drop --printops-api or --offline",
            command=command,
        )
    try:
        env_api, env_token = (None, None) if offline else load_config()
    except ApiConfigError as e:
        raise UsageError(str(e), command=command) from e
    named = printer or (axis_limits if axis_limits and ":" not in axis_limits else None)
    accepted = frozenset(x.strip() for x in (accept or "").split(",") if x.strip())
    if accepted - ACCEPTABLE:
        raise UsageError(
            f"--accept names {', '.join(sorted(accepted - ACCEPTABLE))}; accepted: {', '.join(sorted(ACCEPTABLE))}",
            command=command,
        )
    if accept is not None and not accepted:
        raise UsageError("--accept needs a check name", command=command)
    if accepted and not (reason or "").strip():
        raise UsageError(
            "--accept needs --reason TEXT: why this mismatch is printed on purpose", command=command
        )
    if reason is not None and not accepted:
        raise UsageError("--reason belongs to --accept CHECK", command=command)
    return PrintopsContext(
        accept=accepted,
        reason=(reason or "").strip(),
        printer=named,
        api=None if offline else (api or env_api),
        token=env_token,
        offline=offline,
        snapshot=Path(snapshot) if snapshot else None,
    )


@dataclass
class _Source:
    state: Mapping[str, Any]
    check: Check


def _age_h(taken_at: Any, now: datetime.datetime) -> float | None:
    try:
        ts = datetime.datetime.fromisoformat(str(taken_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, (now - ts).total_seconds() / 3600)


def _load_source(
    ctx: PrintopsContext,
    printer: str,
    fleet_state: Mapping[str, Any],
    max_age_h: float,
    now: datetime.datetime,
) -> _Source:
    from printops import gates
    from printops.client import ApiClient, ApiError

    if ctx.state is not None:
        return _Source(
            ctx.state,
            Check("printops", PASS, f"state from {ctx.state.get('source', '?')}"),
        )
    if ctx.offline:
        path = ctx.snapshot or gates.snapshot_path()
        try:
            snap = gates.read_snapshot(path)
        except gates.SnapshotError as e:
            return _Source(
                fleet_state, Check("printops", UNVERIFIED, f"{e}; fleet facts only")
            )
        state = snap["printers"].get(printer)
        if not isinstance(state, dict):
            return _Source(
                fleet_state,
                Check(
                    "printops",
                    UNVERIFIED,
                    f"snapshot {path} has no printer {printer}; fleet facts only",
                ),
            )
        age = _age_h(snap.get("taken_at"), now)
        when = (
            f"{age:.1f} h old"
            if age is not None
            else f"taken_at {snap.get('taken_at')!r} unreadable"
        )
        if age is None or age > max_age_h:
            return _Source(
                state,
                Check(
                    "printops",
                    WARN,
                    f"snapshot {path} is {when} (> {max_age_h:g} h): "
                    "refresh with `3d printops gates snapshot`",
                ),
            )
        return _Source(state, Check("printops", PASS, f"snapshot {path}, {when}"))
    if ctx.api:
        try:
            state = ApiClient(ctx.api, ctx.token).get(
                "/v1/gates/state", printer=printer
            )
        except ApiError as e:
            return _Source(
                fleet_state,
                Check(
                    "printops", UNVERIFIED, f"3d-api {ctx.api}: {e}; fleet facts only"
                ),
            )
        return _Source(state, Check("printops", PASS, f"live from 3d-api {ctx.api}"))
    return _Source(
        fleet_state,
        Check(
            "printops",
            UNVERIFIED,
            "no PrintOps state (fleet facts only): pass --printops-api URL / set PRINTOPS_API, "
            "or --offline with a `3d printops gates snapshot`",
        ),
    )


# -- checks -----------------------------------------------------------------------------------
@dataclass
class _Ctx:
    job: JobFacts
    state: Mapping[str, Any]
    policy: Any  # printops.gates_policy.GatesPolicy (imported lazily)
    origin: str
    slicer: Mapping[str, str] = field(default_factory=dict)  # fleet `slicer:` presets
    model: str = ""  # the fleet printer's model (resolves its vendor's Orca presets)
    colourless: bool = False  # the slicer found no filament colour in the project (footer = profile default)

    @property
    def known(self) -> bool:
        return self.state.get("bindings") == "ledger"

    @property
    def unbound_reason(self) -> str:
        """Why a used slot has no spool to check: unbound in the ledger, or no ledger at all."""
        return (
            "no spool bound to this slot"
            if self.known
            else "spool not known (no PrintOps state)"
        )

    def tools(
        self,
    ) -> Iterator[tuple[int, str, Mapping[str, Any] | None, Mapping[str, Any] | None]]:
        """(tool, slot label, tool entry, head) for every tool the G-code uses."""
        entries = {int(t["tool"]): t for t in self.state.get("tools") or []}
        heads = {h["label"]: h for h in self.state.get("heads") or []}
        for t in self.job.used_tools:
            entry = entries.get(t)
            slot = str(entry["slot"]) if entry else f"tool {t}"
            head = heads.get(str(entry.get("head"))) if entry else None
            yield t, slot, entry, head

    def material(self, t: int, spool: Mapping[str, Any] | None) -> str | None:
        """The loaded spool's material when known, else the G-code's filament_type."""
        name = (spool or {}).get("material") or self.job.text("filament_type", t)
        canon: str | None = self.policy.material(name)
        return canon


def _f(
    check: str,
    grade: str,
    text: str,
    t: int | None = None,
    slot: str | None = None,
    kind: str = "",
) -> Finding:
    return Finding(check, grade, text, t, slot, kind)


def check_printer_profile(c: _Ctx) -> list[Finding]:
    name = "printer_profile_match"
    want = str(c.state.get("model") or "")
    got = c.job.config.get("printer_model")
    if not got:
        settings = c.job.config.get("printer_settings_id", "")
        if want and settings.casefold().startswith(want.casefold()):
            return [_f(name, "ok", f"printer_settings_id {settings!r} is a {want}")]
        return [
            _f(
                name,
                "unknown",
                "the G-code has no `; printer_model`"
                + (f" (printer_settings_id {settings!r})" if settings else ""),
            )
        ]
    if got.strip().casefold() == want.casefold():
        return [_f(name, "ok", f"sliced for {got} = {c.state.get('printer')}")]
    return [
        _f(
            name,
            "hard",
            f"sliced for {got!r}, but {c.state.get('printer')} is a {want!r}",
            kind="model",
        )
    ]


def _abrasive(
    c: _Ctx, t: int, spool: Mapping[str, Any] | None
) -> tuple[bool | None, str]:
    """(abrasive?, why). Invalid or absent evidence stays unknown unless a positive source wins."""
    flag = (spool or {}).get("abrasive")
    if flag is True:
        return True, "Spoolman filament abrasive"
    invalid: list[str] = []
    for src, value in (
        ("spool material", (spool or {}).get("material")),
        ("spool name", (spool or {}).get("name")),
        ("G-code type", c.job.text("filament_type", t)),
    ):
        if value is not None and not isinstance(value, str):
            invalid.append(f"invalid {src} (expected text or null, got {type(value).__name__})")
        elif c.policy.is_abrasive_material(value):
            return True, f"{src} {value!r}"
    if flag is not None and not isinstance(flag, bool):
        invalid.append("invalid spool abrasive flag (expected boolean or null)")
    if invalid:
        return None, "; ".join(invalid)
    if flag is False:
        return False, "Spoolman filament not abrasive"
    if spool is not None:
        return (
            False,
            f"{spool.get('label')}: no abrasive marker (Spoolman `abrasive` not set)",
        )
    return None, "no spool data to tell whether the filament is abrasive"


def check_nozzle(c: _Ctx) -> list[Finding]:
    name = "nozzle_compat"
    out: list[Finding] = []
    for t, slot, entry, head in c.tools():
        nozzle = (head or {}).get("nozzle")
        spool = (entry or {}).get("spool")
        if not nozzle:
            out.append(
                _f(
                    name,
                    "unknown",
                    "nozzle not known (fleet/printers.yaml heads[].nozzle)",
                    t,
                    slot,
                )
            )
            continue
        want = c.job.num("nozzle_diameter", t)
        if want is not None and abs(want - float(nozzle["diameter_mm"])) > 0.01:
            out.append(
                _f(
                    name,
                    "hard",
                    f"G-code nozzle_diameter {want:g} mm, installed {nozzle['diameter_mm']:g} mm",
                    t,
                    slot,
                    "diameter",
                )
            )
            continue
        material = nozzle.get("material")
        required_hrc = c.job.num("required_nozzle_HRC", t)
        hardness_note = ""
        if required_hrc is not None and required_hrc > 0:
            hardness = (
                NOZZLE_HARDNESS_HRC.get(material)
                if isinstance(material, str)
                else None
            )
            if hardness is None:
                out.append(
                    _f(
                        name,
                        "unknown",
                        f"G-code requires nozzle HRC {required_hrc:g}, but installed "
                        f"{material or 'unknown'} nozzle hardness is not known",
                        t,
                        slot,
                        "hardness",
                    )
                )
            elif required_hrc > hardness:
                out.append(
                    _f(
                        name,
                        "hard",
                        f"G-code requires nozzle HRC {required_hrc:g}, installed "
                        f"{material} nozzle has nominal HRC {hardness:g}",
                        t,
                        slot,
                        "hardness",
                    )
                )
                continue
            else:
                hardness_note = (
                    f"; G-code requires nozzle HRC {required_hrc:g}, "
                    f"installed nominal HRC {hardness:g}"
                )
        abrasive, why = _abrasive(c, t, spool)
        why += hardness_note
        dia = f"{float(nozzle['diameter_mm']):g} mm"
        if abrasive is None:
            out.append(_f(name, "unknown", f"{dia} ok; {why}", t, slot, "abrasive"))
        elif not abrasive:
            out.append(
                _f(
                    name,
                    "ok",
                    f"{dia} {material or ''} nozzle; {why}".replace("  ", " "),
                    t,
                    slot,
                )
            )
        elif material is None:
            out.append(
                _f(
                    name,
                    "unknown",
                    f"abrasive ({why}) and the nozzle material is not known",
                    t,
                    slot,
                    "abrasive",
                )
            )
        elif material in c.policy.abrasive_ok:
            out.append(
                _f(name, "ok", f"abrasive ({why}) on a {material} nozzle", t, slot)
            )
        else:
            out.append(
                _f(
                    name,
                    "hard",
                    f"abrasive filament ({why}) on a {material} nozzle; needs "
                    f"{'/'.join(c.policy.abrasive_ok)}",
                    t,
                    slot,
                    "abrasive",
                )
            )
    return out


def _window(
    spool: Mapping[str, Any], lo_key: str, hi_key: str
) -> tuple[float | None, float | None]:
    def num(v: Any) -> float | None:
        return (
            float(v)
            if isinstance(v, (int, float)) and not isinstance(v, bool)
            else None
        )

    return num(spool.get(lo_key)), num(spool.get(hi_key))


def _bed_temps(job: JobFacts, t: int) -> list[tuple[str, float]]:
    bed_type = job.config.get("curr_bed_type", "")
    keys = BED_TYPES.get(bed_type, ("", "", ()))[:2] if bed_type else ()
    out = [
        (k, v)
        for k in (*keys, "bed_temperature", "first_layer_bed_temperature")
        if k and (v := job.num(k, t)) is not None
    ]
    if job.body_bed_max is not None:
        out.append(("M140/M190", job.body_bed_max))
    return out


def check_temps(c: _Ctx) -> list[Finding]:
    name = "filament_temp_range"
    out: list[Finding] = []
    for t, slot, entry, _head in c.tools():
        spool = (entry or {}).get("spool")
        if spool is None:
            out.append(_f(name, "unknown", c.unbound_reason, t, slot))
            continue
        problems: list[tuple[str, str]] = []
        unknown: list[str] = []
        nmin, nmax = _window(spool, "nozzle_temp_min", "nozzle_temp_max")
        nozzle = [
            (k, v)
            for k in ("nozzle_temperature", "nozzle_temperature_initial_layer")
            if (v := c.job.num(k, t)) is not None
        ]
        if t in c.job.body_nozzle_max:
            nozzle.append((f"M104/M109 T{t}", c.job.body_nozzle_max[t]))
        if nmin is None or nmax is None:
            unknown.append("nozzle window (filament nozzle_temp_min/max not set)")
        else:
            for key, v in nozzle:
                # body heat-ups also park heads at standby temps: only too HOT counts there
                if v > nmax or (v < nmin and not key.startswith("M10")):
                    problems.append(
                        ("nozzle_temp", f"{key} {v:g} °C outside {nmin:g}–{nmax:g}")
                    )
        bmin, bmax = _window(spool, "bed_temp_min", "bed_temp_max")
        if bmin is None or bmax is None:
            unknown.append("bed window (filament bed_temp_min/max not set)")
        else:
            for key, v in _bed_temps(c.job, t):
                if not bmin <= v <= bmax:
                    problems.append(
                        ("bed_temp", f"bed {key} {v:g} °C outside {bmin:g}–{bmax:g}")
                    )
        who = spool.get("label") or f"spool #{spool.get('id')}"
        if problems:
            out.append(
                _f(
                    name,
                    "hard",
                    f"{who}: " + "; ".join(p for _, p in problems),
                    t,
                    slot,
                    problems[0][0],
                )
            )
        elif unknown:
            out.append(_f(name, "unknown", f"{who}: " + ", ".join(unknown), t, slot))
        else:
            top = max((v for _, v in nozzle), default=None)
            out.append(
                _f(
                    name,
                    "ok",
                    f"{who}: nozzle ≤{top:g} °C in {nmin:g}–{nmax:g}, bed in {bmin:g}–{bmax:g}"
                    if top is not None
                    else f"{who}: within its windows",
                    t,
                    slot,
                )
            )
    return out


_NO_FILAMENT_COLOURS = "no filament colors found in projects"


def project_has_no_filament_colour(origin: Path, gcode: Path) -> bool:
    """True when the slice's recorded slicer warnings (NAME.slice-warnings.json next to `origin`,
    written by `3d slice-warnings record`) contain Orca's "no filament colors found in projects":
    the project (an STL slice, a 3MF without per-filament colours) declares none, so the footer's
    `; filament_colour` is the filament profile's default and says nothing about the spool to load.
    A missing, unreadable or stale sidecar (another G-code's hash) proves nothing: False."""
    from slicing import slice_warnings as sw

    side = sw.sidecar_path(origin)
    try:
        data = sw.load(side)
        if data.get("gcode_sha256") != sw.sha256_file(gcode):
            return False
    except (sw.SidecarError, OSError):
        return False
    return any(sw.normalise(w.message) == _NO_FILAMENT_COLOURS for w in sw.warnings_of(data))


def check_loaded(c: _Ctx) -> list[Finding]:
    name = "loaded_filament_match"
    out: list[Finding] = []
    rule = c.policy.colour_rule(c.origin)
    for t, slot, entry, _head in c.tools():
        spool = (entry or {}).get("spool")
        if spool is None:
            out.append(_f(name, "unknown", c.unbound_reason, t, slot))
            continue
        who = spool.get("label") or f"spool #{spool.get('id')}"
        want_type = c.policy.material(c.job.text("filament_type", t))
        got_type = c.policy.material(spool.get("material"))
        if want_type and got_type and want_type != got_type:
            out.append(
                _f(
                    name,
                    "hard",
                    f"G-code {want_type}, loaded {who} is {got_type}",
                    t,
                    slot,
                    "type",
                )
            )
            continue
        notes: list[str] = []
        grade, kind = "ok", ""
        if not (want_type and got_type):
            grade, kind = "unknown", "type"
            notes.append(
                "type not known on " + ("the spool" if want_type else "the G-code")
            )
        want_c = _hex(c.job.text("filament_colour", t))
        got_c = _hex(spool.get("measured_hex")) or _hex(spool.get("color_hex"))
        if c.colourless:
            if grade == "ok":
                grade, kind = "unknown", "colour"
            notes.append(
                f"colour not checked: the project declares no filament colour (G-code {want_c or '?'} "
                "is the slicer profile default)"
            )
        elif want_c and got_c:
            de = hex_delta_e(want_c, got_c)
            msg = f"ΔE {de:.1f} G-code {want_c} vs loaded {got_c}"
            if de > rule.fail_delta_e:
                grade, kind = "hard", "colour"
                notes.insert(0, f"{msg} > {rule.fail_delta_e:g}")
            elif de > rule.warn_delta_e:
                grade, kind = ("soft", "colour") if grade != "hard" else (grade, kind)
                notes.append(f"{msg} > {rule.warn_delta_e:g}")
            else:
                notes.append(msg)
        elif grade == "ok":
            grade, kind = "unknown", "colour"
            notes.append(
                "colour not known on " + ("the spool" if want_c else "the G-code")
            )
        want_v, got_v = c.job.text("filament_vendor", t), spool.get("vendor")
        if want_v and got_v and want_v.casefold() != str(got_v).casefold():
            if grade in ("ok", "unknown"):
                grade, kind = "soft", "vendor"
            notes.append(
                f"vendor: G-code profile {want_v}, loaded {got_v} (check flow/PA calibration)"
            )
        lot = f", lot {spool['lot_nr']}" if spool.get("lot_nr") else ""
        out.append(
            _f(
                name,
                grade,
                f"{who}{lot}: {want_type or '?'}; " + "; ".join(notes),
                t,
                slot,
                kind,
            )
        )
    return out


def check_binding(c: _Ctx) -> list[Finding]:
    name = "slot_binding_known"
    out: list[Finding] = []
    for t, slot, entry, _head in c.tools():
        spool = (entry or {}).get("spool")
        if not c.known:
            out.append(
                _f(
                    name,
                    "unknown",
                    "spool bindings not known (no PrintOps state)",
                    t,
                    slot,
                )
            )
        elif spool is None:
            out.append(
                _f(
                    name,
                    "soft",
                    "no spool bound in the ledger: usage will not be booked; "
                    f"bind one: 3d printops spool load SPOOL_ID {c.state.get('printer')}/{slot}",
                    t,
                    slot,
                )
            )
        elif spool.get("conflict"):
            ids = ", ".join(f"#{i}" for i in spool["conflict"])
            out.append(
                _f(
                    name,
                    "soft",
                    f"{spool.get('label')} and {ids} are all bound here: unload the wrong one",
                    t,
                    slot,
                )
            )
        else:
            out.append(_f(name, "ok", str(spool.get("label")), t, slot))
    return out


def check_sufficient(c: _Ctx) -> list[Finding]:
    name = "filament_sufficient"
    p = c.policy
    out: list[Finding] = []
    for t, slot, entry, _head in c.tools():
        spool = (entry or {}).get("spool")
        grams = c.job.grams(t)
        if grams is None:
            out.append(
                _f(
                    name,
                    "unknown",
                    "the G-code has no `; filament used [g]` for this tool",
                    t,
                    slot,
                )
            )
            continue
        need = grams + p.purge_g_per_tool + max(p.margin_g, grams * p.margin_pct / 100)
        rem = (spool or {}).get("remaining")
        if spool is None or not isinstance(rem, dict):
            out.append(
                _f(name, "unknown", f"needs {need:.0f} g; {c.unbound_reason}", t, slot)
            )
            continue
        lo, hi = float(rem.get("lo_g") or 0), float(rem.get("hi_g") or 0)
        weigh = f" — weigh it: 3d printops spool measure {spool.get('id')} GROSS_G"
        if not rem.get("anchored") and spool.get("spoolman_remaining_g") is None:
            # never weighed and Spoolman has no remaining weight either: unknown, not empty
            out.append(
                _f(
                    name,
                    "soft",
                    f"{spool.get('label')}: needs {need:.0f} g, remaining not known"
                    + weigh,
                    t,
                    slot,
                    "weigh",
                )
            )
            continue
        have = (
            f"{rem.get('mid_g'):g} ± {rem.get('sigma_g'):g} g"
            if rem.get("anchored")
            else f"≤{hi:g} g (never weighed)"
        )
        msg = f"{spool.get('label')}: needs {need:.0f} g ({grams:.1f} + purge + margin), has {have}"
        if need > hi:
            out.append(_f(name, "hard", msg, t, slot, "short"))
        elif need > lo:
            out.append(_f(name, "soft", msg + weigh, t, slot, "weigh"))
        else:
            out.append(_f(name, "ok", msg, t, slot))
    return out


def check_drying(c: _Ctx) -> list[Finding]:
    name = "drying_due"
    out: list[Finding] = []
    for t, slot, entry, _head in c.tools():
        spool = (entry or {}).get("spool")
        if spool is None:
            out.append(_f(name, "unknown", c.unbound_reason, t, slot))
            continue
        d = spool.get("drying") or {}
        state = d.get("state")
        reasons = "; ".join(d.get("reasons") or []) or d.get("error") or ""
        text = (
            f"{spool.get('label')}: {state or 'not assessed'} ({d.get('class', '?')})"
            + (f" — {reasons}" if reasons else "")
        )
        grade = {
            "wet": "hard",
            "due": "soft",
            "drying": "soft",
            "fresh": "ok",
            "ok": "ok",
        }.get(str(state), "unknown")
        out.append(_f(name, grade, text, t, slot, str(state or "")))
    return out


def check_maintenance(c: _Ctx) -> list[Finding]:
    name = "maintenance_due"
    # added by #166: {open_faults: [{head, symptom}], due: [{task, head}]}
    maint = c.state.get("maintenance")
    out: list[Finding] = []
    for t, slot, _entry, head in c.tools():
        if head is None:
            out.append(_f(name, "unknown", "head not in fleet/printers.yaml", t, slot))
            continue
        if not head.get("usable", True):
            out.append(
                _f(
                    name,
                    "hard",
                    f"head {head['label']} is not usable: {head.get('reason') or '-'}",
                    t,
                    slot,
                    "unusable",
                )
            )
            continue
        if not isinstance(maint, dict):
            out.append(
                _f(
                    name,
                    "unknown",
                    f"head {head['label']} usable; no maintenance records in PrintOps yet",
                    t,
                    slot,
                )
            )
            continue
        faults = [
            f
            for f in maint.get("open_faults") or []
            if f.get("head") in (None, head["label"])
        ]
        due = [
            d for d in maint.get("due") or [] if d.get("head") in (None, head["label"])
        ]
        if faults:
            out.append(
                _f(
                    name,
                    "hard",
                    "open fault: " + "; ".join(str(f.get("symptom")) for f in faults),
                    t,
                    slot,
                    "fault",
                )
            )
        elif due:
            out.append(
                _f(
                    name,
                    "soft",
                    "maintenance due: " + "; ".join(str(d.get("task")) for d in due),
                    t,
                    slot,
                )
            )
        else:
            out.append(
                _f(
                    name,
                    "ok",
                    f"head {head['label']}: no open faults, nothing due",
                    t,
                    slot,
                )
            )
    return out


def check_enclosure(c: _Ctx) -> list[Finding]:
    name = "enclosure_material"
    enclosure = c.state.get("enclosure")
    want = set(c.policy.enclosure_materials)
    hot = []
    for t, slot, entry, _head in c.tools():
        m = c.material(t, (entry or {}).get("spool"))
        if m and want & set(re.split(r"[^A-Z0-9]+", m)):
            hot.append(f"{slot} {m}")
    if not hot:
        return [_f(name, "ok", "no material that needs an enclosure")]
    if enclosure is None:
        return [
            _f(
                name,
                "unknown",
                f"{', '.join(hot)} wants an enclosure; the printer's is not known",
            )
        ]
    if enclosure == "full":
        return [_f(name, "ok", f"{', '.join(hot)} in a full enclosure")]
    grade = "hard" if enclosure == "none" else "soft"
    return [
        _f(
            name,
            grade,
            f"{', '.join(hot)} on a printer with enclosure: {enclosure} (warping, fumes)",
            kind="enclosure",
        )
    ]


def check_plate(c: _Ctx) -> list[Finding]:
    name = "plate_material"
    bed = c.job.config.get("curr_bed_type")
    plates = c.state.get("plates")
    if not bed:
        return [_f(name, "unknown", "the G-code has no `; curr_bed_type`")]
    fleet_types = BED_TYPES.get(bed, ("", "", ()))[2]
    if not fleet_types:
        return [
            _f(name, "unknown", f"plate {bed!r} has no fleet plate type to compare")
        ]
    if plates is None:
        return [
            _f(name, "unknown", f"sliced for {bed}; the printer's plates are not known")
        ]
    on_hand = [p for p in fleet_types if p in plates]
    if not on_hand:
        return [
            _f(
                name,
                "hard",
                f"sliced for {bed}, printer has {', '.join(plates) or 'no plates'}",
                kind="plate",
            )
        ]
    out = [
        _f(name, "hard", f"{m} on {bed}: {rule.why}", t, slot, "pairing")
        for t, slot, entry, _head in c.tools()
        if (m := c.material(t, (entry or {}).get("spool")))
        for rule in c.policy.plate_rules
        if m in rule.materials and set(on_hand) & set(rule.plates)
    ]
    return out or [
        _f(name, "ok", f"{bed} ({'/'.join(on_hand)}) on hand, no bad pairing")
    ]


# Process values the footer must carry from the fleet's process preset: Orca writes the
# preset's value unless it was edited in the slicer without saving (the G-code then prints
# settings the preset — and every calibration made with it — does not have).
PRESET_VALUES = ("default_acceleration", "outer_wall_acceleration")
# Filament values the footer must carry from the spool binding's filament preset (#506): flow and
# pressure advance edited in the slicer and not saved, or taken from another spool's preset, print
# differently from the profile every calibration of this spool was made with.
FILAMENT_PRESET_VALUES = (
    "filament_flow_ratio",
    "pressure_advance",
    "enable_pressure_advance",
)
# `3d calibrate gen` (slicing.calibration_gen.presets_stamp) marks its G-code with this line: a
# calibration print sets its own accelerations on purpose, so only the preset names are compared.
CALIBRATION_STAMP = "; 3d calibrate presets: "


def _process_preset(model: str, name: str) -> tuple[dict[str, Any] | None, str]:
    """The resolved process preset `name` (system + user presets), or None and why not."""
    return _resolved_preset(model, "process", name)


def _resolved_preset(
    model: str, kind: str, name: str
) -> tuple[dict[str, Any] | None, str]:
    """The resolved `kind` (process | filament) preset `name`, or None and why not."""
    from cli.env import find_slicer
    from errors import ThreeDError
    from slicing.calibration_gen import (
        GenError,
        ProfileIndex,
        presets_for,
        profile_roots,
        user_preset_roots,
    )

    slicer = find_slicer()
    try:
        try:
            vendor = presets_for(model).vendor
        except GenError:  # no calibration presets for this model: user presets only
            vendor = ""
        index = ProfileIndex(
            profile_roots(slicer[1] if slicer else None), user_preset_roots()
        )
        return index.flat(vendor, kind, name), ""
    except (
        GenError,
        ThreeDError,
    ) as e:  # ThreeDError: a broken slicer preset_folder config
        return None, str(e)


def _preset_num(preset: dict[str, Any], key: str) -> float | None:
    """A preset option as a finite number; None when absent or not a number."""
    from slicing.calibration_gen import GenError, _first

    try:
        return _first(preset, key)
    except GenError:
        return None


def check_preset_match(c: _Ctx) -> list[Finding]:
    # The spool's filament preset needs no fleet `slicer:` mapping: its names are in the binding.
    return _printer_process_findings(c) + _filament_findings(c)


def _filament_findings(c: _Ctx) -> list[Finding]:
    """The G-code's filament_settings_id and flow / pressure advance against the filament preset
    the spool bound to each used tool names (`slicer_filament_preset`, #294). A spool without one
    is not compared (nothing says what it prints with)."""
    name = "preset_match"
    out: list[Finding] = []
    for t, slot, entry, _head in c.tools():
        spool = (entry or {}).get("spool") or {}
        want = spool.get("slicer_filament_preset")
        if not (isinstance(want, str) and want):
            continue
        got = c.job.text("filament_settings_id", t)
        if not got:
            out.append(
                _f(name, "unknown", "the G-code has no `; filament_settings_id`", t, slot)
            )
            continue
        if got != want:
            out.append(
                _f(
                    name,
                    "hard",
                    f"sliced with filament preset {got!r}, the spool on {slot} prints with {want!r}",
                    t,
                    slot,
                    "filament_settings_id",
                )
            )
            continue
        out.append(_f(name, "ok", f"filament preset {got!r}", t, slot))
        if c.job.calibration_print:
            out.append(
                _f(
                    name,
                    "ok",
                    "calibration print: it varies flow and pressure advance, values not compared",
                    t,
                    slot,
                )
            )
            continue
        preset, why = _resolved_preset(c.model, "filament", want)
        if preset is None:
            out.append(_f(name, "ok", f"filament values not compared: {why}", t, slot))
            continue
        for key in FILAMENT_PRESET_VALUES:
            want_n = _preset_num(preset, key)
            if want_n is None:
                continue
            got_v = c.job.num(key, tool=t)
            if got_v is None:
                out.append(
                    _f(
                        name,
                        "unknown",
                        f"the G-code has no `; {key}` (preset {want_n:g})",
                        t,
                        slot,
                    )
                )
            elif math.isclose(got_v, want_n, rel_tol=1e-6, abs_tol=1e-9):
                out.append(_f(name, "ok", f"{key} {got_v:g} = preset", t, slot))
            else:
                out.append(
                    _f(
                        name,
                        "hard",
                        f"{key} {got_v:g}, the filament preset {want!r} has {want_n:g} "
                        "(edited in the slicer and not saved?)",
                        t,
                        slot,
                        key,
                    )
                )
    return out


def _printer_process_findings(c: _Ctx) -> list[Finding]:
    name = "preset_match"
    if not c.slicer:
        return [
            _f(
                name,
                "unknown",
                f"no `slicer: {{machine, process}}` for {c.state.get('printer')} in "
                "fleet/printers.yaml: nothing to compare the G-code's presets with",
            )
        ]
    out: list[Finding] = []
    process_name_matched = False
    for key, footer in (
        ("machine", "printer_settings_id"),
        ("process", "print_settings_id"),
    ):
        want = c.slicer.get(key)
        if not want:
            out.append(_f(name, "unknown", f"fleet `slicer:` has no {key} preset"))
            continue
        got = (c.job.config.get(footer) or "").strip().strip('"')
        if not got:
            out.append(_f(name, "unknown", f"the G-code has no `; {footer}`"))
        elif got == want:
            out.append(_f(name, "ok", f"{key} preset {got!r}"))
            if key == "process":
                process_name_matched = True
        else:
            out.append(
                _f(
                    name,
                    "hard",
                    f"sliced with {key} preset {got!r}, the printer prints with {want!r}",
                    kind=key,
                )
            )
    process = c.slicer.get("process")
    if not (process and process_name_matched):
        return out
    # The names are the primary input; a value comparison that cannot run is a note under them
    # (docs/commands/print-preflight.md), not an UNVERIFIED gate.
    if c.job.calibration_print:
        out.append(
            _f(
                name,
                "ok",
                "calibration print: it sets its own accelerations, values not compared",
            )
        )
        return out
    preset, why = _process_preset(c.model, process)
    if preset is None:
        out.append(_f(name, "ok", f"process values not compared: {why}"))
        return out
    for key in PRESET_VALUES:
        want_n = _preset_num(preset, key)
        if want_n is None:
            continue
        got_v = c.job.num(key, tool=0)
        if got_v is None:
            out.append(
                _f(name, "unknown", f"the G-code has no `; {key}` (preset {want_n:g})")
            )
        elif math.isclose(got_v, want_n, rel_tol=1e-6):
            out.append(_f(name, "ok", f"{key} {got_v:g} = preset"))
        else:
            out.append(
                _f(
                    name,
                    "hard",
                    f"{key} {got_v:g}, the process preset {process!r} has {want_n:g} "
                    "(edited in the slicer and not saved?)",
                    kind=key,
                )
            )
    return out


CHECKS = {
    "printer_profile_match": check_printer_profile,
    "nozzle_compat": check_nozzle,
    "filament_temp_range": check_temps,
    "loaded_filament_match": check_loaded,
    "slot_binding_known": check_binding,
    "filament_sufficient": check_sufficient,
    "drying_due": check_drying,
    "maintenance_due": check_maintenance,
    "enclosure_material": check_enclosure,
    "plate_material": check_plate,
    "preset_match": check_preset_match,
}


def _target(job: JobFacts, fleet: Any, printer: str | None) -> tuple[Any, str]:
    """(fleet printer or None, how it was chosen / why there is none)."""
    if fleet is None:
        return None, "no fleet registry (fleet/printers.yaml, or THREED_FLEET is empty)"
    if printer:
        fp = fleet.printers.get(printer)
        return (
            (fp, "target printer")
            if fp
            else (None, f"{printer} is not in {fleet.path}")
        )
    model = job.config.get("printer_model")
    if not model:
        return None, "no --printer and the G-code has no `; printer_model` to find one"
    matches = [
        p
        for p in fleet.printers.values()
        if p.model.casefold() == model.strip().casefold()
    ]
    if len(matches) == 1:
        return matches[0], f"the G-code's printer_model {model!r}"
    if matches:
        return (
            None,
            f"several fleet printers are a {model}: pass --printer ({', '.join(p.name for p in matches)})",
        )
    return None, f"no fleet printer is a {model!r}: pass --printer NAME"


def run_checks(
    gcode: Path,
    ctx: PrintopsContext,
    *,
    origin: Path | None = None,
    now: datetime.datetime | None = None,
) -> Result:
    """Every PrintOps gate for `gcode`. `origin` is the real location of a snapshot copy (the
    colour rule's project glob matches it)."""
    from printops.fleet import FleetError, load_fleet
    from printops.gates import fleet_state
    from printops.policy import PolicyError, section

    moment = now or datetime.datetime.now(datetime.timezone.utc)
    result = Result()
    try:
        policy = section("gates")
    except PolicyError as e:
        result.checks.append(Check("printops", FAIL, f"gates policy unusable: {e}"))
        return result
    job = read_job(gcode)
    try:
        fleet = load_fleet()
    except FleetError as e:
        fleet, fp, why = None, None, e.message
    else:
        fp, why = _target(job, fleet, ctx.printer)
    if fleet is None or fp is None:
        result.checks.append(
            Check("printops", UNVERIFIED, f"material gates not run: {why}")
        )
        return result
    result.printer = fp.name
    src = _load_source(ctx, fp.name, fleet_state(fp), policy.snapshot_max_age_h, moment)
    src.check.detail = f"{fp.name} ({why}); {src.check.detail}"
    result.checks.append(src.check)
    result.source = src.check.detail
    c = _Ctx(
        job,
        src.state,
        policy,
        str((origin or gcode).resolve()),
        slicer=dict(fp.slicer),
        model=fp.model,
        colourless=project_has_no_filament_colour(origin or gcode, gcode),
    )
    for name, fn in CHECKS.items():
        level = policy.level(name, fp.name)
        if level == "off":
            continue
        raw = fn(c)
        if name in ctx.accept and any(f.grade == "hard" for f in raw):
            level = "warn"
            raw = [*raw, _f(name, "ok", f"accepted with --accept {name}: {ctx.reason}")]
        findings = [
            Finding(
                f.check,
                f.grade,
                f.text,
                f.tool,
                f.slot,
                f.kind,
                _status(f.grade, level),
            )
            for f in raw
        ]
        result.findings.extend(findings)
        result.checks.append(_summarise(name, findings))
    return result
