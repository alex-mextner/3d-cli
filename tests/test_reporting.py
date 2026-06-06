from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from reporting import build_report, render_json, report_to_json_data, render_text
from errors import UsageError

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_THREED = os.path.join(_REPO, "bin", "3d")


def test_build_report_composes_gate_log_and_score_metrics(tmp_path: Path) -> None:
    root = tmp_path
    check_log = root / "check.log"
    score_log = root / "score.log"
    check_log.write_text(
        "\n".join(
            [
                "=== check (acceptance gate) ===",
                "  MANIFOLD    PASS  1 file(s) clean (mesh-verified)",
                "  PRINTABILITY FAIL  >>> PRINTABILITY: FAIL  (thin walls)",
                ">>> CHECK: FAIL",
            ]
        )
        + "\n"
    )
    score_log.write_text(
        "\n".join(
            [
                "AE=12",
                "AE_NORM=0.000125",
                "IoU=0.8750",
                "OVERLAY=/tmp/overlay.png",
            ]
        )
        + "\n"
    )

    report = build_report([str(score_log), str(check_log)], title="Nightly gate")
    data = report_to_json_data(report)

    assert data["title"] == "Nightly gate"
    assert data["overall"] == "FAIL"
    assert data["gates"] == [
        {
            "name": "MANIFOLD",
            "status": "PASS",
            "source": str(check_log),
            "detail": "1 file(s) clean (mesh-verified)",
        },
        {
            "name": "PRINTABILITY",
            "status": "FAIL",
            "source": str(check_log),
            "detail": ">>> PRINTABILITY: FAIL  (thin walls)",
        },
        {"name": "CHECK", "status": "FAIL", "source": str(check_log), "detail": ""},
    ]
    assert data["values"] == [
        {"name": "AE", "value": 12, "source": str(score_log)},
        {"name": "AE_NORM", "value": 0.000125, "source": str(score_log)},
        {"name": "IoU", "value": 0.875, "source": str(score_log)},
        {"name": "OVERLAY", "value": "/tmp/overlay.png", "source": str(score_log)},
    ]


def test_build_report_reads_jsonl_metrics_and_gates_deterministically(tmp_path: Path) -> None:
    root = tmp_path
    metrics = root / "metrics.jsonl"
    metrics.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "command": "match",
                        "round": 2,
                        "metrics": {"IoU": 0.72, "AE": 110},
                        "gates": [{"name": "MANIFOLD", "status": "PASS"}],
                    }
                ),
                json.dumps(
                    {
                        "tool": "check",
                        "gate": "PRINTABILITY",
                        "status": "FAIL",
                        "detail": "overhang exceeds threshold",
                    }
                ),
            ]
        )
        + "\n"
    )

    report = build_report([str(metrics)])
    data = report_to_json_data(report)

    assert data["overall"] == "FAIL"
    assert data["artifacts"] == [{"path": str(metrics), "kind": "jsonl", "records": 2}]
    assert data["gates"] == [
        {"name": "MANIFOLD", "status": "PASS", "source": str(metrics), "detail": ""},
        {
            "name": "PRINTABILITY",
            "status": "FAIL",
            "source": str(metrics),
            "detail": "overhang exceeds threshold",
        },
    ]
    assert data["values"] == [
        {"name": "AE", "value": 110, "source": str(metrics)},
        {"name": "IoU", "value": 0.72, "source": str(metrics)},
    ]


def test_render_text_is_stable_and_human_readable(tmp_path: Path) -> None:
    root = tmp_path
    log = root / "score.log"
    log.write_text("IoU=0.9100\nAE=44\n>>> CHECK: PASS\n")

    text = render_text(build_report([str(log)], title="Run 42"))

    assert text.splitlines() == [
        "Run 42",
        "Overall: PASS",
        "",
        "Gates:",
        f"- CHECK PASS ({log})",
        "",
        "Values:",
        f"- AE=44 ({log})",
        f"- IoU=0.91 ({log})",
    ]


def test_separator_gate_rows_are_preserved_as_info(tmp_path: Path) -> None:
    log = tmp_path / "check.log"
    log.write_text("SUPPORT ---- not applicable\n")

    data = report_to_json_data(build_report([str(log)]))

    assert data["overall"] == "INFO"
    assert data["gates"] == [
        {"name": "SUPPORT", "status": "INFO", "source": str(log), "detail": "not applicable"}
    ]


def test_warning_only_report_preserves_warn_overall(tmp_path: Path) -> None:
    log = tmp_path / "check.log"
    log.write_text("PRINTABILITY WARN brim suggested\n")

    data = report_to_json_data(build_report([str(log)]))

    assert data["overall"] == "WARN"
    assert data["gates"] == [
        {"name": "PRINTABILITY", "status": "WARN", "source": str(log), "detail": "brim suggested"}
    ]


def test_unreadable_artifact_raises_structured_error(tmp_path: Path) -> None:
    log = tmp_path / "binary.log"
    log.write_bytes(b"\xff\xfe\x00")

    try:
        build_report([str(log)])
    except UsageError as exc:
        assert exc.command == "report"
        assert "could not read artifact" in str(exc)
    else:
        raise AssertionError("expected UsageError")


def test_report_command_outputs_json(tmp_path: Path) -> None:
    log = tmp_path / "score.log"
    log.write_text("IoU=0.5000\n>>> CHECK: PASS\n")
    env = dict(os.environ)
    env["REPO_ROOT"] = _REPO

    result = subprocess.run(
        [sys.executable, _THREED, "report", "--json", str(log)],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["overall"] == "PASS"
    assert data["values"] == [{"name": "IoU", "value": 0.5, "source": str(log)}]


def test_generated_report_json_can_be_used_as_input_without_losing_values(tmp_path: Path) -> None:
    log = tmp_path / "score.log"
    log.write_text("IoU=0.5000\n>>> CHECK: PASS\n")
    report_json = tmp_path / "report.json"
    report_json.write_text(render_json(build_report([str(log)])))

    data = report_to_json_data(build_report([str(report_json)]))

    assert data["overall"] == "PASS"
    assert data["gates"] == [
        {"name": "CHECK", "status": "PASS", "source": str(report_json), "detail": ""}
    ]
    assert data["values"] == [{"name": "IoU", "value": 0.5, "source": str(report_json)}]


def test_empty_json_artifact_raises_structured_error(tmp_path: Path) -> None:
    report_json = tmp_path / "report.json"
    report_json.write_text("")

    try:
        build_report([str(report_json)])
    except UsageError as exc:
        assert exc.command == "report"
        assert "invalid JSON" in str(exc)
    else:
        raise AssertionError("expected UsageError")
