"""printer_ops.camera: one reader of `watch.vision.camera` for vision, guard, bed, dossier and HA.

The U1's `monitor.jpg` + keepalive is one camera KIND (data in the fleet, default per model in
lib/data/vision/cameras.yaml), not a default of every Moonraker printer: a printer without a camera
setting or a model default has no camera, and every consumer says so instead of fetching a U1 path.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from fleet_fixtures import REPO_FLEET

from errors import InvalidArgument, UsageError
from printer_ops import camera
from printer_ops.devices import DeviceConfigError, _parse, fleet_devices
from printops.fleet import FleetError, load_fleet, validate


def device(
    watch: dict[str, Any] | None,
    model: str | None = None,
    host: str = "10.0.0.9",
    port: int = 7125,
) -> Any:
    """What camera_spec reads of a Device."""
    return SimpleNamespace(
        name="p",
        host=host,
        port=port,
        model=model,
        watch=watch or {},
        source=Path("devices.yaml"),
        adapter="moonraker",
    )


def test_the_repo_fleet_cameras_are_data_not_defaults() -> None:
    devices = fleet_devices(load_fleet(REPO_FLEET))
    u1, k1 = camera.camera_spec(devices["u1"]), camera.camera_spec(devices["k1se"])

    assert (u1.kind, u1.snapshot, u1.keepalive, u1.streams) == (
        camera.MONITOR,
        camera.MONITOR_PATH,
        True,
        (),
    )
    assert u1.source == "u1.watch.vision.camera"
    assert (k1.kind, k1.keepalive) == (camera.MJPG, False)
    assert k1.snapshot == "http://192.168.0.27:8080/?action=snapshot"
    assert k1.streams == (
        "http://192.168.0.27:8080/?action=stream",
        "http://192.168.0.27:4408/webcam/?action=stream",
    )


def test_mjpg_streamer_derives_its_urls_from_the_host_and_port() -> None:
    spec = camera.camera_spec(
        device(
            {"vision": {"camera": {"kind": "mjpg-streamer", "port": 8081}}},
            host="192.0.2.7",
        )
    )

    assert spec.snapshot == "http://192.0.2.7:8081/?action=snapshot"
    assert spec.streams == ("http://192.0.2.7:8081/?action=stream",)
    assert spec.keepalive is False


def test_the_snapshot_shorthand_is_a_snapshot_url_camera_and_a_bare_path_is_the_printers() -> (
    None
):
    spec = camera.camera_spec(
        device({"vision": {"snapshot": "webcam/?action=snapshot"}}, host="192.0.2.7")
    )

    assert (spec.kind, spec.keepalive) == (camera.SNAPSHOT_URL, False)
    assert (
        spec.snapshot_url("192.0.2.7", 7125)
        == "http://192.0.2.7:7125/webcam/?action=snapshot"
    )
    assert (
        camera.camera_spec(
            device({"vision": {"snapshot": "http://cam/s.jpg"}})
        ).snapshot_url("x", 1)
        == "http://cam/s.jpg"
    )


def test_a_model_default_covers_an_override_without_a_watch_block() -> None:
    u1 = camera.camera_spec(device({}, model="Snapmaker U1"))

    assert (u1.kind, u1.keepalive) == (
        camera.MONITOR,
        True,
    ) and "model default" in u1.source
    assert camera.camera_spec(device({}, model="snapmaker-u1")).kind == camera.MONITOR


def test_a_printer_without_camera_setting_or_model_default_has_none_not_a_u1_path() -> (
    None
):
    for model in (None, "Creality K1 SE", "Bambu Lab P1S"):
        spec = camera.camera_spec(device({"tg_fallback": True}, model=model))
        assert (
            not spec.present
            and spec.snapshot is None
            and spec.snapshot_url("h", 1) is None
        )

    with pytest.raises(UsageError) as exc:
        camera.require_camera(device({}, model="Creality K1 SE"), "printer vision")
    text = exc.value.render()
    assert (
        "p: no camera" in text and "mjpg-streamer" in text and "monitor.jpg" not in text
    )


def test_kind_none_is_explicit_and_beats_the_model_default() -> None:
    spec = camera.camera_spec(
        device({"vision": {"camera": {"kind": "none"}}}, model="Snapmaker U1")
    )

    assert not spec.present and spec.keepalive is False


def test_keepalive_can_be_switched_per_printer_in_either_place() -> None:
    assert (
        camera.camera_spec(
            device({"vision": {"keepalive": False}}, model="Snapmaker U1")
        ).keepalive
        is False
    )
    block = {"camera": {"kind": "moonraker-monitor", "keepalive": False}}
    assert camera.camera_spec(device({"vision": block})).keepalive is False
    assert camera.camera_spec(
        device(
            {
                "vision": {
                    "camera": {"kind": "snapshot-url", "url": "http://c/s"},
                    "keepalive": True,
                }
            }
        )
    ).keepalive


@pytest.mark.parametrize(
    ("vision", "fragment"),
    [
        ({"camera": {"kind": "webcam"}}, "kind: 'webcam' is not one of"),
        ({"camera": "mjpg-streamer"}, "must be a mapping"),
        ({"camera": {"kind": "snapshot-url"}}, "url: required for snapshot-url"),
        ({"camera": {"kind": "mjpg-streamer", "url": ""}}, "url: must be a non-empty"),
        (
            {"camera": {"kind": "moonraker-monitor", "port": 1}},
            "port: not a moonraker-monitor key",
        ),
        (
            {"camera": {"kind": "mjpg-streamer", "port": 70000}},
            "port: an integer 1-65535",
        ),
        (
            {"camera": {"kind": "mjpg-streamer", "streams": ["/stream"]}},
            "streams: a non-empty list of absolute",
        ),
        (
            {"camera": {"kind": "mjpg-streamer", "keepalive": "yes"}},
            "keepalive: true or false",
        ),
        ({"camera": {"kind": "none"}, "snapshot": "http://c/s"}, "not both"),
        ({"snapshot": 7}, "snapshot: must be a non-empty"),
        ({"keepalive": "no"}, "keepalive: true or false"),
    ],
)
def test_every_broken_camera_setting_is_named(
    vision: dict[str, Any], fragment: str
) -> None:
    problems = camera.vision_problems(vision, "u.watch.vision")

    assert any(fragment in p for p in problems), problems
    with pytest.raises(InvalidArgument):
        camera.camera_spec(device({"vision": vision}))


def test_the_fleet_loader_and_the_device_parser_both_refuse_a_broken_camera(
    tmp_path: Path,
) -> None:
    entry = {
        "model": "Creality K1 SE",
        "owner": "own",
        "access": "full",
        "adapter": "moonraker",
        "host": "10.0.0.5",
        "heads": [{"label": "T1", "index": 0}],
        "watch": {"vision": {"camera": {"kind": "usb"}}},
    }
    printers, problems = validate({"k": entry}, tmp_path / "printers.yaml")

    assert not printers and [p.message for p in problems] == [
        "k.watch.vision.camera.kind: 'usb' is not one of moonraker-monitor, mjpg-streamer, snapshot-url, none"
    ]
    path = tmp_path / "printer-devices.yaml"
    with pytest.raises(DeviceConfigError, match="watch.vision.camera.kind"):
        _parse(
            path,
            yaml.safe_load(
                yaml.safe_dump(
                    {
                        "k": {
                            "adapter": "moonraker",
                            "host": "h",
                            **{"watch": entry["watch"]},
                        }
                    }
                )
            ),
        )
    fleet = tmp_path / "fleet.yaml"
    fleet.write_text(yaml.safe_dump({"k": entry}), encoding="utf-8")
    with pytest.raises(FleetError):
        load_fleet(fleet)


def test_open_camera_builds_the_frame_source_of_the_spec() -> None:
    monitor = camera.open_camera(
        device({}, model="Snapmaker U1"),
        camera.camera_spec(device({}, model="Snapmaker U1")),
    )
    mjpg = camera.open_camera(
        device({}),
        camera.camera_spec(device({"vision": {"camera": {"kind": "mjpg-streamer"}}})),
    )

    assert (
        monitor.url == f"http://10.0.0.9:7125{camera.MONITOR_PATH}"
        and monitor.keepalive_enabled
    )
    assert (
        mjpg.url == "http://10.0.0.9:8080/?action=snapshot"
        and not mjpg.keepalive_enabled
    )
    assert camera.open_camera(
        device({}),
        camera.camera_spec(device({"vision": {"camera": {"kind": "mjpg-streamer"}}})),
        keepalive=True,
    ).keepalive_enabled


def test_bed_state_uses_the_same_camera_and_refuses_a_printer_without_one() -> None:
    import bed_state

    assert bed_state.camera_for(device({}, model="Snapmaker U1")).url.endswith(
        camera.MONITOR_PATH
    )
    with pytest.raises(UsageError, match="no camera"):
        bed_state.camera_for(device({}, model="Creality K1 SE"))


def test_a_snapshot_override_takes_the_keepalive_of_the_file_it_names() -> None:
    """`--snapshot` swaps the file: the configured camera's keepalive (the Snapmaker monitor
    wake-up) must follow the URL, not stay with the printer (reviewer finding, PR #482)."""
    u1 = device({"vision": {"camera": {"kind": camera.MONITOR}}}, model=None)
    spec = camera.camera_spec(u1)
    assert spec.keepalive is True
    assert camera.open_camera(u1, spec).keepalive_enabled is True
    assert camera.open_camera(u1, spec, snapshot="http://cam/x.jpg").keepalive_enabled is False
    k1 = device({"vision": {"camera": {"kind": camera.MJPG}}})
    other = camera.camera_spec(k1)
    assert camera.open_camera(k1, other).keepalive_enabled is False
    assert camera.open_camera(k1, other, snapshot=camera.MONITOR_PATH).keepalive_enabled is True
    assert camera.open_camera(k1, other, snapshot="/x.jpg", keepalive=True).keepalive_enabled is True
