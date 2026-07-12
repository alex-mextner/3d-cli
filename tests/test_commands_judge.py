"""Tests for the `3d judge` command surface — argv parsing, the blind-mock path via
run(), and one real bin/3d invocation driven by the deterministic $THREED_AI_MOCK_RESPONSE
hook (no network, no real model). The mock backend is text-only (supports_images=False),
so the end-to-end path exercises the BLIND surfacing: a text-only judge is labelled, never
passed off as a real visual score."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
from typing import Any

import pytest

from commands.judge import run
from errors import InputNotFound, UsageError

_REPO = pathlib.Path(__file__).resolve().parents[1]
_THREED = _REPO / "bin" / "3d"

_CANNED = json.dumps(
    {
        "silhouette_proportion": 3,
        "feature_completeness": 2,
        "structural_correctness": 4,
        "detail_fidelity": 3,
        "rationale": "canned",
    }
)


def _two_images(tmp_path: pathlib.Path) -> tuple[str, str]:
    render = tmp_path / "render.png"
    reference = tmp_path / "reference.png"
    render.write_bytes(b"\x89PNG\r\n\x1a\n")  # bytes never read by the mock backend
    reference.write_bytes(b"\x89PNG\r\n\x1a\n")
    return str(render), str(reference)


def test_judge_no_args_returns_1() -> None:
    assert run([]) == 1


def test_judge_help_returns_0() -> None:
    assert run(["--help"]) == 0


def test_judge_missing_render_raises() -> None:
    with pytest.raises(InputNotFound):
        run(["/nope/render.png", "/nope/ref.png"])


def test_judge_unknown_option_raises(tmp_path: pathlib.Path) -> None:
    render, reference = _two_images(tmp_path)
    with pytest.raises(UsageError, match="unknown option"):
        run([render, reference, "--bogus"])


def test_judge_bad_stability_n_raises(tmp_path: pathlib.Path) -> None:
    render, reference = _two_images(tmp_path)
    with pytest.raises(UsageError, match="stability-n"):
        run([render, reference, "--stability-n", "notanint"])


def test_judge_mock_path_surfaces_blind_and_masks_numbers(
    tmp_path: pathlib.Path, monkeypatch: Any, capsys: Any
) -> None:
    # $THREED_AI_MOCK_RESPONSE -> MockBackend (text-only). The verdict must be BLIND, and
    # the numeric fields must be masked so a MEAN-keyed consumer can't read a blind score.
    monkeypatch.setenv("THREED_AI_MOCK_RESPONSE", _CANNED)
    render, reference = _two_images(tmp_path)
    assert run([render, reference, "--stability-n", "1"]) == 0
    out = capsys.readouterr().out
    assert "LABEL=blind" in out
    assert "BLIND=true" in out
    assert "MEAN=NA" in out
    assert "MEAN=3.0" not in out
    assert "DIM.silhouette_proportion=NA" in out


def test_judge_sighted_path_emits_numbers_and_ok(
    tmp_path: pathlib.Path, monkeypatch: Any, capsys: Any
) -> None:
    # Cover the non-blind text branch (masked out of the mock-only e2e path): two distinct
    # SIGHTED judges -> real numbers + an 'ok' label + a numeric cross-judge spread.
    class _Sighted:
        supports_images = True
        model = None

        def __init__(self, name: str) -> None:
            self.name = name

        def complete(self, system: str, user: str, images: Any = None, timeout: float = 1200.0) -> str:
            return _CANNED  # silhouette=3 feature=2 structural=4 detail=3 -> mean 3.0

    monkeypatch.setattr(
        "commands.judge._resolve_backends",
        lambda opts: [_Sighted("j1"), _Sighted("j2")],
    )
    render, reference = _two_images(tmp_path)
    assert run([render, reference, "--stability-n", "0", "--backend", "x", "--backend", "y"]) == 0
    out = capsys.readouterr().out
    assert "LABEL=ok" in out
    assert "MEAN=3.0" in out
    assert "DIM.structural_correctness=4.0" in out
    assert "CROSS_JUDGE_SPREAD=0.0" in out
    assert "BLIND=false" in out


def test_judge_json_output_is_wellformed(
    tmp_path: pathlib.Path, monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setenv("THREED_AI_MOCK_RESPONSE", _CANNED)
    render, reference = _two_images(tmp_path)
    assert run([render, reference, "--stability-n", "0", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["label"] == "blind"
    assert payload["per_dim"]["structural_correctness"] == 4.0
    assert payload["mean"] == 3.0
    assert payload["judges"][0]["blind"] is True


def test_judge_equals_form_backend_and_feature_context(
    tmp_path: pathlib.Path, monkeypatch: Any, capsys: Any
) -> None:
    # The `--flag=value` split form must parse for --backend and --feature-context.
    monkeypatch.setenv("THREED_AI_MOCK_RESPONSE", _CANNED)
    render, reference = _two_images(tmp_path)
    rc = run([render, reference, "--backend=mock", "--feature-context=columns; dome", "--stability-n=0"])
    assert rc == 0
    assert "LABEL=blind" in capsys.readouterr().out


def test_judge_config_flag_reads_backend_from_file(
    tmp_path: pathlib.Path, monkeypatch: Any, capsys: Any
) -> None:
    # --config points at an ai.json; the mock env still overrides to the deterministic mock.
    monkeypatch.setenv("THREED_AI_MOCK_RESPONSE", _CANNED)
    cfg = tmp_path / "ai.json"
    cfg.write_text(json.dumps({"backend": "claude"}), encoding="utf-8")
    render, reference = _two_images(tmp_path)
    assert run([render, reference, "--config", str(cfg), "--stability-n", "0"]) == 0
    assert "LABEL=blind" in capsys.readouterr().out


def test_judge_bin_3d_end_to_end_blind(tmp_path: pathlib.Path) -> None:
    """Drive the actual bin/3d dispatcher with the mock hook — proves the command is
    registered, discovered, and emits the BLIND verdict through the real entry point."""
    render, reference = _two_images(tmp_path)
    config_home = tmp_path / "config"
    (config_home / "3d-cli").mkdir(parents=True)
    (config_home / "3d-cli" / ".bootstrapped").write_text("", encoding="utf-8")
    env = dict(os.environ)
    env.update(
        {
            "THREED_AI_MOCK_RESPONSE": _CANNED,
            "XDG_CONFIG_HOME": str(config_home),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "HOME": str(tmp_path / "home"),
        }
    )
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [sys.executable, str(_THREED), "judge", render, reference, "--stability-n", "1"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "LABEL=blind" in proc.stdout
    assert "MEAN=NA" in proc.stdout  # blind score is masked, never a real number
