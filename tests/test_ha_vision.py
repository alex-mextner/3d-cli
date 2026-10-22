"""`3d ha vision` (lib/ha_vision.py): the Home Assistant side of camera vision, generated for EVERY
fleet printer with a camera, so nothing exists for the U1 alone.

The fleet-iterating tests read the REAL fleet/printers.yaml and the COMMITTED integration files: a
camera printer added to the fleet without its generated entities / dashboard card / stream, or a
generated file edited by hand, fails here.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from fleet_fixtures import REPO_FLEET

import ha_vision
import vision_geometry
from errors import UsageError
from ha_deploy import load_ha_yaml
from printer_ops.camera import camera_spec
from printer_ops.devices import fleet_devices
from printops.fleet import load_fleet

REPO = Path(__file__).resolve().parents[1]
HA = REPO / "integrations" / "homeassistant"
MOONRAKER_ENTRY = """\
{name}:
  model: {model}
  owner: own
  access: full
  adapter: moonraker
  host: {host}
  heads: [{{label: T1, index: 0, usable: true}}]
{watch}"""
MJPG_WATCH = "  watch:\n    vision:\n      camera: {kind: mjpg-streamer, port: 8081}\n"


def fleet_with(tmp_path: Path, *entries: str) -> Any:
    """The repo fleet plus extra entries, as a loaded Fleet."""
    path = tmp_path / "fleet.yaml"
    path.write_text(
        REPO_FLEET.read_text(encoding="utf-8") + "\n" + "\n".join(entries),
        encoding="utf-8",
    )
    fleet = load_fleet(path)
    assert fleet is not None
    return fleet


def copy_tree(tmp_path: Path) -> Path:
    """A scratch checkout holding only integrations/homeassistant (what the generator reads/writes)."""
    root = tmp_path / "checkout"
    shutil.copytree(
        HA,
        root / "integrations" / "homeassistant",
        ignore=shutil.ignore_patterns(
            "__pycache__", "tests", "www", "docs", "scripts", "requirements"
        ),
    )
    return root


def settings(root: Path) -> ha_vision.Settings:
    return ha_vision.load_settings(root)


@pytest.fixture(scope="module")
def repo_fleet() -> Any:
    fleet = load_fleet(REPO_FLEET)
    assert fleet is not None
    return fleet


# ---- the real fleet vs the committed files --------------------------------------------------------


def test_the_committed_files_are_what_the_fleet_generates(repo_fleet: Any) -> None:
    files, printers, _ = ha_vision.generate(REPO, repo_fleet)

    assert ha_vision.drift(REPO, files) == [], (
        "run `3d ha vision --write` and commit the result"
    )
    assert ha_vision.missing(REPO, printers) == []


def test_every_fleet_printer_with_a_camera_has_its_vision_entities_stream_and_card(
    repo_fleet: Any,
) -> None:
    """(a) the fleet-iterating gate: a camera printer (camera config or a bundled calibration) with
    any generated artifact missing from the committed files fails, naming the printer and entity."""
    devices = fleet_devices(repo_fleet)
    with_camera = [n for n, d in devices.items() if camera_spec(d).present]
    _, printers, _ = ha_vision.generate(REPO, repo_fleet)

    assert [p.name for p in printers] == with_camera
    assert {"u1", "k1se"} <= set(with_camera)
    for name, printer in repo_fleet.printers.items():
        if vision_geometry.has_bundled_calibration(printer.model):
            assert name in with_camera, (
                f"{name}: has a camera calibration but no camera in watch.vision.camera"
            )
        if name in devices and "vision" in devices[name].watch:
            assert name in with_camera, (
                f"{name}: watch.vision is set but resolves to no camera"
            )
    assert ha_vision.missing(REPO, printers) == []


def test_the_ghost_exists_exactly_for_printers_with_a_bundled_calibration(
    repo_fleet: Any,
) -> None:
    _, printers, _ = ha_vision.generate(REPO, repo_fleet)
    package = (HA / "packages" / "printer_vision.yaml").read_text(encoding="utf-8")

    by_name = {p.name: p for p in printers}
    assert by_name["u1"].ghost and not by_name["k1se"].ghost
    assert "input_boolean.u1_camera_ghost" in (
        HA / "dashboards" / "ui-3d-printers.yaml"
    ).read_text(encoding="utf-8")
    assert "k1se_camera_ghost" not in package.replace("# k1se: NO ghost", "")
    assert (
        "# k1se: NO ghost — no bundled camera calibration for Creality K1 SE" in package
    )
    assert "No ghost overlay for K1 SE" in (
        HA / "dashboards" / "ui-3d-printers.yaml"
    ).read_text(encoding="utf-8")


def test_the_u1_output_is_the_one_that_was_hand_written(repo_fleet: Any) -> None:
    """The U1's proxy service, ghost helpers and automation are what the hand-written files held."""
    compose = yaml.safe_load(
        (HA / "compose" / "3d.docker-compose.yaml").read_text(encoding="utf-8")
    )
    proxy = compose["services"]["u1-camera-proxy"]
    assert proxy["command"] == ["python", "/config/scripts/u1_mjpeg_proxy.py"]
    assert proxy["environment"] == [
        "U1_CAMERA_URL=http://192.168.0.26:7125/server/files/camera/monitor.jpg",
        "U1_CAMERA_FPS=1.0",
        "U1_CAMERA_PORT=8090",
        "U1_CAMERA_START_MONITOR=true",
        "U1_CAMERA_START_MONITOR_URL=http://192.168.0.26:7125/server/mqtt/publish",
        "U1_CAMERA_START_MONITOR_INTERVAL=240",
        "U1_CAMERA_NAME=u1",  # the script's own defaults, now explicit
        "U1_GHOST_MODEL=Snapmaker U1",
        "U1_PRINTER_URL=http://192.168.0.26:7125",
    ]
    assert (
        proxy["expose"] == ["8090"] and "k1se-camera-proxy" not in compose["services"]
    )
    go2rtc = yaml.safe_load(
        (HA / "go2rtc" / "go2rtc.yaml").read_text(encoding="utf-8")
    )["streams"]
    assert go2rtc["u1_camera"] == [
        "ffmpeg:http://u1-camera-proxy:8090/u1.mjpeg#video=h264",
        "http://u1-camera-proxy:8090/u1.mjpeg",
    ]
    assert go2rtc["k1se_camera"] == [
        "http://192.168.0.27:8080/?action=stream",
        "http://192.168.0.27:4408/webcam/?action=stream",
    ]
    package = load_ha_yaml(
        (HA / "packages" / "printer_vision.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(package, dict)
    assert package["rest_command"] == {
        "u1_camera_ghost_set": {
            "url": "http://u1-camera-proxy:8090/ghost",
            "method": "post",
            "content_type": "application/json",
            "payload": '{"enabled": {{ enabled | bool | tojson }}}',
            "timeout": 10,
        }
    }
    assert package["input_boolean"] == {
        "u1_camera_ghost": {
            "name": "U1: призрак остатка на камере",
            "icon": "mdi:cube-outline",
        }
    }
    sync = package["automation"][0]
    assert (
        sync["id"] == "u1_camera_ghost_sync"
        and sync["alias"] == "U1: призрак остатка → прокси камеры"
    )
    assert sync["actions"] == [
        {
            "action": "rest_command.u1_camera_ghost_set",
            "data": {
                "enabled": "{{ is_state('input_boolean.u1_camera_ghost', 'on') }}"
            },
        }
    ]


# ---- the check can fail ---------------------------------------------------------------------------


def broken(tmp_path: Path, relative: str, old: str, new: str) -> Path:
    root = copy_tree(tmp_path)
    target = root / relative
    text = target.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not in {relative}"
    target.write_text(text.replace(old, new), encoding="utf-8")
    return root


@pytest.mark.parametrize(
    ("relative", "old", "new", "expected"),
    [
        (
            "packages/printer_vision.yaml",
            "unique_id: printer_k1_se_vision_events",
            "unique_id: printer_k1_se_vision_eventz",
            "k1se: sensor.printer_k1_se_vision_events is not defined in",
        ),
        (
            "packages/printer_vision.yaml",
            "input_boolean:\n  u1_camera_ghost:",
            "input_boolean:\n  u1_camera_ghosts:",
            "u1: input_boolean.u1_camera_ghost is not defined in",
        ),
        (
            "packages/printer_vision.yaml",
            "u1_camera_ghost_sync",
            "u1_camera_ghost_syncs",
            "u1: automation u1_camera_ghost_sync is not defined in",
        ),
        (
            "packages/printer_vision.yaml",
            "/v1/printers/k1se/vision",
            "/v1/printers/k1/vision",
            "k1se: no REST resource of /v1/printers/k1se/vision",
        ),
        (
            "go2rtc/go2rtc.yaml",
            "  k1se_camera:",
            "  k1se_cam:",
            "k1se: go2rtc stream k1se_camera is not in",
        ),
        (
            "compose/3d.docker-compose.yaml",
            "  u1-camera-proxy:",
            "  u1-camera-prox:",
            "u1: compose service u1-camera-proxy is not in",
        ),
        (
            "dashboards/ui-3d-printers.yaml",
            "binary_sensor.printer_u1_vision_alive",
            "binary_sensor.printer_u1_vision_alve",
            "u1: binary_sensor.printer_u1_vision_alive is not on the dashboard",
        ),
        (
            "dashboards/ui-3d-printers.yaml",
            "- url: k1se_camera",
            "- url: k1se_cam",
            "the dashboard camera card of stream k1se_camera is missing",
        ),
        (
            "manifest.yaml",
            "src: packages/printer_vision.yaml",
            "src: packages/printer_visio.yaml",
            "packages/printer_vision.yaml is not a `files` entry",
        ),
    ],
)
def test_a_missing_entity_stream_service_or_card_is_reported(
    repo_fleet: Any, tmp_path: Path, relative: str, old: str, new: str, expected: str
) -> None:
    root = broken(tmp_path, f"integrations/homeassistant/{relative}", old, new)
    _, printers, _ = ha_vision.generate(REPO, repo_fleet)

    problems = ha_vision.missing(root, printers)

    assert any(expected in p for p in problems), problems


def test_a_hand_edit_of_a_generated_file_is_drift(
    repo_fleet: Any, tmp_path: Path
) -> None:
    root = broken(
        tmp_path,
        "integrations/homeassistant/packages/printer_vision.yaml",
        "scan_interval: 30",
        "scan_interval: 31",
    )
    files, _, _ = ha_vision.generate(root, repo_fleet)

    assert ha_vision.drift(root, files) == [ha_vision.PACKAGE]


# ---- a printer the repo fleet does not have yet ------------------------------------------------------


def test_a_new_camera_printer_gets_everything_but_the_ghost_and_a_proxy(
    tmp_path: Path,
) -> None:
    root = copy_tree(tmp_path)
    fleet = fleet_with(
        tmp_path,
        MOONRAKER_ENTRY.format(
            name="voron", model="Voron Trident 350", host="192.0.2.50", watch=MJPG_WATCH
        ),
    )
    begin, end = ha_vision.markers("dashboard:voron")

    with (
        pytest.raises(UsageError) as exc
    ):  # a column needs its status cards: the generator says where to put the region
        ha_vision.generate(root, fleet)
    assert begin in exc.value.render()
    dash = root / ha_vision.DASHBOARD
    dash.write_text(
        dash.read_text(encoding="utf-8").replace(
            "\n      - type: grid\n        cards:\n          - type: gauge\n            name: U1 Progress",
            f"\n      - type: grid\n        cards:\n          {begin}\n          {end}\n          - type: gauge\n            name: U1 Progress",
        ),
        encoding="utf-8",
    )
    files, printers, _ = ha_vision.generate(root, fleet)
    for path, text in files.items():
        (root / path).write_text(text, encoding="utf-8")

    voron = next(p for p in printers if p.name == "voron")
    assert (voron.title, voron.slug, voron.kind, voron.ghost, voron.proxied) == (
        "Trident 350",
        "trident_350",
        "mjpg-streamer",
        False,
        False,
    )
    assert ha_vision.missing(root, printers) == []
    streams = yaml.safe_load((root / ha_vision.GO2RTC).read_text(encoding="utf-8"))[
        "streams"
    ]
    assert streams["voron_camera"] == ["http://192.0.2.50:8081/?action=stream"]
    assert "voron-camera-proxy" not in (root / ha_vision.COMPOSE).read_text(
        encoding="utf-8"
    )
    package = (root / ha_vision.PACKAGE).read_text(encoding="utf-8")
    assert (
        "unique_id: printer_trident_350_vision_verdict" in package
        and "voron_camera_ghost" not in package.replace("# voron: NO ghost", "")
    )
    assert (
        "# voron: NO ghost — no bundled camera calibration for Voron Trident 350"
        in package
    )
    assert any(
        line.startswith(
            "voron: camera mjpg-streamer, direct stream, vision sensors, NO ghost"
        )
        for line in ha_vision.plan(fleet, printers)
    )


def test_a_printer_with_a_bundled_calibration_gets_the_ghost_and_a_proxy_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        vision_geometry._BUNDLED, "voron24", "u1-camera.yaml"
    )  # stands in for lib/data/vision/voron24-camera.yaml
    root = copy_tree(tmp_path)
    fleet = fleet_with(
        tmp_path,
        MOONRAKER_ENTRY.format(
            name="voron", model="Voron 2.4", host="192.0.2.50", watch=MJPG_WATCH
        ),
    )
    begin, end = ha_vision.markers("dashboard:voron")
    dash = root / ha_vision.DASHBOARD
    dash.write_text(
        dash.read_text(encoding="utf-8") + f"          {begin}\n          {end}\n",
        encoding="utf-8",
    )

    files, printers, _ = ha_vision.generate(root, fleet)
    for path, text in files.items():
        (root / path).write_text(text, encoding="utf-8")

    voron = next(p for p in printers if p.name == "voron")
    assert (
        voron.ghost and voron.proxy_port == 8091
    )  # u1 holds 8090: ports follow the fleet order
    assert ha_vision.missing(root, printers) == []
    service = yaml.safe_load((root / ha_vision.COMPOSE).read_text(encoding="utf-8"))[
        "services"
    ]["voron-camera-proxy"]
    assert service["environment"] == [
        "U1_CAMERA_URL=http://192.0.2.50:8081/?action=snapshot",
        "U1_CAMERA_FPS=1.0",
        "U1_CAMERA_PORT=8091",
        "U1_CAMERA_START_MONITOR=false",
        "U1_CAMERA_NAME=voron",
        "U1_GHOST_MODEL=Voron 2.4",
        "U1_PRINTER_URL=http://192.0.2.50:7125",
    ]
    streams = yaml.safe_load((root / ha_vision.GO2RTC).read_text(encoding="utf-8"))[
        "streams"
    ]
    assert streams["voron_camera"][-1] == "http://voron-camera-proxy:8091/voron.mjpeg"
    package = load_ha_yaml((root / ha_vision.PACKAGE).read_text(encoding="utf-8"))
    assert isinstance(package, dict) and set(package["input_boolean"]) == {
        "u1_camera_ghost",
        "voron_camera_ghost",
    }


def test_a_printer_without_a_camera_gets_nothing_and_does_not_crash(
    tmp_path: Path,
) -> None:
    fleet = fleet_with(
        tmp_path,
        MOONRAKER_ENTRY.format(
            name="bare", model="Voron 2.4", host="192.0.2.51", watch=""
        ),
        MOONRAKER_ENTRY.format(
            name="nocam",
            model="Snapmaker U1",
            host="192.0.2.52",
            watch="  watch:\n    vision:\n      camera: {kind: none}\n",
        ),
    )
    _, printers, _ = ha_vision.generate(REPO, fleet)

    lines = ha_vision.plan(fleet, printers)
    assert [p.name for p in printers] == ["u1", "k1se"]
    assert (
        "bare: no camera (bare: no watch.vision.camera and no model default) -> nothing generated"
        in lines
    )
    assert "nocam: no camera (nocam.watch.vision.camera) -> nothing generated" in lines
    assert "p1s: no camera (offline: never contacted) -> nothing generated" in lines


def test_the_stream_modes_follow_the_camera_kind() -> None:
    proxied = ha_vision.VisionPrinter(
        "u1",
        "U1",
        "u1",
        "Snapmaker U1",
        "h",
        7125,
        "moonraker-monitor",
        "http://h/x",
        (),
        True,
        True,
        8090,
    )
    direct = ha_vision.VisionPrinter(
        "k",
        "K1 SE",
        "k1_se",
        "Creality K1 SE",
        "h",
        7125,
        "mjpg-streamer",
        "http://h/x",
        ("http://h/s",),
        False,
        False,
        None,
    )
    none = ha_vision.VisionPrinter(
        "w",
        "W",
        "w",
        "W",
        "h",
        7125,
        "snapshot-url",
        "http://h/x",
        (),
        False,
        False,
        None,
    )

    assert ha_vision.render_dashboard(proxied).count(
        "mode: "
    ) == 3 and "mode: webrtc" in ha_vision.render_dashboard(proxied)
    assert ha_vision.render_dashboard(direct).count(
        "mode: "
    ) == 1 and ".play {display: none" in ha_vision.render_dashboard(direct)
    assert "custom:webrtc-camera" not in ha_vision.render_dashboard(
        none
    ) and "no live stream" in ha_vision.render_dashboard(none)
    assert (
        ha_vision.render_go2rtc([none])
        == "streams:\n  w_camera:\n    []  # no live stream: set watch.vision.camera.streams in fleet/printers.yaml\n"
    )
    assert ha_vision.render_go2rtc([]) == "streams: {}\n"


def test_titles_and_slugs_follow_the_models() -> None:
    assert [
        ha_vision.title_of(m)
        for m in (
            "Snapmaker U1",
            "Creality K1 SE",
            "Bambu Lab P1S",
            "Prusa MK4",
            "Generic",
        )
    ] == ["U1", "K1 SE", "P1S", "MK4", "Generic"]
    assert [ha_vision.slug_of(t) for t in ("U1", "K1 SE", "Trident 350")] == [
        "u1",
        "k1_se",
        "trident_350",
    ]


@pytest.mark.parametrize(
    "block",
    [
        "generate:\n  printops_api: http://x\n",
        "generate:\n  printops_api: ftp://x\n  proxy_port_base: 8090\n  scan_interval_s: 30\n  alive_max_age_s: 120\n",
        "generate:\n  printops_api: http://x\n  proxy_port_base: 0\n  scan_interval_s: 30\n  alive_max_age_s: 120\n",
    ],
)
def test_a_broken_generate_block_is_refused(tmp_path: Path, block: str) -> None:
    root = tmp_path / "c"
    (root / "integrations" / "homeassistant").mkdir(parents=True)
    (root / ha_vision.MANIFEST).write_text(block, encoding="utf-8")

    with pytest.raises(Exception, match="generate"):
        ha_vision.load_settings(root)
