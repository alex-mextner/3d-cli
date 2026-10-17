"""A tiny synthetic Raspberry Pi OS disk image for `3d camera image build` tests.

WHAT: MBR (FAT32-LBA boot partition at LBA 2048 + a Linux partition) + an 8 MB FAT made by
  `mformat` holding the stock cloud-init seed (cmdline.txt, user-data, meta-data,
  network-config) + a patterned "root file system", so a test can check that the builder edits
  only the boot partition and copies everything else byte for byte.
WHY: the real image is 2.7 GB and needs the network; the builder's logic does not.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

BOOT_LBA = 2048
BOOT_SECTORS = 16384  # 8 MiB
ROOT_LBA = BOOT_LBA + BOOT_SECTORS
ROOT_SECTORS = 2048  # 1 MiB
CMDLINE = (
    "console=serial0,115200 console=tty1 root=PARTUUID=4c032286-02 rootfstype=ext4 "
    "fsck.repair=yes rootwait resize\n"
)
STOCK_SEED = {
    "cmdline.txt": CMDLINE,
    "user-data": "#cloud-config\n#hostname: raspberrypi\n",
    "meta-data": "dsmode: local\ninstance_id: rpios-image\n",
    "network-config": "# netplan\n",
    "config.txt": "arm_boost=1\n",
}


def need_mtools() -> None:
    missing = [
        b for b in ("mformat", "mcopy", "mtype", "mdir") if shutil.which(b) is None
    ]
    if missing or shutil.which("xz") is None:
        pytest.skip(
            f"mtools/xz not installed ({', '.join(missing) or 'xz'}): "
            "brew install mtools xz, or apt-get install mtools xz-utils"
        )


def mbr(parts: list[tuple[int, int, int]]) -> bytes:
    """A 512-byte MBR with (type, start_lba, sectors) primary partitions."""
    sector = bytearray(512)
    for i, (ptype, start, count) in enumerate(parts):
        entry = struct.pack(
            "<B3sB3sII",
            0x80 if i == 0 else 0,
            b"\0\0\0",
            ptype,
            b"\0\0\0",
            start,
            count,
        )
        sector[446 + 16 * i : 462 + 16 * i] = entry
    sector[510:512] = b"\x55\xaa"
    return bytes(sector)


def root_bytes() -> bytes:
    pattern = b"ROOTFS-ext4-not-touched-"
    size = ROOT_SECTORS * 512
    return (pattern * (size // len(pattern) + 1))[:size]


def make_base_image(
    where: Path, *, seed: dict[str, str] | None = None, xz: bool = False
) -> Path:
    """Build the fixture image in `where`; returns base.img (or base.img.xz)."""
    need_mtools()
    where.mkdir(parents=True, exist_ok=True)
    fat = where / "fat.img"
    subprocess.run(
        [
            "mformat",
            "-C",
            "-i",
            str(fat),
            "-T",
            str(BOOT_SECTORS),
            "-v",
            "BOOTFS",
            "::",
        ],
        check=True,
    )
    files = STOCK_SEED if seed is None else seed
    for name, text in files.items():
        src = where / f"seed-{name}"
        src.write_text(text)
        subprocess.run(
            ["mcopy", "-i", str(fat), str(src), f"::/{name}"],
            check=True,
            env=_env(),
        )
        src.unlink()
    img = where / "2026-10-06-raspios-trixie-armhf-lite.img"
    with img.open("wb") as out:
        out.write(mbr([(0x0C, BOOT_LBA, BOOT_SECTORS), (0x83, ROOT_LBA, ROOT_SECTORS)]))
        out.write(b"\0" * (BOOT_LBA * 512 - 512))
        out.write(fat.read_bytes())
        out.write(root_bytes())
    fat.unlink()
    if xz:
        subprocess.run(["xz", "-T1", "-0", str(img)], check=True)
        return img.with_name(img.name + ".xz")
    return img


def _env() -> dict[str, str]:
    return {**os.environ, "MTOOLS_SKIP_CHECK": "1"}


def fat_read(image: Path, name: str) -> bytes:
    """A file from the boot partition of a raw image."""
    return subprocess.run(
        ["mtype", "-i", f"{image}@@{BOOT_LBA * 512}", f"::/{name}"],
        check=True,
        capture_output=True,
        env=_env(),
    ).stdout


def unxz(src: Path, dest: Path) -> Path:
    with dest.open("wb") as out:
        subprocess.run(["xz", "-dc", "-T1", str(src)], check=True, stdout=out)
    return dest
