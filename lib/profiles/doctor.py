"""doctor.py — static checks of slicer user presets (the repo's `profiles/<slicer>/` and the slicer's own).

WHAT: `diagnose()` reads user presets (`{machine,process,filament}/*.json` + `.info`) and reports, per
  finding, a severity, a code, what is wrong and the exact fix:
    parent-missing / parent-elsewhere / inherits-cycle   `inherits` must name an existing preset (user
        preset of the same kind in the checked trees, else a vendor system preset);
    layer-height-range   effective layer_height inside the machine's min..max_layer_height (Orca refuses a
        slice otherwise), and min <= max wherever both are given;
    adaptive-clamped     a preset meant for adaptive layers (`adaptive_layer_height=1`, or named
        `... adaptive A-B ...`) whose range the machine limits would clamp (ERROR when the name declares the
        range, WARN for the default band);
    machine-band / machine-keys   a U1 machine preset: nozzle/limit keys present, one entry per toolhead
        (4), a "(0.4 nozzle)" preset really 0.4, and limits that cover the requested 0.08..0.32 band;
    info-*   the `.info` next to a preset: present, not orphaned, `base_id` = the parent system preset's
        `setting_id`, `setting_id` equal to the JSON's.
WHY: a Chekhov print was prepared on a process preset whose parent did not exist in the repo and a machine
  whose limits clamped the adaptive 0.08-0.32 band; both surfaced only at slice time, hours in (#431).
INVARIANTS: stdlib only; reads files, never writes. Without the vendor presets (`resources/profiles`) the
  checks that need them are skipped and ONE warning says so — never an error for what cannot be known.
ASSUMPTIONS: a user preset's file stem is its name (the slicer saves it so); system presets are found by
  `orca_cli.SystemPresets` (file stem, then inner name).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from profiles import preset as preset_fmt
from profiles import slicers
from slicing import orca_cli

Severity = Literal["ERROR", "WARN", "INFO"]
KINDS = slicers.KINDS
DEFAULT_BAND = (0.08, 0.32)
U1_TOOLHEADS = 4
_ADAPTIVE_NAME = re.compile(
    r"adaptive\s+(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)", re.IGNORECASE
)
_NOZZLE_NAME = re.compile(r"\((\d+(?:\.\d+)?) nozzle\)")


@dataclass(frozen=True)
class Finding:
    severity: Severity
    code: str
    where: str
    message: str
    fix: str

    def line(self) -> str:
        return f"{self.severity:5} {self.where}: {self.message} [{self.code}] -> fix: {self.fix}"

    def to_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "where": self.where,
            "message": self.message,
            "fix": self.fix,
        }


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)

    @property
    def errors(self) -> int:
        return sum(f.severity == "ERROR" for f in self.findings)

    @property
    def exit_code(self) -> int:
        return 1 if self.errors else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "errors": self.errors,
            "warnings": sum(f.severity == "WARN" for f in self.findings),
            "findings": [f.to_dict() for f in self.findings],
        }


@dataclass
class Preset:
    kind: str
    name: str
    path: Path
    data: dict[str, Any]
    info: dict[str, str] | None


@dataclass
class Tree:
    """One directory of user presets: the repo's `profiles/<slicer>` or the slicer's `user/<folder>`."""

    label: str
    root: Path
    presets: dict[str, dict[str, Preset]] = field(
        default_factory=lambda: {k: {} for k in KINDS}
    )
    unreadable: list[tuple[Path, str]] = field(default_factory=list)
    orphan_infos: list[Path] = field(default_factory=list)

    @classmethod
    def load(cls, label: str, root: Path) -> Tree:
        tree = cls(label, root)
        for kind in KINDS:
            kdir = root / kind
            files = sorted(kdir.glob("*.json")) + sorted((kdir / "base").glob("*.json"))
            for path in files:
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        raise ValueError("the top level is not a JSON object")
                except (OSError, ValueError) as exc:
                    tree.unreadable.append((path, str(exc)))
                    continue
                info_path = path.with_suffix(".info")
                info = (
                    preset_fmt.parse_info(info_path.read_text(encoding="utf-8"))
                    if info_path.is_file()
                    else None
                )
                tree.presets[kind][path.stem] = Preset(
                    kind, path.stem, path, data, info
                )
            known = {p.path.with_suffix(".info") for p in tree.presets[kind].values()}
            tree.orphan_infos += [
                i
                for i in sorted(kdir.glob("*.info"))
                + sorted((kdir / "base").glob("*.info"))
                if i not in known
                and i.with_suffix(".json") not in {p for p, _ in tree.unreadable}
            ]
        return tree


def _floats(value: object) -> list[float] | None:
    items = value if isinstance(value, list) else [value]
    try:
        return [float(str(v)) for v in items]
    except ValueError:
        return None


class _Context:
    """Name resolution over the trees under check and the vendor system presets."""

    def __init__(
        self, trees: list[Tree], system: orca_cli.SystemPresets | None
    ) -> None:
        self.trees, self.system = trees, system

    def user(self, kind: str, name: str, prefer: Tree) -> tuple[Tree, Preset] | None:
        for tree in [prefer, *[t for t in self.trees if t is not prefer]]:
            p = tree.presets[kind].get(name)
            if p is not None:
                return tree, p
        return None

    def effective(
        self, kind: str, preset: Preset, tree: Tree
    ) -> tuple[dict[str, Any], str | None, str | None, str | None]:
        """(merged settings, error code, missing/cyclic name, nearest system ancestor's setting_id).

        Error code is None, 'parent-missing', 'parent-unverifiable' (system parent, no vendor presets) or
        'inherits-cycle'; the settings then hold what could be merged."""
        chain: list[dict[str, Any]] = []
        seen: set[str] = {preset.name}
        cur_tree, cur = tree, preset
        while True:
            chain.append(cur.data)
            parent = cur.data.get("inherits")
            if not isinstance(parent, str) or not parent:
                return _merge(chain), None, None, None
            if parent in seen:
                return _merge(chain), "inherits-cycle", parent, None
            seen.add(parent)
            found = self.user(kind, parent, cur_tree)
            if found is not None:
                cur_tree, cur = found
                continue
            if self.system is None:
                return _merge(chain), "parent-unverifiable", parent, None
            try:
                sys_chain = self.system.chain(kind, parent)
            except KeyError as exc:
                return _merge(chain), "parent-missing", str(exc.args[0]), None
            except ValueError:
                return _merge(chain), "inherits-cycle", parent, None
            flat = self.system.flat(kind, parent)
            setting_id = next(
                (str(d["setting_id"]) for _, d in sys_chain if d.get("setting_id")),
                None,
            )
            return {**flat, **_merge(chain)}, None, None, setting_id


def _merge(chain: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for data in reversed(chain):
        merged.update({k: v for k, v in data.items() if k != "inherits"})
    return merged


def _limits(machine: dict[str, Any]) -> tuple[float, float] | None:
    """The layer-height band every extruder of the machine supports: (max of mins, min of maxes)."""
    lo, hi = (
        _floats(machine.get("min_layer_height")),
        _floats(machine.get("max_layer_height")),
    )
    if not lo or not hi:
        return None
    return max(lo), min(hi)


def _is_u1(name: str, flat: dict[str, Any]) -> bool:
    return "Snapmaker U1" in name or flat.get("printer_model") == "Snapmaker U1"


def diagnose(
    trees: list[Tree],
    system: orca_cli.SystemPresets | None,
    *,
    band: tuple[float, float] = DEFAULT_BAND,
    system_note: str = "",
) -> Report:
    """Check every preset of `trees`. `system` is None when the vendor presets are not installed."""
    report = Report(checked=[t.label for t in trees])
    ctx = _Context(trees, system)
    out = report.findings
    if system is None:
        out.append(
            Finding(
                "WARN",
                "system-presets-missing",
                ", ".join(t.label for t in trees),
                "vendor system presets not found: parents and limits that come from them were not verified"
                + (f" ({system_note})" if system_note else ""),
                "install the slicer, or set ORCA_RES to its resources/profiles dir",
            )
        )
    for tree in trees:
        for path, why in tree.unreadable:
            out.append(
                Finding(
                    "ERROR",
                    "preset-unreadable",
                    f"{tree.label} {path.parent.name}/{path.name}",
                    f"not a readable preset: {why}",
                    "fix the JSON, or delete the file; the slicer writes one JSON object per preset",
                )
            )
        for info in tree.orphan_infos:
            out.append(
                Finding(
                    "WARN",
                    "info-orphan",
                    f"{tree.label} {info.parent.name}/{info.name}",
                    "`.info` without a preset JSON",
                    f"delete {info}, or restore {info.with_suffix('.json').name}",
                )
            )
        for kind in KINDS:
            for preset in tree.presets[kind].values():
                _check_preset(report, ctx, tree, preset, band)
    return report


def _check_preset(
    report: Report, ctx: _Context, tree: Tree, preset: Preset, band: tuple[float, float]
) -> None:
    out = report.findings
    where = f"{tree.label} {preset.kind}/{preset.path.name}"
    flat, err, missing, sys_id = ctx.effective(preset.kind, preset, tree)
    parent = preset.data.get("inherits")
    if err == "parent-missing":
        out.append(
            Finding(
                "ERROR",
                "parent-missing",
                where,
                f'inherits "{missing}", which exists neither as a {preset.kind} user preset nor as a vendor preset',
                f'restore the preset "{missing}" (copy it into this folder) or set "inherits" in '
                f"{preset.path.name} to an existing {preset.kind} preset",
            )
        )
    elif err == "inherits-cycle":
        out.append(
            Finding(
                "ERROR",
                "inherits-cycle",
                where,
                f'the inherits chain loops back at "{missing}"',
                f'break the loop: set "inherits" in {preset.path.name} to a vendor preset',
            )
        )
    elif isinstance(parent, str) and parent and ctx.user(preset.kind, parent, tree):
        owner, _ = ctx.user(preset.kind, parent, tree) or (tree, preset)
        if owner is not tree:
            out.append(
                Finding(
                    "WARN",
                    "parent-elsewhere",
                    where,
                    f'parent "{parent}" exists only in {owner.label}, not in this tree',
                    f'run `3d profiles sync` so "{parent}" exists in both places',
                )
            )
    _check_info(report, tree, preset, where, sys_id)
    if preset.kind == "process":
        _check_process(report, ctx, tree, preset, where, flat, band)
    elif (
        preset.kind == "machine" and err is None
    ):  # an unresolved parent would fake "missing keys"
        _check_machine(report, preset, where, flat, band)


def _check_info(
    report: Report, tree: Tree, preset: Preset, where: str, sys_id: str | None
) -> None:
    out = report.findings
    info_name = preset.path.with_suffix(".info").name
    if preset.info is None:
        out.append(
            Finding(
                "WARN",
                "info-missing",
                where,
                "no `.info` file: the slicer cannot tell which system preset this one derives from",
                f"create {info_name} containing `base_id = {sys_id or '<setting_id of the parent system preset>'}`"
                " (`3d profiles sync --pull` copies it from the slicer)",
            )
        )
        return
    base = preset.info.get("base_id", "")
    if base == "null":
        base = ""
    if sys_id and base and base != sys_id:
        out.append(
            Finding(
                "ERROR",
                "info-base-id",
                where,
                f"{info_name} says base_id = {base} but the parent system preset's setting_id is {sys_id}",
                f"set `base_id = {sys_id}` in {info_name}",
            )
        )
    elif sys_id and not base:
        out.append(
            Finding(
                "WARN",
                "info-base-id",
                where,
                f"{info_name} has no base_id; the parent system preset's setting_id is {sys_id}",
                f"set `base_id = {sys_id}` in {info_name}",
            )
        )
    json_id = str(preset.data.get("setting_id") or "")
    info_id = preset.info.get("setting_id", "")
    if json_id and info_id and json_id != info_id:
        out.append(
            Finding(
                "ERROR",
                "info-setting-id",
                where,
                f'{info_name} setting_id "{info_id}" differs from the JSON\'s "{json_id}"',
                f"make both equal, or empty `setting_id` in {info_name}",
            )
        )


def _machine_candidates(
    ctx: _Context, tree: Tree, flat: dict[str, Any]
) -> list[tuple[str, dict[str, Any]]]:
    names = flat.get("compatible_printers")
    out: list[tuple[str, dict[str, Any]]] = []
    for name in names if isinstance(names, list) else []:
        found = ctx.user("machine", str(name), tree)
        if found is not None:
            eff = ctx.effective("machine", found[1], found[0])[0]
        elif (
            ctx.system is not None and ctx.system.path("machine", str(name)) is not None
        ):
            eff = ctx.system.flat("machine", str(name))
        else:
            continue
        out.append((str(name), eff))
    return out


def _check_process(
    report: Report,
    ctx: _Context,
    tree: Tree,
    preset: Preset,
    where: str,
    flat: dict[str, Any],
    band: tuple[float, float],
) -> None:
    out = report.findings
    height = _floats(flat.get("layer_height"))
    own_min, own_max = (
        _floats(flat.get("min_layer_height")),
        _floats(flat.get("max_layer_height")),
    )
    if own_min and own_max and max(own_min) > min(own_max):
        out.append(
            Finding(
                "ERROR",
                "layer-height-range",
                where,
                f"min_layer_height {max(own_min):g} is above max_layer_height {min(own_max):g}",
                f"set min_layer_height <= max_layer_height in {preset.path.name}",
            )
        )
    if height and own_min and height[0] < max(own_min) - 1e-9:
        out.append(
            Finding(
                "ERROR",
                "layer-height-range",
                where,
                f"layer_height {height[0]:g} is below its min_layer_height {max(own_min):g}",
                f"set layer_height >= {max(own_min):g} (or lower min_layer_height) in {preset.path.name}",
            )
        )
    if height and own_max and height[0] > min(own_max) + 1e-9:
        out.append(
            Finding(
                "ERROR",
                "layer-height-range",
                where,
                f"layer_height {height[0]:g} is above its max_layer_height {min(own_max):g}",
                f"set layer_height <= {min(own_max):g} (or raise max_layer_height) in {preset.path.name}",
            )
        )
    machines = _machine_candidates(ctx, tree, flat)
    if not machines and flat.get("compatible_printers"):
        return
    named = _ADAPTIVE_NAME.search(preset.name)
    adaptive = named is not None or str(flat.get("adaptive_layer_height", "0")) == "1"
    want = (float(named.group(1)), float(named.group(2))) if named else band
    for mname, machine in machines:
        lim = _limits(machine)
        if lim is None:
            continue
        lo, hi = lim
        if height and not lo - 1e-9 <= height[0] <= hi + 1e-9:
            out.append(
                Finding(
                    "ERROR",
                    "layer-height-range",
                    where,
                    f'layer_height {height[0]:g} is outside the {lo:g}..{hi:g} mm limits of machine "{mname}"',
                    f"set layer_height in {preset.path.name} to a value in {lo:g}..{hi:g}, or widen "
                    f'min_layer_height/max_layer_height of the machine preset "{mname}"',
                )
            )
        if adaptive and (want[0] < lo - 1e-9 or want[1] > hi + 1e-9):
            out.append(
                Finding(
                    "ERROR" if named else "WARN",
                    "adaptive-clamped",
                    where,
                    f'the adaptive range {want[0]:g}..{want[1]:g} mm would be clamped to {lo:g}..{hi:g} by machine "{mname}"',
                    f'set min_layer_height <= {want[0]:g} and max_layer_height >= {want[1]:g} in the machine preset "{mname}"',
                )
            )


def _check_machine(
    report: Report,
    preset: Preset,
    where: str,
    flat: dict[str, Any],
    band: tuple[float, float],
) -> None:
    out = report.findings
    fname = preset.path.name
    lo, hi = (
        _floats(flat.get("min_layer_height")),
        _floats(flat.get("max_layer_height")),
    )
    nozzles = _floats(flat.get("nozzle_diameter"))
    if (
        lo
        and hi
        and (len(lo) != len(hi) or any(a > b for a, b in zip(lo, hi, strict=False)))
    ):
        out.append(
            Finding(
                "ERROR",
                "layer-height-range",
                where,
                f"min_layer_height {lo} and max_layer_height {hi} disagree (count or order)",
                f"give every extruder min_layer_height <= max_layer_height in {fname}",
            )
        )
    if not _is_u1(preset.name, flat):
        return
    for key, value in (
        ("nozzle_diameter", nozzles),
        ("min_layer_height", lo),
        ("max_layer_height", hi),
    ):
        if not value:
            out.append(
                Finding(
                    "ERROR",
                    "machine-keys",
                    where,
                    f"U1 machine preset has no usable {key}",
                    f'add "{key}" with {U1_TOOLHEADS} entries to {fname}, or inherit a vendor U1 preset',
                )
            )
    if nozzles and len(nozzles) != U1_TOOLHEADS:
        out.append(
            Finding(
                "WARN",
                "machine-keys",
                where,
                f"nozzle_diameter has {len(nozzles)} entries; the U1 has {U1_TOOLHEADS} toolheads",
                f"list {U1_TOOLHEADS} nozzle diameters in {fname}",
            )
        )
    size = _NOZZLE_NAME.search(preset.name)
    if nozzles and size and any(abs(n - float(size.group(1))) > 1e-9 for n in nozzles):
        out.append(
            Finding(
                "ERROR",
                "machine-keys",
                where,
                f'named "({size.group(1)} nozzle)" but nozzle_diameter is {nozzles}',
                f"set nozzle_diameter to {size.group(1)} for every toolhead in {fname}",
            )
        )
    limits = _limits(flat)
    if limits and (band[0] < limits[0] - 1e-9 or band[1] > limits[1] + 1e-9):
        out.append(
            Finding(
                "WARN",
                "machine-band",
                where,
                f"layer-height limits {limits[0]:g}..{limits[1]:g} do not cover the requested band "
                f"{band[0]:g}..{band[1]:g}: adaptive layers would be clamped",
                f"set min_layer_height {band[0]:g} and max_layer_height {band[1]:g} for every toolhead in {fname}",
            )
        )


# --------------------------------------------------------------------------- per slicer


def _system_for(slicer: slicers.Slicer) -> tuple[orca_cli.SystemPresets | None, str]:
    """Vendor presets are only located for Snapmaker Orca (the install the repo's tooling uses)."""
    if slicer.id != "snapmaker-orca":
        return None, f"no known resources location for {slicer.label}"
    res = orca_cli.find_resources(cli=orca_cli.find_orca())
    return (orca_cli.SystemPresets(res), "") if res else (None, "")


def diagnose_slicer(
    slicer: slicers.Slicer, *, band: tuple[float, float] = DEFAULT_BAND
) -> Report | None:
    """Check the repo presets of `slicer` and the slicer's own user presets; None when neither exists."""
    trees: list[Tree] = []
    repo = slicers.profiles_root() / slicer.id
    if repo.is_dir():
        trees.append(Tree.load(f"{slicer.id}/repo", repo))
    data_dir = slicers.find_data_dir(slicer)
    if data_dir is not None:
        user = slicers.user_presets_dir(data_dir, slicer)
        if user.is_dir():
            trees.append(Tree.load(f"{slicer.id}/slicer", user))
    if not trees:
        return None
    system, note = _system_for(slicer)
    return diagnose(trees, system, band=band, system_note=note)
