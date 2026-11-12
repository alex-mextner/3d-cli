"""`3d init --env` backup + Home Assistant stage against a fake HA and a scripted host (printops.env_stage & friends)."""

from __future__ import annotations

import dataclasses
import json
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from printops_ha_fakes import TOKEN, FakeHa

from errors import InvalidArgument
from printops import backup_setup, env_stage, ha_detect, ha_locations, ha_wire
from printops.backup_setup import BackupContext
from printops.ha_rest import HaRest, HaRestError

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / ha_wire.PACKAGE_REL
DISCOVERY = (
    "@@lsblk\n"
    + json.dumps(
        {
            "blockdevices": [
                {
                    "name": "sdb",
                    "path": "/dev/sdb",
                    "size": 512 * 10**9,
                    "type": "disk",
                    "children": [
                        {
                            "name": "sdb1",
                            "path": "/dev/sdb1",
                            "size": 512 * 10**9,
                            "fstype": "ext4",
                            "mountpoints": ["/mnt/backup512"],
                            "type": "part",
                        }
                    ],
                }
            ]
        }
    )
    + "\n@@df\nFilesystem Type 1024-blocks Used Available Capacity Mounted on\n"
    "/dev/sdb1 ext4 500000000 1000 499000000 1% /mnt/backup512\n@@data\n/dev/sda1 1 1 1 1% /x\n"
)


class Host:
    """A scripted Spoolman host: records every script, answers discovery and the docker exec calls."""

    def __init__(self, *, selftest_ok: bool = True, writable: bool = True) -> None:
        self.scripts: list[str] = []
        self.selftest_ok = selftest_ok
        self.writable = writable

    def __call__(self, script: str) -> tuple[int, str]:
        self.scripts.append(script)
        if "@@lsblk" in script:
            return 0, DISCOVERY
        if script.startswith("test -d"):
            return (0 if self.writable else 1), ""
        if "backup target add" in script:
            return 0, "target 'default' -> /backup (disk id abc) saved"
        if "backup selftest" in script:
            return (0, "selftest ok") if self.selftest_ok else (1, "FAIL")
        if "df -P" in script:
            return 1, ""  # not the same disk as the data
        return 0, ""

    def wrote(self) -> list[str]:
        return [s for s in self.scripts if "backup target add" in s or "selftest" in s]


class Prompts:
    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []

    def __call__(self, question: str) -> str:
        self.asked.append(question)
        return self.answers.pop(0) if self.answers else ""


@pytest.fixture
def fake_ha() -> Iterator[FakeHa]:
    ha = FakeHa()
    yield ha
    ha.close()


@pytest.fixture(autouse=True)
def cfg_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.delenv("HA_TOKEN", raising=False)
    return tmp_path


@pytest.fixture
def ha_dir(tmp_path: Path) -> Path:
    d = tmp_path / "ha-config"
    d.mkdir()
    (d / "configuration.yaml").write_text(
        "homeassistant:\n  packages: !include_dir_named packages\n", encoding="utf-8"
    )
    (d / "secrets.yaml").write_text("existing: 1\n", encoding="utf-8")
    return d


def stage(
    host: Host,
    ask: Prompts,
    out: list[str],
    *,
    interactive: bool,
    ha: FakeHa,
    redeploys: list[str],
) -> env_stage.StageEnv:
    ctx = BackupContext(
        run=host,
        where="test box",
        data_dir="/srv/printops/data",
        container="printops-api",
        redeploy=redeploys.append,
        ask=ask,
        say=out.append,
        interactive=interactive,
    )
    return env_stage.StageEnv(
        ctx=ctx,
        repo_root=REPO,
        env={"HA_TOKEN": TOKEN},
        api_url_for_ha="http://10.0.0.9:8765",
        spoolman_url_for_ha="http://10.0.0.9:7912",
        mint_ha_token=lambda: "po_hatoken",
        host_names=["10.0.0.9"],
        fetch=ha_detect.http_get,
        mdns=lambda: [],
    )


def opts(ha: FakeHa, ha_dir: Path | None = None, **over: str) -> env_stage.StageOptions:
    values = {"--ha": ha.url, **({"--ha-dir": str(ha_dir)} if ha_dir else {}), **over}
    return env_stage.parse_stage_options(values, set())


# ------------------------------------------------------------------------- the package file


def test_the_package_is_valid_yaml_with_one_scheduler_and_the_notification_path() -> (
    None
):
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_constructor("!secret", lambda loader, node: f"<secret {node.value}>")
    doc = yaml.load(PACKAGE.read_text(encoding="utf-8"), Loader)  # noqa: S506 — SafeLoader subclass
    assert set(doc) == {"rest_command", "input_datetime", "script", "automation"}
    daily = next(a for a in doc["automation"] if a["id"] == "printops_backup_daily")
    assert daily["triggers"] == [{"trigger": "time", "at": "04:00:00"}]
    script = doc["script"]["printops_backup_run"]
    assert set(script["variables"]) == set(ha_wire.VALUE_KEYS)
    steps = json.dumps(script["sequence"])
    assert (
        "persistent_notification.create" in steps and "printops_backup_failed" in steps
    )
    assert (
        doc["rest_command"]["printops_backup_run"]["headers"]["Authorization"]
        == "<secret printops_api_auth>"
    )
    assert doc["rest_command"]["printops_backup_run"]["timeout"] >= 600


def test_render_changes_only_the_three_edit_spots() -> None:
    text = PACKAGE.read_text(encoding="utf-8")
    out = ha_wire.render_package(
        text,
        {"api_url": "http://nas:8765", "keep_daily": 14, "subdir": "x y"},
        "03:30:00",
    )
    diff = [
        (a, b)
        for a, b in zip(text.splitlines(), out.splitlines(), strict=True)
        if a != b
    ]
    assert (
        len(diff) == 4
        and 'at: "03:30:00"' in out
        and "keep_daily: 14" in out
        and 'subdir: "x y"' in out
    )
    assert 'url: "http://nas:8765/v1/backup/run"' in out
    assert 'at: "12:07:00"' in out  # the watchdog's own time is untouched
    assert yaml.safe_load(out.replace("!secret", "")) is not None


def test_render_refuses_a_package_whose_block_was_edited_away() -> None:
    broken = PACKAGE.read_text(encoding="utf-8").replace("      keep_weekly: 4\n", "")
    with pytest.raises(ValueError, match="keep_weekly"):
        ha_wire.render_package(broken, {"keep_weekly": 2})


# ------------------------------------------------------------------------- detection


def test_detect_uses_configured_url_known_names_and_never_writes(
    fake_ha: FakeHa,
) -> None:
    def only_the_fake(url: str) -> tuple[int, str]:
        # hermetic (#748): the known-name probes (homeassistant.local, ...) must not reach the machine's real
        # name resolution, where a real Home Assistant may answer
        return ha_detect.http_get(url) if url.startswith(fake_ha.url) else (0, "")

    found = ha_detect.detect_ha(
        env={"HA_URL": fake_ha.url}, hosts=[], fetch=only_the_fake, mdns=lambda: []
    )
    assert [(f.url, f.via) for f in found] == [(fake_ha.url, "configured")]
    assert {r[0] for r in fake_ha.requests} == {"GET"}

    probed: list[str] = []

    def fetch(url: str) -> tuple[int, str]:
        probed.append(url)
        return (
            (200, '{"name": "Home Assistant"}')
            if url.startswith("http://box:8123")
            else (0, "")
        )

    names = ha_detect.detect_ha(
        env={}, hosts=["box"], fetch=fetch, mdns=lambda: ["http://192.168.1.5:8123"]
    )
    assert [(f.url, f.via) for f in names] == [("http://box:8123", "spoolman host")]
    assert (
        "http://192.168.1.5:8123/manifest.json" in probed
        and "http://homeassistant.local:8123/manifest.json" in probed
    )


def test_mdns_answer_is_parsed() -> None:
    import socket
    import struct

    def name(s: str) -> bytes:
        return b"".join(bytes([len(p)]) + p.encode() for p in s.split(".")) + b"\x00"

    srv = struct.pack(">HHH", 0, 0, 8123) + name("ha.local")
    pkt = (
        struct.pack(">HHHHHH", 0, 0x8400, 0, 1, 0, 1)
        + name("x._home-assistant._tcp.local")
        + struct.pack(">HHIH", 33, 1, 120, len(srv))
        + srv
        + name("ha.local")
        + struct.pack(">HHIH", 1, 1, 120, 4)
        + socket.inet_aton("192.168.7.7")
    )
    assert ha_detect.parse_mdns(pkt) == ["http://192.168.7.7:8123"]
    assert ha_detect.parse_mdns(b"garbage") == []


def test_token_comes_from_env_then_a_mode_600_file_and_a_loose_file_is_refused(
    cfg_home: Path,
) -> None:
    assert ha_detect.resolve_token({"HA_TOKEN": " t "})[0] == "t"
    path = ha_detect.save_ha_config({"url": "http://x:8123", "token": "filetoken"})
    assert path.stat().st_mode & 0o777 == 0o600
    assert ha_detect.resolve_token({}) == ("filetoken", str(path))
    path.chmod(0o644)
    with pytest.raises(Exception, match="readable by others"):
        ha_detect.resolve_token({})


# ------------------------------------------------------------------------- wiring flows


def test_declining_writes_nothing_anywhere(
    fake_ha: FakeHa, ha_dir: Path, cfg_home: Path
) -> None:
    out: list[str] = []
    host, ask = Host(), Prompts("s", "n")
    redeploys: list[str] = []
    rc = env_stage.run_stage(
        stage(host, ask, out, interactive=True, ha=fake_ha, redeploys=redeploys),
        opts(fake_ha, ha_dir),
    )
    assert rc == 0
    assert (
        fake_ha.writes() == []
    )  # only GETs; the HA backup-location reads are read-only websocket queries
    assert set(fake_ha.ws_commands) <= {
        "backup/agents/info",
        "backup/config/info",
        "supervisor/api",
    }
    assert (
        not (ha_dir / "packages").exists()
        and (ha_dir / "secrets.yaml").read_text(encoding="utf-8") == "existing: 1\n"
    )
    assert (
        not ha_detect.config_file().exists() and host.wrote() == [] and redeploys == []
    )
    assert any("left alone (nothing was written there)" in line for line in out)


def test_confirmed_flow_configures_backup_installs_the_package_and_wires_spoolman(
    fake_ha: FakeHa, ha_dir: Path
) -> None:
    out: list[str] = []
    host, ask = Host(), Prompts("1", "y", "y", "y")
    redeploys: list[str] = []
    rc = env_stage.run_stage(
        stage(host, ask, out, interactive=True, ha=fake_ha, redeploys=redeploys),
        opts(
            fake_ha,
            ha_dir,
            **{"--keep": "daily=10,weekly=3,monthly=2", "--backup-at": "03:45"},
        ),
    )
    assert rc == 0, out
    assert redeploys == [
        "/mnt/backup512"
    ]  # the picked disk is bound into the api container, Spoolman is not touched
    assert any(
        "backup target add default /backup --init-marker" in s for s in host.scripts
    ) and any("selftest" in s for s in host.scripts)
    assert (
        fake_ha.entries
        and fake_ha.entries[0]["data"]["spoolman_url"] == "http://10.0.0.9:7912"
    )
    package = (ha_dir / "packages" / "printops_backup.yaml").read_text(encoding="utf-8")
    assert (
        'at: "03:45:00"' in package
        and "keep_daily: 10" in package
        and 'url: "http://10.0.0.9:8765/v1/backup/run"' in package
    )
    assert 'target: "default"' in package
    assert 'printops_api_auth: "Bearer po_hatoken"' in (
        ha_dir / "secrets.yaml"
    ).read_text(encoding="utf-8")
    paths = [r[1] for r in fake_ha.requests if r[0] == "POST"]
    assert "/api/config/core/check_config" in paths and paths.index(
        "/api/config/core/check_config"
    ) < paths.index("/api/services/script/reload")
    assert [p for p in paths if p.endswith("/reload")] == [
        f"/api/services/{d}/reload"
        for d in ("script", "automation", "rest_command", "input_datetime")
    ]
    assert all("restart" not in p for p in paths)
    again = env_stage.run_stage(
        stage(
            host,
            Prompts("1", "y", "y", "y"),
            [],
            interactive=True,
            ha=fake_ha,
            redeploys=[],
        ),
        opts(fake_ha, ha_dir),
    )
    assert (
        again == 0 and len(fake_ha.entries) == 1
    )  # idempotent: no second integration entry


def test_no_input_writes_to_ha_only_with_an_explicit_yes(
    fake_ha: FakeHa, ha_dir: Path
) -> None:
    out: list[str] = []
    host = Host()
    rc = env_stage.run_stage(
        stage(host, Prompts(), out, interactive=False, ha=fake_ha, redeploys=[]),
        opts(fake_ha, ha_dir, **{"--backup": "path:/mnt/backup512"}),
    )
    assert rc == 0 and fake_ha.writes() == [] and not (ha_dir / "packages").exists()
    rc = env_stage.run_stage(
        stage(host, Prompts(), out, interactive=False, ha=fake_ha, redeploys=[]),
        opts(
            fake_ha, ha_dir, **{"--backup": "path:/mnt/backup512", "--ha-wire": "yes"}
        ),
    )
    assert (
        rc == 0
        and (ha_dir / "packages" / "printops_backup.yaml").is_file()
        and fake_ha.entries
    )


def test_no_input_never_picks_a_disk_by_itself() -> None:
    out: list[str] = []
    host = Host()
    redeploys: list[str] = []
    ctx = BackupContext(
        run=host,
        where="box",
        data_dir="/d",
        container="c",
        redeploy=redeploys.append,
        ask=Prompts(),
        say=out.append,
        interactive=False,
    )
    assert env_stage.backup_stage(ctx, env_stage.parse_stage_options({}, set())) is None
    assert redeploys == [] and host.wrote() == []
    assert any("/mnt/backup512" in line for line in out) and any(
        "chooses nothing" in line for line in out
    )


def test_the_agent_choice_prints_a_self_contained_prompt_and_changes_nothing() -> None:
    out: list[str] = []
    host = Host()
    ctx = BackupContext(
        run=host,
        where="USER@nas",
        data_dir="/srv/printops/data",
        container="printops-api",
        redeploy=lambda _d: None,
        ask=Prompts("a"),
        say=out.append,
        interactive=True,
        hint_gb=512,
    )
    assert env_stage.backup_stage(ctx, env_stage.parse_stage_options({}, set())) is None
    text = "\n".join(out)
    for needle in (
        "/mnt/backup512",
        "backup target add default /backup --init-marker",
        "backup selftest",
        "Do NOT format",
        "CONFIRM",
        "04:00",
        "/srv/printops/data",
        "selftest ok",
        "USER@nas",
    ):
        assert needle in text
    assert host.wrote() == []


def test_a_failing_selftest_means_not_configured_and_no_package(
    fake_ha: FakeHa, ha_dir: Path
) -> None:
    out: list[str] = []
    rc = env_stage.run_stage(
        stage(
            Host(selftest_ok=False),
            Prompts("1", "y", "y"),
            out,
            interactive=True,
            ha=fake_ha,
            redeploys=[],
        ),
        opts(fake_ha, ha_dir),
    )
    assert rc == 1 and not (ha_dir / "packages").exists()
    assert any("NOT configured" in line for line in out) and any(
        "not installed (no backup target" in line for line in out
    )


def test_the_disk_with_the_data_is_refused_unless_the_user_insists() -> None:
    out: list[str] = []
    host = Host()
    host_same = lambda s: (0, "same") if "cut -d" in s else host(s)  # noqa: E731
    ctx = BackupContext(
        run=host_same,
        where="box",
        data_dir="/d",
        container="c",
        redeploy=lambda _d: None,
        ask=Prompts(),
        say=out.append,
        interactive=False,
    )
    result = env_stage.backup_stage(
        ctx, env_stage.parse_stage_options({"--backup": "path:/d/backup"}, set())
    )
    assert result is not None and not result.ok and host.wrote() == []


def test_missing_spoolman_integration_gives_the_install_steps_and_creates_nothing(
    ha_dir: Path,
) -> None:
    ha = FakeHa(spoolman_installed=False)
    try:
        out = ha_wire.wire_spoolman(HaRest(ha.url, TOKEN), "http://10.0.0.9:7912")
    finally:
        ha.close()
    assert (
        out.status == "needs-install"
        and any("HACS" in s for s in out.steps)
        and ha.entries == []
    )
    assert any("core ships no Spoolman integration" in s for s in out.steps)


def test_ha_rejecting_the_spoolman_url_aborts_the_flow() -> None:
    ha = FakeHa(spoolman_url_ok=False)
    try:
        out = ha_wire.wire_spoolman(HaRest(ha.url, TOKEN), "http://nowhere:1")
    finally:
        ha.close()
    assert (
        out.status == "failed"
        and "Error testing spoolman url" in out.detail
        and ha.entries == []
    )
    assert ha.requests[-1][0] == "DELETE"


def test_invalid_check_config_puts_the_old_package_back_and_reloads_nothing(
    ha_dir: Path,
) -> None:
    ha = FakeHa(check_valid=False)
    pkg = ha_dir / "packages" / "printops_backup.yaml"
    pkg.parent.mkdir()
    pkg.write_text("# previous\n", encoding="utf-8")
    try:
        out = ha_wire.install_package(
            backup_setup.local_shell(),
            HaRest(ha.url, TOKEN),
            str(ha_dir),
            REPO,
            {"api_url": "http://x:8765"},
            lambda: "po_x",
        )
    finally:
        ha.close()
    assert out.status == "failed" and pkg.read_text(encoding="utf-8") == "# previous\n"
    assert not [r for r in ha.requests if "/reload" in r[1]]


def test_a_missing_reload_service_asks_for_one_restart_not_a_failure(
    ha_dir: Path,
) -> None:
    ha = FakeHa(reload_missing=("rest_command",))
    try:
        out = ha_wire.install_package(
            backup_setup.local_shell(),
            HaRest(ha.url, TOKEN),
            str(ha_dir),
            REPO,
            {},
            lambda: "po_x",
        )
    finally:
        ha.close()
    assert out.status == "needs-restart" and "rest_command" in out.detail


def test_a_ha_config_managed_by_3d_ha_deploy_is_refused(
    fake_ha: FakeHa, ha_dir: Path
) -> None:
    managed = ha_dir / "3d-cli" / "integrations" / "homeassistant"
    managed.mkdir(parents=True)
    (managed / "manifest.yaml").write_text("ha_repo: {}\n", encoding="utf-8")
    out = ha_wire.install_package(
        backup_setup.local_shell(),
        HaRest(fake_ha.url, TOKEN),
        str(ha_dir),
        REPO,
        {},
        lambda: "t",
    )
    assert (
        out.status == "refused"
        and "3d ha deploy" in out.detail
        and not (ha_dir / "packages").exists()
    )


def test_config_without_packages_include_is_refused(
    fake_ha: FakeHa, ha_dir: Path
) -> None:
    (ha_dir / "configuration.yaml").write_text("default_config:\n", encoding="utf-8")
    out = ha_wire.install_package(
        backup_setup.local_shell(),
        HaRest(fake_ha.url, TOKEN),
        str(ha_dir),
        REPO,
        {},
        lambda: "t",
    )
    assert out.status == "refused" and "packages:" in out.detail


def test_an_existing_secret_is_kept_and_no_token_is_minted(
    fake_ha: FakeHa, ha_dir: Path
) -> None:
    (ha_dir / "secrets.yaml").write_text(
        'printops_api_auth: "Bearer keepme"\n', encoding="utf-8"
    )

    def boom() -> str:
        raise AssertionError("minted although the secret exists")

    out = ha_wire.install_package(
        backup_setup.local_shell(),
        HaRest(fake_ha.url, TOKEN),
        str(ha_dir),
        REPO,
        {},
        boom,
    )
    assert (
        out.status == "done"
        and (ha_dir / "secrets.yaml").read_text(encoding="utf-8")
        == 'printops_api_auth: "Bearer keepme"\n'
    )


# ------------------------------------------------------------------------- HA's own backup locations


def test_ha_backup_locations_are_read_over_the_websocket(fake_ha: FakeHa) -> None:
    cands = ha_locations.ha_backup_locations(fake_ha.url, TOKEN)
    assert [c.label for c in cands] == ["backup.local", "hassio.nas", "nas"]
    assert all(c.kind == "ha" and not c.selectable for c in cands)
    assert "10.0.0.5:/export/ha" in cands[2].notes[0]
    assert set(fake_ha.ws_commands) == {
        "backup/agents/info",
        "backup/config/info",
        "supervisor/api",
    }
    assert ha_locations.ha_backup_locations(fake_ha.url, "wrong") == []


def test_ha_rest_error_carries_status(fake_ha: FakeHa) -> None:
    with pytest.raises(HaRestError) as err:
        HaRest(fake_ha.url, "wrong").get("/api/config")
    assert err.value.status == 401
    with pytest.raises(HaRestError) as down:
        HaRest("http://127.0.0.1:9", TOKEN, timeout=1).get("/api/")
    assert down.value.status == 0


def test_stage_flag_validation() -> None:
    for bad in (
        {"--backup": "everything"},
        {"--ha-wire": "maybe"},
        {"--backup-at": "25:00"},
        {"--backup-disk-gb": "0"},
    ):
        with pytest.raises(InvalidArgument):
            env_stage.parse_stage_options(bad, set())
    parsed: Any = env_stage.parse_stage_options(
        {"--backup": "path:/x", "--backup-disk-gb": "256"}, {"--allow-same-disk"}
    )
    assert (
        parsed.hint_gb == 256 and parsed.allow_same_disk and parsed.backup == "path:/x"
    )


@pytest.fixture
def fake_block_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`df` + `lsblk -s` for: /srv/data on sda1, /mnt/second on sda2 (same disk sda), /mnt/other on sdb1."""
    bin_dir = tmp_path / "blockbin"
    bin_dir.mkdir()
    (bin_dir / "df").write_text(
        "#!/bin/sh\necho 'Filesystem 1024-blocks Used Available Capacity Mounted on'\n"
        'case "$2" in\n  /srv/data*) echo "/dev/sda1 1 1 1 1% /srv/data";;\n'
        '  /mnt/second*) echo "/dev/sda2 1 1 1 1% /mnt/second";;\n  *) echo "/dev/sdb1 1 1 1 1% /mnt/other";;\nesac\n',
        encoding="utf-8",
    )
    (bin_dir / "lsblk").write_text(
        '#!/bin/sh\nfor last; do :; done\ncase "$last" in\n'
        "  /dev/sda1) printf 'sda1 part\\nsda disk\\n';;\n  /dev/sda2) printf 'sda2 part\\nsda disk\\n';;\n"
        "  /dev/sdb1) printf 'sdb1 part\\nsdb disk\\n';;\nesac\n",
        encoding="utf-8",
    )
    for tool in bin_dir.iterdir():
        tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def _stage_ctx(host: backup_setup.RunHost) -> BackupContext:
    return BackupContext(
        run=host,
        where="this machine",
        data_dir="/srv/data",
        container="printops-api",
        redeploy=lambda _d: None,
        ask=lambda _q: "",
        say=lambda _m: None,
        interactive=False,
    )


def test_a_backup_dir_on_another_partition_of_the_data_disk_counts_as_the_same_disk(
    fake_block_tools: None,
) -> None:
    ctx = _stage_ctx(backup_setup.local_shell())
    assert env_stage._same_disk(ctx, "/srv/data/backup")  # the same filesystem
    assert env_stage._same_disk(ctx, "/mnt/second")  # sda2 vs sda1: one physical disk
    assert not env_stage._same_disk(ctx, "/mnt/other")  # sdb: a real second disk


# ------------------------------------------------------------------------- the token goes only to a trusted origin


def _only_the_fake(fake: FakeHa) -> ha_detect.Fetch:
    """Real HTTP to the fake HA; every other well-known name (homeassistant.local, ...) is unreachable."""

    def fetch(url: str) -> tuple[int, str]:
        return ha_detect.http_get(url) if url.startswith(fake.url) else (0, "")

    return fetch


def _discovered_stage(
    fake: FakeHa, prompts: Prompts, *, interactive: bool, out: list[str]
) -> env_stage.StageEnv:
    base = stage(Host(), prompts, out, interactive=interactive, ha=fake, redeploys=[])
    return dataclasses.replace(
        base, mdns=lambda: [fake.url], fetch=_only_the_fake(fake)
    )


@pytest.mark.parametrize("wire", ["no", "yes"])
def test_an_ha_that_only_answered_on_the_lan_never_receives_the_token_with_no_input(
    fake_ha: FakeHa, cfg_home: Path, wire: str
) -> None:
    """The sole mDNS answer used to get the saved token over a WebSocket auth frame before any confirmation,
    even with `--ha-wire no --backup skip`. Credentials, not just writes, must not reach it."""
    out: list[str] = []
    se = _discovered_stage(fake_ha, Prompts(), interactive=False, out=out)
    rc = env_stage.run_stage(
        se,
        env_stage.parse_stage_options({"--backup": "skip", "--ha-wire": wire}, set()),
    )
    assert fake_ha.tokens_received() == [] and fake_ha.writes() == []
    assert rc == (1 if wire == "yes" else 0)
    if wire == "yes":
        assert any(
            "only discovered on the network" in line and f"--ha {fake_ha.url}" in line
            for line in out
        )


def test_an_interactive_user_must_approve_a_discovered_origin_before_the_token_is_sent(
    fake_ha: FakeHa, ha_dir: Path, cfg_home: Path
) -> None:
    out: list[str] = []
    declined = Prompts(
        "1", "y", "n"
    )  # which HA, "add it?", "send the token to this origin?"
    se = _discovered_stage(fake_ha, declined, interactive=True, out=out)
    env_stage.run_stage(
        se,
        env_stage.parse_stage_options(
            {"--backup": "skip", "--ha-dir": str(ha_dir)}, set()
        ),
    )
    assert fake_ha.tokens_received() == [] and not declined.answers
    assert any("Send your Home Assistant token to it?" in q for q in declined.asked)

    approved = Prompts(
        "1", "y", "y", "y"
    )  # ... then yes to the origin, then "Do that?" for the package
    se = _discovered_stage(fake_ha, approved, interactive=True, out=out)
    env_stage.run_stage(
        se,
        env_stage.parse_stage_options(
            {"--backup": "skip", "--ha-dir": str(ha_dir)}, set()
        ),
    )
    assert TOKEN in fake_ha.tokens_received() and fake_ha.entries


def test_a_configured_url_is_trusted_without_asking(
    fake_ha: FakeHa, cfg_home: Path
) -> None:
    out: list[str] = []
    base = _discovered_stage(fake_ha, Prompts(), interactive=False, out=out)
    se = dataclasses.replace(base, env={"HA_TOKEN": TOKEN, "HA_URL": fake_ha.url})
    env_stage.run_stage(
        se,
        env_stage.parse_stage_options({"--backup": "skip", "--ha-wire": "no"}, set()),
    )
    assert (
        TOKEN in fake_ha.tokens_received()
    )  # the user named this origin: HA's backup locations were read


# ------------------------------------------------------------------------- the API secret stays private


def test_the_api_secret_is_created_private_and_its_token_never_appears_in_a_command_line(
    tmp_path: Path,
) -> None:
    secrets = tmp_path / "ha" / "secrets.yaml"
    secrets.parent.mkdir()
    seen: list[tuple[str, str | None]] = []
    real = backup_setup.local_shell()

    def spy(script: str, stdin: str | None = None) -> tuple[int, str]:
        seen.append((script, stdin))
        return real(script, stdin)

    old = os.umask(0o022)  # the umask that made the old `touch` + append world-readable
    try:
        assert ha_wire._ensure_secret(spy, str(secrets), lambda: "po_supersecret") == (
            0,
            "",
        )
        assert (
            secrets.stat().st_mode & 0o077 == 0
            and "Bearer po_supersecret" in secrets.read_text(encoding="utf-8")
        )
        assert all(
            "po_supersecret" not in script for script, _ in seen
        )  # not in argv / ssh command
        assert any(stdin and "po_supersecret" in stdin for _, stdin in seen)
        minted: list[str] = []

        def mint() -> str:
            minted.append("again")
            return "x"

        assert ha_wire._ensure_secret(spy, str(secrets), mint) == (0, "")
        assert minted == []  # already there: nothing minted, nothing appended
        other = tmp_path / "ha" / "existing.yaml"
        other.write_text("keep: 1\n", encoding="utf-8")
        other.chmod(0o640)
        ha_wire._ensure_secret(real, str(other), lambda: "po_second")
        assert (
            other.stat().st_mode & 0o777 == 0o640
        )  # an existing file keeps the owner's mode
        assert other.read_text(encoding="utf-8").startswith("keep: 1\n")
    finally:
        os.umask(old)


# ------------------------------------------------------------------------- activation is retried


class _FlakyHa(HaRest):
    def __init__(self, url: str) -> None:
        super().__init__(url, TOKEN)
        self.fail_next_post = True

    def post(
        self, path: str, body: object = None, *, timeout: float | None = None
    ) -> object:
        if self.fail_next_post:
            self.fail_next_post = False
            raise HaRestError("Home Assistant is unreachable: connection reset")
        return super().post(path, body, timeout=timeout)


def _install_values() -> dict[str, object]:
    return {
        "api_url": "http://10.0.0.9:8765",
        "target": "default",
        "subdir": "3d-printops",
        "keep_daily": 7,
        "keep_weekly": 4,
        "keep_monthly": 6,
    }


def test_a_package_written_before_an_api_failure_is_activated_by_the_retry(
    fake_ha: FakeHa, ha_dir: Path
) -> None:
    """check_config died after the file landed; the retry saw identical content and returned `unchanged`
    without validating or reloading, so the schedule was never loaded."""
    ha = _FlakyHa(fake_ha.url)
    shell = backup_setup.local_shell()
    first = ha_wire.install_package(
        shell, ha, str(ha_dir), REPO, _install_values(), lambda: "po_t"
    )
    assert first.status == "failed" and "retry" in first.detail
    assert (ha_dir / "packages" / "printops_backup.yaml").is_file()
    assert [
        r for r in fake_ha.requests if r[1] == "/api/config/core/check_config"
    ] == []
    second = ha_wire.install_package(
        shell, ha, str(ha_dir), REPO, _install_values(), lambda: "po_t"
    )
    assert second.status == "unchanged" and "validated and reloaded" in second.detail
    paths = [r[1] for r in fake_ha.requests if r[0] == "POST"]
    assert (
        "/api/config/core/check_config" in paths
        and "/api/services/script/reload" in paths
    )


def test_an_identical_package_that_ha_rejects_on_retry_is_reported_not_deleted(
    fake_ha: FakeHa, ha_dir: Path
) -> None:
    ha = HaRest(fake_ha.url, TOKEN)
    shell = backup_setup.local_shell()
    assert (
        ha_wire.install_package(
            shell, ha, str(ha_dir), REPO, _install_values(), lambda: "po_t"
        ).status
        == "done"
    )
    fake_ha.check_valid = False
    again = ha_wire.install_package(
        shell, ha, str(ha_dir), REPO, _install_values(), lambda: "po_t"
    )
    assert again.status == "failed" and "left in place" in again.detail
    assert (
        ha_dir / "packages" / "printops_backup.yaml"
    ).is_file()  # only a package THIS call wrote is put back


# ------------------------------------------------------------------------- the mDNS parser


def _name(s: str) -> bytes:
    return b"".join(bytes([len(p)]) + p.encode() for p in s.split(".")) + b"\x00"


def _returns_quickly(packet: bytes) -> list[str]:
    result: list[list[str]] = []
    worker = threading.Thread(
        target=lambda: result.append(ha_detect.parse_mdns(packet)), daemon=True
    )
    worker.start()
    worker.join(5)
    assert not worker.is_alive(), "parse_mdns is spinning on this packet"
    return result[0]


def test_a_dns_compression_pointer_loop_is_rejected_not_followed_forever() -> None:
    import struct

    header = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0)
    assert (
        _returns_quickly(header + b"\xc0\x0c" + struct.pack(">HH", 12, 1)) == []
    )  # points at itself
    two = header + b"\xc0\x0e\xc0\x0c"  # a -> b -> a
    assert _returns_quickly(two) == []


def test_truncated_dns_names_are_rejected() -> None:
    import struct

    header = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0)
    assert _returns_quickly(header + b"\x05ab") == []  # label longer than the packet
    assert _returns_quickly(header + b"\xc0") == []  # pointer cut in half
    assert _returns_quickly(header + b"\x01a") == []  # no terminating zero


def test_a_compressed_srv_target_still_resolves() -> None:
    """Legitimate compression (the SRV target points back at the A record's name) keeps working."""
    import socket
    import struct

    a_record = (
        _name("ha.local")
        + struct.pack(">HHIH", 1, 1, 120, 4)
        + socket.inet_aton("192.168.7.7")
    )
    srv_data = (
        struct.pack(">HHH", 0, 0, 8123) + b"\xc0\x0c"
    )  # target = the name at offset 12
    srv_record = (
        _name("x._home-assistant._tcp.local")
        + struct.pack(">HHIH", 33, 1, 120, len(srv_data))
        + srv_data
    )
    packet = struct.pack(">HHHHHH", 0, 0x8400, 0, 2, 0, 0) + a_record + srv_record
    assert ha_detect.parse_mdns(packet) == ["http://192.168.7.7:8123"]
