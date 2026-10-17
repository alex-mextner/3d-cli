"""topcam.image — `3d camera image build`: a ready-to-flash SD image for the camera node.

WHAT: takes Raspberry Pi OS Lite armhf (the latest release by default; downloaded once into
  the cache and checked against the published sha256), writes a cloud-init first-boot seed
  into its FAT boot partition (hostname, Wi-Fi with the regulatory country, the login user
  with an SSH key, the ustreamer package, the `topcam` package copied from this repo, the
  node config + API token, a systemd unit and an avahi `_http._tcp` service), and writes
  NAME.img.xz + NAME.img.xz.sha256 + FLASH.md.
WHY: the Pi Zero W above the bed must come up on Wi-Fi as NAME.local and register with
  3d-api without a keyboard, a screen or a shell session on the Pi (issue #343).
HOW, without root: the MBR is parsed with `struct`, only the head of the image (MBR + FAT
  boot partition, ~520 MB) is extracted and edited with mtools (`mcopy -i head@@OFFSET`),
  then the output is streamed as head + the untouched rest of the base image straight into
  `xz`, so the 2.7 GB raw image never lands on disk or in memory. Decompression is
  single-threaded (`xz -T1`) and compression uses `cli.resources.max_workers()` threads under
  `--memlimit-compress=1500MiB`, so a build fits a 2 GB memory cap on the shared machine.
INVARIANTS:
  - Secrets (Wi-Fi PSK, API token, password hash) are never printed, logged or put in
    `--json`; they only exist in the output image, in 0600 files inside the 0700 work dir
    (deleted at the end) and never inside the repo.
  - On the Pi the PSK lands in the cloud-init `network-config` and the token in `user-data`
    on the FAT partition; the first-boot script replaces both with secret-free stubs and
    disables cloud-init once the node is installed.
  - Only cloud-init images (Trixie-based, 2025-10 and later) are supported: older
    Bookworm images without a `user-data`/`meta-data` seed are refused with a clear error.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import shlex
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
import contextlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from errors import (
    InputNotFound,
    InvalidArgument,
    MissingDependency,
    ThreeDError,
    UsageError,
)
from topcam.node import LOCK_KEYS, NODE_FILES, Config, NodeError

CMD_NAME = "camera image build"
LATEST_URL = "https://downloads.raspberrypi.com/raspios_lite_armhf_latest"
PACKAGES = ("ustreamer", "avahi-daemon", "python3", "raspi-utils", "v4l-utils")
SERVICE_USER = "topcamd"
NODE_PORT = 8080
TOKEN_PATH = "/etc/topcam/token"
CONFIG_PATH = "/etc/topcam/config.json"
FIRSTBOOT_PATH = "/usr/local/sbin/topcam-firstboot"
XZ_MEMLIMIT = "1500MiB"
REDACTED = "<redacted>"
FAT_TYPES = {0x01, 0x04, 0x06, 0x0B, 0x0C, 0x0E}
CHUNK = 4 << 20
RESERVED_USERS = {"root", SERVICE_USER, "daemon", "nobody"}


# --------------------------------------------------------------------------- spec + parsing


@dataclass(frozen=True)
class NodeSpec:
    """Everything rendered into the first-boot seed except the secrets."""

    hostname: str
    user: str
    wifi_ssid: str
    wifi_country: str
    api_url: str
    ssh_keys: tuple[str, ...] = ()
    ssh: bool = True
    led_gpio: int | None = None
    lock: Mapping[str, int | str] = field(default_factory=dict)


@dataclass(frozen=True)
class Secrets:
    wifi_psk: str = field(repr=False)
    token: str = field(repr=False)
    password_hash: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Partition:
    index: int
    type: int
    bootable: bool
    start_lba: int
    sectors: int

    @property
    def offset(self) -> int:
        return self.start_lba * 512

    @property
    def end(self) -> int:
        return (self.start_lba + self.sectors) * 512


def parse_mbr(sector: bytes) -> list[Partition]:
    """The non-empty primary partitions of a DOS/MBR partition table (first 512 bytes)."""
    if len(sector) < 512 or sector[510:512] != b"\x55\xaa":
        raise ValueError("no MBR boot signature (0x55AA) in the first sector")
    parts: list[Partition] = []
    for i in range(4):
        entry = sector[446 + 16 * i : 462 + 16 * i]
        status, ptype = entry[0], entry[4]
        start, count = struct.unpack("<II", entry[8:16])
        if ptype == 0 or count == 0:
            continue
        if ptype == 0xEE:
            raise ValueError("GPT protective MBR: the image uses GPT, not an MBR table")
        parts.append(Partition(i + 1, ptype, status == 0x80, start, count))
    return parts


def boot_partition(parts: Sequence[Partition]) -> Partition:
    """The FAT boot partition: the first FAT-typed primary partition."""
    for p in parts:
        if p.type in FAT_TYPES:
            return p
    raise ValueError(
        "no FAT partition in the MBR (types: "
        + ", ".join(f"0x{p.type:02x}" for p in parts)
        + ")"
    )


_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_KEY_PREFIXES = ("ssh-", "ecdsa-", "sk-ssh-", "sk-ecdsa-")


def validate_hostname(name: str) -> str:
    if not _HOST_RE.fullmatch(name):
        raise InvalidArgument(
            "--hostname",
            name,
            accepted=[
                "lowercase letters, digits and '-' (1-63, no leading/trailing '-')"
            ],
            command=CMD_NAME,
        )
    return name


def validate_user(name: str) -> str:
    if not _USER_RE.fullmatch(name) or name in RESERVED_USERS:
        raise InvalidArgument(
            "--user",
            name,
            accepted=[
                "a lowercase login name ([a-z_][a-z0-9_-]{0,31}), not "
                + "/".join(sorted(RESERVED_USERS))
            ],
            command=CMD_NAME,
        )
    return name


def validate_ssid(ssid: str) -> str:
    try:
        size = len(ssid.encode("utf-8"))
    except UnicodeEncodeError:  # undecodable argv bytes arrive as surrogates
        size = 0
    if not 1 <= size <= 32:
        raise UsageError("--wifi-ssid must be 1-32 bytes of UTF-8", command=CMD_NAME)
    return ssid


def validate_psk(psk: str) -> str:
    """WPA2-PSK: an 8-63 character passphrase or the 64-hex-digit raw key. The value itself
    never appears in the error."""
    if re.fullmatch(r"[0-9a-fA-F]{64}", psk):
        return psk
    if not 8 <= len(psk) <= 63 or not all(32 <= ord(c) < 127 for c in psk):
        raise UsageError(
            f"the Wi-Fi PSK must be 8-63 printable ASCII characters or 64 hex digits "
            f"(got {len(psk)} characters; the value is not shown)",
            command=CMD_NAME,
        )
    return psk


def validate_country(code: str) -> str:
    up = code.upper()
    if not re.fullmatch(r"[A-Z]{2}", up):
        raise InvalidArgument(
            "--wifi-country",
            code,
            accepted=["an ISO 3166-1 alpha-2 code, e.g. RS, DE, GB, US"],
            command=CMD_NAME,
        )
    return up


def validate_api_url(url: str) -> str:
    try:
        parts = urllib.parse.urlsplit(url)
        _ = parts.port
        ok = (parts.scheme in ("http", "https") and bool(parts.hostname) and not parts.query
              and parts.username is None  # no credentials in a URL that FLASH.md and logs print
              and not parts.fragment and not any(c.isspace() or ord(c) < 32 for c in url))
    except ValueError:
        ok = False
    if not ok:
        raise InvalidArgument(
            "--api-url",
            url,
            accepted=["http://HOST:PORT or https://HOST"],
            command=CMD_NAME,
        )
    return url.rstrip("/")


def validate_token(token: str) -> str:
    if not token or any(c.isspace() for c in token):
        raise UsageError(
            "the API token must be non-empty without whitespace (the value is not shown)",
            command=CMD_NAME,
        )
    return token


def validate_led(raw: str) -> int:
    if not re.fullmatch(r"[0-9]{1,2}", raw) or not 0 <= int(raw) <= 27:
        raise InvalidArgument(
            "--led-gpio",
            raw,
            accepted=["a BCM GPIO number 0-27, e.g. 17"],
            command=CMD_NAME,
        )
    return int(raw)


def parse_lock(raw: str) -> dict[str, int | str]:
    """`focus=30,exposure=300,…` → {key: int, or the raw text for non-integer values such as
    focus=auto}; the node's config parser (`render_config`) judges the values."""
    out: dict[str, int | str] = {}
    for part in raw.split(","):
        key, sep, value = (s.strip() for s in part.partition("="))
        if not sep or key not in LOCK_KEYS or not value:
            raise InvalidArgument(
                "--still-lock",
                raw,
                accepted=[
                    f"KEY=VALUE pairs, keys {', '.join(LOCK_KEYS)}; "
                    "e.g. focus=30,exposure=300,gain=0,wb=4500"
                ],
                command=CMD_NAME,
            )
        out[key] = int(value) if re.fullmatch(r"-?[0-9]+", value) else value
    return out


def parse_ssh_keys(text: str, source: str) -> tuple[str, ...]:
    keys = tuple(
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    if not keys or not all(k.startswith(_KEY_PREFIXES) for k in keys):
        raise UsageError(
            f"{source} is not an OpenSSH public key file (lines must start with ssh-…, ecdsa-…)",
            command=CMD_NAME,
            remediation=["Pass the .pub file, e.g. --ssh-key ~/.ssh/id_ed25519.pub"],
        )
    return keys


def default_ssh_key() -> Path | None:
    for name in ("id_ed25519.pub", "id_rsa.pub"):
        path = Path.home() / ".ssh" / name
        if path.is_file():
            return path
    return None


# --------------------------------------------------------------------------- rendering


def _q(value: str) -> str:
    """A YAML double-quoted scalar (a JSON string is one). Non-ASCII stays literal: YAML has no
    surrogate-pair escapes, so json's default \\ud83d\\ude00 would break an emoji SSID."""
    return json.dumps(value, ensure_ascii=False)


def package_sources(root: Path | None = None) -> dict[str, bytes]:
    """The topcam package files shipped to /opt/topcam/topcam/ on the Pi, read from this
    checkout: `topcam.node.NODE_FILES` (the node and the modules it imports), so a new node
    module ships without touching the builder."""
    base = root or Path(__file__).resolve().parent
    return {name: (base / name).read_bytes() for name in NODE_FILES}


def render_config(spec: NodeSpec) -> dict[str, Any]:
    """/etc/topcam/config.json (the `topcam.node` schema), checked by the node's parser."""
    cfg: dict[str, Any] = {"name": spec.hostname, "port": NODE_PORT, "device": "auto"}
    if spec.lock:
        cfg["still"] = {"lock": dict(spec.lock)}
    if spec.led_gpio is not None:
        cfg["led"] = {"gpio": spec.led_gpio}
    cfg["api"] = {"url": spec.api_url, "token_file": TOKEN_PATH}
    try:
        Config.from_json(cfg)
    except (NodeError, ValueError, TypeError) as exc:
        raise InvalidArgument(
            "--still-lock",
            ",".join(f"{k}={v}" for k, v in spec.lock.items()),
            accepted=[f"values the node accepts ({exc})"],
            command=CMD_NAME,
        ) from exc
    return cfg


def render_unit() -> str:
    return f"""[Unit]
Description=topcam camera node (topcam.node, rendered by `3d camera image build`)
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
# A dedicated system user: the camera needs group video (/dev/video*), the LED needs group gpio
# (pinctrl drives /dev/gpiomem, 0660 root:gpio on Raspberry Pi OS); nothing here needs root.
User={SERVICE_USER}
Group={SERVICE_USER}
SupplementaryGroups=video gpio
WorkingDirectory=/opt/topcam
Environment=PYTHONPATH=/opt/topcam
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 -m topcam.node --config {CONFIG_PATH}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def render_avahi(spec: NodeSpec) -> str:
    return f"""<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<!-- topcam camera node: http://{spec.hostname}.local:{NODE_PORT}/health -->
<service-group>
  <name replace-wildcards="yes">topcam {spec.hostname} on %h</name>
  <service>
    <type>_http._tcp</type>
    <port>{NODE_PORT}</port>
    <txt-record>path=/health</txt-record>
    <txt-record>node={spec.hostname}</txt-record>
  </service>
</service-group>
"""


def render_firstboot(ssh: bool = True) -> str:
    """The once-only script cloud-init runs (runcmd → scripts_user, after the packages)."""
    return f"""#!/bin/sh
# topcam first boot, rendered by `3d camera image build`. cloud-init runs it once after the
# package install. Every step reports and continues, so the secret scrub at the end always runs.
set -u
log() {{ echo "topcam-firstboot: $*"; }}

if ! command -v ustreamer >/dev/null 2>&1; then
  # cloud-init's package step can run before Wi-Fi associates; retry for up to ~10 minutes.
  for _ in $(seq 1 60); do
    apt-get update -q && apt-get install -y -q ustreamer && break
    log "apt not reachable yet, retrying in 10 s"; sleep 10
  done
fi
id -u {SERVICE_USER} >/dev/null 2>&1 || useradd --system --user-group --no-create-home \\
  --home-dir /nonexistent --shell /usr/sbin/nologin --groups video,gpio {SERVICE_USER} \\
  || log "useradd {SERVICE_USER} failed"
chown {SERVICE_USER}:{SERVICE_USER} {TOKEN_PATH} && chmod 0600 {TOKEN_PATH} || log "token chown failed"
chmod 0755 /opt/topcam /opt/topcam/topcam /etc/topcam
systemctl daemon-reload
systemctl enable --now topcam.service || log "topcam.service failed to start"
{"systemctl enable ssh.service 2>/dev/null || true" if ssh else "systemctl disable --now ssh.service 2>/dev/null || true"}
systemctl reload-or-restart avahi-daemon.service || log "avahi reload failed"

# The FAT boot partition is readable by anyone holding the card. The Wi-Fi PSK (now in
# /etc/netplan, root only) and the API token (now in {TOKEN_PATH}) are installed, so replace
# the seeds with secret-free stubs and keep cloud-init from running again.
BOOT=/boot/firmware; [ -d "$BOOT" ] || BOOT=/boot
# On FAT a truncate only frees the clusters: zero the old bytes in place first.
for f in "$BOOT/user-data" "$BOOT/network-config"; do
  [ -f "$f" ] && dd if=/dev/zero of="$f" bs="$(stat -c %s "$f")" count=1 conv=notrunc,fsync 2>/dev/null
done
printf '#cloud-config\\n# topcam: provisioned on first boot; secrets removed\\n' > "$BOOT/user-data"
printf '# topcam: provisioned on first boot; Wi-Fi settings live in /etc/netplan\\n' > "$BOOT/network-config"
rm -f /var/lib/cloud/instance/user-data.txt /var/lib/cloud/instance/user-data.txt.i \\
  /var/lib/cloud/instance/cloud-config.txt /var/lib/cloud/instance/obj.pkl
touch /etc/cloud/cloud-init.disabled
sync
log "done: http://$(hostname).local:{NODE_PORT}/health"
"""


def _write_file(path: str, content: bytes, mode: str) -> list[str]:
    return [
        f"  - path: {path}",
        f"    permissions: '{mode}'",
        "    owner: root:root",
        "    encoding: b64",
        # quoted: an empty file (topcam/__init__.py) must stay "", not YAML null
        f'    content: "{base64.b64encode(content).decode("ascii")}"',
    ]


def render_user_data(
    spec: NodeSpec, secrets: Secrets, sources: Mapping[str, bytes], version: str
) -> str:
    """The cloud-init `user-data` (YAML) for Raspberry Pi OS Trixie (cloud-init 25.x with the
    raspberry-pi-os distro: the first user listed replaces the default `pi` user)."""
    lines = [
        "#cloud-config",
        f"# topcam camera node, rendered by `3d camera image build` (3d-cli {version}).",
        "# Holds the API token: the first-boot script replaces this file with a stub.",
        f"hostname: {_q(spec.hostname)}",
        "manage_etc_hosts: true",
        f"enable_ssh: {'true' if spec.ssh else 'false'}",
        f"ssh_pwauth: {'true' if spec.ssh and secrets.password_hash and not spec.ssh_keys else 'false'}",
        "users:",
        f"  - name: {_q(spec.user)}",
        "    groups: [adm, sudo, video, gpio, plugdev, netdev, dialout, users]",
        "    shell: /bin/bash",
        '    sudo: "ALL=(ALL) NOPASSWD:ALL"',
    ]
    if secrets.password_hash:
        lines += [
            "    lock_passwd: false",
            f"    hashed_passwd: {_q(secrets.password_hash)}",
        ]
    else:
        lines.append("    lock_passwd: true")
    if spec.ssh_keys:
        lines.append("    ssh_authorized_keys:")
        lines += [f"      - {_q(k)}" for k in spec.ssh_keys]
    # Regulatory domain before the network comes up, so the radio is not left rfkill-blocked
    # (raspi-config also unblocks rfkill and persists the country).
    lines += [
        "bootcmd:",
        "  - [cloud-init-per, once, topcam-wifi-country, raspi-config, nonint, "
        f"do_wifi_country, {_q(spec.wifi_country)}]",
        "package_update: true",
        "packages: [" + ", ".join(PACKAGES) + "]",
        "write_files:",
    ]
    for name in NODE_FILES:
        lines += _write_file(f"/opt/topcam/topcam/{name}", sources[name], "0644")
    config = json.dumps(render_config(spec), indent=2) + "\n"
    lines += _write_file(CONFIG_PATH, config.encode(), "0644")
    lines += _write_file(TOKEN_PATH, (secrets.token + "\n").encode(), "0600")
    lines += _write_file(
        "/etc/systemd/system/topcam.service", render_unit().encode(), "0644"
    )
    lines += _write_file(
        "/etc/avahi/services/topcam.service", render_avahi(spec).encode(), "0644"
    )
    lines += _write_file(FIRSTBOOT_PATH, render_firstboot(spec.ssh).encode(), "0700")
    lines += ["runcmd:", f"  - [{FIRSTBOOT_PATH}]", ""]
    return "\n".join(lines)


def render_network_config(spec: NodeSpec, secrets: Secrets) -> str:
    """cloud-init network-config (netplan v2) for the Pi's wlan0 via NetworkManager."""
    return "\n".join(
        [
            "# topcam Wi-Fi, rendered by `3d camera image build`.",
            "# Holds the PSK: the first-boot script replaces this file with a stub.",
            "network:",
            "  version: 2",
            "  wifis:",
            "    renderer: NetworkManager",
            "    wlan0:",
            "      dhcp4: true",
            "      optional: true",
            f"      regulatory-domain: {_q(spec.wifi_country)}",
            "      access-points:",
            f"        {_q(spec.wifi_ssid)}:",
            f"          password: {_q(secrets.wifi_psk)}",
            "",
        ]
    )


def render_meta_data(spec: NodeSpec, stamp: str) -> str:
    return (
        "# topcam, rendered by `3d camera image build`.\n"
        "dsmode: local\n"
        f"instance_id: topcam-{spec.hostname}-{stamp}\n"
    )


def patch_cmdline(text: str, country: str) -> str:
    """cmdline.txt with `cfg80211.ieee80211_regdom=CC` (what raspi-config and Raspberry Pi
    Imager set), replacing any earlier value; stays one line."""
    words = [w for w in text.split() if not w.startswith("cfg80211.ieee80211_regdom=")]
    return " ".join([*words, f"cfg80211.ieee80211_regdom={country}"]) + "\n"


def boot_files(
    spec: NodeSpec,
    secrets: Secrets,
    cmdline: str,
    *,
    sources: Mapping[str, bytes] | None = None,
    version: str = "dev",
    stamp: str = "0",
) -> dict[str, bytes]:
    """File name → content for the FAT boot partition."""
    return {
        "user-data": render_user_data(
            spec, secrets, sources or package_sources(), version
        ).encode(),
        "network-config": render_network_config(spec, secrets).encode(),
        "meta-data": render_meta_data(spec, stamp).encode(),
        "cmdline.txt": patch_cmdline(cmdline, spec.wifi_country).encode(),
    }


# --------------------------------------------------------------------------- tools


def _install(tool: str) -> str:
    from cli.env import install_cmd

    return install_cmd(tool)


def require_tools() -> dict[str, str]:
    found: dict[str, str] = {}
    for binary in ("mcopy", "mtype", "mdir"):
        path = shutil.which(binary)
        if path is None:
            raise MissingDependency(
                "mtools",
                install=_install("mtools"),
                degrades="the FAT boot partition cannot be edited without root",
                command=CMD_NAME,
            )
        found[binary] = path
    xz = shutil.which("xz")
    if xz is None:
        raise MissingDependency(
            "xz",
            install=_install("xz"),
            degrades="the image cannot be (de)compressed",
            command=CMD_NAME,
        )
    found["xz"] = xz
    return found


def _mtools_env() -> dict[str, str]:
    return {**os.environ, "MTOOLS_SKIP_CHECK": "1"}


def _mtools(tools: Mapping[str, str], binary: str, args: Sequence[str]) -> str:
    proc = subprocess.run(
        [tools[binary], *args],
        capture_output=True,
        text=True,
        env=_mtools_env(),
        check=False,
    )
    if proc.returncode != 0:
        raise ThreeDError(
            f"{binary} {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}",
            command=CMD_NAME,
        )
    return proc.stdout


class Fat:
    """The FAT boot partition inside an image file, through mtools (no mount, no root)."""

    def __init__(self, tools: Mapping[str, str], image: Path, offset: int) -> None:
        self.tools = tools
        self.drive = f"{image}@@{offset}"

    def names(self) -> list[str]:
        out = _mtools(self.tools, "mdir", ["-b", "-i", self.drive, "::/"])
        return sorted(
            line.strip().removeprefix("::/").rstrip("/")
            for line in out.splitlines()
            if line.strip()
        )

    def listing(self) -> str:
        return _mtools(self.tools, "mdir", ["-i", self.drive, "::/"])

    def read(self, name: str) -> bytes:
        proc = subprocess.run(
            [self.tools["mtype"], "-i", self.drive, f"::/{name}"],
            capture_output=True,
            env=_mtools_env(),
            check=False,
        )
        if proc.returncode != 0:
            raise ThreeDError(
                f"mtype ::/{name} failed: {proc.stderr.decode(errors='replace').strip()}",
                command=CMD_NAME,
            )
        return proc.stdout

    def write(self, name: str, src: Path) -> None:
        _mtools(self.tools, "mcopy", ["-o", "-i", self.drive, str(src), f"::/{name}"])


# --------------------------------------------------------------------------- base image


@dataclass(frozen=True)
class Base:
    path: Path
    name: (
        str  # file stem without .img/.img.xz, e.g. 2026-10-06-raspios-trixie-armhf-lite
    )
    source: str  # URL or local path
    sha256: (
        str | None
    )  # verified published sha256 of the downloaded file; None for local files
    cached: bool


def _stem(name: str) -> str:
    for suffix in (".img.xz", ".img", ".xz"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _log(msg: str) -> None:
    print(f"[camera image] {msg}", file=sys.stderr, flush=True)


def _urlopen(url: str, method: str = "GET", timeout: float = 60) -> Any:
    req = urllib.request.Request(
        url, method=method, headers={"User-Agent": "3d-cli topcam-image"}
    )
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 — http(s)/file URLs by design


def resolve_latest(url: str = LATEST_URL) -> str:
    """The versioned image URL the `*_latest` redirect points at."""
    with _urlopen(url, method="HEAD") as resp:
        return str(resp.geturl())


def parse_sha256_file(text: str) -> str:
    digest = text.strip().split()[0].lower() if text.strip() else ""
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("not a sha256 sidecar (expected '<64 hex>  <file>')")
    return digest


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _fetch_text(url: str) -> str:
    with _urlopen(url) as resp:
        return bytes(resp.read(4096)).decode("utf-8", errors="replace")


def _download(url: str, dest: Path, want: str) -> None:
    part = dest.with_name(f"{dest.name}.{os.getpid()}.part")  # two builds never share one
    h = hashlib.sha256()
    _log(f"downloading {url}")
    try:
        with _urlopen(url, timeout=120) as resp, part.open("wb") as out:
            while chunk := resp.read(CHUNK):
                h.update(chunk)
                out.write(chunk)
    except (OSError, http.client.HTTPException) as exc:  # a dropped connection mid-download
        part.unlink(missing_ok=True)
        raise ThreeDError(f"download of {url} failed: {exc}", command=CMD_NAME,
                          remediation=["Retry; the partial file was removed."]) from exc
    except BaseException:  # Ctrl-C: never leave a half-downloaded image behind
        part.unlink(missing_ok=True)
        raise
    if h.hexdigest() != want:
        part.unlink(missing_ok=True)
        raise ThreeDError(
            f"sha256 mismatch for {url}: got {h.hexdigest()}, published {want}",
            command=CMD_NAME,
            remediation=[
                "Retry; a persistent mismatch means a broken mirror or a tampered file."
            ],
        )
    part.replace(dest)


def fetch_base(url: str, cache: Path) -> Base:
    """Download `url` (+ its published `.sha256`) into `cache`, or reuse the cached copy after
    re-checking it against the sidecar."""
    name = urllib.parse.unquote(urllib.parse.urlsplit(url).path).rstrip("/").rsplit("/", 1)[-1]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise UsageError(f"base image URL {url!r} does not end in a plain file name", command=CMD_NAME)
    dest = cache / name
    sidecar = cache / (name + ".sha256")
    try:
        want = parse_sha256_file(_fetch_text(url + ".sha256"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        if dest.is_file() and sidecar.is_file():
            want = parse_sha256_file(sidecar.read_text())
            _log(f"cannot fetch {url}.sha256 ({exc}); using the cached sidecar")
        else:
            raise ThreeDError(
                f"cannot get the published sha256 for {url}: {exc}",
                command=CMD_NAME,
                remediation=[
                    "The base image is verified against URL.sha256 (published next to every "
                    "Raspberry Pi OS image). For an image without one, download it yourself "
                    "and pass --base-image FILE.",
                ],
            ) from exc
    _private_dir(cache)
    cached = dest.is_file() and file_sha256(dest) == want
    if not cached:
        _download(url, dest, want)  # raises ThreeDError on any network failure
    sidecar.write_text(f"{want}  {name}\n")
    return Base(dest, _stem(name), url, want, cached)


def newest_cached(cache: Path) -> Path | None:
    found = sorted(
        (p for p in cache.glob("*.img.xz") if (cache / (p.name + ".sha256")).is_file()),
        key=lambda p: p.name,
    )
    return found[-1] if found else None


def resolve_base(arg: str | None, cache: Path) -> Base:
    """--base-image: None → latest Raspberry Pi OS Lite armhf; URL → that image; else a file."""
    if arg and not re.match(r"^(https?|file)://", arg):
        path = Path(arg).expanduser()
        if not path.is_file():
            raise InputNotFound(str(path), command=CMD_NAME)
        return Base(path, _stem(path.name), str(path), None, True)
    if arg:
        return fetch_base(arg, cache)
    try:
        url = resolve_latest()
    except (urllib.error.URLError, OSError) as exc:
        old = newest_cached(cache)
        if old is None:
            raise ThreeDError(
                f"cannot resolve the latest Raspberry Pi OS Lite armhf ({LATEST_URL}): {exc}",
                command=CMD_NAME,
                remediation=["Check the network, or pass --base-image FILE|URL."],
            ) from exc
        want = parse_sha256_file((cache / (old.name + ".sha256")).read_text())
        if file_sha256(old) != want:
            raise ThreeDError(
                f"cached base {old} fails its sha256; delete it", command=CMD_NAME
            ) from exc
        _log(f"offline ({exc}); using the cached {old.name}")
        return Base(old, _stem(old.name), str(old), want, True)
    return fetch_base(url, cache)


# --------------------------------------------------------------------------- streaming


@contextmanager
def open_raw(path: Path, xz: str) -> Iterator[tuple[IO[bytes], Callable[[], int]]]:
    """A reader of the raw image bytes and a `finish()` returning the decompressor's exit
    status (0 for a plain file). `.xz` is decompressed single-threaded (`xz -d -T1`): its
    memory stays at a few MB, the 2 GB cgroup cap is for page cache."""
    with path.open("rb") as fh:
        compressed = fh.read(6) == b"\xfd7zXZ\x00"  # by content: a URL need not end in .xz
    if compressed:
        proc = subprocess.Popen([xz, "-dc", "-T1", str(path)], stdout=subprocess.PIPE)
        assert proc.stdout is not None
        try:
            yield proc.stdout, proc.wait
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()
            proc.wait()
    else:
        with path.open("rb") as fh:
            yield fh, lambda: 0


def _copy(src: IO[bytes], dst: IO[bytes] | None, count: int | None) -> int:
    """Copy `count` bytes (all when None) in CHUNK pieces, discarding them when `dst` is
    None; returns the bytes read."""
    done = 0
    while count is None or done < count:
        want = CHUNK if count is None else min(CHUNK, count - done)
        chunk = src.read(want)
        if not chunk:
            break
        if dst is not None:
            dst.write(chunk)
        done += len(chunk)
    return done


def extract_head(base: Path, head: Path, xz: str) -> tuple[list[Partition], Partition]:
    """Write the image bytes up to the end of the boot partition to `head`."""
    with open_raw(base, xz) as (raw, _finish), head.open("wb") as out:
        mbr = raw.read(512)
        try:
            parts = parse_mbr(mbr)
            boot = boot_partition(parts)
        except ValueError as exc:
            raise ThreeDError(
                f"{base}: not a Raspberry Pi OS image ({exc})",
                command=CMD_NAME,
                remediation=[
                    "Pass a Raspberry Pi OS Lite armhf .img or .img.xz as --base-image."
                ],
            ) from exc
        out.write(mbr)
        got = 512 + _copy(raw, out, boot.end - 512)
    if got != boot.end:
        raise ThreeDError(
            f"{base}: image ends at {got} bytes, inside the boot partition (ends at {boot.end})",
            command=CMD_NAME,
        )
    return parts, boot


def stream_output(base: Path, head: Path, out: Path, xz: str, threads: int) -> int:
    """out = xz(head + base[len(head):]); returns the raw image size."""
    skip = head.stat().st_size
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as sink:
        comp = subprocess.Popen(
            [xz, "-zc", "-6", f"-T{threads}", f"--memlimit-compress={XZ_MEMLIMIT}"],
            stdin=subprocess.PIPE,
            stdout=sink,
        )
        assert comp.stdin is not None
        total = 0
        broken: BrokenPipeError | None = None
        try:
            with head.open("rb") as fh:
                total += _copy(fh, comp.stdin, None)
            with open_raw(base, xz) as (raw, finish):
                if _copy(raw, None, skip) != skip:
                    raise ThreeDError(
                        f"{base}: shorter than its boot partition", command=CMD_NAME
                    )
                total += _copy(raw, comp.stdin, None)
                status = finish()
            if status != 0:
                raise ThreeDError(f"xz -d {base} exited {status}", command=CMD_NAME)
        except BrokenPipeError as exc:  # the compressor died; its exit code says why
            broken = exc
        finally:
            with contextlib.suppress(BrokenPipeError):
                comp.stdin.close()
            code = comp.wait()
    if code != 0 or broken is not None:
        raise ThreeDError(f"xz compression exited {code}", command=CMD_NAME,
                          remediation=["Check free disk space and memory, then rebuild."])
    return total


# --------------------------------------------------------------------------- build


@dataclass
class BuildResult:
    image: Path
    sha256: str
    size: int
    raw_size: int
    flash: Path
    base: Base
    spec: NodeSpec
    password: bool
    boot_listing: list[str]
    seconds: dict[str, float]

    def as_json(self) -> dict[str, Any]:
        return {
            "schema": "topcam-image/1",
            "image": str(self.image),
            "sha256": self.sha256,
            "sha256_file": str(self.image) + ".sha256",
            "flash_md": str(self.flash),
            "size": self.size,
            "raw_size": self.raw_size,
            "base": {
                "source": self.base.source,
                "file": str(self.base.path),
                "sha256": self.base.sha256,
                "verified": self.base.sha256 is not None,
                "cached": self.base.cached,
            },
            "first_boot": "cloud-init",
            "hostname": self.spec.hostname,
            "user": self.spec.user,
            "wifi": {
                "ssid": self.spec.wifi_ssid,
                "country": self.spec.wifi_country,
                "psk": REDACTED,
            },
            "api": {
                "url": self.spec.api_url,
                "token": REDACTED,
                "token_file": TOKEN_PATH,
            },
            "ssh": {
                "enabled": self.spec.ssh,
                "keys": len(self.spec.ssh_keys),
                "password": self.password,
            },
            "led_gpio": self.spec.led_gpio,
            "still_lock": dict(self.spec.lock),
            "boot_files": self.boot_listing,
            "seconds": self.seconds,
        }


def _private_dir(path: Path) -> None:
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not existed:
        path.chmod(0o700)
    elif path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        _log(
            f"note: {path} is readable by others; the image (with secrets) is written 0600"
        )


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    path.chmod(0o600)


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def render_flash_md(result_name: str, spec: NodeSpec, out: Path, base: Base) -> str:
    img = shlex.quote(str(out / f"{result_name}.img.xz"))  # pasted into a shell below
    qout = shlex.quote(str(out))
    qsha = shlex.quote(f"{result_name}.img.xz.sha256")
    return f"""# Flashing {result_name}.img.xz (topcam camera node)

Built by `3d camera image build` from `{base.source}`.
Host name: `{spec.hostname}`, login user: `{spec.user}`, Wi-Fi: `{spec.wifi_ssid}` ({spec.wifi_country}).
The image holds the Wi-Fi password and the 3d-api token: keep it private and delete it
when the card works (rebuild any time with the same command).

## 1. Check the download

    cd {qout}
    sha256sum -c {qsha}

## 2. Write it to the SD card (8 GB or larger)

Raspberry Pi Imager: *Choose OS → Use custom* → `{img}`, choose the card, and answer
**No** to "apply OS customisation settings" (the image is already customised; Imager's
settings would replace them).

Or with `dd` on Linux — find the card first and triple-check the device, `dd` overwrites
whatever you point it at:

    lsblk -o NAME,SIZE,MODEL,TRAN,MOUNTPOINTS     # the card is the ~8-64G "sdX"/"mmcblkN" disk
    sudo umount /dev/sdX?* 2>/dev/null
    xzcat {img} | sudo dd of=/dev/sdX bs=4M conv=fsync status=progress
    sync

(macOS: `diskutil list`, `diskutil unmountDisk /dev/diskN`, then `of=/dev/rdiskN bs=4m`.)

## 3. First boot

Put the card in the Pi Zero W, plug the C920 into the micro-USB OTG port, power it up.
The first boot takes **about 10-20 minutes** on a Pi Zero W: cloud-init expands the root
file system, joins `{spec.wifi_ssid}`, installs `ustreamer` from apt, creates the
`{SERVICE_USER}` service user and starts `topcam.service`. Later boots take under a minute.
At the end of the first boot the Wi-Fi password and the token are wiped from the FAT boot
partition (they stay only in `/etc/netplan` and `{TOKEN_PATH}`, root/service-user only).

## 4. Check it

    curl http://{spec.hostname}.local:{NODE_PORT}/health
    3d camera controls --camera {spec.hostname}
    3d camera still --camera {spec.hostname} --out plate.png
    ssh {spec.user}@{spec.hostname}.local        # with your SSH key
    #  on the Pi: systemctl status topcam; journalctl -u topcam -b; cloud-init status --long

The node also registers itself with 3d-api at `{spec.api_url}` every minute
(`PUT /v1/camera-nodes/{spec.hostname}`), so `--camera {spec.hostname}` resolves through the API
too; a `camera` token registers only the node of its own name, so it must have been minted as
`{spec.hostname}` (`journalctl -u topcam` on the Pi shows a refused registration).
If `{spec.hostname}.local` does not resolve after 20 minutes, look for the Pi in your
router's DHCP list; a wrong Wi-Fi password or country is the usual cause — rebuild and reflash.
"""


def build(
    spec: NodeSpec,
    secrets: Secrets,
    *,
    base_arg: str | None,
    out: Path,
    cache: Path,
    repo: Path | None = None,
    threads: int | None = None,
    version: str = "dev",
) -> BuildResult:
    if repo is not None and _inside(out, repo):
        raise UsageError(
            f"--out {out} is inside the repository; the image holds secrets",
            command=CMD_NAME,
            remediation=[
                "Use a directory outside the checkout (default ~/.cache/3d-cli/topcam-image)."
            ],
        )
    tools = require_tools()
    if threads is None:
        from cli.resources import max_workers

        threads = max_workers()
    seconds: dict[str, float] = {}
    t0 = time.monotonic()
    _private_dir(out)  # before the base fetch: the default cache lives under it
    base = resolve_base(base_arg, cache)
    seconds["base"] = round(time.monotonic() - t0, 1)
    _log(
        f"base: {base.path}"
        + (f" (sha256 {base.sha256[:12]}… verified)" if base.sha256 else "")
    )
    name = f"{spec.hostname}-{base.name}"
    image = out / f"{name}.img.xz"
    work = Path(tempfile.mkdtemp(prefix=".work-", dir=out))
    staged = work / image.name  # the previous good image stays until this one is complete
    try:
        t = time.monotonic()
        head = work / "head.img"
        parts, boot = extract_head(base.path, head, tools["xz"])
        seconds["extract_head"] = round(time.monotonic() - t, 1)
        t = time.monotonic()
        fat = Fat(tools, head, boot.offset)
        present = {n.lower() for n in fat.names()}
        missing = {"user-data", "meta-data", "cmdline.txt"} - present
        if missing:
            raise ThreeDError(
                f"{base.path}: the boot partition has no cloud-init seed ({', '.join(sorted(missing))} "
                "missing); this builder supports the cloud-init first boot of Raspberry Pi OS "
                "Trixie (2025-10 and later)",
                command=CMD_NAME,
                remediation=[
                    "Use the current Raspberry Pi OS Lite armhf (the default --base-image)."
                ],
            )
        cmdline = fat.read("cmdline.txt").decode("utf-8")
        files = boot_files(
            spec, secrets, cmdline, version=version, stamp=time.strftime("%Y%m%d%H%M%S")
        )
        for fname, data in files.items():
            local = work / fname
            _write_private(local, data)
            fat.write(fname, local)
            local.unlink()
        listing = fat.names()
        seconds["customize"] = round(time.monotonic() - t, 1)
        t = time.monotonic()
        _log(f"compressing {image.name} (xz -T{threads}, memlimit {XZ_MEMLIMIT})")
        raw_size = stream_output(base.path, head, staged, tools["xz"], threads)
        need = max(p.end for p in parts)
        if raw_size < need:
            raise ThreeDError(f"{base.path}: truncated ({raw_size} bytes, the partitions end at {need})",
                              command=CMD_NAME, remediation=["Download the base image again."])
        seconds["compress"] = round(time.monotonic() - t, 1)
        digest = file_sha256(staged)
        staged.replace(image)
        _write_private(Path(f"{image}.sha256"), f"{digest}  {image.name}\n".encode())
    finally:
        shutil.rmtree(work, ignore_errors=True)
    flash = out / "FLASH.md"
    _write_private(flash, render_flash_md(name, spec, out, base).encode())
    seconds["total"] = round(time.monotonic() - t0, 1)
    return BuildResult(
        image=image,
        sha256=digest,
        size=image.stat().st_size,
        raw_size=raw_size,
        flash=flash,
        base=base,
        spec=spec,
        password=secrets.password_hash is not None,
        boot_listing=listing,
        seconds=seconds,
    )


# --------------------------------------------------------------------------- CLI


VALUES = {
    "--wifi-ssid",
    "--wifi-psk",
    "--wifi-psk-file",
    "--wifi-country",
    "--hostname",
    "--user",
    "--api-url",
    "--token",
    "--token-file",
    "--ssh-key",
    "--password-hash",
    "--led-gpio",
    "--still-lock",
    "--base-image",
    "--out",
}
FLAGS = {"--no-ssh", "--json"}


def _secret(opts: Mapping[str, str], flag: str, file_flag: str) -> str:
    if flag in opts and file_flag in opts:
        raise UsageError(f"give {flag} or {file_flag}, not both", command=CMD_NAME)
    if file_flag in opts:
        path = Path(opts[file_flag]).expanduser()
        if not path.is_file():
            raise InputNotFound(str(path), command=CMD_NAME)
        return path.read_text(encoding="utf-8").rstrip("\r\n")
    if flag in opts:
        return opts[flag]
    raise UsageError(
        f"{flag} or {file_flag} is required",
        command=CMD_NAME,
        remediation=[
            f"`{file_flag} FILE` keeps the secret out of shell history and `ps`."
        ],
    )


def spec_from_opts(
    opts: Mapping[str, str], flags: set[str]
) -> tuple[NodeSpec, Secrets]:
    if "--wifi-ssid" not in opts or "--api-url" not in opts:
        raise UsageError(
            "--wifi-ssid and --api-url are required",
            command=CMD_NAME,
            remediation=["See `3d camera image build --help`."],
        )
    no_ssh = "--no-ssh" in flags
    password_hash = opts.get("--password-hash")
    if password_hash is not None and not password_hash.startswith("$"):
        raise UsageError(
            "--password-hash wants a crypt(3) hash such as `openssl passwd -6` prints ($6$…)",
            command=CMD_NAME,
        )
    keys: tuple[str, ...] = ()
    if no_ssh and "--ssh-key" in opts:
        raise UsageError(
            "--no-ssh and --ssh-key contradict each other", command=CMD_NAME
        )
    if not no_ssh:
        key_path = (
            Path(opts["--ssh-key"]).expanduser()
            if "--ssh-key" in opts
            else default_ssh_key()
        )
        if key_path is None:
            if password_hash is None:
                raise UsageError(
                    "no SSH public key: ~/.ssh/id_ed25519.pub and ~/.ssh/id_rsa.pub are absent",
                    command=CMD_NAME,
                    remediation=[
                        "Pass --ssh-key FILE.pub (create one: ssh-keygen -t ed25519),",
                        'or --password-hash "$(openssl passwd -6)" for password login,',
                        "or --no-ssh for a node without SSH.",
                    ],
                )
        elif not key_path.is_file():
            raise InputNotFound(str(key_path), command=CMD_NAME)
        else:
            keys = parse_ssh_keys(key_path.read_text(encoding="utf-8"), str(key_path))
    spec = NodeSpec(
        hostname=validate_hostname(opts.get("--hostname", "topcam")),
        user=validate_user(opts.get("--user", "topcam")),
        wifi_ssid=validate_ssid(opts["--wifi-ssid"]),
        wifi_country=validate_country(opts.get("--wifi-country", "RS")),
        api_url=validate_api_url(opts["--api-url"]),
        ssh_keys=keys,
        ssh=not no_ssh,
        led_gpio=validate_led(opts["--led-gpio"]) if "--led-gpio" in opts else None,
        lock=parse_lock(opts["--still-lock"]) if "--still-lock" in opts else {},
    )
    render_config(
        spec
    )  # the node's own parser rejects a bad --still-lock before any download
    secrets = Secrets(
        wifi_psk=validate_psk(_secret(opts, "--wifi-psk", "--wifi-psk-file")),
        token=validate_token(_secret(opts, "--token", "--token-file")),
        password_hash=password_hash,
    )
    return spec, secrets


def _version() -> str:
    from cli.dispatch import VERSION

    return VERSION


def run(argv: list[str]) -> int:
    """`3d camera image build …` (argv after `image`)."""
    if not argv or argv[0] != "build":
        raise UsageError(
            "usage: 3d camera image build --wifi-ssid S --wifi-psk-file F --api-url URL --token-file F",
            command="camera image",
            remediation=["See `3d camera image build --help`."],
        )
    from cli.env import repo_root
    from cli.paths import cache_dir
    from printops.cli._common import parse

    p = parse(argv[1:], "image build", values=VALUES, flags=FLAGS, prefix="camera")
    spec, secrets = spec_from_opts(p.opts, p.flags)
    root = cache_dir() / "topcam-image"
    out = Path(p.opts["--out"]).expanduser() if "--out" in p.opts else root
    result = build(
        spec,
        secrets,
        base_arg=p.opts.get("--base-image"),
        out=out.absolute(),
        cache=root / "base",
        repo=Path(repo_root()),
        version=_version(),
    )
    if "--json" in p.flags:
        print(json.dumps(result.as_json(), indent=2))
    else:
        mb = result.size / 1e6
        print(f"{result.image}  ({mb:.0f} MB, raw {result.raw_size / 1e9:.2f} GB)")
        print(f"{result.image}.sha256  sha256 {result.sha256}")
        print(f"{result.flash}  — how to flash and check the node")
        print(
            f"hostname {spec.hostname}, user {spec.user}, Wi-Fi {spec.wifi_ssid!r} ({spec.wifi_country}), "
            f"API {spec.api_url}; secrets redacted; built in {result.seconds['total']:.0f} s"
        )
    return 0
