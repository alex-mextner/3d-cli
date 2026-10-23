"""camera.py — is the printer's camera configured and alive?

CHECKS
  camera-config    the `watch.vision` block resolves (`printer_ops.camera.vision_problems`,
                   `camera_spec`): a bad key or kind is named. Reads config only. Skipped when the
                   printer has no camera.
  camera-snapshot  the snapshot URL of the resolved camera answers HTTP 200 with a JPEG (first
                   bytes FF D8) within the request timeout. A camera that sleeps until Moonraker's
                   `camera.start_monitor` is renewed (`keepalive`, the Snapmaker monitor file) is
                   woken once through the same call the vision service uses and asked again up to
                   WAKE_TRIES times, so a sleeping camera is not reported dead.
WHY: `3d printer vision`/`watch`/`bed` and the Home Assistant stream all read this URL; a dead
  camera means the print is unwatched while every service still looks alive.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from printer_doctor.context import Context
from printer_doctor.model import CannotSee, Check, CheckResult, Fix, JsonValue, Verdict

if TYPE_CHECKING:
    from printer_ops.camera import CameraSpec

WAKE_TRIES = 3
JPEG_MAGIC = b"\xff\xd8"


def _spec(ctx: Context) -> CameraSpec:
    from errors import ThreeDError
    from printer_ops.camera import camera_spec

    try:
        return camera_spec(ctx.device)
    except ThreeDError as exc:
        raise CannotSee(f"camera config invalid: {exc.message}", "error") from exc


def _config_applies(ctx: Context) -> str | None:
    from printer_ops.camera import vision_problems

    device = ctx.target.device
    if device is None:
        return "no device (offline printer)"
    if vision_problems(
        (device.watch or {}).get("vision"), f"{device.name}.watch.vision"
    ):
        return None  # a broken block is exactly what this check reports
    spec = _spec(ctx)
    return None if spec.present else f"no camera configured ({spec.source})"


def _config(ctx: Context) -> Verdict:
    from printer_ops.camera import vision_problems

    device = ctx.device
    problems = vision_problems(
        (device.watch or {}).get("vision"), f"{device.name}.watch.vision"
    )
    if problems:
        return Verdict.fail("; ".join(problems), "error", problems=problems)
    spec = _spec(ctx)
    return Verdict.ok(
        f"camera kind {spec.kind}, snapshot {spec.snapshot_url(device.host, device.port)}",
        kind=spec.kind,
        source=spec.source,
        keepalive=spec.keepalive,
    )


def _config_steps(ctx: Context, result: CheckResult) -> list[str]:
    return [
        f"Fix `watch.vision` of {ctx.device.name} in {ctx.device.source}.",
        "Accepted shape: `camera: {kind: moonraker-monitor | mjpg-streamer | snapshot-url | none, ...}` "
        'or `snapshot: URL` (docs/commands/printer.md "Fleet registry", lib/printer_ops/camera.py).',
    ]


def _snapshot_applies(ctx: Context) -> str | None:
    if ctx.target.device is None:
        return "no device (offline printer)"
    try:
        spec = _spec(ctx)
    except CannotSee:
        return "camera config is invalid (see check camera-config)"
    return None if spec.present else f"no camera configured ({spec.source})"


def _grab(ctx: Context, url: str) -> tuple[int | None, bytes, str | None, str | None]:
    """(status, body, content-type, transport error)."""
    try:
        got = ctx.external("GET", url)
    except CannotSee as exc:
        return None, b"", None, exc.reason
    return got.reply.status, got.reply.body, got.reply.headers.get("content-type"), None


def _snapshot(ctx: Context) -> Verdict:
    from errors import ThreeDError
    from printer_ops.webui import redact_url

    device = ctx.device
    spec = _spec(ctx)
    url = spec.snapshot_url(device.host, device.port)
    assert url is not None
    status, body, ctype, error = _grab(ctx, url)
    woke = False
    for _ in range(WAKE_TRIES if spec.keepalive else 0):
        if status == 200 and body.startswith(JPEG_MAGIC):
            break
        if not woke:
            try:
                ctx.wake_camera(spec)
            except (ThreeDError, OSError) as exc:
                error = f"keepalive failed: {exc}"
            woke = True
        ctx.sleep(1.0)
        status, body, ctype, error = _grab(ctx, url)
    evidence: dict[str, JsonValue] = {
        "url": redact_url(url),
        "kind": spec.kind,
        "http_status": status,
        "bytes": len(body),
        "content_type": ctype,
        "woken_with_keepalive": woke,
    }
    if status == 200 and body.startswith(JPEG_MAGIC):
        return Verdict.ok(f"snapshot is a JPEG ({len(body)} bytes)", **evidence)
    if status is None:
        return Verdict.fail(
            f"snapshot URL does not answer: {error}", "error", **evidence
        )
    if status != 200:
        return Verdict.fail(f"snapshot URL answers HTTP {status}", "error", **evidence)
    return Verdict.fail(
        f"snapshot answers 200 but is not a JPEG (starts {body[:4].hex() or 'empty'})",
        "error",
        **evidence,
    )


def _snapshot_steps(ctx: Context, result: CheckResult) -> list[str]:
    url = result.evidence.get("url")
    kind = result.evidence.get("kind")
    steps = [f"Open {url} in a browser: it must show one still frame."]
    if kind == "mjpg-streamer":
        steps.append(
            "The streamer is not running: restart it on the printer (Creality Helper Script: "
            "Camera menu -> restart mjpg-streamer, or `/etc/init.d/S50mjpg_streamer restart` [INFERENCE: path differs per install])."
        )
    elif kind == "moonraker-monitor":
        steps.append(
            "The printer's camera service (unisrv) writes the file only while `camera.start_monitor` "
            "is renewed; restart the printer's camera service or reboot the printer."
        )
    else:
        steps.append(
            "Restart whatever serves that URL, or fix `watch.vision.camera.url` in "
            + str(ctx.device.source)
            + "."
        )
    steps.append(f"Then `3d printers doctor {ctx.name} --check camera-snapshot`.")
    return steps


CHECKS = (
    Check(
        id="camera-config",
        title="watch.vision camera settings resolve",
        severity="error",
        applies_to="printers with a camera (configured or the model's default)",
        contacts_printer=False,
        applies=_config_applies,
        run=_config,
        fix=Fix(
            "manual-only",
            "fix watch.vision in the fleet / printer-devices.yaml",
            _config_steps,
        ),
    ),
    Check(
        id="camera-snapshot",
        title="the camera snapshot URL answers a JPEG",
        severity="error",
        applies_to="printers with a camera",
        applies=_snapshot_applies,
        run=_snapshot,
        fix=Fix(
            "manual-only",
            "restart the camera streamer / fix the snapshot URL",
            _snapshot_steps,
        ),
    ),
)
