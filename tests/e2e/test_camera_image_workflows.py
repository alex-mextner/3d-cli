"""`3d camera image build` end to end through bin/3d against a tiny synthetic SD image."""

from __future__ import annotations

import json
import hashlib
import stat
from pathlib import Path

import yaml
from topcam_image_fixtures import fat_read, make_base_image, unxz

from .workflow_helper import REPO_ROOT, run_cli

FAKE_PSK = "FAKE-psk-not-real"
FAKE_CRED = "po_" + "FAKE_not_real"
FAKE_PUBKEY = "ssh-ed25519 " + "AAAAC3NzaC1lZDI1NTE5AAAAIFAKE" + " alex@pc"


def test_image_build_help_documents_every_flag(tmp_path: Path) -> None:
    """Alex reads `3d camera image build --help` to learn the one command that makes the card."""
    proc = run_cli(tmp_path, "camera", "image", "build", "--help")
    assert proc.returncode == 0, proc.stderr
    doc = (REPO_ROOT / "docs" / "commands" / "camera.md").read_text(encoding="utf-8")
    for flag in (
        "--wifi-ssid",
        "--wifi-psk",
        "--wifi-psk-file",
        "--wifi-country",
        "--api-url",
        "--token",
        "--token-file",
        "--hostname",
        "--user",
        "--ssh-key",
        "--password-hash",
        "--no-ssh",
        "--led-gpio",
        "--still-lock",
        "--base-image",
        "--out",
        "--json",
    ):
        assert flag in proc.stdout, flag
        assert "3d camera image build" in doc and flag in doc, flag
    assert "3d camera image build --wifi-ssid home --wifi-psk-file" in proc.stdout


def test_image_build_writes_a_flashable_image_without_leaking_secrets(
    tmp_path: Path,
) -> None:
    """Alex keeps the Wi-Fi password and token in files, builds the card image with --json,
    pipes the JSON to a file, checks the sha256 like FLASH.md says, and looks inside the boot
    partition: Wi-Fi, host name, SSH key and the node config are there; the secrets appear
    nowhere in the terminal output."""
    base = make_base_image(tmp_path / "base", xz=True)
    (tmp_path / "psk").write_text(FAKE_PSK + "\n")
    (tmp_path / "token").write_text(FAKE_CRED + "\n")
    (tmp_path / "id.pub").write_text(FAKE_PUBKEY + "\n")
    out = tmp_path / "card"
    proc = run_cli(
        tmp_path,
        "camera",
        "image",
        "build",
        "--wifi-ssid",
        "proof-ssid",
        "--wifi-psk-file",
        str(tmp_path / "psk"),
        "--wifi-country",
        "rs",
        "--api-url",
        "http://192.168.0.39:8765",
        "--token-file",
        str(tmp_path / "token"),
        "--hostname",
        "topcam",
        "--ssh-key",
        str(tmp_path / "id.pub"),
        "--led-gpio",
        "17",
        "--still-lock",
        "focus=30,exposure=300",
        "--base-image",
        str(base),
        "--out",
        str(out),
        "--json",
        env_extra={"THREED_MAX_WORKERS": "1"},
    )
    assert proc.returncode == 0, proc.stderr
    for secret in (FAKE_PSK, FAKE_CRED):
        assert secret not in proc.stdout and secret not in proc.stderr
    (tmp_path / "result.json").write_text(proc.stdout)
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["schema"] == "topcam-image/1" and result["first_boot"] == "cloud-init"
    assert result["wifi"] == {
        "ssid": "proof-ssid",
        "country": "RS",
        "psk": "<redacted>",
    }
    assert result["api"]["token"] == "<redacted>" and result["ssh"]["keys"] == 1
    image = Path(result["image"])
    assert image == out / "topcam-2026-10-06-raspios-trixie-armhf-lite.img.xz"
    assert stat.S_IMODE(image.stat().st_mode) == 0o600
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    assert (out / f"{image.name}.sha256").read_text() == f"{digest}  {image.name}\n"
    flash = (out / "FLASH.md").read_text()
    assert "rpi-imager" in flash.lower() or "Raspberry Pi Imager" in flash
    assert f"xzcat {image} | sudo dd of=/dev/sdX" in flash
    raw = unxz(image, tmp_path / "card.img")
    user_data = yaml.safe_load(fat_read(raw, "user-data"))
    assert user_data["hostname"] == "topcam"
    assert user_data["users"][0]["ssh_authorized_keys"] == [FAKE_PUBKEY]
    net = yaml.safe_load(fat_read(raw, "network-config"))
    assert net["network"]["wifis"]["wlan0"]["regulatory-domain"] == "RS"
    assert b"cfg80211.ieee80211_regdom=RS" in fat_read(raw, "cmdline.txt")


def test_image_build_without_wifi_is_a_usage_error(tmp_path: Path) -> None:
    """A forgotten --wifi-ssid is reported as a usage error (exit 2) naming the flag."""
    proc = run_cli(
        tmp_path,
        "camera",
        "image",
        "build",
        "--api-url",
        "http://h:8765",
        "--token",
        "x",
    )
    assert proc.returncode == 2
    assert "--wifi-ssid" in proc.stderr
