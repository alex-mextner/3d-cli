"""`3d calibrate photo|rectify` end to end: bin/3d → a topcam node around a fake C920 that sees a
synthetic plate (known geometry) + the loopback FakeMoonraker + a temporary fleet file (#342)."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml
from printer_fakes import FakeMoonraker
from topcam_fixtures import FakeCamera, install_fake_camera, serve_node

from .workflow_helper import run_cli

cv2 = pytest.importorskip("cv2")
if not hasattr(cv2, "aruco") or not hasattr(cv2.aruco, "ArucoDetector"):
    pytest.skip("OpenCV >= 4.7 with aruco needed", allow_module_level=True)

from calib_scene import FIDUCIALS, render_scene, to_yuyv, write_card  # noqa: E402

FID = ";".join(f"{f['id']}:{f['center_mm'][0]},{f['center_mm'][1]}" for f in FIDUCIALS)


@pytest.fixture
def fake_camera(monkeypatch: pytest.MonkeyPatch) -> FakeCamera:
    return install_fake_camera(monkeypatch)


@pytest.fixture
def moonraker() -> Iterator[FakeMoonraker]:
    fake = FakeMoonraker()
    fake.status["toolhead"] = {
        "homed_axes": "xyz",
        "position": [100.0, 100.0, 5.0, 0.0],
        "axis_minimum": [-5.0, -0.5, -3.0, 0.0],
        "axis_maximum": [229.0, 227.0, 250.0, 0.0],
    }
    with fake.serve():
        yield fake


def _fleet(tmp_path: Path, port: int, url: str, card: Path | None = None) -> Path:
    photo: dict[str, Any] = {
        "camera": url,
        "res": "2304x1536",
        "exposure": 300,
        "focus": 30,
        "wb": 4500,
        "frames": 2,
        "printer_light": {
            "on": "SET_PIN PIN=LED VALUE=1",
            "off": "SET_PIN PIN=LED VALUE=0",
        },
        "pose": {"x": 110, "y": 220, "z": 150, "speed": 6000},
    }
    if card is not None:
        photo["scale_ref"] = card.name
    f = tmp_path / "printers.yaml"
    f.write_text(
        "# test fleet\n"
        + yaml.safe_dump(
            {
                "k1se": {
                    "model": "Creality K1 SE",
                    "heads": [{"label": "T1", "index": 0}],
                    "owner": "own",
                    "access": "full",
                    "adapter": "moonraker",
                    "host": "127.0.0.1",
                    "port": port,
                    "photo": photo,
                }
            },
            sort_keys=False,
        )
    )
    return f


def _env(fleet: Path) -> dict[str, str]:
    return {"THREED_FLEET": str(fleet)}


def test_photo_rectifies_a_plate_and_measures_the_scale(
    tmp_path: Path,
    fake_camera: FakeCamera,
    moonraker: FakeMoonraker,
) -> None:
    """The user shoots the plate with the top camera; the photo is rectified and the printer's X/Y scale error is measured from the card."""
    fake_camera.scene = to_yuyv(
        render_scene(
            tmp_path / "scene.png", card_at=(70.0, 80.0), printer_scale=(1.004, 0.997)
        )
    )
    card = write_card(tmp_path / "card.json")
    out = tmp_path / "shot"
    with serve_node(fake_camera) as (url, _):
        fleet = _fleet(tmp_path, moonraker.port, url, card)
        result = run_cli(
            tmp_path,
            "calibrate",
            "photo",
            "--printer",
            "k1se",
            "--fiducials",
            FID,
            "--frames",
            "3",
            "--out",
            str(out),
            env_extra=_env(fleet),
        )
    assert result.returncode == 0, result.stderr
    assert "rectify: 4 fiducials [0, 1, 2, 3]" in result.stdout
    record = json.loads((out / "calib-photo.json").read_text())
    assert record["schema"] == "calib-photo/1" and record["status"] == "ok"
    assert (
        record["origin"]["frames"] == "cli" and record["origin"]["exposure"] == "fleet"
    )
    assert (
        record["photo"]["frames"] == 3
        and record["camera"]["controls"]["exposure_time_absolute"] == 300
    )
    assert fake_camera.captures[-1]["controls"]["focus"] == 30
    rect = record["rectify"]
    assert rect["residual_mm"]["rms"] < 0.15
    assert rect["scale"]["true_mm_per_bed_mm"]["x"] == pytest.approx(1.004, abs=0.001)
    assert rect["scale"]["true_mm_per_bed_mm"]["y"] == pytest.approx(0.997, abs=0.001)
    for name in ("photo.png", "rectified.png", "detections.png"):
        assert (out / name).is_file()
    assert record["pose"] == {
        "moved": False,
        "configured": {"x": 110, "y": 220, "z": 150, "speed": 6000},
    }
    assert (
        moonraker.requests == []
    )  # no --move/--printer-light: the printer was not contacted


def test_move_needs_yes_and_sends_nothing(
    tmp_path: Path, fake_camera: FakeCamera, moonraker: FakeMoonraker
) -> None:
    """The user asks for --move without --yes: nothing is sent to the printer and the camera is not touched."""
    with serve_node(fake_camera) as (url, _):
        fleet = _fleet(tmp_path, moonraker.port, url)
        result = run_cli(
            tmp_path,
            "calibrate",
            "photo",
            "--printer",
            "k1se",
            "--move",
            "--no-rectify",
            env_extra=_env(fleet),
        )
    assert result.returncode == 2 and "add --yes" in result.stderr
    assert moonraker.requests == [] and fake_camera.captures == []


def test_move_and_light_then_capture(
    tmp_path: Path, fake_camera: FakeCamera, moonraker: FakeMoonraker
) -> None:
    """With --move --yes the printer goes to the photo pose and lights up, then the camera captures."""
    out = tmp_path / "shot"
    with serve_node(fake_camera) as (url, _):
        fleet = _fleet(tmp_path, moonraker.port, url)
        result = run_cli(
            tmp_path,
            "calibrate",
            "photo",
            "--printer",
            "k1se",
            "--move",
            "--yes",
            "--pose",
            "100,200,120",
            "--printer-light",
            "on",
            "--no-rectify",
            "--out",
            str(out),
            "--json",
            env_extra=_env(fleet),
        )
    assert result.returncode == 0, result.stderr
    assert moonraker.scripts == [
        "G90\nG1 Z120.000 F600\nG1 X100.000 Y200.000 F3000\nM400",
        "SET_PIN PIN=LED VALUE=1",
        "SET_PIN PIN=LED VALUE=0",
    ]
    record = json.loads(result.stdout)
    assert record["pose"]["moved"] is True and record["printer_light"] == "on"
    assert record["printer_gcode"] == moonraker.scripts and "rectify" not in record


def test_printing_printer_is_refused_before_anything(
    tmp_path: Path, fake_camera: FakeCamera, moonraker: FakeMoonraker
) -> None:
    """A printing printer refuses the photo before any G-code or capture."""
    moonraker.status["print_stats"]["state"] = "printing"
    with serve_node(fake_camera) as (url, _):
        fleet = _fleet(tmp_path, moonraker.port, url)
        result = run_cli(
            tmp_path,
            "calibrate",
            "photo",
            "--printer",
            "k1se",
            "--move",
            "--yes",
            "--no-rectify",
            env_extra=_env(fleet),
        )
    assert result.returncode == 1 and "is printing" in result.stderr
    assert moonraker.scripts == [] and fake_camera.captures == []


def test_light_without_fleet_gcode_is_a_usage_error(
    tmp_path: Path, fake_camera: FakeCamera, moonraker: FakeMoonraker
) -> None:
    """Asking for the light on a printer with no light G-code in the fleet is a usage error."""
    fleet = tmp_path / "printers.yaml"
    fleet.write_text(
        yaml.safe_dump(
            {
                "k1se": {
                    "model": "Creality K1 SE",
                    "heads": [{"label": "T1", "index": 0}],
                    "owner": "own",
                    "access": "full",
                    "adapter": "moonraker",
                    "host": "127.0.0.1",
                    "port": moonraker.port,
                }
            }
        )
    )
    result = run_cli(
        tmp_path,
        "calibrate",
        "photo",
        "--printer",
        "k1se",
        "--printer-light",
        "on",
        "--no-rectify",
        env_extra=_env(fleet),
    )
    assert result.returncode == 2 and "photo.printer_light" in result.stderr
    assert moonraker.requests == []


def test_save_stores_the_settings_in_the_fleet_file(
    tmp_path: Path, fake_camera: FakeCamera, moonraker: FakeMoonraker
) -> None:
    """--save writes the photo settings into the printer's photo: block in the fleet file."""
    with serve_node(fake_camera) as (url, _):
        fleet = _fleet(tmp_path, moonraker.port, url)
        result = run_cli(
            tmp_path,
            "calibrate",
            "photo",
            "--printer",
            "k1se",
            "--exposure",
            "250",
            "--gain",
            "4",
            "--ctrl",
            "brightness=100",
            "--no-rectify",
            "--save",
            "--out",
            str(tmp_path / "s"),
            env_extra=_env(fleet),
        )
    assert result.returncode == 0, result.stderr
    text = fleet.read_text()
    assert text.startswith("# test fleet\n")
    photo = yaml.safe_load(text)["k1se"]["photo"]
    assert (
        photo["exposure"] == 250
        and photo["gain"] == 4
        and photo["ctrl"] == {"brightness": 100}
    )
    assert (
        photo["focus"] == 30 and "skip" not in photo
    )  # defaults are not frozen into the fleet


def test_too_few_fiducials_exit_1_but_keep_the_photo(
    tmp_path: Path, fake_camera: FakeCamera, moonraker: FakeMoonraker
) -> None:
    """Too few fiducials found: exit 1, but the averaged photo is kept for a retry."""
    out = tmp_path / "shot"
    with serve_node(fake_camera) as (url, _):
        fleet = _fleet(tmp_path, moonraker.port, url)
        result = run_cli(
            tmp_path,
            "calibrate",
            "photo",
            "--printer",
            "k1se",
            "--fiducials",
            FID,
            "--out",
            str(out),
            env_extra=_env(fleet),
        )
    assert result.returncode == 1
    assert "0 of 4 fiducials found" in result.stdout
    assert (out / "photo.png").is_file()
    assert json.loads((out / "calib-photo.json").read_text())["status"] == "failed"


class _SessionApi:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        api = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                api.posts.append(
                    {
                        "path": self.path,
                        "auth": self.headers.get("Authorization"),
                        "type": self.headers["Content-Type"],
                        "body": body,
                    }
                )
                data = json.dumps({"photo_id": "p1", "status": "accepted"}).encode()
                self.send_response(202)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


def test_session_upload(
    tmp_path: Path, fake_camera: FakeCamera, moonraker: FakeMoonraker
) -> None:
    """--session uploads the photo and its record to the calibration session."""
    api = _SessionApi()
    try:
        with serve_node(fake_camera) as (url, _):
            fleet = _fleet(tmp_path, moonraker.port, url)
            result = run_cli(
                tmp_path,
                "calibrate",
                "photo",
                "--printer",
                "k1se",
                "--no-rectify",
                "--session",
                "17",
                "--api",
                api.url,
                "--token",
                "po_test",
                "--json",
                "--out",
                str(tmp_path / "s"),
                env_extra=_env(fleet),
            )
    finally:
        api.server.shutdown()
    assert result.returncode == 0, result.stderr
    post = api.posts[0]
    assert (
        post["path"] == "/v1/calibration/sessions/17/photos"
        and post["auth"] == "Bearer po_test"
    )
    assert post["type"].startswith("multipart/form-data; boundary=")
    assert (
        b'name="source"' in post["body"]
        and b"camera" in post["body"]
        and b"\x89PNG" in post["body"]
    )
    assert json.loads(result.stdout)["session"] == {
        "photo_id": "p1",
        "status": "accepted",
    }


def test_rectify_an_existing_photo(tmp_path: Path) -> None:
    """The user rectifies an already taken photo with `3d calibrate rectify`."""
    img = render_scene(tmp_path / "phone.png")
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"schema": "calib-plate/1", "fiducials": FIDUCIALS}))
    result = run_cli(
        tmp_path,
        "calibrate",
        "rectify",
        str(img),
        "--manifest",
        str(manifest),
        "--px-per-mm",
        "5",
        "--area",
        "0,0,220,220",
        "--out",
        str(tmp_path / "r"),
        "--json",
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["rectified_size"] == [1100, 1100] and data["status"] == "ok"
    missing = run_cli(tmp_path, "calibrate", "rectify", str(img))
    assert missing.returncode == 2 and "--manifest or --fiducials" in missing.stderr


def test_calibrate_help_and_docs_cover_photo() -> None:
    """`3d calibrate --help` and the docs describe photo and rectify."""
    import importlib

    photo_cli = importlib.import_module("topcam.photo_cli")
    doc = (
        Path(__file__).resolve().parents[2] / "docs" / "commands" / "calibrate.md"
    ).read_text()
    for line in photo_cli.EXAMPLES.splitlines():
        assert line.strip() in doc, (
            f"example missing from docs/commands/calibrate.md: {line.strip()}"
        )
    for flag in sorted(
        {
            w.split("=")[0].rstrip(";,).")
            for w in photo_cli.OPTIONS.split()
            if w.startswith("--")
        }
    ):
        assert f"`{flag}" in doc, f"{flag} undocumented in docs/commands/calibrate.md"
