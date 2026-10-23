"""Read-only backups, the manifest and the freshness gate `fw apply` passes through."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from printer_fw.backup import (
    MANIFEST,
    Manifest,
    find_fresh_backup,
    list_manifests,
    take_backup,
)
from printer_fw.profiles import load_profiles
from printer_fw_fakes import (
    PRINTER_CFG,
    FakeHttp,
    FakeRunner,
    ep_of,
    k1_http,
    k1_runner,
    make_backends,
    target,
    tgz,
)

PROFILES = load_profiles()
K1, U1 = PROFILES["creality-k1"], PROFILES["snapmaker-u1"]
NOW = 1_800_000_000.0
HOUR = 3600.0


def k1_backup(
    tmp_path: Path,
    runner: FakeRunner | None = None,
    *,
    now: float = NOW,
    state: str | None = "rooted",
    printer: str = "alpha",
) -> Manifest:
    tgt = target(printer, "Creality K1 SE", adapter="creality-stock", port=80)
    be, *_ = make_backends(runner or k1_runner(), k1_http())
    return take_backup(
        K1,
        ep_of(K1, tgt),
        be,
        printer=printer,
        model=tgt.model,
        state=state,
        dest_root=tmp_path,
        now=now,
    )


def test_a_backup_writes_files_hashes_and_a_manifest(tmp_path: Path) -> None:
    m = k1_backup(tmp_path)
    assert (
        m.ok
        and m.directory.parent == tmp_path
        and m.directory.name.startswith("alpha-creality-k1-")
    )
    doc = json.loads((m.directory / MANIFEST).read_text())
    assert (
        doc["printer"] == "alpha"
        and doc["profile"] == "creality-k1"
        and doc["model"] == "Creality K1 SE"
    )
    assert (
        doc["state"] == "rooted"
        and doc["created_epoch"] == NOW
        and doc["created_at"].startswith("2027-")
    )
    by_id = {i["id"]: i for i in doc["items"]}
    assert by_id["info-json"]["status"] == "ok" and by_id["info-json"]["size"] > 0
    assert (
        by_id["printer-cfg"]["sha256"]
        and (m.directory / by_id["printer-cfg"]["file"]).read_text() == PRINTER_CFG
    )
    assert by_id["fluidd-release-info"]["status"] == "missing"  # optional 404
    assert by_id["userdata-tree"]["status"] == "missing"  # optional, no such path
    assert (
        by_id["ws-baseline"]["status"] == "manual"
        and "section 3.1" in by_id["ws-baseline"]["detail"]
    )
    assert (
        "ws-baseline" not in m.ok_ids()
        and {"info-json", "printer-cfg", "config-tree"} <= m.ok_ids()
    )


def test_backup_files_are_private(tmp_path: Path) -> None:
    m = k1_backup(tmp_path)
    assert stat.S_IMODE(m.directory.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(f.stat().st_mode) == 0o600 for f in m.directory.iterdir())
    sums = (m.directory / "SHA256SUMS").read_text().splitlines()
    assert len(sums) == len(m.ok_ids())


def test_backup_only_pulls_over_ssh_with_cat_and_tar(tmp_path: Path) -> None:
    runner = k1_runner()
    k1_backup(tmp_path, runner)
    assert runner.mutating() == []
    assert {c.command[0] for c in runner.calls if isinstance(c.command, tuple)} == {
        "cat",
        "tar",
    }


def test_ssh_items_are_skipped_when_the_printer_state_does_not_have_ssh(
    tmp_path: Path,
) -> None:
    runner = k1_runner()
    m = k1_backup(tmp_path, runner, state="stock")
    status = {i.id: i.status for i in m.items}
    assert (
        status["printer-cfg"] == status["config-tree"] == "skipped"
        and status["info-json"] == "ok"
    )
    assert runner.calls == [] and m.ok


def test_two_backups_in_the_same_second_get_separate_directories(
    tmp_path: Path,
) -> None:
    assert k1_backup(tmp_path).directory != k1_backup(tmp_path).directory


@pytest.mark.parametrize(
    ("runner", "item", "fragment"),
    [
        (
            k1_runner(cfg="[printer]\n"),
            "printer-cfg",
            "does not match",
        ),  # no SAVE_CONFIG block
        (k1_runner(tree=b"not a tarball"), "config-tree", "archive is broken"),
        (
            k1_runner(tree=tgz({"printer_data/config/printer.cfg": b"x"})),
            "config-tree",
            "lacks printer_data/config/gcode_macro.cfg",
        ),
        (FakeRunner(), "printer-cfg", "cat exited 1"),
    ],
)
def test_a_failed_item_makes_the_backup_not_ok(
    tmp_path: Path, runner: FakeRunner, item: str, fragment: str
) -> None:
    m = k1_backup(tmp_path, runner)
    failed = {i.id: i for i in m.items if i.status == "failed"}
    assert not m.ok and item in failed and fragment in failed[item].detail


def test_an_http_error_fails_a_required_http_item(tmp_path: Path) -> None:
    http = FakeHttp()
    http.set("GET", 7125, "/machine/system_info", 500, "")
    tgt = target("beta", "Snapmaker U1")
    be, *_ = make_backends(http=http)
    m = take_backup(
        U1,
        ep_of(U1, tgt),
        be,
        printer="beta",
        model=tgt.model,
        state="stock",
        dest_root=tmp_path,
        now=NOW,
    )
    bad = {i.id: i for i in m.items if i.status == "failed"}
    assert "system-info" in bad and "HTTP 500" in bad["system-info"].detail and not m.ok
    assert any(i.id == "full-config-tree" and i.status == "manual" for i in m.items)


def test_no_ssh_fails_ssh_items_instead_of_silently_skipping(tmp_path: Path) -> None:
    tgt = target("alpha", "Creality K1 SE", adapter="creality-stock", port=80)
    be, runner, *_ = make_backends(k1_runner(), k1_http(), use_ssh=False)
    m = take_backup(
        K1,
        ep_of(K1, tgt),
        be,
        printer="alpha",
        model=tgt.model,
        state="rooted",
        dest_root=tmp_path,
        now=NOW,
    )
    assert (
        not m.ok
        and runner.calls == []
        and "ssh disabled" in next(i for i in m.items if i.id == "printer-cfg").detail
    )


# ---- freshness gate --------------------------------------------------------------------------------


def gate(
    root: Path,
    *,
    printer: str = "alpha",
    now: float = NOW,
    max_age_h: float = 24,
    required: set[str] | None = None,
    profile: str = "creality-k1",
) -> tuple[Manifest | None, str]:
    return find_fresh_backup(
        root,
        printer,
        profile,
        max_age_s=max_age_h * HOUR,
        required=required or set(),
        now=now,
    )


def test_a_fresh_backup_passes_the_gate(tmp_path: Path) -> None:
    k1_backup(tmp_path)
    found, why = gate(
        tmp_path, now=NOW + 2 * HOUR, required={"printer-cfg", "config-tree"}
    )
    assert found is not None and why == ""


def test_no_backup_is_refused(tmp_path: Path) -> None:
    found, why = gate(tmp_path)
    assert found is None and "no backup of alpha" in why


def test_a_stale_backup_is_refused_and_the_age_limit_is_configurable(
    tmp_path: Path,
) -> None:
    k1_backup(tmp_path)
    found, why = gate(tmp_path, now=NOW + 25 * HOUR)
    assert found is None and "25.0 h old; the limit is 24 h" in why
    assert gate(tmp_path, now=NOW + 25 * HOUR, max_age_h=48)[0] is not None
    assert gate(tmp_path, now=NOW + 2 * HOUR, max_age_h=1)[0] is None


def test_a_backup_dated_in_the_future_is_refused(tmp_path: Path) -> None:
    k1_backup(tmp_path, now=NOW + 5 * HOUR)
    assert "future" in gate(tmp_path)[1]


def test_a_tampered_or_deleted_backup_file_is_refused(tmp_path: Path) -> None:
    m = k1_backup(tmp_path)
    cfg = next(m.directory / i.file for i in m.items if i.id == "printer-cfg")
    cfg.write_text("tampered")
    assert "no longer matches its recorded SHA-256" in gate(tmp_path)[1]
    cfg.unlink()
    assert "is missing" in gate(tmp_path)[1]


def test_a_newer_failed_backup_is_not_masked_by_an_older_good_one(
    tmp_path: Path,
) -> None:
    k1_backup(tmp_path, now=NOW)
    k1_backup(tmp_path, FakeRunner(), now=NOW + HOUR)
    found, why = gate(tmp_path, now=NOW + 2 * HOUR)
    assert found is None and "has failed items" in why


def test_the_backup_must_hold_every_required_item(tmp_path: Path) -> None:
    k1_backup(tmp_path, state="stock")  # no ssh items could be taken
    found, why = gate(tmp_path, required={"printer-cfg"})
    assert found is None and "lacks required items: printer-cfg" in why
    assert gate(tmp_path, required={"info-json"})[0] is not None


def test_a_backup_of_another_printer_or_profile_does_not_count(tmp_path: Path) -> None:
    k1_backup(tmp_path, printer="other")
    assert gate(tmp_path)[0] is None
    k1_backup(tmp_path)
    assert gate(tmp_path, profile="snapmaker-u1")[0] is None
    assert [m.printer for m in list_manifests(tmp_path, "other")] == ["other"]


def test_an_unreadable_manifest_is_ignored(tmp_path: Path) -> None:
    bad = tmp_path / "broken"
    bad.mkdir()
    (bad / MANIFEST).write_text("{not json")
    assert list_manifests(tmp_path, "alpha") == []


# ---- optional items, content checks, concurrency ------------------------------------------------


def test_an_optional_http_item_whose_port_refuses_is_skipped_not_failed(
    tmp_path: Path,
) -> None:
    """Nothing listens on the Fluidd port: the optional release_info item is skipped and the backup stays usable."""
    http = FakeHttp()
    http.set(
        "GET", 80, "/info", 200, {"model": "K1 SE"}
    )  # no route at all on 4408: refused
    tgt = target("alpha", "Creality K1 SE", adapter="creality-stock", port=80)
    be, *_ = make_backends(k1_runner(), http)
    m = take_backup(
        K1,
        ep_of(K1, tgt),
        be,
        printer="alpha",
        model=tgt.model,
        state="rooted",
        dest_root=tmp_path,
        now=NOW,
    )
    item = next(i for i in m.items if i.id == "fluidd-release-info")
    assert item.status == "skipped" and item.detail == "optional: connection refused"
    assert m.ok
    found, why = gate(tmp_path, now=NOW + HOUR, required={"printer-cfg", "config-tree"})
    assert found is not None and why == ""


def test_a_required_http_item_whose_port_refuses_still_fails_the_backup(
    tmp_path: Path,
) -> None:
    tgt = target("beta", "Snapmaker U1")
    be, *_ = make_backends(http=FakeHttp())  # Moonraker unreachable
    m = take_backup(
        U1,
        ep_of(U1, tgt),
        be,
        printer="beta",
        model=tgt.model,
        state="stock",
        dest_root=tmp_path,
        now=NOW,
    )
    failed = {i.id: i.detail for i in m.items if i.status == "failed"}
    assert failed["system-info"] == "connection refused" and not m.ok


def test_an_optional_item_with_another_http_error_is_a_failure(tmp_path: Path) -> None:
    http = k1_http()
    http.set("GET", 4408, "/release_info.json", 502, "")
    m = k1_backup_with(tmp_path, http)
    item = next(i for i in m.items if i.id == "fluidd-release-info")
    assert item.status == "failed" and item.detail == "HTTP 502" and not m.ok


def k1_backup_with(tmp_path: Path, http: FakeHttp) -> Manifest:
    tgt = target("alpha", "Creality K1 SE", adapter="creality-stock", port=80)
    be, *_ = make_backends(k1_runner(), http)
    return take_backup(
        K1,
        ep_of(K1, tgt),
        be,
        printer="alpha",
        model=tgt.model,
        state="rooted",
        dest_root=tmp_path,
        now=NOW,
    )


def test_expect_regex_is_enforced_for_http_items_too(tmp_path: Path) -> None:
    from dataclasses import replace

    from printer_fw.backup import _fetch_http

    item = next(b for b in U1.backups if b.id == "printer-cfg")
    strict = replace(item, expect_regex=r"(?m)^#\*# \[")
    http = FakeHttp()
    http.set("GET", 7125, "/server/files/config/printer.cfg", 200, "[printer]\n")
    tgt = target("beta", "Snapmaker U1")
    be, *_ = make_backends(http=http)
    res, data = _fetch_http(strict, ep_of(U1, tgt), be)
    assert res.status == "failed" and "does not match" in res.detail and data == b""
    http.set("GET", 7125, "/server/files/config/printer.cfg", 200, PRINTER_CFG)
    assert _fetch_http(strict, ep_of(U1, tgt), be)[0].status == "ok"


def test_two_backups_in_the_same_second_never_collide_even_when_the_name_is_taken(
    tmp_path: Path,
) -> None:
    first = k1_backup(tmp_path)
    (
        first.directory.parent / f"{first.directory.name}-2"
    ).mkdir()  # a stray directory took the next name
    second = k1_backup(tmp_path)
    third = k1_backup(tmp_path)
    names = {first.directory.name, second.directory.name, third.directory.name}
    assert (
        len(names) == 3
        and second.directory.name.endswith("-3")
        and third.directory.name.endswith("-4")
    )


def test_backup_files_are_created_exclusively_and_privately(tmp_path: Path) -> None:
    from printer_fw.backup import _write_private

    target_file = tmp_path / "x"
    _write_private(target_file, b"secret")
    assert (
        stat.S_IMODE(target_file.stat().st_mode) == 0o600
        and target_file.read_bytes() == b"secret"
    )
    with pytest.raises(FileExistsError):
        _write_private(target_file, b"again")
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(FileExistsError):
        _write_private(link, b"x")  # a planted symlink is never followed
    assert not (tmp_path / "elsewhere").exists()
