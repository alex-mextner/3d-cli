"""`3d calibrate record|ls --mix` end to end: bin/3d → real uvicorn 3d-api (#412, plan §20.4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fleet_fixtures import REPO_FLEET
from printops_fixtures import LiveApi

from .workflow_helper import run_cli


@pytest.fixture(autouse=True)
def repo_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THREED_FLEET", str(REPO_FLEET))
    monkeypatch.delenv("PRINTOPS_POLICY", raising=False)
    monkeypatch.delenv("PRINTOPS_CALIBRATIONS", raising=False)


def test_user_records_a_measured_mix_and_finds_it_by_any_rotation(
    tmp_path: Path, live_api: LiveApi
) -> None:
    """The user read a U1 tester: mix 431 on filaments 12, 7, 3 measured #B5654F. A re-measure
    written with the pattern started elsewhere replaces it; another mix stays; ls --mix finds the
    record by any rotation of the cycle; plan for the U1 does not ask for colour calibrations."""
    env = {"PRINTOPS_API": live_api.url, "PRINTOPS_TOKEN": live_api.tokens["rw"]}

    def record(mix: str, values: str) -> str:
        r = run_cli(
            tmp_path,
            "calibrate",
            "record",
            "mix_color",
            "--printer",
            "u1",
            "--mix",
            mix,
            "--values",
            values,
            "--method",
            "mix_tester_photo",
            env_extra=env,
        )
        assert r.returncode == 0, r.stdout + r.stderr
        return r.stdout

    first = record(
        "h0.12:f12.f7.f3", "hex=#b5654f,predicted_hex=#A86A55,delta_e_predicted=6.1"
    )
    assert first.startswith("recorded #1 mix_color u1 mix h0.12:f3.f12.f7: ")
    assert "hex=#B5654F" in first
    record("h0.12:f1.f4", "hex=#808080")
    record("h0.12:f7.f3.f12", "hex=#B26250")

    listed = run_cli(
        tmp_path,
        "calibrate",
        "ls",
        "--kind",
        "mix_color",
        "--mix",
        "h0.12:f3.f12.f7",
        "--json",
        env_extra=env,
    )
    assert listed.returncode == 0, listed.stderr
    rows = json.loads(listed.stdout)["calibrations"]
    assert [(r["id"], r["mix"], r["values"]["hex"]) for r in rows] == [
        (3, "h0.12:f3.f12.f7", "#B26250")
    ]
    everything = run_cli(
        tmp_path, "calibrate", "ls", "--kind", "mix_color", "--all", env_extra=env
    )
    assert (
        "superseded by #3" in everything.stdout
        and everything.stdout.count("mix h0.12:") == 3
    )

    bad = run_cli(
        tmp_path,
        "calibrate",
        "record",
        "mix_color",
        "--printer",
        "u1",
        "--mix",
        "431",
        "--values",
        "hex=#FFFFFF",
        env_extra=env,
    )
    assert bad.returncode != 0 and "not a mix key" in bad.stderr

    plan = run_cli(
        tmp_path,
        "calibrate",
        "plan",
        "--printer",
        "u1",
        "--all",
        "--json",
        env_extra=env,
    )
    assert plan.returncode == 0, plan.stderr
    assert not {i["kind"] for i in json.loads(plan.stdout)["items"]} & {
        "mix_color",
        "filament_color",
    }
