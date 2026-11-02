"""backup_discovery.py — find places a Spoolman/PrintOps backup could go. READ-ONLY, never selects.

ACCESSED VIA: `printops.backup_setup` (`3d init --env`), `3d printops backup discover`.

WHAT: runs ONE read-only shell probe on the host that runs Spoolman (`run` = local bash or ssh — the
install target the user chose) and turns `lsblk` / `df` / `findmnt` / `ls` output into `Candidate`s:
  mounted      a mounted filesystem with a real fstype (ext4, xfs, btrfs, ntfs, exfat, vfat, ...), with its
               size and free space; removable/USB flagged from lsblk (RM / HOTPLUG / TRAN=usb)
  network      nfs / cifs / smb3 / sshfs mounts
  unmounted    a block device with a filesystem that nobody mounted — LISTED ONLY, never selectable:
               3d does not mount, format or partition anything (the hint says how YOU would mount it)
  ha           Home Assistant's own configured backup locations (agents + network mounts), when HA is known
Each candidate also carries: existing backup directories found two levels below the mount point, an
existing `.3d-backup-disk` marker id, `likely` (the whole disk's size is within ±8 % of the size hint,
default 512 GB — a hint for sorting and for the label, never an automatic choice), and `same_as_data`
(the Spoolman data lives on that very filesystem, so it is NOT a backup).

INVARIANTS: the probe only runs lsblk, df, findmnt, ls, test and stat (no mount, no write, no mkdir);
the pseudo/system mounts are counted in `skipped`, not hidden silently; nothing is chosen here.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

RunHost = Callable[
    [str], tuple[int, str]
]  # shell script on the Spoolman host -> (exit code, stdout)


class Shell(Protocol):
    """A RunHost that can also feed the script's stdin: a secret goes there, never into argv (`ps`, ssh logs)."""

    def __call__(self, script: str, stdin: str | None = None) -> tuple[int, str]: ...

DEFAULT_HINT_GB = 512
HINT_TOLERANCE = 0.08
NETWORK_FS = frozenset(
    {"nfs", "nfs4", "cifs", "smb3", "smbfs", "fuse.sshfs", "sshfs", "9p"}
)
DISK_FS = frozenset(
    {
        "ext2",
        "ext3",
        "ext4",
        "xfs",
        "btrfs",
        "f2fs",
        "zfs",
        "ntfs",
        "ntfs3",
        "fuseblk",
        "exfat",
        "vfat",
        "hfsplus",
        "apfs",
        "jfs",
        "reiserfs",
    }
)
SYSTEM_MOUNT_PREFIXES = (
    "/boot",
    "/efi",
    "/snap",
    "/var/lib/docker",
    "/var/lib/containerd",
    "/run",
    "/sys",
    "/proc",
    "/dev",
    "/var/snap",
    "/usr/lib/modules",
)
SEP = "@@"
PROBE = """export LC_ALL=C
echo '@@lsblk'; lsblk -J -b -o NAME,PATH,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINTS,RM,HOTPLUG,TRAN,TYPE,MODEL,PKNAME 2>/dev/null \
|| lsblk -J -b -o NAME,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINT,RM,TYPE,MODEL 2>/dev/null
echo '@@df'; df -PkT 2>/dev/null
echo '@@data'; df -Pk {data} 2>/dev/null | tail -n 1
"""
SCAN = """export LC_ALL=C
for m in {mounts}; do
  echo "@@mount $m"
  if [ -f "$m/.3d-backup-disk" ]; then echo "marker $(tr -d '\\n ' < "$m/.3d-backup-disk" | head -c 400)"; fi
  timeout 6 find "$m" -maxdepth 2 -type d \\( -iname '*backup*' -o -iname '3d-printops' \\) \
-not -path '*/.*' -print 2>/dev/null | head -n 6 | sed 's/^/dir /'
done
"""


@dataclass
class Candidate:
    kind: str  # mounted | network | unmounted | ha
    path: str  # mount point ('' for unmounted / ha-only entries)
    device: str = ""
    fstype: str = ""
    size_bytes: int = 0
    free_bytes: int = 0
    label: str = ""
    uuid: str = ""
    model: str = ""
    removable: bool = False
    likely: bool = False
    same_as_data: bool = False
    marker_text: str = ""
    existing_dirs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def selectable(self) -> bool:
        return (
            self.kind in ("mounted", "network")
            and not self.same_as_data
            and bool(self.path)
        )


@dataclass
class Discovery:
    candidates: list[Candidate]
    skipped: list[str]
    data_device: str = ""


def gb(n: int) -> str:
    return f"{n / 1e9:.0f} GB" if n >= 1e10 else f"{n / 1e9:.1f} GB"


# ---------------------------------------------------------------------------------- parsing


def _flatten(
    nodes: Iterable[dict[str, Any]], parent: dict[str, Any] | None = None
) -> Iterable[dict[str, Any]]:
    for node in nodes:
        node["_parent"] = parent
        yield node
        yield from _flatten(node.get("children") or [], node)


def parse_lsblk(text: str) -> list[dict[str, Any]]:
    try:
        tree = json.loads(text).get("blockdevices") or []
    except ValueError:
        return []
    return list(_flatten(tree))


def _mountpoints(node: dict[str, Any]) -> list[str]:
    raw = node.get("mountpoints") if "mountpoints" in node else [node.get("mountpoint")]
    return [m for m in raw or [] if m]


def parse_df(text: str) -> list[dict[str, Any]]:
    rows = []
    for line in text.splitlines()[1:]:
        parts = line.split(None, 6)
        if len(parts) == 7 and parts[2].isdigit():
            rows.append(
                {
                    "device": parts[0],
                    "fstype": parts[1],
                    "size": int(parts[2]) * 1024,
                    "free": int(parts[4]) * 1024,
                    "mount": parts[6],
                }
            )
    return rows


def _section(text: str, name: str) -> str:
    out: list[str] = []
    active = False
    for line in text.splitlines():
        if line.startswith(SEP) and not line.startswith(SEP + "mount"):
            active = line[len(SEP) :] == name
        elif active:
            out.append(line)
    return "\n".join(out)


def _top(node: dict[str, Any]) -> dict[str, Any]:
    """The whole disk a (partition) node sits on: lsblk puts RM/HOTPLUG/TRAN/MODEL there, not on the partition."""
    while node.get("_parent") is not None:
        node = node["_parent"]
    return node


def _removable(node: dict[str, Any]) -> bool:
    top = _top(node)
    return bool(
        top.get("rm")
        or top.get("hotplug")
        or top.get("tran") == "usb"
        or node.get("rm")
    )


def _whole_disk_size(node: dict[str, Any]) -> int:
    return int(_top(node).get("size") or 0)


def same_physical_disk(a: str, b: str, blocks: dict[str, dict[str, Any]]) -> bool:
    """Two filesystem devices (`/dev/sda1`, `/dev/sda2`, an LV on `/dev/sda3`) share one physical disk.
    A copy on another PARTITION of the data's disk dies with it, so it is not a separate backup."""
    if not a or not b:
        return False
    if a == b:
        return True
    na, nb = blocks.get(a), blocks.get(b)
    if na is None or nb is None:
        return False
    return _disk_path(na) == _disk_path(nb)


def _disk_path(node: dict[str, Any]) -> str:
    top = _top(node)
    return str(top.get("path") or f"/dev/{top.get('name')}")


def is_likely(size_bytes: int, hint_gb: int) -> bool:
    """Within ±8 % of the size hint (decimal GB: a '512 GB' SSD is 512e9 bytes, a '500 GB' HDD 500e9)."""
    want = hint_gb * 1e9
    return size_bytes > 0 and abs(size_bytes - want) <= want * HINT_TOLERANCE


def _is_system_mount(mount: str) -> bool:
    return any(mount == p or mount.startswith(p + "/") for p in SYSTEM_MOUNT_PREFIXES)


# ------------------------------------------------------------------------------ candidates


def _candidate(
    row: dict[str, Any],
    blocks: dict[str, dict[str, Any]],
    data_device: str,
    hint_gb: int,
) -> Candidate:
    network = row["fstype"] in NETWORK_FS
    node = blocks.get(row["device"], {})
    whole = _whole_disk_size(node) if node else row["size"]
    cand = Candidate(
        "network" if network else "mounted",
        row["mount"],
        row["device"],
        row["fstype"],
        row["size"],
        row["free"],
        label=str(node.get("label") or ""),
        uuid=str(node.get("uuid") or ""),
        model=str(_top(node).get("model") or "").strip() if node else "",
        likely=is_likely(row["size"] if network else whole, hint_gb),
        removable=_removable(node) if node else False,
    )
    cand.same_as_data = same_physical_disk(row["device"], data_device, blocks)
    return cand


def _from_df(
    rows: list[dict[str, Any]],
    blocks: dict[str, dict[str, Any]],
    data_device: str,
    hint_gb: int,
    skipped: list[str],
) -> list[Candidate]:
    out: list[Candidate] = []
    for row in rows:
        usable = row["fstype"] in NETWORK_FS or row["fstype"] in DISK_FS
        if not usable or _is_system_mount(row["mount"]):
            skipped.append(f"{row['mount']} ({row['fstype']})")
            continue
        out.append(_candidate(row, blocks, data_device, hint_gb))
    return out


def _unmounted(nodes: list[dict[str, Any]], hint_gb: int) -> list[Candidate]:
    out = []
    for n in nodes:
        if (
            n.get("type") not in ("part", "disk")
            or _mountpoints(n)
            or n.get("children")
        ):
            continue
        fs = str(n.get("fstype") or "")
        if fs in ("", "swap", "LVM2_member", "crypto_LUKS", "linux_raid_member"):
            continue
        cand = Candidate(
            "unmounted",
            "",
            str(n.get("path") or f"/dev/{n.get('name')}"),
            fs,
            int(n.get("size") or 0),
            label=str(n.get("label") or ""),
            uuid=str(n.get("uuid") or ""),
            model=str(_top(n).get("model") or "").strip(),
            removable=_removable(n),
            likely=is_likely(_whole_disk_size(n), hint_gb),
        )
        cand.notes.append(
            "not mounted: mount it yourself (3d never mounts, formats or partitions a disk), "
            f"e.g. `sudo mount {cand.device} /mnt/backup`, then run the setup again"
        )
        out.append(cand)
    return out


def _apply_scan(cands: list[Candidate], text: str) -> None:
    by_mount = {c.path: c for c in cands}
    current: Candidate | None = None
    for line in text.splitlines():
        if line.startswith(SEP + "mount "):
            current = by_mount.get(line[len(SEP + "mount ") :])
        elif current is not None and line.startswith("marker "):
            current.marker_text = line[len("marker ") :]
        elif current is not None and line.startswith("dir "):
            current.existing_dirs.append(line[len("dir ") :])


def _rank(c: Candidate) -> tuple[int, int, int]:
    return (0 if c.selectable else 1, 0 if c.likely else 1, -c.free_bytes)


def discover_backup_targets(
    run: RunHost,
    *,
    data_dir: str = "/",
    hint_gb: int = DEFAULT_HINT_GB,
    extra: Iterable[Candidate] = (),
) -> Discovery:
    """Probe the host (2 read-only shell calls) and return every candidate; chooses nothing."""
    rc, text = run(PROBE.format(data=shlex.quote(data_dir)))
    if rc != 0 or not text.strip():
        return Discovery([*extra], [f"probe failed (exit {rc})"])
    nodes = parse_lsblk(_section(text, "lsblk"))
    blocks = {str(n.get("path") or f"/dev/{n.get('name')}"): n for n in nodes}
    data_row = _section(text, "data").split()
    data_device = data_row[0] if data_row else ""
    skipped: list[str] = []
    cands = _from_df(
        parse_df(_section(text, "df")), blocks, data_device, hint_gb, skipped
    )
    if cands:
        rc2, scan = run(
            SCAN.format(mounts=" ".join(shlex.quote(c.path) for c in cands))
        )
        if rc2 == 0:
            _apply_scan(cands, scan)
    cands += _unmounted(nodes, hint_gb)
    cands += list(extra)
    return Discovery(sorted(cands, key=_rank), skipped, data_device)


# ------------------------------------------------------------------------------- rendering


def _flags(c: Candidate) -> list[str]:
    return [
        f
        for f, on in (
            ("removable/USB", c.removable),
            ("network", c.kind == "network"),
            ("LIKELY: size ~ the backup disk hint", c.likely),
            ("HAS 3d MARKER", bool(c.marker_text)),
            ("SAME DISK AS THE SPOOLMAN DATA — not a backup", c.same_as_data),
        )
        if on
    ]


def _size_text(c: Candidate) -> str:
    if not c.size_bytes:
        return ""
    free = f", {gb(c.free_bytes)} free" if c.kind != "unmounted" else ""
    return f"{gb(c.size_bytes)} total{free}"


def describe(c: Candidate) -> str:
    names = " ".join(x for x in (c.label, c.model) if x)
    bits = [c.path or c.device or "(HA)", c.fstype, _size_text(c), names]
    line = "  ".join(b for b in bits if b)
    flags = _flags(c)
    if flags:
        line += "  [" + "; ".join(flags) + "]"
    if c.existing_dirs:
        line += "  existing: " + ", ".join(c.existing_dirs[:3])
    return line
