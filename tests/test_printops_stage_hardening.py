"""Failure modes of the `3d init --env` backup/HA stage: a failed redeploy, bad flags, a hidden token prompt, a hung
or hostile network, relative backup paths, the same-disk probe, and the one-line backup health report."""

from __future__ import annotations

import socket
import struct
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from printops_ha_fakes import FakeHa
from test_printops_ha_stage import Host, Prompts, stage

from errors import InvalidArgument
from printops import backup_setup, ha_detect
from printops.backup_setup import BackupContext
from printops import env_stage


@pytest.fixture
def fake_ha() -> Iterator[FakeHa]:
    ha = FakeHa()
    yield ha
    ha.close()


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.delenv("HA_TOKEN", raising=False)


# ---------------------------------------------------------------------------- a failed redeploy


def test_a_failed_compose_step_is_a_reported_not_configured_not_a_traceback(
    fake_ha: FakeHa,
) -> None:
    out: list[str] = []
    se = stage(Host(), Prompts(), out, interactive=False, ha=fake_ha, redeploys=[])

    def broken(_host_dir: str) -> None:
        raise RuntimeError(
            "step 2 (build + start spoolman and 3d-api) failed with exit 1"
        )

    se.ctx.redeploy = broken
    rc = env_stage.run_stage(
        se,
        env_stage.parse_stage_options(
            {"--backup": "path:/mnt/backup512", "--ha": "skip"}, set()
        ),
    )
    assert rc == 1
    assert any("step 2" in line for line in out)
    assert any("NOT configured" in line for line in out)
    assert (
        fake_ha.writes() == []
    )  # the HA stage was not reached with a half-configured backup


# ---------------------------------------------------------------------------- flags


@pytest.mark.parametrize(
    "bad", ["yes", "host:8123", "ftp://h:8123", "http://", "//h:8123"]
)
def test_ha_must_be_auto_skip_or_an_http_url(bad: str) -> None:
    with pytest.raises(InvalidArgument):
        env_stage.parse_stage_options({"--ha": bad}, set())


@pytest.mark.parametrize(
    "good", ["auto", "skip", "http://homeassistant.local:8123", "https://ha.example"]
)
def test_valid_ha_values_parse(good: str) -> None:
    assert env_stage.parse_stage_options({"--ha": good}, set()).ha == good


# ---------------------------------------------------------------------------- the token prompt


def test_the_ha_token_is_asked_without_echo(fake_ha: FakeHa) -> None:
    ask = Prompts("n")  # the answer to "store it?"
    se = stage(Host(), ask, [], interactive=True, ha=fake_ha, redeploys=[])
    secret_prompts: list[str] = []
    se.env = {}

    def hidden(question: str) -> str:
        secret_prompts.append(question)
        return "typed-token"

    se.ask_secret = hidden
    assert env_stage._ha_token(se, fake_ha.url) == "typed-token"
    assert secret_prompts and "token" in secret_prompts[0]
    assert not any(
        "long-lived" in q for q in ask.asked
    )  # never through the echoing prompt


def test_the_default_secret_prompt_is_getpass() -> None:
    import getpass

    assert (
        env_stage.StageEnv.__dataclass_fields__["ask_secret"].default is getpass.getpass
    )


# ---------------------------------------------------------------------------- mDNS


def _a_record(rdata: bytes, rdlen: int = 4) -> bytes:
    name = b"".join(bytes([len(p)]) + p.encode() for p in ["ha", "local"]) + b"\x00"
    return (
        struct.pack(">HHHHHH", 0, 0x8400, 0, 1, 0, 0)
        + name
        + struct.pack(">HHIH", 1, 1, 120, rdlen)
        + rdata
    )


def _answer() -> bytes:
    def name(s: str) -> bytes:
        return b"".join(bytes([len(p)]) + p.encode() for p in s.split(".")) + b"\x00"

    srv = struct.pack(">HHH", 0, 0, 8123) + name("ha.local")
    return (
        struct.pack(">HHHHHH", 0, 0x8400, 0, 1, 0, 1)
        + name("x._home-assistant._tcp.local")
        + struct.pack(">HHIH", 33, 1, 120, len(srv))
        + srv
        + name("ha.local")
        + struct.pack(">HHIH", 1, 1, 120, 4)
        + socket.inet_aton("192.168.7.7")
    )


class _Sock:
    """A multicast socket whose LAN keeps talking: `datagrams` first, then (optionally) junk forever."""

    instances: list[_Sock] = []

    def __init__(self, *_a: object) -> None:
        self.script = list(self.queue)
        self.reads = 0
        _Sock.instances.append(self)

    queue: list[bytes] = []
    forever = False

    def settimeout(self, _t: float) -> None:
        return None

    def sendto(self, *_a: object) -> None:
        return None

    def close(self) -> None:
        return None

    def recvfrom(self, _n: int) -> tuple[bytes, tuple[str, int]]:
        self.reads += 1
        if self.reads > 300:
            raise TimeoutError  # the test's own bound: reaching it means the deadline did not stop the loop
        if self.script:
            return self.script.pop(0), ("192.168.7.7", 5353)
        if not self.forever:
            raise TimeoutError
        time.sleep(0.01)
        return b"junk", ("192.168.7.99", 5353)


def test_a_truncated_a_record_ends_that_packet_only_not_the_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        ha_detect.parse_mdns(_a_record(b"\x01\x02")) == []
    )  # inet_ntoa of 2 bytes raises OSError
    _Sock.queue, _Sock.forever, _Sock.instances = (
        [_a_record(b"\x01\x02"), _answer()],
        False,
        [],
    )
    monkeypatch.setattr(ha_detect.socket, "socket", _Sock)
    assert ha_detect.mdns_browse(timeout=0.5) == ["http://192.168.7.7:8123"]


def test_a_peer_that_keeps_sending_cannot_stretch_the_mdns_browse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Sock.queue, _Sock.forever, _Sock.instances = [], True, []
    monkeypatch.setattr(ha_detect.socket, "socket", _Sock)
    started = time.monotonic()
    assert ha_detect.mdns_browse(timeout=0.3) == []
    assert time.monotonic() - started < 2.0
    assert (
        _Sock.instances[0].reads < 300
    )  # stopped by the overall deadline, not by running out of datagrams


# ---------------------------------------------------------------------------- hung shells


def test_a_hung_probe_is_cut_off_instead_of_hanging_the_stage() -> None:
    run = backup_setup.local_shell(timeout=0.5)
    started = time.monotonic()
    rc, out = run("sleep 30")
    assert rc == 124 and "timed out" in out and time.monotonic() - started < 10


def test_discovery_goes_through_the_bounded_probe_shell_and_everything_else_through_run() -> (
    None
):
    probes: list[str] = []
    runs: list[str] = []

    def probe(script: str) -> tuple[int, str]:
        probes.append(script)
        return 1, ""

    def run(script: str) -> tuple[int, str]:
        runs.append(script)
        return 0, ""

    ctx = BackupContext(
        run=run,
        probe=probe,
        where="box",
        data_dir="/d",
        container="c",
        redeploy=lambda _p: None,
        ask=lambda _q: "",
        say=lambda _l: None,
        interactive=False,
    )
    backup_setup.discover(ctx)
    assert probes and not runs
    assert (
        "timeout" in probes[0]
    )  # and the probe itself bounds every call that touches a mount


# ---------------------------------------------------------------------------- relative backup paths


def _real_ctx(redeployed: list[str], say: list[str]) -> BackupContext:
    return BackupContext(
        run=backup_setup.local_shell(),
        where="this machine",
        data_dir="/nonexistent-data",
        container="c",
        redeploy=redeployed.append,
        ask=lambda _q: "",
        say=say.append,
        interactive=False,
    )


def test_a_relative_backup_path_is_made_absolute_before_it_is_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "disk").mkdir()
    monkeypatch.chdir(tmp_path)
    redeployed: list[str] = []
    backup_setup.configure_backup(_real_ctx(redeployed, []), "disk")
    assert redeployed == [
        str(tmp_path / "disk")
    ]  # compose resolves a relative path against services/3d-api/


def test_a_tilde_backup_path_expands_on_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "usb").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    redeployed: list[str] = []
    backup_setup.configure_backup(_real_ctx(redeployed, []), "~/usb")
    assert redeployed == [str(tmp_path / "usb")]


def test_a_missing_relative_path_is_still_refused_by_its_own_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    redeployed: list[str] = []
    result = backup_setup.configure_backup(_real_ctx(redeployed, []), "nope")
    assert not result.ok and "nope" in result.detail and redeployed == []


# ---------------------------------------------------------------------------- the same-disk probe


def test_same_disk_is_found_when_the_matching_disk_is_not_the_last_pair_compared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A volume spanning sda+sdb with the data on sda: the loop prints `same` for sda=sda, then compares sdb=sda
    last, so the script's exit code is 1. The verdict must come from the output."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "df").write_text(
        '#!/bin/sh\necho "Filesystem 1 1 1 1% /x"\ncase "$2" in *backup*) echo "/dev/mapper/vg-lv 1 1 1 1% /m";'
        ' ;; *) echo "/dev/sda1 1 1 1 1% /d";; esac\n',
        encoding="utf-8",
    )
    (bindir / "lsblk").write_text(
        '#!/bin/sh\nfor last; do :; done\ncase "$last" in /dev/mapper/vg-lv) printf "vg-lv lvm\\nsda disk\\nsdb disk\\n";;'
        ' /dev/sda1) printf "sda1 part\\nsda disk\\n";; esac\n',
        encoding="utf-8",
    )
    for tool in ("df", "lsblk"):
        (bindir / tool).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    ctx = BackupContext(
        run=backup_setup.local_shell(),
        where="box",
        data_dir="/data",
        container="c",
        redeploy=lambda _p: None,
        ask=lambda _q: "",
        say=lambda _l: None,
        interactive=False,
    )
    assert env_stage._same_disk(ctx, "/mnt/backup") is True
    assert (
        env_stage._same_disk(ctx, "/other") is True
    )  # same device string as the data: also the same disk
