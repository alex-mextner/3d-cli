"""slots.py — read what each printer says is loaded in its filament slots (READ-ONLY).

WHAT: `read_printer(device)` makes exactly one read-only request to a fleet printer and returns a
  `Reading`: normalized state + one `LiveSlot` per head. Readers per adapter (plan §5.4):
  - moonraker (Snapmaker U1): one `GET /printer/objects/query?webhooks&print_stats&
    virtual_sdcard&print_task_config&filament_detect`. `print_task_config` gives filament_exist /
    filament_type / filament_color_rgba / filament_vendor / filament_sub_type per head (the
    firmware's "NONE" = not known); `filament_detect.info[i]` gives the RFID tag the head read
    (CARD_UID 0 = no tag; stock firmware reads only signed Snapmaker tags, plan §10.1). paxx12
    extended firmware keeps these keys and adds CARD_TYPE/CARD_EVENT_TIME (runbook
    docs/printers/u1-paxx12-upgrade.md), so the same parser serves both.
  - creality-stock (K1 SE): one ws:9999 connection listened to for `listen_s`. Without a CFS
    the printer reports no material or colour, only its runout switch: `materialDetect` 1 = the
    switch is enabled, then `materialStatus` 0 = filament present, 1 = runout (mapping of
    ha_creality_ws sensor.py MAPPED_SPECS "filament_status"); with the switch off presence is
    unknown (None).
  Offline fleet printers (other people's, decision D7) and adapters without a reader get no
  reading at all — they are never contacted.

WHY: reconciliation (printops.reconcile) compares this with the spools bound to each slot in
  Spoolman (plan §5.3).

INVARIANTS: never sends anything that changes a printer (GET / ws listen only); one request per
  call, so the poller's interval is the per-printer request rate.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from errors import ThreeDError
from printer_ops import make_adapter
from printer_ops.creality import CrealityStockAdapter
from printer_ops.creality import parse_status as creality_status
from printer_ops.devices import Device
from printer_ops.moonraker import MoonrakerAdapter, parse_slots
from printer_ops.moonraker import parse_status as moonraker_status
from printops.color import parse_hex
from printops.db import utc_now

READERS = ("moonraker", "creality-stock")
U1_QUERY = "webhooks&print_stats&virtual_sdcard&print_task_config&filament_detect"
_UNKNOWN = {"", "NONE", "NULL", "UNKNOWN"}


@dataclass(frozen=True)
class LiveSlot:
    head: str  # fleet head label T1..Tn
    present: bool | None  # filament loaded; None = the printer cannot tell
    material: str | None = None
    color: str | None = None  # "#RRGGBB"
    vendor: str | None = None
    sub_type: str | None = None
    tag_uid: str | None = None  # RFID tag UID the slot read (U1 CARD_UID)
    source: str = ""  # which printer object this came from


@dataclass(frozen=True)
class Reading:
    printer: str
    at: str  # ISO-8601 UTC of the read
    ok: bool
    state: str | None = None  # printer_ops normalized state (idle, printing, ...)
    raw_state: str | None = None
    filename: str | None = None
    progress: float | None = None
    error: str | None = None
    slots: tuple[LiveSlot, ...] = field(default_factory=tuple)

    def slot(self, head: str) -> LiveSlot | None:
        return next((s for s in self.slots if s.head == head), None)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["slots"] = [asdict(s) for s in self.slots]
        return d


def _known(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return None if s.upper() in _UNKNOWN else s


def _tag(info: Any) -> tuple[str | None, str | None, str | None]:
    """filament_detect.info[i] -> (uid, material, colour) when a tag was read."""
    if not isinstance(info, dict):
        return None, None, None
    uid = info.get("CARD_UID")
    if uid in (None, 0, "0", ""):
        return None, None, None
    rgb = info.get("RGB_1")
    color = (
        parse_hex(f"{rgb:06X}")
        if isinstance(rgb, int) and 0 <= rgb <= 0xFFFFFF
        else None
    )
    return str(uid), _known(info.get("MAIN_TYPE")), color


def u1_slots(status: dict[str, Any]) -> tuple[LiveSlot, ...]:
    """Moonraker status (print_task_config + filament_detect) -> one LiveSlot per head."""
    infos = (status.get("filament_detect") or {}).get("info")
    out = []
    for s in parse_slots(status.get("print_task_config") or {}):
        material = _known(s.material)
        uid, tag_material, tag_color = _tag(
            infos[s.index] if isinstance(infos, list) and s.index < len(infos) else None
        )
        # The U1 shows white (FFFFFFFF) for a head whose filament it knows nothing about: a
        # colour without a material is a default, not a reading.
        color = s.color if material else None
        out.append(
            LiveSlot(
                head=s.label,
                present=s.present,
                material=material or tag_material,
                color=color or tag_color,
                vendor=_known(s.vendor),
                sub_type=_known(s.sub_type),
                tag_uid=uid,
                source="print_task_config+filament_detect"
                if uid
                else "print_task_config",
            )
        )
    return tuple(out)


def k1_slots(pushed: dict[str, Any], heads: tuple[str, ...]) -> tuple[LiveSlot, ...]:
    """Creality ws state -> the single head's LiveSlot (presence from the runout switch only)."""
    if not heads:
        return ()
    detect, status = pushed.get("materialDetect"), pushed.get("materialStatus")
    present: bool | None = None
    if detect == 1 and status in (0, 1):
        present = status == 0
    return (LiveSlot(head=heads[0], present=present, source="materialStatus"),)


def read_printer(
    device: Device,
    heads: tuple[str, ...],
    *,
    listen_s: float = 3.0,
    timeout: float = 10.0,
) -> Reading:
    """One read-only request to `device` -> Reading (ok=False with the error when unreachable)."""
    at = utc_now()
    options: dict[str, float] = {"timeout": timeout}
    if device.adapter == "creality-stock":
        options["listen_s"] = listen_s
    adapter = make_adapter(device, **options)
    try:
        if isinstance(adapter, MoonrakerAdapter):
            raw = adapter.query(U1_QUERY)
            st = moonraker_status(raw)
            slots = u1_slots(raw)
        elif isinstance(adapter, CrealityStockAdapter):
            raw = adapter.pushed_state()
            st = creality_status(raw)
            slots = k1_slots(raw, heads)
        else:  # pragma: no cover - make_adapter only builds the two above
            return Reading(
                device.name,
                at,
                False,
                error=f"no slot reader for adapter {device.adapter}",
            )
    except ThreeDError as e:
        return Reading(device.name, at, False, error=e.message)
    return Reading(
        device.name,
        at,
        True,
        state=st.state,
        raw_state=st.raw_state,
        filename=st.filename,
        progress=st.progress,
        slots=slots,
    )
