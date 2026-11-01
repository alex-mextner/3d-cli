"""backup_policy.py — naming and retention rules of the Spoolman/PrintOps backups (pure, no I/O).

ACCESSED VIA: `printops.backup_store` (write/prune), `3d printops backup ls|prune|run`.

WHAT: a backup SET is one directory `3d-backup-YYYYMMDDTHHMMSSZ` (UTC) holding the compressed DB
snapshots, `MANIFEST.json` and `SHA256SUMS`. Retention is grandfather-father-son: keep the newest
set of each of the last N days, N ISO weeks and N months; everything else is a prune candidate.
The default is 7 daily + 4 weekly + 6 monthly. The real values come from the caller — Home Assistant's
package passes them on every run (integrations/homeassistant/packages/printops_backup.yaml).

INVARIANTS:
  - `SET_RE` is the ONLY thing prune may match: a name that does not fit it is never a candidate,
    whatever it holds. The set's age is read from its NAME, never from mtime (a copied disk keeps
    the same retention).
  - `plan_prune` always keeps the newest set, so a misconfigured policy cannot empty the disk.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Iterable
from dataclasses import dataclass

from errors import UsageError

SET_PREFIX = "3d-backup-"
SET_RE = re.compile(r"^3d-backup-(\d{8}T\d{6}Z)$")
TMP_RE = re.compile(r"^\.3d-backup-tmp-[A-Za-z0-9_]+$")
STAMP_FMT = "%Y%m%dT%H%M%SZ"


@dataclass(frozen=True)
class Retention:
    daily: int = 7
    weekly: int = 4
    monthly: int = 6

    def __post_init__(self) -> None:
        if min(self.daily, self.weekly, self.monthly) < 0:
            raise UsageError("retention counts must be >= 0", command="printops backup")
        if self.daily + self.weekly + self.monthly == 0:
            raise UsageError(
                "retention keeps nothing (daily, weekly and monthly are all 0)",
                command="printops backup",
                remediation=["Keep at least one: --keep daily=7,weekly=4,monthly=6"],
            )

    def describe(self) -> str:
        return f"{self.daily} daily + {self.weekly} weekly + {self.monthly} monthly"

    def to_dict(self) -> dict[str, int]:
        return {"daily": self.daily, "weekly": self.weekly, "monthly": self.monthly}


def parse_keep(raw: str, base: Retention | None = None) -> Retention:
    """`daily=7,weekly=4,monthly=6` (any subset; missing keys keep `base`'s value)."""
    values = (base or Retention()).to_dict()
    for part in filter(None, (p.strip() for p in raw.split(","))):
        key, sep, num = part.partition("=")
        if not sep or key not in values or not num.isdigit():
            raise UsageError(
                f"bad --keep entry {part!r}",
                command="printops backup",
                remediation=[
                    "Use --keep daily=7,weekly=4,monthly=6 (non-negative integers)."
                ],
            )
        values[key] = int(num)
    return Retention(**values)


def stamp_now(now: datetime.datetime | None = None) -> str:
    return (now or datetime.datetime.now(datetime.timezone.utc)).strftime(STAMP_FMT)


def set_name(stamp: str) -> str:
    return SET_PREFIX + stamp


def parse_stamp(name: str) -> datetime.datetime | None:
    """The UTC time encoded in a set name, or None when `name` is not a backup set name."""
    m = SET_RE.fullmatch(name)
    if m is None:
        return None
    try:
        return datetime.datetime.strptime(m.group(1), STAMP_FMT).replace(
            tzinfo=datetime.timezone.utc
        )
    except ValueError:
        return None


def _newest_per(
    names: dict[str, datetime.datetime], bucket: str, count: int
) -> set[str]:
    """Newest set of each of the `count` newest buckets (`day` | `week` | `month`)."""

    def key(t: datetime.datetime) -> tuple[int, ...]:
        if bucket == "day":
            return (t.year, t.month, t.day)
        if bucket == "week":
            iso = t.isocalendar()
            return (iso.year, iso.week)
        return (t.year, t.month)

    seen: dict[tuple[int, ...], str] = {}
    for name in sorted(names, key=lambda n: names[n], reverse=True):
        seen.setdefault(key(names[name]), name)
    newest_first = sorted(seen, reverse=True)[:count]
    return {seen[k] for k in newest_first}


def plan_prune(
    names: Iterable[str], retention: Retention
) -> tuple[list[str], list[str]]:
    """(keep, prune) set names, each newest first. Names that are not set names are ignored."""
    stamped = {n: t for n in names if (t := parse_stamp(n)) is not None}
    keep = (
        _newest_per(stamped, "day", retention.daily)
        | _newest_per(stamped, "week", retention.weekly)
        | _newest_per(stamped, "month", retention.monthly)
    )
    if stamped:
        keep.add(
            max(stamped, key=lambda n: stamped[n])
        )  # never leave the disk without a backup
    order = sorted(stamped, key=lambda n: stamped[n], reverse=True)
    return [n for n in order if n in keep], [n for n in order if n not in keep]
