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


@dataclass
class World:
    tmp_path: Path
    moon: LoopbackHttp = field(default_factory=LoopbackHttp)
    banner: BannerServer = field(default_factory=BannerServer)
    k1: FakeCreality = field(default_factory=FakeCreality)
    extra_env: dict[str, str] = field(default_factory=dict)
    web_is_adapter: bool = False

    @property
    def dest(self) -> Path:
        return self.tmp_path / "backups"

    def fleet(self) -> Path:
        path = self.tmp_path / "fleet.yaml"
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

    def u1(self, *, extended: bool, printing: bool = False) -> None:
        m = self.moon
        m.set("GET /machine/system_info", body={"result": {"system_info": {}}})
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
                return (
                    200,
                    "=== switch-to-backup-firmware ===\nSUCCESS: Completed successfully\n",
                )

            m.set(f"POST {POST_PATH}", body=reboot)

    # -- the K1 world: fake ssh + banner -------------------------------------------------------------

    def k1_rooted(self) -> Path:
        m = self.moon
        m.set("GET /server/info", body={"result": {"klippy_state": "startup"}})
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
                "needle": "test -e /usr/data/helper-script/helper.sh",
                "needs": ["cloned"],
                "rc": 0,
            },
            {"needle": "test -e /usr/data/helper-script/helper.sh", "rc": 1},
            {
                "needle": "git clone --depth 1",
                "sets": ["cloned"],
                "out_b64": base64.b64encode(b"cloned\n").decode(),
            },
            {
                "needle": "cat /usr/data/printer_data/config/printer.cfg",
                "out_b64": base64.b64encode(PRINTER_CFG.encode()).decode(),
            },
            {
                "needle": "tar czf - -C /usr/data printer_data/config",
                "out_b64": base64.b64encode(tree).decode(),
            },
            {
                "needle": "tar czf - -C /usr/data creality/userdata",
                "rc": 2,
                "err": "tar: creality/userdata: No such file or directory",
            },
        ]
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
        return (
            [json.loads(line)["command"] for line in log.read_text().splitlines()]
            if log.exists()
            else []
        )


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
    world.u1(extended=False)
    stock = doc(world.fw("status", "alpha", "--json"))["printers"][0]
    assert (stock["profile"], stock["state"]) == ("snapmaker-u1", "stock")
    world.u1(extended=True)
    ext = doc(world.fw("status", "alpha", "--json"))["printers"][0]
    assert ext["state"] == "extended" and ext["detection"]["matched"] == ["extended"]
    assert world.moon.mutations() == []
    text = world.fw("status", "alpha", "--verbose").stdout
    assert (
        "alpha (Snapmaker U1, access full): profile snapmaker-u1" in text
        and "state: extended" in text
        and "firmware-config" in text
    )


def test_status_all_no_live_resolves_profiles_without_any_request(world: World) -> None:
    out = doc(world.fw("status", "--all", "--no-live", "--json"))["printers"]
    by = {e["printer"]: e for e in out}
    assert (
        by["alpha"]["profile"] == "snapmaker-u1"
        and by["kappa"]["profile"] == "creality-k1"
        and by["gamma"]["profile"] == "bambu-lab-p1-a1"
    )
    assert (
        by["delta"]["profile"] is None
        and by["delta"]["note"] == "no firmware profile for model Creality CR-200B Pro"
    )
    assert world.moon.requests == [] and world.k1.http_requests == []


def test_status_of_an_offline_printer_never_contacts_it_and_a_missing_name_is_usage(
    world: World,
) -> None:
    out = world.fw("status", "gamma")
    assert out.returncode == 0 and "never contacted" in out.stdout
    bad = world.fw("status")
    assert bad.returncode == 2 and "needs a PRINTER or --all" in bad.stderr
    assert world.fw("status", "nobody").returncode == 2


# ---- plan --------------------------------------------------------------------------------------------------


def test_plan_prints_the_runbook_steps_and_never_executes(world: World) -> None:
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
    assert (
        as_json["steps"][0]["kind"] == "manual"
        and as_json["violations"] == []
        and as_json["detected"]["state"] == "stock"
    )


def test_plan_for_an_offline_printer_needs_from_and_contacts_nothing(
    world: World,
) -> None:
    refused = world.fw("plan", "gamma", "--to", "developer-mode")
    assert refused.returncode == 2 and "offline fleet printer" in refused.stderr
    out = world.fw("plan", "gamma", "--from", "stock", "--to", "developer-mode")
    assert (
        out.returncode == 0
        and "enable-lan-only-mode" in out.stdout
        and "enable-developer-mode-on-screen" in out.stdout
    )
    assert world.moon.requests == []


def test_plan_exit_codes_unknown_state_unreachable_forbidden_and_klipper_on_bambu(
    world: World,
) -> None:
    world.u1(extended=False)
    unknown = world.fw("plan", "alpha", "--to", "warp-drive")
    assert unknown.returncode == 2 and "accepted:" in unknown.stderr
    unreachable = world.fw("plan", "gamma", "--from", "developer-mode", "--to", "stock")
    assert unreachable.returncode == 1 and "no path from" in unreachable.stderr
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
        forbidden.returncode == 1
        and "REFUSED: guard no-require-login" in forbidden.stdout
        and "force_logins" in forbidden.stdout
    )
    klipper = world.fw("plan", "gamma", "--from", "stock", "--to", "klipper-ready")
    assert klipper.returncode == 2 and "Klipper cannot be installed" in klipper.stderr
    no_to = world.fw("plan", "alpha")
    assert no_to.returncode == 2 and "needs --to STATE" in no_to.stderr


def test_plan_also_adds_an_optional_step_in_order(world: World) -> None:
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
    assert out["steps"][0]["upload"]["path"] == "/firmware-config/api/upgrade/upload"


# ---- backup ------------------------------------------------------------------------------------------------


def test_backup_writes_a_manifest_and_files_read_only(world: World) -> None:
    world.u1(extended=True)
    out = world.fw("backup", "alpha", "--dest", str(world.dest), "--json")
    assert out.returncode == 0, out.stderr
    manifest = doc(out)
    assert (
        manifest["printer"] == "alpha"
        and manifest["profile"] == "snapmaker-u1"
        and manifest["state"] == "extended"
        and manifest["ok"] is True
    )
    saved = Path(manifest["directory"])
    assert (
        json.loads((saved / "manifest.json").read_text())["items"]
        and (saved / "SHA256SUMS").is_file()
    )
    by_id = {i["id"]: i for i in manifest["items"]}
    assert (
        by_id["printer-cfg"]["status"] == "ok"
        and (saved / by_id["printer-cfg"]["file"]).read_text() == "cfg-content"
    )
    assert by_id["full-config-tree"]["status"] == "manual"
    assert world.moon.mutations() == []
    text = world.fw("backup", "alpha", "--dest", str(world.dest))
    assert text.returncode == 0 and "ok       printer-cfg" in text.stdout


def test_backup_exits_1_when_a_required_item_cannot_be_read(world: World) -> None:
    world.u1(extended=True)
    world.moon.routes.pop("GET /server/files/config/printer.cfg")
    out = world.fw("backup", "alpha", "--dest", str(world.dest))
    assert (
        out.returncode == 1
        and "BACKUP INCOMPLETE" in out.stdout
        and "failed   printer-cfg" in out.stdout
    )


def test_backup_of_an_offline_printer_is_refused(world: World) -> None:
    out = world.fw("backup", "gamma", "--dest", str(world.dest))
    assert out.returncode == 1 and "offline fleet printer" in out.stderr


# ---- apply ---------------------------------------------------------------------------------------------------


def test_apply_is_a_dry_run_by_default_and_posts_nothing(world: World) -> None:
    world.u1(extended=True)
    out = world.fw("apply", "alpha", "--to", "stock")
    assert out.returncode == 0, out.stderr
    assert (
        "DRY RUN (nothing is changed; add --yes to apply)" in out.stdout
        and f"POST web:{POST_PATH}" in out.stdout
    )
    assert (
        "gate backup: MISSING" in out.stdout
        and "gate idle: OK" in out.stdout
        and "dry run: nothing was changed (not ready" in out.stdout
    )
    assert world.moon.mutations() == []


def test_apply_yes_story_confirm_backup_idle_then_the_documented_post(
    world: World,
) -> None:
    world.u1(extended=True)
    base = ["apply", "alpha", "--to", "stock", "--yes", "--backup-dir", str(world.dest)]

    no_name = world.fw(*base)
    assert no_name.returncode == 2 and "--confirm alpha" in no_name.stderr
    wrong = world.fw(*base, "--confirm", "k1")
    assert wrong.returncode == 2 and "not the printer's name" in wrong.stderr

    no_backup = world.fw(*base, "--confirm", "alpha")
    assert no_backup.returncode == 1 and "REFUSED: no fresh backup" in no_backup.stdout
    assert world.moon.mutations() == []

    assert world.fw("backup", "alpha", "--dest", str(world.dest)).returncode == 0

    world.u1(extended=True, printing=True)
    busy = world.fw(*base, "--confirm", "alpha")
    assert (
        busy.returncode == 1
        and "is not idle" in busy.stdout
        and "toolhead MCUs" in busy.stdout
    )
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
    assert (
        result["result"],
        result["exit_code"],
        result["printer"],
        result["from"],
        result["final_state"],
    ) == ("applied", 0, "alpha", "extended", "stock")
    kinds = [e["kind"] for e in result["events"]]
    assert kinds[0] == "start" and "gate" in kinds and kinds[-1] == "done"


def test_apply_refuses_offline_printers_unreachable_targets_and_forbidden_options(
    world: World,
) -> None:
    world.u1(extended=True)
    offline = world.fw(
        "apply", "gamma", "--to", "developer-mode", "--yes", "--confirm", "gamma"
    )
    assert offline.returncode == 1 and "offline fleet printer" in offline.stdout
    assert world.fw("backup", "alpha", "--dest", str(world.dest)).returncode == 0
    base = ["--yes", "--confirm", "alpha", "--backup-dir", str(world.dest)]
    forbidden = world.fw(
        "apply", "alpha", "--to", "extended", "--also", "factory-reset", *base
    )
    assert (
        forbidden.returncode == 1
        and "guard no-touchscreen-factory-reset" in forbidden.stdout
    )
    unreachable = world.fw("apply", "alpha", "--to", "ghost-state", *base)
    assert unreachable.returncode == 2
    assert world.moon.mutations() == []


def test_apply_stops_when_the_backup_is_older_than_the_allowed_age(
    world: World,
) -> None:
    world.u1(extended=True)
    assert world.fw("backup", "alpha", "--dest", str(world.dest)).returncode == 0
    for flag in ("0s", "nonsense", "-1h"):
        assert (
            world.fw(
                "apply", "alpha", "--to", "stock", "--backup-max-age", flag
            ).returncode
            == 2
        )
    ok = world.fw(
        "apply",
        "alpha",
        "--to",
        "stock",
        "--backup-dir",
        str(world.dest),
        "--backup-max-age",
        "90m",
    )
    assert ok.returncode == 0 and "gate backup: OK" in ok.stdout


def test_apply_upload_step_needs_its_file_and_sends_it(
    world: World, tmp_path: Path
) -> None:
    world.u1(extended=True)
    world.moon.set(
        "POST /firmware-config/api/upgrade/upload",
        body="=== Upgrade Started ===\nSUCCESS: Completed successfully.\n",
    )
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
    assert (
        missing.returncode == 2
        and "--file upgrade-extended-firmware=PATH" in missing.stderr
    )
    image = tmp_path / "update.bin"
    image.write_bytes(b"\0" * (50 * 1024 * 1024))
    sent = world.fw(*base, "--file", str(image))
    assert sent.returncode == 0, sent.stdout + sent.stderr
    assert any(
        p == "/firmware-config/api/upgrade/upload" and n > 50 * 1024 * 1024
        for _, p, n in world.moon.mutations()
    )
    assert "NOT in the profile's known list" in sent.stdout


# ---- K1: ssh through the real runner and a fake ssh binary ---------------------------------------------------


def test_k1_status_backup_and_dry_run_use_only_read_only_ssh_commands(
    world: World,
) -> None:
    world.k1_rooted()
    status = doc(world.fw("status", "kappa", "--json"))["printers"][0]
    assert (status["profile"], status["state"]) == ("creality-k1", "rooted")
    backup = world.fw("backup", "kappa", "--dest", str(world.dest), "--json")
    assert backup.returncode == 0, backup.stderr
    items = {i["id"]: i["status"] for i in doc(backup)["items"]}
    assert (
        items["printer-cfg"] == items["config-tree"] == "ok"
        and items["userdata-tree"] == "missing"
    )
    dry = world.fw(
        "apply", "kappa", "--to", "helper-installed", "--backup-dir", str(world.dest)
    )
    assert dry.returncode == 0, dry.stdout + dry.stderr
    assert (
        "git clone --depth 1 https://github.com/Guilouz/Creality-Helper-Script.git"
        in dry.stdout
        and "gate backup: OK" in dry.stdout
    )
    assert not any("git clone" in c for c in world.ssh_commands())


def test_k1_apply_yes_runs_the_runbook_script_over_ssh(world: World) -> None:
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
    assert (
        len(clones) == 1
        and clones[0].startswith("set -e\nping -c1 github.com")
        and "git fetch origin main" in clones[0]
    )


def test_k1_forbidden_helper_script_action_is_refused_before_any_ssh_write(
    world: World,
) -> None:
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
        out.returncode == 1
        and "Remove Creality Web Interface" in out.stdout
        and "creality-stock adapter" in out.stdout
    )
    assert not any("git clone" in c or "k1drive" in c for c in world.ssh_commands())


def test_a_missing_ssh_binary_is_exit_127(world: World, tmp_path: Path) -> None:
    empty = tmp_path / "empty-path"
    empty.mkdir()
    world.extra_env["PATH"] = str(empty)
    out = world.fw("status", "kappa")
    assert (
        out.returncode == 127
        and "ssh not found" in out.stderr
        and "openssh-client" in out.stderr
    )


# ---- help / docs -----------------------------------------------------------------------------------------------


def test_help_and_alias_and_unknown_subcommand(world: World) -> None:
    helped = world.fw("--help")
    assert (
        helped.returncode == 0
        and "3d printers fw <status|plan|backup|apply|profiles>" in helped.stdout
    )
    assert world.fw("apply", "--help").returncode == 0
    alias = run_cli(
        world.tmp_path,
        "printers",
        "firmware",
        "profiles",
        "--json",
        env_extra=world.env(),
    )
    assert alias.returncode == 0 and doc(alias)["profiles"]
    listing = run_cli(world.tmp_path, "printers", "--help")
    assert "fw" in listing.stdout
    bad = world.fw("explode")
    assert bad.returncode == 2 and "accepted:" in bad.stderr


def test_every_flag_has_an_example_in_the_help_and_in_the_docs(world: World) -> None:
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
