"""`topcam.image`: MBR parsing, first-boot rendering, base-image cache, and a full build
against the synthetic disk image (no network, no real Raspberry Pi OS image)."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import stat
from pathlib import Path
from typing import Any

import pytest
import yaml
from topcam_image_fixtures import (
    BOOT_LBA,
    BOOT_SECTORS,
    CMDLINE,
    ROOT_LBA,
    ROOT_SECTORS,
    fat_read,
    make_base_image,
    mbr,
    root_bytes,
    unxz,
)

from errors import (
    InputNotFound,
    InvalidArgument,
    MissingDependency,
    ThreeDError,
    UsageError,
)
from topcam import image
from topcam.node import NODE_FILES

ROOT = Path(__file__).resolve().parents[1]
# Obviously fake placeholders; the tests assert they never reach stdout/JSON/FLASH.md.
FAKE_PSK = "FAKE-psk-not-real"
FAKE_CRED = "po_" + "FAKE_not_real"
FAKE_HASH = "$6$" + "FAKEsalt$FAKEhash"
FAKE_PUBKEY = "ssh-ed25519 " + "AAAAC3NzaC1lZDI1NTE5AAAAIFAKE" + " alex@pc"

SPEC = image.NodeSpec(
    hostname="topcam",
    user="topcam",
    wifi_ssid="proof-ssid",
    wifi_country="RS",
    api_url="http://192.168.0.39:8765",
    ssh_keys=(FAKE_PUBKEY,),
    led_gpio=17,
    lock={"focus": 30, "exposure": 300, "gain": 0, "wb": 4500},
)
SECRETS = image.Secrets(FAKE_PSK, FAKE_CRED)


def _files(user_data: dict[str, Any]) -> dict[str, tuple[bytes, str]]:
    return {
        f["path"]: (base64.b64decode(f["content"]), f["permissions"])
        for f in user_data["write_files"]
    }


def _spec(**changes: Any) -> image.NodeSpec:
    return image.NodeSpec(**{**SPEC.__dict__, **changes})


# ----------------------------------------------------------------------------- MBR


def test_parse_mbr_finds_the_fat_boot_partition() -> None:
    sector = mbr([(0x0C, 16384, 1048576), (0x83, 1064960, 4374528)])
    parts = image.parse_mbr(sector)
    assert [(p.index, p.type, p.start_lba, p.sectors) for p in parts] == [
        (1, 0x0C, 16384, 1048576),
        (2, 0x83, 1064960, 4374528),
    ]
    boot = image.boot_partition(parts)
    assert boot.offset == 16384 * 512 and boot.end == (16384 + 1048576) * 512
    assert boot.bootable


def test_parse_mbr_rejects_garbage_gpt_and_no_fat() -> None:
    with pytest.raises(ValueError, match="0x55AA"):
        image.parse_mbr(b"\0" * 512)
    with pytest.raises(ValueError, match="GPT"):
        image.parse_mbr(mbr([(0xEE, 1, 100)]))
    with pytest.raises(ValueError, match="no FAT partition"):
        image.boot_partition(image.parse_mbr(mbr([(0x83, 2048, 100)])))


# ----------------------------------------------------------------------------- rendering


def test_user_data_is_valid_cloud_config_with_everything_the_node_needs() -> None:
    text = image.render_user_data(SPEC, SECRETS, image.package_sources(), "9.9.9")
    assert text.startswith("#cloud-config\n")
    doc = yaml.safe_load(text)
    assert doc["hostname"] == "topcam" and doc["manage_etc_hosts"] is True
    assert doc["enable_ssh"] is True and doc["ssh_pwauth"] is False
    (user,) = doc["users"]
    assert user["name"] == "topcam" and user["lock_passwd"] is True
    assert user["ssh_authorized_keys"] == [FAKE_PUBKEY]
    assert {"video", "gpio", "sudo"} <= set(user["groups"])
    assert doc["package_update"] is True
    assert doc["packages"] == [
        "ustreamer",
        "avahi-daemon",
        "python3",
        "raspi-utils",
        "v4l-utils",
    ]
    assert doc["bootcmd"] == [
        [
            "cloud-init-per",
            "once",
            "topcam-wifi-country",
            "raspi-config",
            "nonint",
            "do_wifi_country",
            "RS",
        ]
    ]
    assert doc["runcmd"] == [[image.FIRSTBOOT_PATH]]
    files = _files(doc)
    for name in NODE_FILES:
        want = (ROOT / "lib" / "topcam" / name).read_bytes()
        assert files[f"/opt/topcam/topcam/{name}"] == (want, "0644")
    assert files["/etc/topcam/token"] == (f"{FAKE_CRED}\n".encode(), "0600")
    config = json.loads(files["/etc/topcam/config.json"][0])
    assert config == {
        "name": "topcam",
        "port": 8080,
        "device": "auto",
        "still": {"lock": {"focus": 30, "exposure": 300, "gain": 0, "wb": 4500}},
        "led": {"gpio": 17},
        "api": {"url": "http://192.168.0.39:8765", "token_file": "/etc/topcam/token"},
    }
    unit = files["/etc/systemd/system/topcam.service"][0].decode()
    assert (
        "ExecStart=/usr/bin/python3 -m topcam.node --config /etc/topcam/config.json"
        in unit
    )
    assert (
        "WorkingDirectory=/opt/topcam" in unit
        and "Environment=PYTHONPATH=/opt/topcam" in unit
    )
    assert "Restart=always" in unit and "User=topcamd" in unit
    assert "SupplementaryGroups=video gpio" in unit
    avahi = files["/etc/avahi/services/topcam.service"][0].decode()
    assert "<type>_http._tcp</type>" in avahi and "<port>8080</port>" in avahi
    assert "<txt-record>path=/health</txt-record>" in avahi
    script, mode = files[image.FIRSTBOOT_PATH]
    assert mode == "0700"
    body = script.decode()
    assert "systemctl enable --now topcam.service" in body
    assert "useradd --system" in body and "--groups video,gpio topcamd" in body
    assert "secrets removed" in body and '> "$BOOT/user-data"' in body
    assert '> "$BOOT/network-config"' in body
    assert "touch /etc/cloud/cloud-init.disabled" in body
    # the scrub runs after the service is enabled, and nothing stops the script before it
    assert body.index("enable --now topcam") < body.index('"$BOOT/user-data"')
    assert "set -e" not in body


def test_password_hash_and_no_ssh_variants() -> None:
    pw = image.Secrets(FAKE_PSK, FAKE_CRED, password_hash=FAKE_HASH)
    doc = yaml.safe_load(
        image.render_user_data(_spec(ssh_keys=()), pw, image.package_sources(), "1")
    )
    assert doc["ssh_pwauth"] is True
    assert doc["users"][0]["lock_passwd"] is False
    assert doc["users"][0]["hashed_passwd"] == FAKE_HASH
    assert "ssh_authorized_keys" not in doc["users"][0]
    off = _spec(ssh_keys=(), ssh=False)
    doc = yaml.safe_load(
        image.render_user_data(off, SECRETS, image.package_sources(), "1")
    )
    assert doc["enable_ssh"] is False and doc["ssh_pwauth"] is False


def test_network_config_sets_ssid_psk_and_regulatory_country() -> None:
    weird = _spec(wifi_ssid='My "Wi-Fi": 5G #1')
    phrase = 'p@ss: "word" #x'
    doc = yaml.safe_load(
        image.render_network_config(weird, image.Secrets(phrase, FAKE_CRED))
    )
    wifis = doc["network"]["wifis"]
    assert doc["network"]["version"] == 2 and wifis["renderer"] == "NetworkManager"
    wlan = wifis["wlan0"]
    assert wlan["regulatory-domain"] == "RS" and wlan["dhcp4"] is True
    assert wlan["access-points"] == {'My "Wi-Fi": 5G #1': {"password": phrase}}


def test_cmdline_gets_the_regulatory_domain_once() -> None:
    once = image.patch_cmdline(CMDLINE, "RS")
    assert (
        once.endswith(" resize cfg80211.ieee80211_regdom=RS\n")
        and once.count("\n") == 1
    )
    again = image.patch_cmdline(once, "DE")
    assert "regdom=RS" not in again and again.count("cfg80211.ieee80211_regdom=") == 1


def test_validators_reject_bad_input_without_echoing_secrets() -> None:
    with pytest.raises(UsageError) as exc:
        image.validate_psk("short")
    assert "short" not in str(exc.value)
    assert image.validate_psk("a" * 64) == "a" * 64
    with pytest.raises(UsageError):
        image.validate_token("has space")
    with pytest.raises(InvalidArgument):
        image.validate_hostname("Top_Cam")
    with pytest.raises(InvalidArgument):
        image.validate_user("root")
    with pytest.raises(InvalidArgument):
        image.validate_country("Serbia")
    assert image.validate_country("rs") == "RS"
    with pytest.raises(InvalidArgument):
        image.validate_led("40")
    with pytest.raises(InvalidArgument):
        image.parse_lock("iso=100")
    assert image.parse_lock("focus=30,wb=4500") == {"focus": 30, "wb": 4500}
    with pytest.raises(UsageError):
        image.parse_ssh_keys("not a key\n", "x.pub")
    assert image.validate_api_url("http://h:8765/") == "http://h:8765"
    for bad in ("http://h:8765 junk", "http://h:abc", "http://h/?x=1", "ftp://h"):
        with pytest.raises(InvalidArgument):
            image.validate_api_url(bad)
    with pytest.raises(InvalidArgument):
        image.validate_led("²")  # str.isdigit() accepts it, int() does not
    assert image.parse_lock("gain=--1") == {"gain": "--1"}  # left for the node's parser


def test_firstboot_follows_the_ssh_switch_and_ssid_stays_valid_yaml() -> None:
    assert "systemctl enable ssh.service" in image.render_firstboot(True)
    off = image.render_firstboot(False)
    assert "systemctl enable ssh.service" not in off and "disable --now ssh.service" in off
    spec = dataclasses.replace(SPEC, wifi_ssid="Café 😀")
    doc = yaml.safe_load(image.render_network_config(spec, SECRETS))
    assert "Café 😀" in doc["network"]["wifis"]["wlan0"]["access-points"]


def _opts(tmp_path: Path, **extra: str) -> dict[str, str]:
    psk = tmp_path / "psk"
    psk.write_text(FAKE_PSK + "\n")
    key = tmp_path / "id.pub"
    key.write_text(FAKE_PUBKEY + "\n")
    return {
        "--wifi-ssid": "proof-ssid",
        "--wifi-psk-file": str(psk),
        "--api-url": "http://192.168.0.39:8765",
        "--token": FAKE_CRED,
        "--ssh-key": str(key),
        **extra,
    }


def test_spec_from_opts_reads_secret_files_and_defaults(tmp_path: Path) -> None:
    spec, secrets = image.spec_from_opts(_opts(tmp_path), set())
    assert secrets.wifi_psk == FAKE_PSK and secrets.token == FAKE_CRED
    assert (
        spec.hostname == "topcam"
        and spec.user == "topcam"
        and spec.wifi_country == "RS"
    )
    assert spec.ssh_keys == (FAKE_PUBKEY,)
    with pytest.raises(UsageError, match="not both"):
        image.spec_from_opts(_opts(tmp_path, **{"--wifi-psk": FAKE_PSK}), set())
    with pytest.raises(UsageError, match="contradict"):
        image.spec_from_opts(_opts(tmp_path), {"--no-ssh"})


def test_still_lock_values_are_judged_by_the_node_config_parser(tmp_path: Path) -> None:
    assert image.parse_lock("gain=abc,wb=4500") == {"gain": "abc", "wb": 4500}
    with pytest.raises(InvalidArgument, match="--still-lock"):
        image.spec_from_opts(_opts(tmp_path, **{"--still-lock": "gain=abc"}), set())
    spec, _ = image.spec_from_opts(
        _opts(tmp_path, **{"--still-lock": "focus=sweep:0-250:25,exposure=300"}), set()
    )
    assert image.render_config(spec)["still"]["lock"] == {
        "focus": "sweep:0-250:25",
        "exposure": 300,
    }
    assert "focus.py" in NODE_FILES  # the software focus sweep ships with the node


def test_no_ssh_key_is_refused_unless_password_or_no_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    opts = _opts(tmp_path)
    del opts["--ssh-key"]
    with pytest.raises(UsageError, match="no SSH public key"):
        image.spec_from_opts(opts, set())
    spec, _ = image.spec_from_opts(opts, {"--no-ssh"})
    assert spec.ssh is False
    spec, secrets = image.spec_from_opts({**opts, "--password-hash": FAKE_HASH}, set())
    assert spec.ssh and secrets.password_hash == FAKE_HASH
    home_ssh = tmp_path / "empty-home" / ".ssh"
    home_ssh.mkdir(parents=True)
    (home_ssh / "id_rsa.pub").write_text("ssh-rsa AAAAB3NzaFAKE rsa@pc\n")
    spec, _ = image.spec_from_opts(opts, set())
    assert spec.ssh_keys == ("ssh-rsa AAAAB3NzaFAKE rsa@pc",)


def test_missing_mtools_is_a_structured_missing_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(image.shutil, "which", lambda name: None)
    with pytest.raises(MissingDependency) as exc:
        image.require_tools()
    assert exc.value.exit_code == 127 and "mtools" in str(exc.value)


# ----------------------------------------------------------------------------- base cache


def test_fetch_base_verifies_sha256_and_reuses_the_cache(tmp_path: Path) -> None:
    src = tmp_path / "mirror" / "2026-10-06-raspios-trixie-armhf-lite.img.xz"
    src.parent.mkdir()
    src.write_bytes(b"fake xz payload")
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    Path(f"{src}.sha256").write_text(f"{digest}  {src.name}\n")
    cache = tmp_path / "cache"
    first = image.resolve_base(src.as_uri(), cache)
    assert (
        first.path == cache / src.name and first.sha256 == digest and not first.cached
    )
    assert first.name == "2026-10-06-raspios-trixie-armhf-lite"
    second = image.resolve_base(src.as_uri(), cache)
    assert second.cached and second.path.read_bytes() == b"fake xz payload"
    Path(f"{src}.sha256").write_text("0" * 64 + f"  {src.name}\n")
    (cache / src.name).unlink()
    with pytest.raises(ThreeDError, match="sha256 mismatch"):
        image.resolve_base(src.as_uri(), cache)
    assert not (cache / src.name).exists() and not list(cache.glob("*.part"))


def test_base_without_published_sha256_is_refused(tmp_path: Path) -> None:
    src = tmp_path / "x.img.xz"
    src.write_bytes(b"x")
    with pytest.raises(ThreeDError, match="published sha256"):
        image.resolve_base(src.as_uri(), tmp_path / "cache")
    with pytest.raises(InputNotFound):
        image.resolve_base(str(tmp_path / "nope.img"), tmp_path / "cache")


# ----------------------------------------------------------------------------- full build


@pytest.mark.parametrize("compressed", [False, True])
def test_build_against_the_synthetic_image(tmp_path: Path, compressed: bool) -> None:
    base = make_base_image(tmp_path / "base", xz=compressed)
    out = tmp_path / "out"
    result = image.build(
        SPEC,
        SECRETS,
        base_arg=str(base),
        out=out,
        cache=tmp_path / "cache",
        repo=ROOT,
        threads=1,
    )
    assert result.image == out / "topcam-2026-10-06-raspios-trixie-armhf-lite.img.xz"
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    for path in (result.image, Path(f"{result.image}.sha256"), result.flash):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    assert [p.name for p in out.iterdir() if p.name.startswith(".work-")] == []
    digest = hashlib.sha256(result.image.read_bytes()).hexdigest()
    assert (
        Path(f"{result.image}.sha256").read_text() == f"{digest}  {result.image.name}\n"
    )
    raw = unxz(result.image, tmp_path / "flashed.img")
    data = raw.read_bytes()
    assert result.raw_size == len(data) == (ROOT_LBA + ROOT_SECTORS) * 512
    # only the boot partition changed: MBR and root file system are byte-identical
    assert data[:512] == mbr(
        [(0x0C, BOOT_LBA, BOOT_SECTORS), (0x83, ROOT_LBA, ROOT_SECTORS)]
    )
    assert data[ROOT_LBA * 512 :] == root_bytes()
    files = _files(yaml.safe_load(fat_read(raw, "user-data")))
    for name in NODE_FILES:
        want = (ROOT / "lib" / "topcam" / name).read_bytes()
        assert files[f"/opt/topcam/topcam/{name}"][0] == want
    assert files["/etc/topcam/token"] == (f"{FAKE_CRED}\n".encode(), "0600")
    net = yaml.safe_load(fat_read(raw, "network-config"))
    assert net["network"]["wifis"]["wlan0"]["access-points"] == {
        "proof-ssid": {"password": FAKE_PSK}
    }
    meta = yaml.safe_load(fat_read(raw, "meta-data"))
    assert meta["dsmode"] == "local" and meta["instance_id"].startswith(
        "topcam-topcam-"
    )
    cmdline = fat_read(raw, "cmdline.txt").decode()
    assert cmdline == CMDLINE.rstrip("\n") + " cfg80211.ieee80211_regdom=RS\n"
    assert fat_read(raw, "config.txt") == b"arm_boost=1\n"
    assert {
        "user-data",
        "network-config",
        "meta-data",
        "cmdline.txt",
        "config.txt",
    } <= set(result.boot_listing)
    flash = result.flash.read_text()
    assert (
        f"xzcat {result.image} | sudo dd of=/dev/sdX bs=4M conv=fsync status=progress"
        in flash
    )
    assert "lsblk" in flash and "curl http://topcam.local:8080/health" in flash
    assert "3d camera controls --camera topcam" in flash and "10-20 minutes" in flash
    blob = json.dumps(result.as_json())
    for secret in (FAKE_PSK, FAKE_CRED):
        assert secret not in blob and secret not in flash


def test_a_failed_rebuild_keeps_the_previous_good_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = make_base_image(tmp_path / "base")
    out = tmp_path / "out"
    good = image.build(SPEC, SECRETS, base_arg=str(base), out=out, cache=tmp_path / "c", threads=1)
    before = good.image.read_bytes(), Path(f"{good.image}.sha256").read_text()

    def dies(*a: Any, **k: Any) -> int:
        raise ThreeDError("xz compression exited 137", command="camera image build")

    monkeypatch.setattr(image, "stream_output", dies)
    with pytest.raises(ThreeDError, match="exited 137"):
        image.build(SPEC, SECRETS, base_arg=str(base), out=out, cache=tmp_path / "c", threads=1)
    assert (good.image.read_bytes(), Path(f"{good.image}.sha256").read_text()) == before
    assert not list(out.glob(".work-*"))


def test_a_truncated_plain_base_is_refused(tmp_path: Path) -> None:
    base = make_base_image(tmp_path / "base")
    data = base.read_bytes()
    base.write_bytes(data[: len(data) - 4096])
    with pytest.raises(ThreeDError, match="truncated"):
        image.build(SPEC, SECRETS, base_arg=str(base), out=tmp_path / "out", cache=tmp_path / "c", threads=1)


def test_build_refuses_bookworm_style_images_and_repo_output(tmp_path: Path) -> None:
    base = make_base_image(
        tmp_path / "base", seed={"cmdline.txt": CMDLINE, "config.txt": "x\n"}
    )
    out = tmp_path / "out"
    with pytest.raises(ThreeDError, match="no cloud-init seed"):
        image.build(
            SPEC, SECRETS, base_arg=str(base), out=out, cache=tmp_path, threads=1
        )
    assert not list(out.glob("*.img.xz")) and not list(out.glob(".work-*"))
    with pytest.raises(UsageError, match="inside the repository"):
        image.build(
            SPEC,
            SECRETS,
            base_arg=str(base),
            out=ROOT / "build-out",
            cache=tmp_path,
            repo=ROOT,
        )
    assert not (ROOT / "build-out").exists()


def test_build_rejects_a_non_mbr_file(tmp_path: Path) -> None:
    junk = tmp_path / "junk.img"
    junk.write_bytes(b"not an image" * 400)
    with pytest.raises(ThreeDError, match="not a Raspberry Pi OS image"):
        image.build(
            SPEC,
            SECRETS,
            base_arg=str(junk),
            out=tmp_path / "out",
            cache=tmp_path,
            threads=1,
        )
