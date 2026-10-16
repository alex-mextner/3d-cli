"""Read-only, bounded G-code indexing; byte offsets, not decoded character offsets.

No G-code is ever executed. Layer features describe the plan, never proof of
physical motion. A virtual_sdcard cursor is buffered and is labelled accordingly.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
import hashlib
import math
import re
import time
from typing import BinaryIO

# Multi-head jobs routinely pass 128 MiB (a 1193-layer 4-tool U1 job is 135 MB). Memory stays
# bounded by MAX_POINTS; parse time (~0.8 MB/s on an ODROID-N2+) is what the caller budgets.
MAX_BYTES = 512 * 1024 * 1024
MAX_LINE = 256 * 1024
MAX_POINTS = 300_000
MAX_LAYERS = 100_000
FEATURES = {
    "outer wall": ("Наружная стенка", "Формирование внешнего контура детали."),
    "inner wall": ("Внутренняя стенка", "Укрепление оболочки изнутри."),
    "overhang wall": ("Нависание", "Печать нависающего участка стенки."),
    "sparse infill": ("Заполнение", "Печать внутренней разреженной структуры."),
    "internal solid infill": (
        "Сплошное заполнение",
        "Плотный внутренний слой под поверхностью.",
    ),
    "top surface": ("Верхняя поверхность", "Закрытие верхней поверхности детали."),
    "bottom surface": ("Нижняя поверхность", "Формирование нижнего сплошного слоя."),
    "ironing": (
        "Глажка",
        "Дополнительные проходы по поверхности с малой подачей пластика.",
    ),
    "bridge": ("Мост", "Печать перемычки между опорами."),
    "internal bridge": ("Внутренний мост", "Перекрытие внутреннего промежутка."),
    "support": ("Поддержки", "Печать временных опор для нависающих частей."),
    "support interface": (
        "Контактный слой поддержек",
        "Формирование контакта между поддержками и деталью.",
    ),
    "brim": ("Кайма", "Печать каймы для сцепления со столом."),
    "skirt": ("Юбка", "Подготовительный контур вокруг детали."),
    "gap infill": (
        "Заполнение зазоров",
        "Заполнение узких промежутков между стенками.",
    ),
    "wipe tower": (
        "Башня очистки",
        "Подготовка потока материала на вспомогательной башне.",
    ),
    "custom": (
        "Пользовательский G-code",
        "Вспомогательные команды слайсера или макроса.",
    ),
}
ALIASES = {
    "wall-outer": "outer wall",
    "external perimeter": "outer wall",
    "wall-inner": "inner wall",
    "perimeter": "inner wall",
    "overhang perimeter": "overhang wall",
    "fill": "sparse infill",
    "internal infill": "sparse infill",
    "solid infill": "internal solid infill",
    "top solid infill": "top surface",
    "bridge infill": "bridge",
    "internal bridge infill": "internal bridge",
    "support material": "support",
    "support material interface": "support interface",
    "support-interface": "support interface",
    "skirt/brim": "brim",
    "gap fill": "gap infill",
    "prime tower": "wipe tower",
    "skin": "top/bottom skin",
    "support transition": "support interface",
}
COMMANDS = {
    "M109": "Нагрев сопла",
    "M190": "Нагрев стола",
    "M191": "Нагрев камеры",
    "G28": "Поиск начала координат",
    "G29": "Калибровка стола",
    "BED_MESH_CALIBRATE": "Калибровка стола",
    "AUTO_BED_MESH_CALIBRATE": "Калибровка стола",
    "SHAPER_CALIBRATE": "Калибровка вибраций",
    "XYZ_OFFSET_CALIBRATE_ALL": "Калибровка головок",
    "XYZ_OFFSET_CALIBRATE": "Калибровка головок",
    "M600": "Смена материала",
    "PAUSE": "Пауза",
    "M0": "Ожидание пользователя",
    "M1": "Ожидание пользователя",
    "PRINT_START": "Подготовка к печати",
    "PRINT_STRAT": "Подготовка к печати",
    "PRINT_END": "Завершение печати",
    "M400": "Ожидание очереди движений",
    "ROUGHLY_CLEAN_NOZZLE": "Очистка сопла",
    "ROUGHLY_CLEAN_NOZZLE_WITH_DISCARD": "Очистка сопла",
}
TOOL_CHANGE = "Смена головки"
ARG_COMMANDS = frozenset(("G0", "G1", "G2", "G3", "G92", "M73"))
NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
ARG = re.compile(r"([A-Z])(" + NUMBER + r")", re.I)


def describe_feature(name: str) -> dict:
    raw = name.strip()[:100]
    key = ALIASES.get(raw.lower(), raw.lower())
    title, detail = FEATURES.get(
        key,
        (
            raw or "Не определено",
            "Операция указана слайсером; дополнительная классификация отсутствует.",
        ),
    )
    return {"key": key, "label": title, "description": detail, "raw": raw}


@dataclass
class Point:
    offset: int
    layer: int
    feature: str | None
    tool: int | None
    operation: str | None
    remaining_minutes: float | None


@dataclass
class Layer:
    number: int
    start_byte: int
    end_byte: int = 0
    z_mm: float | None = None
    features: dict = field(default_factory=dict)
    operations: list = field(default_factory=list)
    tools: set = field(default_factory=set)
    extrusion_mm: float = 0.0
    move_count: int = 0

    def as_dict(self) -> dict:
        return {
            "layer": self.number,
            "z_mm": self.z_mm,
            "start_byte": self.start_byte,
            "end_byte": self.end_byte,
            "features": list(self.features.values()),
            "special_operations": self.operations,
            "logical_tools": sorted(self.tools),
            "positive_extrusion_mm": round(self.extrusion_mm, 2),
            "move_count": self.move_count,
            "scope": "planned_layer",
            "note": "План слоя, не текущая операция. T-номера в G-code могут быть переназначены на физические головки.",
        }


class GCodeIndex:
    def __init__(self):
        self.points: list[Point] = []
        self.offsets: list[int] = []
        self.layers: dict[int, Layer] = {}
        self.size = 0
        self.sha256 = ""
        self.slicer = "unknown"
        self.declared_layers: int | None = None
        self.warnings: list[str] = []
        self.all_features: set[str] = set()

    @classmethod
    def parse(
        cls,
        source: BinaryIO,
        max_bytes: int = MAX_BYTES,
        deadline: float | None = None,
        shell=None,
    ) -> GCodeIndex:
        """`shell` (optional, a 3d-cli vision_ghost.ShellBuilder) sees every comment and motion of
        the same single pass, so the camera ghost costs no second download or parse."""
        obj = cls()
        digest = hashlib.sha256()
        layer = 0
        feature = None
        tool = None
        operation = None
        remaining = None
        offset = 0
        absolute_e = True
        e_position = 0.0
        metric = True
        # absolute XY in mm, for the camera-ghost shell (G90/G91, G20/G21, G92 applied)
        absolute_xy = True
        xy = [0.0, 0.0]
        xy_offset = [0.0, 0.0]
        last_point = None
        used_cura = False
        z_hint = None
        while True:
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError("G-code indexing deadline exceeded")
            raw = source.readline(MAX_LINE + 1)
            if not raw:
                break
            if len(raw) > MAX_LINE:
                raise ValueError("G-code line exceeds 256 KiB")
            if offset + len(raw) > max_bytes:
                raise ValueError("G-code exceeds indexing size limit")
            if b"\x00" in raw or (
                offset == 0 and raw.startswith((b"GCDE", b"PK\x03\x04"))
            ):
                raise ValueError(
                    "Binary/container G-code is not supported; export annotated text G-code"
                )
            digest.update(raw)
            line = raw.decode("utf-8", errors="replace").strip()
            next_offset = offset + len(raw)
            comment = line[1:].strip() if line.startswith(";") else ""
            previous_layer = layer
            if comment:
                if shell is not None:
                    shell.comment(comment)
                if comment.lower().startswith("generated by"):
                    obj.slicer = comment[13:].strip()[:150]
                if comment.upper() == "LAYER_CHANGE":
                    layer += 1
                    feature = None
                    operation = None
                elif m := re.match(r"LAYER:\s*(-?\d+)", comment, re.I):
                    layer = max(0, int(m[1]) + 1)
                    feature = None
                    used_cura = True
                elif m := re.match(
                    r"layer num/total_layer_count:\s*(\d+)\s*/\s*(\d+)", comment, re.I
                ):
                    layer, obj.declared_layers = int(m[1]), int(m[2])
                elif m := re.match(
                    r"layer (\d+),\s*Z\s*=\s*(" + NUMBER + ")", comment, re.I
                ):
                    layer, z_hint = int(m[1]), float(m[2])
                if m := re.match(r"(?:TYPE|FEATURE):\s*(.+)", comment, re.I):
                    feature = describe_feature(m[1])["key"]
                    operation = None
                    obj.all_features.add(feature)
                if m := re.match(r"Z:\s*(" + NUMBER + ")", comment, re.I):
                    z_hint = float(m[1])
            if layer < 0 or layer > MAX_LAYERS:
                raise ValueError("Layer number outside indexing limits")
            code = line.split(";", 1)[0].strip()
            cmd = code.split(None, 1)[0].upper() if code else ""
            if code:
                operation = (
                    None  # Special commands only cover their own cursor interval.
                )
                if cmd == "SET_PRINT_STATS_INFO":
                    if m := re.search(r"\bCURRENT_LAYER=(\d+)\b", code, re.I):
                        layer = int(m[1])
                    if m := re.search(r"\bTOTAL_LAYER=(\d+)\b", code, re.I):
                        obj.declared_layers = int(m[1])
                    if not 0 <= layer <= MAX_LAYERS:
                        raise ValueError("Layer number outside indexing limits")
                if cmd in COMMANDS:
                    operation = COMMANDS[cmd]
                elif re.fullmatch(r"T\d{1,2}", cmd):
                    # Logical slicer tool; the inspector maps it to the printer's head name.
                    tool = int(cmd[1:])
                    operation = TOOL_CHANGE
                elif cmd.startswith(("FINELY_CLEAN_NOZZLE", "INNER_ROUGHLY_CLEAN")):
                    operation = "Очистка сопла"
                elif cmd in ("M82", "M83"):
                    absolute_e = cmd == "M82"
                elif cmd in ("G20", "G21"):
                    metric = cmd == "G21"
                elif cmd in ("G90", "G91"):
                    absolute_xy = cmd == "G90"
                # Only these commands read arguments; skipping the rest halves parse time.
                args = (
                    {k.upper(): float(v) for k, v in ARG.findall(code[len(cmd) :])}
                    if cmd in ARG_COMMANDS
                    else {}
                )
                if (
                    cmd == "M73"
                    and "R" in args
                    and math.isfinite(args["R"])
                    and args["R"] >= 0
                ):
                    remaining = args["R"]
                if cmd == "G92" and "E" in args:
                    e_position = args["E"]
                if cmd == "G92":
                    # the new logical position; the physical one stays: remember the shift
                    for i, axis in enumerate("XY"):
                        if axis in args:
                            xy_offset[i] += xy[i] - args[axis] * (1 if metric else 25.4)
                            xy[i] = args[axis] * (1 if metric else 25.4)
            if layer != previous_layer:
                if previous_layer in obj.layers:
                    obj.layers[previous_layer].end_byte = offset
                if z_hint is not None and not re.match(r"layer \d+,", comment, re.I):
                    z_hint = None
            if layer > 0:
                if layer not in obj.layers:
                    obj.layers[layer] = Layer(layer, offset)
                item = obj.layers[layer]
                item.end_byte = next_offset
                if z_hint is not None:
                    item.z_mm = round(z_hint, 5)
                if feature:
                    item.features.setdefault(feature, describe_feature(feature))
                if (
                    operation
                    and operation not in item.operations
                    and len(item.operations) < 64
                ):
                    item.operations.append(operation)
                if tool is not None:
                    item.tools.add(tool)
                if cmd in ("G0", "G1", "G2", "G3"):
                    item.move_count += 1
            # E bookkeeping also needs to run in the startup section.
            if code and cmd in ("G0", "G1", "G2", "G3"):
                extruding = False
                if "E" in args:
                    delta = args["E"] - e_position if absolute_e else args["E"]
                    e_position = args["E"] if absolute_e else e_position + args["E"]
                    extruding = math.isfinite(delta) and delta > 0
                    if layer > 0 and math.isfinite(delta):
                        obj.layers[layer].extrusion_mm += max(0, delta) * (
                            1 if metric else 25.4
                        )
                if shell is not None:
                    unit = 1 if metric else 25.4
                    moved = [None, None]
                    for i, axis in enumerate("XY"):
                        if axis in args:
                            xy[i] = args[axis] * unit + (0.0 if absolute_xy else xy[i])
                            moved[i] = xy[i] + xy_offset[i]
                    arc = None
                    if cmd in ("G2", "G3") and ("I" in args or "J" in args):
                        arc = (
                            args.get("I", 0.0) * unit,
                            args.get("J", 0.0) * unit,
                            cmd == "G2",
                        )
                    current = obj.layers.get(layer)
                    shell.move(
                        layer,
                        current.z_mm if current else None,
                        tool,
                        feature,
                        moved[0],
                        moved[1],
                        extruding,
                        arc,
                    )
            point = (layer, feature, tool, operation, remaining)
            if point != last_point:
                if len(obj.points) >= MAX_POINTS:
                    raise ValueError("Too many G-code semantic transitions")
                obj.points.append(Point(offset, *point))
                obj.offsets.append(offset)
                last_point = point
            offset = next_offset
        obj.size = offset
        obj.sha256 = digest.hexdigest()
        if not obj.layers:
            obj.warnings.append("No supported layer markers found")
        if not obj.all_features:
            obj.warnings.append(
                "No TYPE/FEATURE annotations; do not infer ironing from speed"
            )
        if used_cura:
            obj.warnings.append(
                "Cura zero-based layers converted to one-based display; negative raft layers are startup"
            )
        return obj

    def at(self, byte_offset: int) -> Point | None:
        if (
            isinstance(byte_offset, bool)
            or not isinstance(byte_offset, int)
            or not 0 <= byte_offset < self.size
        ):
            return None
        pos = bisect_right(self.offsets, byte_offset) - 1
        return self.points[pos] if pos >= 0 else None

    def layer(self, number: int) -> dict | None:
        item = self.layers.get(number)
        if not item:
            return None
        return {
            **item.as_dict(),
            "job_fingerprint": self.sha256,
            "indexed_layers": len(self.layers),
            "slicer": self.slicer,
            "warnings": self.warnings,
        }

    def summary(self) -> dict:
        return {
            "sha256": self.sha256,
            "size": self.size,
            "indexed_layers": len(self.layers),
            "declared_layers": self.declared_layers,
            "features": sorted(self.all_features),
            "slicer": self.slicer,
            "warnings": self.warnings,
            "ironing_layers": [
                n for n, l in self.layers.items() if "ironing" in l.features
            ],
        }
