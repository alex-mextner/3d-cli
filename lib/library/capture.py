"""library/capture.py — the `capture` half of the `photo` action: the table photo of a make with the bed lifted.

WHAT: resolves the make and its printer, builds the adapter/camera/bed config of that printer, runs
`bed_photo.capture_table_photo` (guarded Z move, fresh frame, restore in a `finally`) and registers the file as
`photo(kind, z_mm)` of the make. Dry run is the default: the printer is READ, the lift is planned and reported,
nothing moves, no frame is taken and no row is written.

IMPORT RULE: this module is imported lazily by `library.actions.photo`; the printer/camera stack and numpy are
imported inside the functions that need them.
"""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path
from typing import Any

from errors import InvalidArgument
from library import actions, db as librarydb, records


def default_out(make_uid: str, kind: str) -> Path:
    from cli import paths

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    unique = uuid.uuid4().hex[
        :8
    ]  # two captures within one second must not share a file
    return (
        paths.data_dir()
        / "library"
        / "photos"
        / make_uid
        / f"{stamp}-{kind}-{unique}.jpg"
    )


def capture(
    *,
    make: str,
    kind: str,
    printer: str | None,
    lift: str | float,
    camera: str | None,
    part_height_mm: float | None,
    out: str | None,
    dry_run: bool,
    db_path: str | None,
) -> actions.ActionResult:
    import bed_photo
    from printer_ops import make_adapter
    from printer_ops.bed import bed_config
    from printer_ops.camera import open_camera, require_camera
    from printer_ops.devices import get_device

    bed_photo.check_part_height(part_height_mm)
    conn = librarydb.open_library(db_path)
    try:
        make_row = records.resolve(conn, "make", make, command="photo")
        name = printer or (str(make_row["printer"]) if make_row["printer"] else "")
        if not name:
            raise InvalidArgument(
                "--printer",
                "(missing)",
                ["a printer name from `3d printer list`; the make has none recorded"],
                command="photo",
            )
        device = get_device(name)
        cfg = bed_config(device)
        view = bed_photo.calibrated_view(device, cfg) if lift == "auto" else None
        adapter = make_adapter(device, timeout=10.0)
        cam = open_camera(device, require_camera(device, "photo"), snapshot=camera)
        target = Path(out) if out else default_out(str(make_row["uid"]), kind)
        doc = bed_photo.capture_table_photo(
            device,
            cfg,
            adapter=adapter,
            camera=cam,
            out=target,
            lift=lift,
            part_height_mm=part_height_mm,
            view=view,
            dry_run=dry_run,
        )
        data: dict[str, Any] = {
            "make": str(make_row["uid"]),
            "kind": kind,
            **doc,
            "photo": None,
        }
        if dry_run:
            return actions.ok_result("photo", data)
        row, _ = records.add_photo(
            conn,
            str(doc["path"]),
            make=str(make_row["uid"]),
            kind=kind,
            z_mm=doc["z_mm"],
        )
        data["photo"] = str(row["uid"])
        if not doc["restored"]:
            return actions.error_result(
                "photo",
                [
                    actions.issue(
                        "bed_not_restored",
                        f"the photo was taken but the bed is NOT back where it was: {doc['restore_error']}",
                        [
                            f"Check the printer now: `3d printer status {device.name}`; move the bed by hand if needed."
                        ],
                    )
                ],
                data,
            )
        warnings = (
            []
            if doc["fresh"]
            else [
                actions.issue(
                    "stale_frame",
                    "the camera returned the same frame as before the move: it may not show the lifted bed",
                    [
                        "Take another with `3d photo capture`, or check the camera with `3d printer vision NAME --once`."
                    ],
                )
            ]
        )
        return actions.ok_result("photo", data, warnings)
    finally:
        conn.close()
