"""Backup target discovery: read-only, shows size/free, flags (never chooses), covers USB, network, unmounted disks."""

from __future__ import annotations

import json
import re
import shlex

from printops import backup_discovery as d

GB = 10**9
LSBLK = json.dumps(
    {
        "blockdevices": [
            {
                "name": "nvme0n1",
                "path": "/dev/nvme0n1",
                "size": 512 * GB,
                "type": "disk",
                "rm": False,
                "hotplug": False,
                "tran": "nvme",
                "model": "Samsung SSD",
                "children": [
                    {
                        "name": "nvme0n1p1",
                        "path": "/dev/nvme0n1p1",
                        "size": 512 * 10**6,
                        "fstype": "vfat",
                        "mountpoints": ["/boot/efi"],
                        "type": "part",
                    },
                    {
                        "name": "nvme0n1p2",
                        "path": "/dev/nvme0n1p2",
                        "size": 500 * GB,
                        "fstype": "ext4",
                        "mountpoints": ["/"],
                        "type": "part",
                    },
                ],
            },
            {
                "name": "sda",
                "path": "/dev/sda",
                "size": 500 * GB,
                "type": "disk",
                "rm": False,
                "hotplug": False,
                "tran": "sata",
                "model": "WDC WD5000",
                "children": [
                    {
                        "name": "sda1",
                        "path": "/dev/sda1",
                        "size": 500 * GB,
                        "fstype": "ext4",
                        "label": "wd500",
                        "uuid": "u-1",
                        "mountpoints": ["/mnt/wd500"],
                        "type": "part",
                    }
                ],
            },
            {
                "name": "sdb",
                "path": "/dev/sdb",
                "size": 2000 * GB,
                "type": "disk",
                "rm": True,
                "hotplug": True,
                "tran": "usb",
                "model": "My Passport",
                "children": [
                    {
                        "name": "sdb1",
                        "path": "/dev/sdb1",
                        "size": 2000 * GB,
                        "fstype": "exfat",
                        "label": "PASSPORT",
                        "uuid": "u-2",
                        "mountpoints": ["/media/ultra/PASSPORT"],
                        "type": "part",
                    }
                ],
            },
            {
                "name": "sdc",
                "path": "/dev/sdc",
                "size": 512 * GB,
                "type": "disk",
                "rm": False,
                "hotplug": False,
                "tran": "sata",
                "model": "Crucial MX500",
                "children": [
                    {
                        "name": "sdc1",
                        "path": "/dev/sdc1",
                        "size": 512 * GB,
                        "fstype": "ext4",
                        "label": "backup512",
                        "uuid": "u-3",
                        "mountpoints": [None],
                        "type": "part",
                    }
                ],
            },
            {
                "name": "sdd",
                "path": "/dev/sdd",
                "size": 1000 * GB,
                "type": "disk",
                "rm": False,
                "tran": "sata",
                "children": [
                    {
                        "name": "sdd1",
                        "path": "/dev/sdd1",
                        "size": 1000 * GB,
                        "fstype": None,
                        "mountpoints": [None],
                        "type": "part",
                    }
                ],
            },
            {
                "name": "loop0",
                "path": "/dev/loop0",
                "size": 60 * 10**6,
                "fstype": "squashfs",
                "mountpoints": ["/snap/core/1"],
                "type": "loop",
            },
        ]
    }
)
DF = f"""Filesystem     Type      1024-blocks      Used Available Capacity Mounted on
/dev/nvme0n1p2 ext4        {500 * GB // 1024}  {400 * GB // 1024}  {90 * GB // 1024}      82% /
tmpfs          tmpfs           1000       0      1000       0% /run
/dev/nvme0n1p1 vfat          {512 * 10**6 // 1024}    1000  {500 * 10**6 // 1024}       1% /boot/efi
/dev/sda1      ext4        {500 * GB // 1024}  {100 * GB // 1024}  {380 * GB // 1024}      21% /mnt/wd500
/dev/sdb1      fuseblk    {2000 * GB // 1024} {300 * GB // 1024} {1700 * GB // 1024}      15% /media/ultra/PASSPORT
10.0.0.5:/export/media nfs4 {4000 * GB // 1024} {1000 * GB // 1024} {3000 * GB // 1024}  25% /mnt/nas
/dev/loop0     squashfs       61440   61440         0     100% /snap/core/1
"""


def host(data_device: str = "/dev/sda1", scan: str = "") -> d.RunHost:
    calls: list[str] = []

    def run(script: str) -> tuple[int, str]:
        calls.append(script)
        if "@@lsblk" in script:
            return (
                0,
                f"@@lsblk\n{LSBLK}\n@@df\n{DF}\n@@data\n{data_device}  1 1 1 1% /x\n",
            )
        return 0, scan

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_every_kind_is_listed_with_size_and_free_and_nothing_is_chosen() -> None:
    found = d.discover_backup_targets(host(), data_dir="/mnt/wd500/printops/data")
    kinds = {(c.kind, c.path or c.device) for c in found.candidates}
    assert ("mounted", "/mnt/wd500") in kinds and (
        "mounted",
        "/media/ultra/PASSPORT",
    ) in kinds
    assert ("network", "/mnt/nas") in kinds and ("unmounted", "/dev/sdc1") in kinds
    assert ("mounted", "/") in kinds
    by = {c.path or c.device: c for c in found.candidates}
    assert (
        by["/mnt/wd500"].size_bytes == 500 * GB
        and by["/mnt/wd500"].free_bytes == 380 * GB
    )
    assert by["/media/ultra/PASSPORT"].removable and by["/mnt/nas"].kind == "network"
    assert not any(
        "/run" in c.path or "/snap" in c.path or "/boot" in c.path
        for c in found.candidates
    )
    assert len(found.skipped) == 3 and "/run (tmpfs)" in found.skipped
    assert "4000 GB total, 3000 GB free" in d.describe(
        by["/mnt/nas"]
    )  # sizes come straight from df


def test_the_disk_with_the_spoolman_data_is_never_selectable_and_unmounted_disks_are_listed_only() -> (
    None
):
    found = d.discover_backup_targets(host(), data_dir="/mnt/wd500/printops/data")
    by = {c.path or c.device: c for c in found.candidates}
    assert by["/mnt/wd500"].same_as_data and not by["/mnt/wd500"].selectable
    assert "SAME DISK AS THE SPOOLMAN DATA" in d.describe(by["/mnt/wd500"])
    sdc = by["/dev/sdc1"]
    assert (
        sdc.kind == "unmounted"
        and not sdc.selectable
        and "never mounts, formats or partitions" in sdc.notes[0]
    )
    assert (
        "/dev/sdd1" not in by
    )  # no filesystem: nothing to offer, and certainly nothing to format
    flags = [c.selectable for c in found.candidates]
    assert flags == sorted(
        flags, reverse=True
    )  # everything pickable is listed before anything that is not


def test_the_size_hint_marks_a_likely_disk_but_is_a_parameter_not_a_choice() -> None:
    found = d.discover_backup_targets(host(), data_dir="/")
    by = {c.path or c.device: c for c in found.candidates}
    assert (
        by["/dev/sdc1"].likely and by["/mnt/wd500"].likely
    )  # 512 GB and 500 GB are within the tolerance of the 512 GB hint
    assert not by["/media/ultra/PASSPORT"].likely
    assert "LIKELY" in d.describe(by["/dev/sdc1"])
    small = d.discover_backup_targets(host(), data_dir="/", hint_gb=2000)
    assert {c.path for c in small.candidates if c.likely} == {"/media/ultra/PASSPORT"}
    assert (
        d.is_likely(512 * GB, 512)
        and not d.is_likely(256 * GB, 512)
        and not d.is_likely(0, 512)
    )


def test_existing_backup_dirs_and_markers_are_reported() -> None:
    scan = '@@mount /mnt/wd500\nmarker {"id":"abc"}\ndir /mnt/wd500/old-backups\n@@mount /mnt/nas\ndir /mnt/nas/3d-printops\n'
    found = d.discover_backup_targets(host(scan=scan), data_dir="/")
    by = {c.path: c for c in found.candidates if c.path}
    assert by["/mnt/wd500"].marker_text == '{"id":"abc"}' and by[
        "/mnt/wd500"
    ].existing_dirs == ["/mnt/wd500/old-backups"]
    assert by["/mnt/nas"].existing_dirs == ["/mnt/nas/3d-printops"]
    assert "HAS 3d MARKER" in d.describe(by["/mnt/wd500"])


def test_the_probe_scripts_are_read_only() -> None:
    run = host()
    d.discover_backup_targets(run, data_dir="/mnt/wd500/data with space")
    scripts = run.calls  # type: ignore[attr-defined]
    assert len(scripts) == 2
    forbidden = re.compile(
        r"\b(mount|umount|mkfs\S*|mkdir|rm|mv|cp|tee|dd|wipefs|parted|fdisk|chmod|chown|touch|sudo)\b|>>?\s*[^&/ ]"
    )
    for script in scripts:
        stripped = re.sub(r"2>/dev/null|2>&1|>/dev/null", "", script)
        stripped = re.sub(
            r"'[^']*'|\"[^\"]*\"", "''", stripped
        )  # quoted text (echo labels, find patterns) is data
        assert not forbidden.search(stripped), script
    assert (
        shlex.quote("/mnt/wd500/data with space") in scripts[0]
    )  # the data dir is quoted, not interpolated raw


def test_a_failing_probe_reports_it_and_lists_nothing_selectable() -> None:
    found = d.discover_backup_targets(lambda _s: (127, ""), data_dir="/")
    assert found.candidates == [] and "probe failed" in found.skipped[0]
