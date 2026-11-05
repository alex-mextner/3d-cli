"""Backup sets: naming, retention, atomic write, verification, disk guard, restore (printops.backup_policy/backup_store)."""

from __future__ import annotations

import datetime
import gzip
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from errors import UsageError
from printops import backup_store as store
from printops.backup_policy import (
    Retention,
    parse_keep,
    parse_stamp,
    plan_prune,
    set_name,
    stamp_now,
)
from printops.backup_store import BackupBroken, BackupRefused, Source

UTC = datetime.timezone.utc


def make_db(path: Path, rows: int = 5) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("CREATE TABLE spool (id INTEGER PRIMARY KEY, name TEXT)")
    conn.executemany(
        "INSERT INTO spool (name) VALUES (?)", [(f"s{i}",) for i in range(rows)]
    )
    conn.commit()
    conn.close()
    return path


def sources(tmp_path: Path) -> list[Source]:
    tokens = tmp_path / "tokens.json"
    tokens.write_text('{"tokens": []}', encoding="utf-8")
    return [
        Source("spoolman", make_db(tmp_path / "spoolman.db", 7), True),
        Source("printops", make_db(tmp_path / "printops.db", 3), True),
        Source("tokens", tokens, False),
    ]


def disk(tmp_path: Path) -> Path:
    root = tmp_path / "disk"
    root.mkdir()
    store.write_marker(root)
    return root


# ------------------------------------------------------------------------------ policy


def names(days: int, end: datetime.datetime) -> list[str]:
    return [set_name(stamp_now(end - datetime.timedelta(days=i))) for i in range(days)]


def test_default_policy_over_thirty_daily_sets() -> None:
    end = datetime.datetime(2026, 10, 31, 4, 0, tzinfo=UTC)
    keep, drop = plan_prune(names(30, end), Retention())
    # 7 newest days (Oct 25-31) + the newest set of the 2 older ISO weeks that exist inside 4 weeks (Oct 18, Oct 11);
    # the month bucket (October) is already the Oct 31 set, September has no set at all
    assert [t.day for n in keep if (t := parse_stamp(n))] == [
        31,
        30,
        29,
        28,
        27,
        26,
        25,
        18,
        11,
    ]
    assert len(keep) == 9 and len(drop) == 21
    assert set(keep).isdisjoint(drop)


def test_monthly_bucket_keeps_the_newest_set_of_each_month() -> None:
    stamps = [
        datetime.datetime(2026, m, d, 4, tzinfo=UTC)
        for m in (5, 6, 7, 8, 9, 10)
        for d in (3, 17)
    ]
    keep, _ = plan_prune(
        [set_name(stamp_now(s)) for s in stamps],
        Retention(daily=0, weekly=0, monthly=3),
    )
    assert [parse_stamp(n).month for n in keep if parse_stamp(n)] == [10, 9, 8]  # type: ignore[union-attr]


def test_newest_set_is_never_pruned_and_foreign_names_are_not_candidates() -> None:
    keep, drop = plan_prune(
        [
            "notes.txt",
            "3d-backup-latest",
            "3d-backup-20261031T040000Z.old",
            "3d-backup-20261031T040000Z",
        ],
        Retention(daily=1, weekly=0, monthly=0),
    )
    assert keep == ["3d-backup-20261031T040000Z"] and drop == []


def test_retention_validation() -> None:
    assert parse_keep("daily=14").weekly == 4 and parse_keep(
        "monthly=0,daily=1,weekly=0"
    ).describe().startswith("1 daily")
    for bad in ("daily=x", "yearly=3", "daily"):
        with pytest.raises(UsageError):
            parse_keep(bad)
    with pytest.raises(UsageError):
        Retention(0, 0, 0)


# ------------------------------------------------------------------------------ the disk


def test_marker_guard_refuses_missing_wrong_and_absent_disks(tmp_path: Path) -> None:
    root = tmp_path / "mnt"
    with pytest.raises(BackupRefused, match="not there"):
        store.check_disk(root, None)
    root.mkdir()
    with pytest.raises(BackupRefused, match="is not the backup disk"):
        store.check_disk(root, None)
    ident = store.write_marker(root)
    assert store.check_disk(root, ident) == ident
    with pytest.raises(BackupRefused, match="another disk"):
        store.check_disk(root, "0" * 32)
    with pytest.raises(BackupRefused, match="already exists"):
        store.write_marker(root)


def test_space_guard_counts_the_reserve(tmp_path: Path) -> None:
    src = sources(tmp_path)
    assert store.check_space(tmp_path, src, 0) > 0
    with pytest.raises(BackupRefused, match="nothing was written"):
        store.check_space(tmp_path, src, 1 << 60)


# ------------------------------------------------------------------------------ write / verify


def test_write_set_is_verified_published_atomically_and_self_describing(
    tmp_path: Path,
) -> None:
    root = disk(tmp_path)
    info = store.write_set(root, sources(tmp_path), target="t")
    assert SET_NAME(info.name) and sorted(p.name for p in info.path.iterdir()) == [
        "MANIFEST.json",
        "SHA256SUMS",
        "printops.db.gz",
        "spoolman.db.gz",
        "tokens.json",
    ]
    assert not [p for p in root.iterdir() if p.name.startswith(".3d-backup-tmp-")]
    items = info.manifest["items"]  # type: ignore[index]
    assert items["spoolman"]["tables"] == {"spool": 7} and items["printops"][
        "tables"
    ] == {"spool": 3}
    assert store.verify_dir(info.path)[1] == []
    assert (info.path / "spoolman.db.gz").stat().st_mode & 0o077 == 0
    assert info.path.stat().st_mode & 0o077 == 0


def SET_NAME(name: str) -> bool:
    return parse_stamp(name) is not None


def test_verify_catches_tampering_missing_files_and_a_corrupt_database(
    tmp_path: Path,
) -> None:
    root = disk(tmp_path)
    info = store.write_set(root, sources(tmp_path))
    gz = info.path / "spoolman.db.gz"
    original = gz.read_bytes()
    gz.write_bytes(original[:-4] + b"\x00\x00\x00\x00")
    assert any("sha256 differs" in p for p in store.verify_dir(info.path)[1])
    gz.write_bytes(original)
    (info.path / "tokens.json").unlink()
    assert any("tokens" in p for p in store.verify_dir(info.path)[1])


def test_a_corrupt_source_database_aborts_the_run_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    root = disk(tmp_path)
    bad = tmp_path / "broken.db"
    bad.write_bytes(b"this is not a sqlite database" * 100)
    with pytest.raises(BackupBroken):
        store.write_set(root, [Source("spoolman", bad, True)])
    assert list(root.glob("3d-backup-*")) == []
    assert not [p for p in root.iterdir() if p.name.startswith(".3d-backup-tmp-")]


def test_the_snapshot_is_consistent_with_concurrent_writers(tmp_path: Path) -> None:
    src = make_db(tmp_path / "live.db", 50)
    live = sqlite3.connect(src)
    live.execute("BEGIN")
    live.execute(
        "INSERT INTO spool (name) VALUES ('uncommitted')"
    )  # must NOT be in the backup
    root = disk(tmp_path)
    info = store.write_set(root, [Source("spoolman", src, True)])
    live.commit()
    live.close()
    assert info.manifest["items"]["spoolman"]["tables"] == {"spool": 50}  # type: ignore[index]


# ------------------------------------------------------------------------------ prune


def fake_set(dest: Path, when: datetime.datetime, manifest: bool = True) -> Path:
    path = dest / set_name(stamp_now(when))
    path.mkdir(parents=True)
    if manifest:
        (path / store.MANIFEST).write_text('{"items": {}}', encoding="utf-8")
    return path


def test_prune_touches_only_manifest_sets_of_the_pattern(tmp_path: Path) -> None:
    dest = tmp_path / "d"
    dest.mkdir()
    end = datetime.datetime(2026, 10, 31, 4, 0, tzinfo=UTC)
    for i in range(30):
        fake_set(dest, end - datetime.timedelta(days=i))
    foreign = [
        dest / "photos",
        dest / "3d-backup-keepme",
        dest / "notes.txt",
        dest / "3d-backup-20261001T040000Z.bak",
    ]
    foreign[0].mkdir()
    foreign[1].mkdir()
    foreign[2].write_text("mine", encoding="utf-8")
    foreign[3].mkdir()
    no_manifest = fake_set(
        dest, datetime.datetime(2026, 1, 1, 4, tzinfo=UTC), manifest=False
    )
    keep, removed = store.prune(dest, Retention(), dry_run=True)
    assert (
        len(keep) == 9
        and len(removed) == 21
        and len(list(dest.glob("3d-backup-2026*Z"))) == 31
    )  # dry run: nothing gone
    store.prune(dest, Retention())
    assert (
        len([p for p in dest.glob("3d-backup-*Z") if (p / store.MANIFEST).exists()])
        == 9
    )
    assert (
        all(p.exists() for p in foreign) and no_manifest.exists()
    )  # nothing that is not a manifest set was touched


def test_prune_remembers_the_policy_so_a_hand_run_never_prunes_harder(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "d"
    dest.mkdir()
    assert store.read_state(dest) is None
    store.write_state(dest, Retention(14, 8, 12))
    assert store.read_state(dest) == Retention(14, 8, 12)


# ------------------------------------------------------------------------------ restore


def test_restore_flat_and_data_layouts_reproduce_the_databases(tmp_path: Path) -> None:
    root = disk(tmp_path)
    info = store.write_set(root, sources(tmp_path))
    flat = tmp_path / "flat"
    store.restore_set(info, flat)
    assert sqlite3.connect(flat / "spoolman.db").execute(
        "SELECT count(*) FROM spool"
    ).fetchone() == (7,)
    data = tmp_path / "data"
    (data / "spoolman").mkdir(parents=True)
    (data / "spoolman" / "spoolman.db").write_text("old", encoding="utf-8")
    (data / "spoolman" / "spoolman.db-wal").write_text("stale wal", encoding="utf-8")
    store.restore_set(info, data, layout="data")
    assert (data / "spoolman" / "spoolman.db.pre-restore").read_text(
        encoding="utf-8"
    ) == "old"
    assert not (data / "spoolman" / "spoolman.db-wal").exists()
    assert (data / "spoolman" / "spoolman.db.pre-restore-wal").read_text(
        encoding="utf-8"
    ) == "stale wal"
    assert sqlite3.connect(data / "api" / "printops.db").execute(
        "SELECT count(*) FROM spool"
    ).fetchone() == (3,)
    assert (data / "api" / "tokens.json").read_text(
        encoding="utf-8"
    ) == '{"tokens": []}'
    assert any("integrity ok" in line for line in store.restore_check(info))


def test_restore_refuses_a_set_that_fails_verification(tmp_path: Path) -> None:
    info = store.write_set(disk(tmp_path), sources(tmp_path))
    with gzip.open(info.path / "printops.db.gz", "wb") as fh:
        fh.write(b"garbage")
    with pytest.raises(BackupBroken, match="not restorable"):
        store.restore_set(info, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def _rewrite_manifest(info: store.SetInfo, edit: object) -> None:
    path = info.path / store.MANIFEST
    manifest = json.loads(path.read_text(encoding="utf-8"))
    edit(manifest)  # type: ignore[operator]
    path.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize("kind", ["parent", "absolute", "nested", "item-name"])
def test_a_manifest_cannot_name_a_path_outside_its_set(
    tmp_path: Path, kind: str
) -> None:
    """A hostile set: the manifest points at a file outside the set dir whose checksum matches. verify and
    restore must refuse it, and nothing may be written to the escaped destination."""
    root = disk(tmp_path)
    info = store.write_set(root, sources(tmp_path))
    victim = tmp_path / "victim" / ".ssh"
    victim.mkdir(parents=True)
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"ssh-ed25519 attacker\n")
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    ref = {
        "parent": "../../payload.bin",
        "absolute": str(payload),
        "nested": "sub/payload.bin",
        "item-name": "tokens.json",
    }[kind]

    def edit(m: dict[str, object]) -> None:
        items = m["items"]
        assert isinstance(items, dict)
        item = items["tokens"]
        item.update(file=ref, sha256=digest)
        if kind == "item-name":
            items["../../victim/.ssh/authorized_keys"] = items.pop("tokens")

    _rewrite_manifest(info, edit)
    info = store.SetInfo(
        info.name,
        info.path,
        info.created,
        json.loads((info.path / store.MANIFEST).read_text()),
    )
    assert any(
        "outside the set" in p or "not a regular file" in p
        for p in store.verify_dir(info.path)[1]
    )
    into = tmp_path / "victim" / "restore"
    with pytest.raises(BackupBroken, match="not restorable"):
        store.restore_set(info, into)
    assert list(victim.iterdir()) == [] and not into.exists()


def test_a_set_file_that_is_a_link_out_of_the_set_is_not_restorable(
    tmp_path: Path,
) -> None:
    info = store.write_set(disk(tmp_path), sources(tmp_path))
    secret = tmp_path / "secret.json"
    secret.write_text(
        '{"tokens": []}', encoding="utf-8"
    )  # same bytes: the checksum still matches
    (info.path / "tokens.json").unlink()
    (info.path / "tokens.json").symlink_to(secret)
    assert any(
        "not a regular file inside the set" in p for p in store.verify_dir(info.path)[1]
    )
    with pytest.raises(BackupBroken, match="not restorable"):
        store.restore_set(info, tmp_path / "out")


def test_a_non_mapping_manifest_item_is_a_problem_not_a_crash(tmp_path: Path) -> None:
    info = store.write_set(disk(tmp_path), sources(tmp_path))
    _rewrite_manifest(info, lambda m: m["items"].update(tokens="oops"))  # type: ignore[attr-defined]
    assert any("outside the set" in p for p in store.verify_dir(info.path)[1])


@pytest.mark.parametrize("layout", ["flat", "data"])
def test_restoring_a_set_taken_without_a_tokens_file_removes_the_stale_one(
    tmp_path: Path, layout: str
) -> None:
    """No tokens file is deny-all. Restoring that state over a live data dir must not keep an authorised token."""
    srcs = [s for s in sources(tmp_path) if s.name != "tokens"]
    info = store.write_set(disk(tmp_path), srcs, absent={"tokens": "tokens.json"})
    assert info.manifest["absent"] == {"tokens": "tokens.json"}  # type: ignore[index]
    assert store.verify_dir(info.path)[1] == []
    into = tmp_path / "live"
    stale = into / ("api" if layout == "data" else ".") / "tokens.json"
    stale.parent.mkdir(parents=True)
    stale.write_text('{"tokens": [{"name": "old"}]}', encoding="utf-8")
    written = store.restore_set(info, into, layout=layout)
    assert not stale.exists() and stale.with_name("tokens.json.pre-restore").is_file()
    assert all(p.name != "tokens.json" for p in written.written)


def test_a_set_that_had_tokens_does_not_record_them_absent(tmp_path: Path) -> None:
    info = store.write_set(disk(tmp_path), sources(tmp_path))
    assert info.manifest["absent"] == {}  # type: ignore[index]


def test_a_manifest_absent_entry_cannot_name_a_path_outside_the_restore_dir(
    tmp_path: Path,
) -> None:
    info = store.write_set(disk(tmp_path), sources(tmp_path))
    _rewrite_manifest(info, lambda m: m.update(absent={"tokens": "../../etc/passwd"}))
    assert any("absent" in p for p in store.verify_dir(info.path)[1])
    with pytest.raises(BackupBroken, match="not restorable"):
        store.restore_set(info, tmp_path / "out")


def test_restore_uses_the_manifest_it_verified_not_the_one_listed_earlier(
    tmp_path: Path,
) -> None:
    """The listing parsed a good manifest; the disk now serves a hostile one (a NAS can). Restore must
    parse and verify what is on disk at restore time, once, and write only names that passed."""
    info = store.write_set(disk(tmp_path), sources(tmp_path))
    victim = tmp_path / "victim"
    victim.mkdir()
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"attacker\n")
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()

    def edit(m: dict[str, object]) -> None:
        items = m["items"]
        assert isinstance(items, dict)
        items["tokens"].update(file="../../payload.bin", sha256=digest)
        m["absent"] = {"../../victim/x": "../../victim/x"}

    _rewrite_manifest(info, edit)  # `info.manifest` still holds the good, earlier copy
    assert info.manifest is not None and "../../payload.bin" not in json.dumps(
        info.manifest
    )
    into = tmp_path / "victim" / "restore"
    with pytest.raises(BackupBroken, match="not restorable"):
        store.restore_set(info, into)
    with pytest.raises(BackupBroken, match="not restorable"):
        store.restore_check(info)
    assert list(victim.iterdir()) == [] and not into.exists()


@pytest.mark.parametrize("hostile", ["../x", "a/../x", ".."])
def test_hostile_absent_and_item_names_are_refused(
    tmp_path: Path, hostile: str
) -> None:
    info = store.write_set(disk(tmp_path), sources(tmp_path))
    _rewrite_manifest(info, lambda m: m.update(absent={hostile: "tokens.json"}))
    with pytest.raises(BackupBroken, match="not restorable"):
        store.restore_set(info, tmp_path / "out")
    _rewrite_manifest(
        info,
        lambda m: (
            m.update(absent={}) or m["items"].update({hostile: m["items"]["tokens"]})
        ),
    )  # type: ignore[attr-defined]
    with pytest.raises(BackupBroken, match="not restorable"):
        store.restore_set(info, tmp_path / "out")


def test_a_second_restore_never_overwrites_the_first_safety_copy(
    tmp_path: Path,
) -> None:
    info = store.write_set(disk(tmp_path), sources(tmp_path))
    live = tmp_path / "live"
    live.mkdir()
    (live / "spoolman.db").write_text("the real one", encoding="utf-8")
    store.restore_set(info, live)
    store.restore_set(
        info, live
    )  # the first restore's result is now "live" and gets moved aside too
    assert (live / "spoolman.db.pre-restore").read_text(
        encoding="utf-8"
    ) == "the real one"
    assert (live / "spoolman.db.pre-restore.1").read_bytes()[:6] == b"SQLite"


def test_restore_unpacks_before_it_moves_the_live_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full disk while unpacking must leave the live DB where it is."""
    info = store.write_set(disk(tmp_path), sources(tmp_path))
    live = tmp_path / "live"
    live.mkdir()
    (live / "spoolman.db").write_text("keep me", encoding="utf-8")
    (live / "spoolman.db-wal").write_text("wal", encoding="utf-8")

    def full_disk(src: Path, dst: Path) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store, "_gunzip", full_disk)
    with pytest.raises(OSError):
        store.restore_set(info, live)
    assert (live / "spoolman.db").read_text(encoding="utf-8") == "keep me"
    assert (live / "spoolman.db-wal").read_text(encoding="utf-8") == "wal"
    assert not list(live.glob("*.pre-restore*")) and not list(live.glob(".*.restoring"))
