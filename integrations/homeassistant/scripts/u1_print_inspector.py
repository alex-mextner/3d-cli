"""U1 telemetry + G-code enrichment, read-only and local to the camera proxy.

Only GET requests to the configured printer origin; redirects are forbidden.
No HA credentials, firmware changes, printer control, arbitrary file URL, or
LLM classification. Public viewers receive rendered pixels, never this API.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, Future
from datetime import datetime, timezone
from functools import lru_cache
import copy
import io
import json
import math
import os
from pathlib import PurePosixPath
import re
import threading
import time
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import build_opener, ProxyHandler, HTTPRedirectHandler, Request

from u1_gcode_index import GCodeIndex, MAX_BYTES, describe_feature

# Source: Snapmaker/u1-klipper, klippy/extras/machine_state_manager.py.
# Unknown codes remain unknown; they must never be guessed from motion speed.
MAIN_STATES = {
    2: "Калибровка смещений головок", 3: "Калибровка стола", 4: "Калибровка потока",
    5: "Калибровка вибраций", 6: "Обновление прошивки", 7: "Ошибка принтера",
    8: "Выравнивание винтов стола", 9: "Автозагрузка пластика", 10: "Автовыгрузка пластика",
    11: "Ручная загрузка пластика", 12: "Калибровка парковки", 13: "Калибровка начала координат",
}
ACTIONS = {
    1: "Поиск начала координат", 2: "Проверка пластины стола", 3: "Прогрев камеры",
    128: "Восстановление после отключения питания", 129: "Пауза", 130: "Возобновление печати",
    131: "Замена закончившегося пластика", 132: "Проверка смены головки", 133: "Автоподача пластика",
    134: "Предварительная экструзия", 135: "Выгрузка пластика", 136: "Проверка стола",
    192: "Ручная очистка сопла T0", 193: "Ручная очистка сопла T1",
    194: "Ручная очистка сопла T2", 195: "Ручная очистка сопла T3",
    196: "Измерение смещения T0", 197: "Измерение смещения T1",
    198: "Измерение смещения T2", 199: "Измерение смещения T3",
    200: "Автоочистка сопла", 201: "Ожидание остывания сопла",
}
STATES = {"printing": "Печать", "paused": "Пауза", "complete": "Печать завершена",
          "cancelled": "Печать отменена", "error": "Ошибка печати", "standby": "Ожидание",
          "unknown": "Состояние неизвестно"}
OBJECTS = ("print_stats", "virtual_sdcard", "display_status", "toolhead", "extruder", "extruder1",
           "extruder2", "extruder3", "heater_bed", "gcode_move", "motion_report", "pause_resume",
           "machine_state_manager", "print_task_config", "webhooks")


def finite(value, default=None):
    if isinstance(value, bool):
        return default
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def integer(value, default=None):
    num = finite(value)
    return int(num) if num is not None and num.is_integer() else default


def bounded_text(value, length=120):
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))[:length]


def safe_filename(filename: str) -> str:
    if not isinstance(filename, str) or not filename or len(filename) > 1024:
        raise ValueError("Invalid printer filename")
    if any(ord(c) < 32 for c in filename) or "\\" in filename:
        raise ValueError("Invalid printer filename")
    parts = filename.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError("Unsafe printer filename")
    if PurePosixPath(filename).suffix.lower() not in (".gcode", ".gco", ".gc"):
        raise ValueError("Only annotated text G-code is supported")
    return "/".join(quote(p, safe="") for p in parts)


def enrich(status: dict, index: GCodeIndex | None, *, stale=False, index_state="unavailable", now=None) -> dict:
    """Pure projection, also used to test pause/offline/completed/buffered states."""
    now = time.time() if now is None else now
    ps = status.get("print_stats") or {}
    sd = status.get("virtual_sdcard") or {}
    machine = status.get("machine_state_manager") or {}
    state = ps.get("state", "unknown")
    layer = integer((ps.get("info") or {}).get("current_layer"))
    total = integer((ps.get("info") or {}).get("total_layer"))
    position = integer(sd.get("file_position"))
    active = (status.get("toolhead") or {}).get("extruder", "")
    ext = status.get(active) or {}
    bed = status.get("heater_bed") or {}
    physical = integer(ext.get("extruder_index"))
    if physical is None and re.fullmatch(r"extruder[0-3]?", active):
        physical = int(active[8:] or "0")
    material = None
    materials = (status.get("print_task_config") or {}).get("filament_type") or []
    if physical is not None and 0 <= physical < len(materials):
        material = bounded_text(materials[physical], 24)
    progress = finite((status.get("display_status") or {}).get("progress"), finite(sd.get("progress")))
    progress = round(max(0, min(1, progress)) * 100, 1) if progress is not None else None
    # Reject changed/truncated files rather than attach a previous index to them.
    size = integer(sd.get("file_size"))
    cursor = index.at(position) if index and position is not None and size == index.size else None
    if index and total in (None, 0):
        total = index.declared_layers or len(index.layers) or None
    if layer in (None, 0) and cursor and cursor.layer > 0:
        layer = cursor.layer
    plan = index.layer(layer) if index and layer is not None else None
    source = "printer_state"
    label = STATES.get(state, bounded_text(state))
    estimate = False
    note = ""
    action = integer(machine.get("action_code"), 0)
    main = integer(machine.get("main_state"), 0)
    if stale:
        label, source = "Нет свежих данных", "stale"
        state = "unavailable"
    elif (status.get("webhooks") or {}).get("state", "ready") not in ("ready",):
        label, source, state = "Принтер не готов", "firmware", "error"
    elif state == "error":
        label = "Ошибка печати"
    elif (status.get("pause_resume") or {}).get("is_paused") or state == "paused":
        label, state = "Пауза", "paused"
    elif action:
        label, source = ACTIONS.get(action, f"Операция прошивки #{action}"), "firmware_action"
        if main in MAIN_STATES:
            note = MAIN_STATES[main]
    elif main in MAIN_STATES:
        label, source = MAIN_STATES[main], "firmware_state"
    elif state == "printing":
        if cursor and (cursor.operation or cursor.feature):
            label = cursor.operation or describe_feature(cursor.feature)["label"]
            source, estimate = "gcode_cursor", True
            note = "По позиции G-code; очередь движений может опережать физическое выполнение."
            if cursor.layer != layer:
                note += f" Позиция относится к слою {cursor.layer}, принтер сообщил {layer}."
        else:
            label, source = "Печать · операция не определена", "layer_only" if plan else "unknown"
            note = "Показан план слоя; текущая операция не подтверждена." if plan else "Ожидание доступной разметки G-code."
    remaining = None
    remaining_source = None
    if state == "complete":
        remaining, remaining_source = 0, "complete"
    elif state == "printing" and cursor and cursor.remaining_minutes is not None:
        remaining, remaining_source = round(cursor.remaining_minutes * 60), "slicer_M73_estimate"
    warnings = list(index.warnings) if index else []
    if index and size not in (None, 0, index.size):
        warnings.append("Runtime file size differs from indexed file; cursor interpretation disabled")
    return {
        "schema_version": 1, "printer_id": "u1", "observed_at": now, "stale": stale,
        "state": state, "state_label": STATES.get(state, "Недоступно"),
        "operation": {"label": label, "source": source, "estimated": estimate, "note": note},
        "layer": layer, "total_layers": total, "gcode_layer": cursor.layer if cursor else None,
        "progress_percent": progress, "physical_tool": physical, "material": material,
        "temperatures": {"nozzle": finite(ext.get("temperature")), "nozzle_target": finite(ext.get("target")),
                         "bed": finite(bed.get("temperature")), "bed_target": finite(bed.get("target"))},
        "elapsed_seconds": finite(ps.get("print_duration")), "remaining_seconds": remaining,
        "remaining_source": remaining_source, "file_name": bounded_text(ps.get("filename"), 180),
        "job_fingerprint": index.sha256 if index else None, "index_state": index_state,
        "index": index.summary() if index else None, "layer_plan": plan,
        "machine": {"main_state": main, "action_code": action}, "warnings": warnings,
    }


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("Printer redirect rejected")


class Inspector:
    def __init__(self, printer_url: str, interval: float = 2.0):
        origin = urlsplit(printer_url)
        if origin.scheme not in ("http", "https") or not origin.hostname or origin.username or origin.password:
            raise ValueError("Inspector requires a fixed HTTP(S) printer origin without credentials")
        if origin.path not in ("", "/") or origin.query or origin.fragment:
            raise ValueError("Inspector URL must be an origin")
        self.base = printer_url.rstrip("/")
        self.interval = max(1.0, float(interval))
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.lock = threading.Lock()
        self.status: dict = {}
        self.updated = 0.0
        self.index: GCodeIndex | None = None
        self.index_state = "waiting"
        self.index_error = None
        self.filename = ""
        self.signature = None
        self.next_metadata = 0.0
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="u1-gcode")
        self.future: Future | None = None
        self.future_name = None
        self.future_signature = None
        self.stop_event = threading.Event()
        self.events: list[dict] = []
        self.last_event = None

    def _open(self, path: str, timeout=8):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Not a local API path")
        return self.opener.open(Request(self.base + path, headers={"Cache-Control": "no-cache"}), timeout=timeout)

    def _json(self, path):
        with self._open(path) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("Printer JSON exceeds limit")
            return json.loads(raw)["result"]

    def _load_index(self, name, expected_size):
        url = safe_filename(name)
        with self._open("/server/files/gcodes/" + url, timeout=25) as response:
            declared = integer(response.headers.get("Content-Length"))
            if declared is not None and declared > MAX_BYTES:
                raise ValueError("G-code exceeds 128 MiB limit")
            parsed = GCodeIndex.parse(response, deadline=time.monotonic() + 45)
        if parsed.size != expected_size:
            raise ValueError("G-code changed during download; retry required")
        return parsed

    def start(self):
        threading.Thread(target=self._run, name="u1-inspector", daemon=True).start()

    def _run(self):
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                result = self._json("/printer/objects/query?" + "&".join(OBJECTS))
                status = result["status"]
                filename = (status.get("print_stats") or {}).get("filename", "") or ""
                with self.lock:
                    previous = self.status.get("print_stats") or {}
                    current = status.get("print_stats") or {}
                    old_cursor = integer((self.status.get("virtual_sdcard") or {}).get("file_position"))
                    new_cursor = integer((status.get("virtual_sdcard") or {}).get("file_position"))
                    restarted = (current.get("state") == "printing" and (
                        previous.get("state") not in ("printing", "paused") or
                        (old_cursor is not None and new_cursor is not None and new_cursor < old_cursor)))
                    self.status, self.updated = status, time.time()
                    if filename != self.filename or restarted:
                        self.filename, self.index, self.signature = filename, None, None
                        self.index_state, self.index_error, self.next_metadata = "waiting", None, 0
                if self.future and self.future.done():
                    try:
                        index = self.future.result()
                        with self.lock:
                            if self.future_name == self.filename and self.future_signature == self.signature:
                                self.index, self.index_state, self.index_error = index, "ready", None
                    except Exception as exc:
                        with self.lock:
                            if self.future_name == self.filename:
                                self.index_state = "unavailable"
                                self.index_error = type(exc).__name__ + ": indexing unavailable"
                                self.signature = None
                    self.future = None
                if filename and self.future is None and time.monotonic() >= self.next_metadata:
                    self.next_metadata = time.monotonic() + 30
                    safe_filename(filename)
                    meta = self._json("/server/files/metadata?" + urlencode({"filename": filename}))
                    size = integer(meta.get("size"))
                    if size is None or not 0 < size <= MAX_BYTES:
                        raise ValueError("Unsupported G-code size")
                    signature = (filename, size, meta.get("modified"), meta.get("uuid"))
                    with self.lock:
                        if signature != self.signature:
                            self.signature, self.index = signature, None
                            self.index_state = "indexing"
                            self.future_name, self.future_signature = filename, signature
                            self.future = self.executor.submit(self._load_index, filename, size)
                data = self.snapshot()
                event = (data["state"], data["layer"], data["operation"]["label"], data["operation"]["source"])
                with self.lock:
                    if event != self.last_event:
                        self.events.append({"at": data["observed_at"], "layer": data["layer"],
                                            "state": data["state"], "operation": data["operation"]})
                        self.events = self.events[-32:]
                        self.last_event = event
            except Exception as exc:
                with self.lock:
                    self.index_error = type(exc).__name__ + ": printer data unavailable"
                    if self.index is None:
                        self.index_state = "unavailable"
            self.stop_event.wait(max(0.1, self.interval - (time.monotonic() - started)))

    def snapshot(self):
        with self.lock:
            data = enrich(self.status, self.index, stale=time.time() - self.updated > 12,
                          index_state=self.index_state, now=self.updated or time.time())
            data["age_seconds"] = round(time.time() - self.updated, 1) if self.updated else None
            data["index_error"] = self.index_error
            data["recent_events"] = copy.deepcopy(self.events[-8:])
            return data

    def layer(self, number: int, expected_job: str | None = None):
        with self.lock:
            if self.index is None:
                return 503, {"error": "index_not_ready", "index_state": self.index_state}
            if expected_job and expected_job != self.index.sha256:
                return 409, {"error": "job_changed"}
            value = self.index.layer(number)
            if value is None:
                return 404, {"error": "layer_not_found"}
            return 200, {**value, "file_name": bounded_text(self.filename, 180)}


@lru_cache(maxsize=16)
def _font(size):
    from PIL import ImageFont
    path = os.environ.get("U1_INSPECTOR_FONT", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    return ImageFont.truetype(path, size=size)


def overlay(frame: bytes, data: dict) -> bytes:
    """Bake the same status into JPEG and MJPEG. No filenames in guest images."""
    from PIL import Image, ImageDraw
    with Image.open(io.BytesIO(frame)) as original:
        original.load()
        if original.width * original.height > 20_000_000:
            raise ValueError("Camera frame exceeds rendering limit")
        image = original.convert("RGBA")
    w, h = image.size
    unit = max(14, round(w / 42))
    margin = max(10, round(w * .018))
    line_h = int(unit * 1.5)
    band_h = min(h, line_h * 3 + margin * 2 + 10)
    pane = Image.new("RGBA", image.size)
    draw = ImageDraw.Draw(pane)
    top = h - band_h
    draw.rectangle((0, top, w, h), fill=(9, 15, 22, 214))
    op = data["operation"]
    prefix = "≈ " if op["estimated"] else ""
    first = "U1 · " + prefix + op["label"]
    second = []
    if data.get("layer") is not None:
        second.append(f"Слой {data['layer']}/{data.get('total_layers') or '?'}")
    if data.get("progress_percent") is not None:
        second.append(f"{data['progress_percent']:g}%")
    if data.get("physical_tool") is not None:
        tool = data["physical_tool"]
        second.append(f"Головка {tool + 1} (T{tool})")
    if data.get("material") not in (None, "NONE", ""):
        second.append(data["material"])
    if (remaining := data.get("remaining_seconds")) and data["state"] == "printing":
        second.append(f"≈ {math.ceil(remaining / 60)} мин")
    t = data.get("temperatures") or {}
    def degrees(key):
        current, target = t.get(key), t.get(key + "_target")
        return f"{current:.0f}/{target:.0f}°C" if current is not None and target is not None else "—"
    stamp = datetime.fromtimestamp(data["observed_at"], timezone.utc).strftime("%H:%M:%S UTC")
    third = f"Сопло {degrees('nozzle')} · Стол {degrees('bed')} · Данные {stamp}"
    if data.get("stale"):
        third = "Связь потеряна: числа на экране — последние известные, не текущие"
    elif op["source"] == "gcode_cursor":
        third = f"Позиция G-code (оценка) · Сопло {degrees('nozzle')} · Стол {degrees('bed')}"
    for i, text in enumerate((first, " · ".join(second), third)):
        font = _font(unit + 5 if i == 0 else unit)
        limit = w - 2 * margin
        while len(text) > 1 and draw.textlength(text, font=font) > limit:
            text = text[:-2].rstrip("…") + "…"
        draw.text((margin, top + margin + i * line_h), text, font=font,
                  fill=(255, 255, 255, 255) if i == 0 else (210, 220, 230, 255))
    progress = data.get("progress_percent")
    if progress is not None and not data.get("stale"):
        draw.rectangle((0, h - 7, round(w * progress / 100), h), fill=(65, 195, 165, 255))
    output = io.BytesIO()
    Image.alpha_composite(image, pane).convert("RGB").save(output, format="JPEG", quality=88)
    return output.getvalue()
