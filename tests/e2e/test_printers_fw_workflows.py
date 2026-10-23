"""E2E stories for `3d printers fw ...` through `bin/3d` against loopback fakes only (AGENTS.md:
never a real printer). A loopback HTTP server plays Moonraker + web UI, a tiny TCP server plays
sshd's banner, and a fake `ssh` executable on PATH records every command and answers from rules,
so even the ssh path runs the real SshRunner with no real connection."""

from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from printer_fakes import FakeCreality, u1_status
from printer_fw_fakes import BannerServer, LoopbackHttp, tgz

from .workflow_helper import REPO_ROOT, run_cli

FAKE_SSH = """#!{python}
import base64, json, os, sys
argv = sys.argv[1:]
text = sys.stdin.read() if argv[-2:] == ["sh", "-s"] else argv[-1]
state_path = os.environ["FAKE_SSH_STATE"]
flags = set(json.load(open(state_path))) if os.path.exists(state_path) else set()
with open(os.environ["FAKE_SSH_LOG"], "a") as fh:
    fh.write(json.dumps({{"argv": argv, "command": text}}) + "\\n")
for rule in json.load(open(os.environ["FAKE_SSH_RULES"])):
    if rule["needle"] in text and set(rule.get("needs", [])) <= flags:
        flags |= set(rule.get("sets", []))
        json.dump(sorted(flags), open(state_path, "w"))
        sys.stdout.buffer.write(base64.b64decode(rule.get("out_b64", "")))
        sys.stderr.write(rule.get("err", ""))
        sys.exit(rule.get("rc", 0))
sys.stderr.write("fake ssh: no rule")
sys.exit(1)
"""

PRINTER_CFG = "[printer]\n#*# [bed_mesh default]\n#*# points = 1\n"
FW_PATH = "/firmware-config/api/status"
POST_PATH = "/firmware-config/api/action/switch-to-backup-firmware"
UPLOAD_PATH = "/firmware-config/api/upgrade/upload"


def b64(text: str | bytes) -> str:
    return base64.b64encode(text if isinstance(text, bytes) else text.encode()).decode()


def lan_config(hardened: bool) -> dict[str, Any]:
    trusted = (
        ["127.0.0.1", "192.168.0.38"] if hardened else ["127.0.0.1", "192.168.0.0/16"]
    )
    return {
        "result": {
            "config": {
                "authorization": {"trusted_clients": trusted},
                "server": {"use_xheaders": not hardened},
            }
        }
    }


@dataclass
class World:
    tmp_path: Path
    moon: LoopbackHttp = field(default_factory=LoopbackHttp)
    banner: BannerServer = field(default_factory=BannerServer)
    k1: FakeCreality = field(default_factory=FakeCreality)
    extra_env: dict[str, str] = field(default_factory=dict)
    web_is_adapter: bool = False
    down: dict[str, int] = field(default_factory=lambda: {"n": 0})

    @property
    def dest(self) -> Path:
        return self.tmp_path / "backups"

    def fleet(self) -> Path:
        path = self.tmp_path / "fleet.yaml"
        if path.exists():
            return path
        path.write_text(
            f"""\
alpha:
  model: Snapmaker U1
  owner: own
  access: full
  adapter: moonraker
  host: 127.0.0.1
  port: {self.moon.port}
  heads: [{{label: T1, index: 0}}]
kappa:
  model: Creality K1 SE
  owner: own
  access: full
  adapter: creality-stock
  host: 127.0.0.1
  port: {self.k1.http_port}
  ws_port: {self.k1.ws_port}
  heads: [{{label: T1, index: 0}}]
gamma:
  model: Bambu Lab P1S
  owner: neighbour
  access: offline
  heads: [{{label: T1, index: 0}}]
delta:
  model: Creality CR-200B Pro
  owner: hackerspace
  access: offline
  heads: [{{label: T1, index: 0}}]
""",
            encoding="utf-8",
        )
        return path

    def env(self) -> dict[str, str]:
        # The adapter's own port already points the K1's `web` service at its fake (the profile's
        # adapter_services); the loopback server plays the web UI of every other printer.
        names = ["moonraker", "fluidd", "ssh"] + (
            [] if self.web_is_adapter else ["web"]
        )
        value = {
            "web": self.moon.port,
            "moonraker": self.moon.port,
            "fluidd": self.moon.port,
            "ssh": self.banner.port,
        }
        ports = ",".join(f"{n}={value[n]}" for n in names)
        base = {"THREED_FLEET": str(self.fleet()), "THREED_FW_SERVICE_PORTS": ports}
        return {**base, **self.extra_env}

    def fw(self, *argv: str, timeout: int = 90) -> subprocess.CompletedProcess[str]:
        return run_cli(
            self.tmp_path,
            "printers",
            "fw",
            *argv,
            env_extra=self.env(),
            timeout=timeout,
        )

    # -- the U1 world -----------------------------------------------------------------------------

    def _system_info(self) -> tuple[int, dict[str, Any] | str]:
        """Moonraker's answer; a rebooting printer answers 503 once (the probe sees it go down)."""
        if self.down["n"] > 0:
            self.down["n"] -= 1
            return 503, ""
        return 200, {"result": {"system_info": {}}}

    def u1(self, *, extended: bool, printing: bool = False) -> None:
        m = self.moon
        m.set("GET /machine/system_info", body=self._system_info)
        m.set("GET /", body="<html>ui</html>")
        m.set(
            "GET /server/config",
            body={
                "result": {
                    "config": {"authorization": {"trusted_clients": ["10.0.0.1"]}}
                }
            },
        )
        state = u1_status("printing" if printing else "standby")
        m.set(
            "GET /printer/objects/query*",
            body={"result": {"eventtime": 1.0, "status": state}},
        )
        for name in ("printer.cfg", "xyz_offset_calibration.cfg"):
            m.set(f"GET /server/files/config/{name}", body="cfg-content")
        if extended:
            m.set(
                f"GET {FW_PATH}",
                body={"version": {"title": "System Information", "items": []}},
            )

            def reboot() -> tuple[int, str]:
                m.routes.pop(f"GET {FW_PATH}", None)
                self.down["n"] = 1
                return (
                    200,
                    "=== switch-to-backup-firmware ===\nSUCCESS: Completed successfully\n",
                )

            def upgrade() -> tuple[int, str]:
                self.down["n"] = 1
                return (
                    200,
                    "=== Upgrade Started ===\nSUCCESS: Completed successfully.\n",
                )

            m.set(f"POST {POST_PATH}", body=reboot)
            m.set(f"POST {UPLOAD_PATH}", body=upgrade)

    # -- the K1 world: fake ssh + banner -------------------------------------------------------------

    def k1_rooted(self, *, lan: str | None = None) -> Path:
        """A rooted K1 behind the fake ssh. `lan` adds a running Moonraker: "open" (trusts the
        whole LAN) or "locked" (narrowed, nginx denies others)."""
        m = self.moon
        self.web_is_adapter = True
        bindir = self.tmp_path / "fakebin"
        bindir.mkdir(exist_ok=True)
        exe = bindir / "ssh"
        exe.write_text(FAKE_SSH.format(python=sys.executable), encoding="utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        tree = tgz(
            {
                "printer_data/config/printer.cfg": b"x",
                "printer_data/config/gcode_macro.cfg": b"y",
            }
        )
        rules: list[dict[str, Any]] = [
            {"needle": "test -d /usr/data", "rc": 0},
            {
                "needle": "cat /usr/data/printer_data/config/printer.cfg",
                "out_b64": b64(PRINTER_CFG),
            },
            {
                "needle": "tar czf - -C /usr/data printer_data/config",
                "out_b64": b64(tree),
            },
            {
                "needle": "tar czf - -C /usr/data creality/userdata",
                "rc": 2,
                "err": "tar: creality/userdata: No such file or directory",
            },
        ]
        if lan is None:
            rules += [
                {
                    "needle": "test -e /usr/data/helper-script/helper.sh",
                    "needs": ["cloned"],
                    "rc": 0,
                },
                {"needle": "test -e /usr/data/helper-script/helper.sh", "rc": 1},
                {"needle": "grep -q '^b46787a'", "needs": ["cloned"], "rc": 0},
                {"needle": "grep -q '^b46787a'", "rc": 1},
                {
                    "needle": 'git clone "$URL"',
                    "sets": ["cloned"],
                    "out_b64": b64("cloned\n"),
                },
            ]
        else:
            m.set("GET /server/info", body={"result": {"klippy_state": "ready"}})
            m.set("GET /server/config", body=lan_config(lan == "locked"))
            rules += [
                {"needle": "test -e /usr/data/helper-script/helper.sh", "rc": 0},
                {
                    "needle": "stat -c %a /usr/data/fluidd/index.html",
                    "out_b64": b64("755\n"),
                },
                {
                    "needle": "grep -c 'deny all'",
                    "rc": 0 if lan == "locked" else 1,
                    "out_b64": b64("2\n" if lan == "locked" else "0\n"),
                },
            ]
            m.set("GET /", body="<html>fluidd</html>")
        rules_file = self.tmp_path / "ssh-rules.json"
        rules_file.write_text(json.dumps(rules), encoding="utf-8")
        log = self.tmp_path / "ssh.log"
        self.extra_env.update(
            {
                "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
                "FAKE_SSH_RULES": str(rules_file),
                "FAKE_SSH_LOG": str(log),
                "FAKE_SSH_STATE": str(self.tmp_path / "ssh-state.json"),
            }
        )
        return log

    def ssh_commands(self) -> list[str]:
        log = self.tmp_path / "ssh.log"
        if not log.exists():
            return []
        return [json.loads(line)["command"] for line in log.read_text().splitlines()]


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    w = World(tmp_path)
    w.moon.start()
    w.banner.start()
    with w.k1.serve():
        yield w
    w.moon.stop()
    w.banner.stop()


def doc(result: subprocess.CompletedProcess[str]) -> Any:
    return json.loads(result.stdout)


# ---- profiles / status -----------------------------------------------------------------------------------


def test_profiles_lists_vendors_models_states_and_sources(world: World) -> None:
    """The user lists the firmware profiles and sees each vendor family, whether Klipper is possible, its
    state transitions and the source URLs, as text and as JSON."""
    out = world.fw("profiles")
    assert out.returncode == 0, out.stderr
    for fragment in (
        "snapmaker-u1",
        "creality-k1",
        "bambu-lab-p1-a1",
        "Klipper NOT possible",
        "stock -> extended",
        "source: https://",
    ):
        assert fragment in out.stdout
    as_json = doc(world.fw("profiles", "--json"))["profiles"]
    assert {p["id"] for p in as_json} >= {
        "snapmaker-u1",
        "creality-k1",
        "bambu-lab-p1-a1",
        "bambu-lab-x1",
    }
    bambu = next(p for p in as_json if p["id"] == "bambu-lab-p1-a1")
    assert bambu["capabilities"]["klipper_possible"] is False


def test_status_detects_stock_and_extended_with_read_only_requests(
    world: World,
) -> None:
    """The user asks which firmware a Snapmaker is running: stock, then after the extended build is on, the
    tool says so and shows the probe evidence, having sent nothing but GET requests."""
    world.u1(extended=False)
    stock = doc(world.fw("status", "alpha", "--json"))["printers"][0]
    assert (stock["profile"], stock["state"]) == ("snapmaker-u1", "stock")
    world.u1(extended=True)
    ext = doc(world.fw("status", "alpha", "--json"))["printers"][0]
    assert ext["state"] == "extended" and ext["detection"]["matched"] == ["extended"]
    assert world.moon.mutations() == []
    text = world.fw("status", "alpha", "--verbose").stdout
    assert "alpha (Snapmaker U1, access full): profile snapmaker-u1" in text
    assert "state: extended" in text and "firmware-config" in text


def test_status_all_no_live_resolves_profiles_without_any_request(world: World) -> None:
    """The user lists every fleet printer's profile offline: each printer gets a profile or the explicit
    no-profile message, and no printer is contacted."""
    out = doc(world.fw("status", "--all", "--no-live", "--json"))["printers"]
    by = {e["printer"]: e for e in out}
    assert by["alpha"]["profile"] == "snapmaker-u1"
    assert (
        by["kappa"]["profile"] == "creality-k1"
        and by["gamma"]["profile"] == "bambu-lab-p1-a1"
    )
    assert by["delta"]["profile"] is None
    assert by["delta"]["note"] == "no firmware profile for model Creality CR-200B Pro"
    assert world.moon.requests == [] and world.k1.http_requests == []


def test_status_of_an_offline_printer_never_contacts_it_and_a_missing_name_is_usage(
    world: World,
) -> None:
    """The user checks a neighbour's offline printer (never contacted, the tool says so), forgets the printer
    name (usage error) and mistypes it (error listing the valid names)."""
    out = world.fw("status", "gamma")
    assert "never contacted" in out.stdout
    assert out.returncode == 0 and world.moon.requests == []
    bad = world.fw("status")
    assert "needs a PRINTER or --all" in bad.stderr
    assert bad.returncode == 2
    unknown = world.fw("status", "nobody")
    assert "alpha" in unknown.stderr and "kappa" in unknown.stderr
    assert unknown.returncode == 2


# ---- plan --------------------------------------------------------------------------------------------------


def test_plan_prints_the_runbook_steps_and_never_executes(world: World) -> None:
    """The user plans the move from stock to the extended firmware: the steps, risks, backups and rollback are
    printed from the runbook and nothing is sent to the printer."""
    world.u1(extended=False)
    out = world.fw("plan", "alpha", "--to", "extended")
    assert out.returncode == 0, out.stderr
    for fragment in (
        "Plan [snapmaker-u1]: stock -> extended",
        "local-update-extended-firmware",
        "risk high",
        "backups required:",
        "rollback:",
        "plan only: nothing was executed.",
    ):
        assert fragment in out.stdout
    assert world.moon.mutations() == []
    as_json = doc(world.fw("plan", "alpha", "--to", "extended", "--json"))
    assert as_json["steps"][0]["kind"] == "manual" and as_json["violations"] == []
    assert as_json["detected"]["state"] == "stock"


def test_plan_for_an_offline_printer_needs_from_and_contacts_nothing(
    world: World,
) -> None:
    """The user plans Developer mode for a neighbour's offline Bambu: the tool refuses to detect it, but with an
    assumed starting state it prints the manual steps and contacts nothing."""
    refused = world.fw("plan", "gamma", "--to", "developer-mode")
    assert "offline fleet printer" in refused.stderr
    assert refused.returncode == 2
    out = world.fw("plan", "gamma", "--from", "stock", "--to", "developer-mode")
    assert (
        "enable-lan-only-mode" in out.stdout
        and "enable-developer-mode-on-screen" in out.stdout
    )
    assert out.returncode == 0 and world.moon.requests == []


def test_plan_exit_codes_unknown_state_unreachable_forbidden_and_klipper_on_bambu(
    world: World,
) -> None:
    """The user makes the planning mistakes: an unknown target state, an unreachable one, a forbidden option
    quoting its reason, Klipper on a Bambu (honestly refused) and a missing --to."""
    world.u1(extended=False)
    unknown = world.fw("plan", "alpha", "--to", "warp-drive")
    assert "accepted:" in unknown.stderr
    assert unknown.returncode == 2
    unreachable = world.fw("plan", "gamma", "--from", "developer-mode", "--to", "stock")
    assert "no path from" in unreachable.stderr
    assert unreachable.returncode == 1
    forbidden = world.fw(
        "plan",
        "alpha",
        "--from",
        "extended",
        "--to",
        "extended",
        "--also",
        "enable-require-login",
    )
    assert (
        "REFUSED: guard no-require-login" in forbidden.stdout
        and "force_logins" in forbidden.stdout
    )
    assert forbidden.returncode == 1
    klipper = world.fw("plan", "gamma", "--from", "stock", "--to", "klipper-ready")
    assert "Klipper cannot be installed" in klipper.stderr
    assert klipper.returncode == 2
    no_to = world.fw("plan", "alpha")
    assert "needs --to STATE" in no_to.stderr
    assert no_to.returncode == 2


def test_plan_also_adds_an_optional_step_in_order(world: World) -> None:
    """The user adds two optional firmware-config actions to the plan and gets them in that order, the first one
    as an upload to the documented endpoint."""
    world.u1(extended=True)
    out = doc(
        world.fw(
            "plan",
            "alpha",
            "--to",
            "extended",
            "--also",
            "upgrade-extended-firmware",
            "--also",
            "reset-extended-to-defaults",
            "--json",
        )
    )
    assert [s["id"] for s in out["steps"]] == [
        "upgrade-extended-firmware",
        "reset-extended-to-defaults",
    ]
    assert out["steps"][0]["upload"]["path"] == UPLOAD_PATH


# ---- backup ------------------------------------------------------------------------------------------------


def test_backup_writes_a_manifest_and_files_read_only(world: World) -> None:
    """The user backs up a Snapmaker before the firmware change: the config files and a manifest with hashes land
    in a timestamped directory and the printer only received GET requests."""
    world.u1(extended=True)
    out = world.fw("backup", "alpha", "--dest", str(world.dest), "--json")
    assert out.returncode == 0, out.stderr
    manifest = doc(out)
    assert (manifest["printer"], manifest["profile"], manifest["state"]) == (
        "alpha",
        "snapmaker-u1",
        "extended",
    )
    assert manifest["ok"] is True
    saved = Path(manifest["directory"])
    assert json.loads((saved / "manifest.json").read_text())["items"]
    assert (saved / "SHA256SUMS").is_file()
    by_id = {i["id"]: i for i in manifest["items"]}
    assert by_id["printer-cfg"]["status"] == "ok"
    assert (saved / by_id["printer-cfg"]["file"]).read_text() == "cfg-content"
    assert by_id["full-config-tree"]["status"] == "manual"
    assert world.moon.mutations() == []
    text = world.fw("backup", "alpha", "--dest", str(world.dest))
    assert "ok       printer-cfg" in text.stdout
    assert text.returncode == 0


def test_backup_exits_1_when_a_required_item_cannot_be_read(world: World) -> None:
    """The printer refuses to serve printer.cfg: the backup is reported incomplete, so a later apply cannot use it."""
    world.u1(extended=True)
    world.moon.routes.pop("GET /server/files/config/printer.cfg")
    out = world.fw("backup", "alpha", "--dest", str(world.dest))
    assert "BACKUP INCOMPLETE" in out.stdout and "failed   printer-cfg" in out.stdout
    assert out.returncode == 1


def test_backup_of_an_offline_printer_is_refused(world: World) -> None:
    """The user asks to back up a neighbour's offline printer: the tool refuses instead of contacting it."""
    out = world.fw("backup", "gamma", "--dest", str(world.dest))
    assert "offline fleet printer" in out.stderr and "never contacted" in out.stderr
    assert out.returncode == 1
    assert not world.dest.exists()


# ---- apply ---------------------------------------------------------------------------------------------------


def test_apply_is_a_dry_run_by_default_and_posts_nothing(world: World) -> None:
    """The user runs apply without --yes: it prints what would be POSTed and which gate (the backup) is still
    open, and nothing is sent."""
    world.u1(extended=True)
    out = world.fw("apply", "alpha", "--to", "stock")
    assert out.returncode == 0, out.stderr
    assert "DRY RUN (nothing is changed; add --yes to apply)" in out.stdout
    assert f"POST web:{POST_PATH}" in out.stdout
    assert "gate backup: MISSING" in out.stdout and "gate idle: OK" in out.stdout
    assert "dry run: nothing was changed (not ready" in out.stdout
    assert world.moon.mutations() == []


def test_apply_yes_story_confirm_backup_idle_then_the_documented_post(
    world: World,
) -> None:
    """The user reverts the Snapmaker to stock: no typed name, wrong name, no backup and a printing printer are
    each refused; once backed up, idle and confirmed, exactly the documented POST is sent and the reboot is seen."""
    world.u1(extended=True)
    base = ["apply", "alpha", "--to", "stock", "--yes", "--backup-dir", str(world.dest)]

    no_name = world.fw(*base)
    assert "--confirm alpha" in no_name.stderr
    assert no_name.returncode == 2
    wrong = world.fw(*base, "--confirm", "k1")
    assert "not the printer's name" in wrong.stderr
    assert wrong.returncode == 2

    no_backup = world.fw(*base, "--confirm", "alpha")
    assert "REFUSED: no fresh backup" in no_backup.stdout
    assert no_backup.returncode == 1
    assert world.moon.mutations() == []

    assert world.fw("backup", "alpha", "--dest", str(world.dest)).returncode == 0

    world.u1(extended=True, printing=True)
    busy = world.fw(*base, "--confirm", "alpha")
    assert "is not idle" in busy.stdout and "toolhead MCUs" in busy.stdout
    assert busy.returncode == 1
    assert world.moon.mutations() == []

    world.u1(extended=True)
    done = world.fw(*base, "--confirm", "alpha")
    assert done.returncode == 0, done.stdout + done.stderr
    assert (
        "verified:" in done.stdout
        and "done: alpha is now in state stock" in done.stdout
    )
    assert [(m, p) for m, p, _ in world.moon.mutations()] == [("POST", POST_PATH)]


def test_apply_json_is_a_machine_readable_event_list(world: World) -> None:
    """A script runs apply with --json and parses one document: the result, the states before and after and the
    ordered event list from the start to the done event."""
    world.u1(extended=True)
    assert world.fw("backup", "alpha", "--dest", str(world.dest)).returncode == 0
    out = world.fw(
        "apply",
        "alpha",
        "--to",
        "stock",
        "--yes",
        "--confirm",
        "alpha",
        "--backup-dir",
        str(world.dest),
        "--json",
    )
    assert out.returncode == 0, out.stderr
    result = doc(out)
    assert (result["result"], result["exit_code"], result["printer"]) == (
        "applied",
        0,
        "alpha",
    )
    assert (result["from"], result["final_state"]) == ("extended", "stock")
    kinds = [e["kind"] for e in result["events"]]
    assert kinds[0] == "start" and "gate" in kinds and kinds[-1] == "done"


def test_apply_json_errors_are_json_too(world: World) -> None:
    """With --json even a usage error (a missing --to, an unknown printer) is one JSON document with the exit code."""
    missing = world.fw("apply", "alpha", "--json")
    assert (
        doc(missing)["result"] == "error"
        and "needs --to STATE" in doc(missing)["error"]
    )
    assert missing.returncode == 2
    unknown = world.fw("apply", "nobody", "--to", "stock", "--json")
    assert doc(unknown)["exit_code"] == 2 and "nobody" in doc(unknown)["error"]
    assert unknown.returncode == 2


def test_apply_refuses_offline_printers_unreachable_targets_and_forbidden_options(
    world: World,
) -> None:
    """The user tries to change an offline printer, an impossible target and a forbidden option: each is refused
    before anything is sent to a printer."""
    world.u1(extended=True)
    offline = world.fw(
        "apply", "gamma", "--to", "developer-mode", "--yes", "--confirm", "gamma"
    )
    assert "offline fleet printer" in offline.stdout
    assert offline.returncode == 1
    assert world.fw("backup", "alpha", "--dest", str(world.dest)).returncode == 0
    base = ["--yes", "--confirm", "alpha", "--backup-dir", str(world.dest)]
    forbidden = world.fw(
        "apply", "alpha", "--to", "extended", "--also", "factory-reset", *base
    )
    assert "guard no-touchscreen-factory-reset" in forbidden.stdout
    assert forbidden.returncode == 1
    unreachable = world.fw("apply", "alpha", "--to", "ghost-state", *base)
    assert "accepted:" in unreachable.stderr
    assert unreachable.returncode == 2
    assert world.moon.mutations() == []


def test_apply_refuses_a_stale_backup_and_accepts_it_with_a_longer_age_limit(
    world: World,
) -> None:
    """The user's last backup is 30 hours old: apply refuses it under the 24 h default and accepts it once
    --backup-max-age allows two days; bad ages are usage errors."""
    world.u1(extended=True)
    made = doc(world.fw("backup", "alpha", "--dest", str(world.dest), "--json"))
    manifest_path = Path(made["directory"]) / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["created_epoch"] = time.time() - 30 * 3600
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    base = ["apply", "alpha", "--to", "stock", "--backup-dir", str(world.dest)]

    stale = world.fw(*base, "--yes", "--confirm", "alpha")
    assert (
        "REFUSED: no fresh backup" in stale.stdout
        and "h old; the limit is 24 h" in stale.stdout
    )
    assert stale.returncode == 1
    assert world.moon.mutations() == []

    for bad in ("0s", "nonsense"):
        refused = world.fw(*base, "--backup-max-age", bad)
        assert "--backup-max-age" in refused.stderr
        assert refused.returncode == 2

    ok = world.fw(*base, "--backup-max-age", "48h")
    assert "gate backup: OK" in ok.stdout
    assert ok.returncode == 0


def test_apply_upload_step_needs_its_file_and_sends_it(
    world: World, tmp_path: Path
) -> None:
    """The user upgrades the extended firmware through firmware-config: without --file the tool says what to pass;
    with the image it uploads it, warns that its hash is not in the profile and sees the printer reboot."""
    world.u1(extended=True)
    assert world.fw("backup", "alpha", "--dest", str(world.dest)).returncode == 0
    base = [
        "apply",
        "alpha",
        "--to",
        "extended",
        "--also",
        "upgrade-extended-firmware",
        "--yes",
        "--confirm",
        "alpha",
        "--backup-dir",
        str(world.dest),
    ]
    missing = world.fw(*base)
    assert "--file upgrade-extended-firmware=PATH" in missing.stderr
    assert missing.returncode == 2
    image = tmp_path / "update.bin"
    image.write_bytes(b"\0" * (50 * 1024 * 1024))
    sent = world.fw(*base, "--file", str(image))
    assert sent.returncode == 0, sent.stdout + sent.stderr
    assert any(
        p == UPLOAD_PATH and n > 50 * 1024 * 1024 for _, p, n in world.moon.mutations()
    )
    assert (
        "NOT in the profile's known list" in sent.stdout and "verified:" in sent.stdout
    )


# ---- K1: ssh through the real runner and a fake ssh binary ---------------------------------------------------


def test_k1_status_backup_and_dry_run_use_only_read_only_ssh_commands(
    world: World,
) -> None:
    """The user inspects a rooted Creality K1: status, backup and a dry run over ssh run only read commands, and
    the dry run prints the pinned Helper Script checkout it would run."""
    world.k1_rooted()
    status = doc(world.fw("status", "kappa", "--json"))["printers"][0]
    assert (status["profile"], status["state"]) == ("creality-k1", "rooted")
    backup = world.fw("backup", "kappa", "--dest", str(world.dest), "--json")
    assert backup.returncode == 0, backup.stderr
    items = {i["id"]: i["status"] for i in doc(backup)["items"]}
    assert items["printer-cfg"] == items["config-tree"] == "ok"
    assert (
        items["userdata-tree"] == "missing"
        and items["fluidd-release-info"] == "missing"
    )
    dry = world.fw(
        "apply", "kappa", "--to", "helper-installed", "--backup-dir", str(world.dest)
    )
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert (
        "git checkout -f --detach b46787a" in dry.stdout
        and "gate backup: OK" in dry.stdout
    )
    assert not any("git clone" in c for c in world.ssh_commands())


def test_k1_apply_yes_runs_the_pinned_runbook_script_over_ssh(world: World) -> None:
    """The user installs the Helper Script on a rooted K1: the one documented ssh script runs, pinned to the
    runbook's commit and asserting the remote, and the tool verifies the checkout."""
    world.k1_rooted()
    assert world.fw("backup", "kappa", "--dest", str(world.dest)).returncode == 0
    out = world.fw(
        "apply",
        "kappa",
        "--to",
        "helper-installed",
        "--yes",
        "--confirm",
        "kappa",
        "--backup-dir",
        str(world.dest),
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "done: kappa is now in state helper-installed" in out.stdout
    clones = [c for c in world.ssh_commands() if "git clone" in c]
    assert len(clones) == 1
    script = clones[0]
    assert script.startswith(
        "set -e\nURL=https://github.com/Guilouz/Creality-Helper-Script.git\nping -c1 github.com"
    )
    assert "git checkout -f --detach b46787a" in script and "origin/main" not in script


def test_k1_forbidden_helper_script_action_is_refused_before_any_ssh_write(
    world: World,
) -> None:
    """The user passes the Helper Script's Remove Creality Web Interface action: the tool refuses with the
    runbook's reason before anything is written over ssh."""
    world.k1_rooted()
    assert world.fw("backup", "kappa", "--dest", str(world.dest)).returncode == 0
    out = world.fw(
        "apply",
        "kappa",
        "--to",
        "klipper-ready",
        "--also",
        "remove-creality-web-interface",
        "--yes",
        "--confirm",
        "kappa",
        "--backup-dir",
        str(world.dest),
    )
    assert (
        "Remove Creality Web Interface" in out.stdout
        and "creality-stock adapter" in out.stdout
    )
    assert out.returncode == 1
    assert not any("git clone" in c or "k1drive" in c for c in world.ssh_commands())


def test_k1_with_moonraker_open_to_the_lan_is_not_klipper_ready_and_apply_stops_at_the_lockdown(
    world: World,
) -> None:
    """The install got Moonraker running but it still trusts the whole LAN: status says moonraker-installed, and a
    re-run of the same apply stops at the manual lock-down step instead of reporting nothing to do."""
    world.k1_rooted(lan="open")
    status = doc(world.fw("status", "kappa", "--json"))["printers"][0]
    assert status["state"] == "moonraker-installed"
    assert world.fw("backup", "kappa", "--dest", str(world.dest)).returncode == 0
    out = world.fw(
        "apply",
        "kappa",
        "--to",
        "klipper-ready",
        "--yes",
        "--confirm",
        "kappa",
        "--backup-dir",
        str(world.dest),
    )
    assert "narrow-trusted-clients" in out.stdout and "deny all;" in out.stdout
    assert "NEXT: do the step, then re-run the same command" in out.stdout
    assert out.returncode == 1


def test_k1_locked_down_is_klipper_ready_and_apply_has_nothing_to_do(
    world: World,
) -> None:
    """With trusted_clients narrowed and nginx denying other hosts the K1 reads as klipper-ready and apply says so."""
    world.k1_rooted(lan="locked")
    status = doc(world.fw("status", "kappa", "--json"))["printers"][0]
    assert status["state"] == "klipper-ready"
    assert world.fw("backup", "kappa", "--dest", str(world.dest)).returncode == 0
    out = world.fw(
        "apply",
        "kappa",
        "--to",
        "klipper-ready",
        "--yes",
        "--confirm",
        "kappa",
        "--backup-dir",
        str(world.dest),
    )
    assert "already in state klipper-ready: nothing to do" in out.stdout
    assert out.returncode == 0


def test_no_ssh_refuses_a_plan_that_needs_ssh_and_never_runs_it(world: World) -> None:
    """The user passes --no-ssh to a plan with an ssh step: the dry run says it is not ready, --yes refuses it
    with a clear message and no ssh command is ever run."""
    world.k1_rooted()
    dry = world.fw("apply", "kappa", "--to", "helper-installed", "--no-ssh")
    assert "not ready" in dry.stdout and "drop --no-ssh" in dry.stdout
    assert dry.returncode == 0
    refused = world.fw(
        "apply",
        "kappa",
        "--to",
        "helper-installed",
        "--no-ssh",
        "--yes",
        "--confirm",
        "kappa",
        "--backup-dir",
        str(world.dest),
    )
    assert (
        "--no-ssh forbids the ssh steps of this plan: fetch-helper-script"
        in refused.stdout
    )
    assert refused.returncode == 1
    assert world.ssh_commands() == []


def test_a_missing_ssh_binary_is_exit_127(world: World, tmp_path: Path) -> None:
    """On a machine without ssh the tool says what to install and exits 127 instead of a traceback."""
    empty = tmp_path / "empty-path"
    empty.mkdir()
    world.extra_env["PATH"] = str(empty)
    out = world.fw("status", "kappa")
    assert "ssh not found" in out.stderr and "openssh-client" in out.stderr
    assert "Traceback" not in out.stderr
    assert out.returncode == 127


# ---- help / docs -----------------------------------------------------------------------------------------------


def test_help_and_alias_and_unknown_subcommand(world: World) -> None:
    """The user asks for help, uses the `firmware` alias and mistypes the subcommand: usage is printed, the alias
    works, `3d printers --help` lists fw and the typo lists the accepted subcommands."""
    helped = world.fw("--help")
    assert "3d printers fw <status|plan|backup|apply|profiles>" in helped.stdout
    assert helped.returncode == 0
    assert world.fw("apply", "--help").returncode == 0
    alias = run_cli(
        world.tmp_path,
        "printers",
        "firmware",
        "profiles",
        "--json",
        env_extra=world.env(),
    )
    assert doc(alias)["profiles"]
    assert alias.returncode == 0
    listing = run_cli(world.tmp_path, "printers", "--help")
    assert "fw" in listing.stdout
    bad = world.fw("explode")
    assert "accepted:" in bad.stderr
    assert bad.returncode == 2


def test_every_flag_has_an_example_in_the_help_and_in_the_docs(world: World) -> None:
    """The user reads `3d printers fw --help` or the docs page and finds every flag with a copy-pasteable example."""
    usage = world.fw("--help").stdout
    docs = (REPO_ROOT / "docs" / "commands" / "printers-fw.md").read_text(
        encoding="utf-8"
    )
    for flag in (
        "--all",
        "--json",
        "--no-live",
        "--no-ssh",
        "--verbose",
        "--to",
        "--from",
        "--also",
        "--dest",
        "--yes",
        "--confirm",
        "--backup-dir",
        "--backup-max-age",
        "--file",
    ):
        for where, text in (("help", usage), ("docs", docs)):
            examples = [
                ln for ln in text.splitlines() if flag in ln and "3d printers fw" in ln
            ]
            assert examples, (
                f"{flag} has no `3d printers fw ...` example in the {where}"
            )
