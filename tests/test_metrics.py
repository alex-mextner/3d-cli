from __future__ import annotations

import json
import pathlib

import pytest

import metrics
from errors import InvalidArgument, UsageError


def test_append_record_writes_command_jsonl_and_read_records(tmp_path: pathlib.Path) -> None:
    record = metrics.append_record(
        command="render",
        tool="openscad",
        inputs={"file": "examples/cube.scad", "view": "left"},
        metrics={"IoU": 0.95, "pass": True},
        wall_time=1.25,
        backend="local",
        model="openscad",
        tokens=0,
        cost=0.0,
        timestamp="2026-06-05T12:00:00Z",
        data_dir=tmp_path,
    )

    path = tmp_path / "metrics" / "render.jsonl"
    assert path.is_file()
    assert json.loads(path.read_text().strip()) == record
    assert metrics.read_records(data_dir=tmp_path) == [record]
    assert metrics.read_records(command="render", data_dir=tmp_path) == [record]
    assert metrics.read_records(command="check", data_dir=tmp_path) == []


def test_read_records_is_deterministic_and_limit_is_most_recent(tmp_path: pathlib.Path) -> None:
    metrics.append_record(
        command="check",
        inputs={"file": "b.scad"},
        metrics={"pass": False},
        timestamp="2026-06-05T12:00:02Z",
        data_dir=tmp_path,
    )
    metrics.append_record(
        command="render",
        inputs={"file": "a.scad"},
        metrics={"wall": 1},
        timestamp="2026-06-05T12:00:01Z",
        data_dir=tmp_path,
    )
    metrics.append_record(
        command="render",
        inputs={"file": "c.scad"},
        metrics={"wall": 3},
        timestamp="2026-06-05T12:00:03Z",
        data_dir=tmp_path,
    )

    records = metrics.read_records(data_dir=tmp_path)
    assert [r["timestamp"] for r in records] == [
        "2026-06-05T12:00:01Z",
        "2026-06-05T12:00:02Z",
        "2026-06-05T12:00:03Z",
    ]
    assert [r["inputs"]["file"] for r in metrics.read_records(limit=2, data_dir=tmp_path)] == [
        "b.scad",
        "c.scad",
    ]


def test_list_metric_files_reports_count_and_latest(tmp_path: pathlib.Path) -> None:
    metrics.append_record(
        command="render",
        inputs={"file": "a.scad"},
        metrics={"IoU": 1.0},
        timestamp="2026-06-05T12:00:01Z",
        data_dir=tmp_path,
    )
    metrics.append_record(
        command="render",
        inputs={"file": "b.scad"},
        metrics={"IoU": 0.9},
        timestamp="2026-06-05T12:00:02Z",
        data_dir=tmp_path,
    )

    assert metrics.list_metric_files(data_dir=tmp_path) == [
        {
            "command": "render",
            "records": 2,
            "latest": "2026-06-05T12:00:02Z",
            "path": str(tmp_path / "metrics" / "render.jsonl"),
        }
    ]


def test_read_records_reports_corrupt_jsonl_as_structured_error(tmp_path: pathlib.Path) -> None:
    metric_dir = tmp_path / "metrics"
    metric_dir.mkdir()
    path = metric_dir / "render.jsonl"
    path.write_text("not-json\n", encoding="utf-8")

    with pytest.raises(InvalidArgument) as exc:
        metrics.read_records(data_dir=tmp_path)

    assert exc.value.exit_code == 2
    assert exc.value.flag == "record"
    assert "render.jsonl" in exc.value.got
    assert "Repair or remove the corrupt metrics line" in exc.value.remediation[0]


def test_list_metric_files_reports_corrupt_jsonl_as_structured_error(tmp_path: pathlib.Path) -> None:
    metric_dir = tmp_path / "metrics"
    metric_dir.mkdir()
    path = metric_dir / "render.jsonl"
    path.write_text("{broken\n", encoding="utf-8")

    with pytest.raises(InvalidArgument) as exc:
        metrics.list_metric_files(data_dir=tmp_path)

    assert exc.value.exit_code == 2
    assert "valid JSON object lines" in exc.value.accepted


def test_list_metric_files_rejects_malformed_metric_record(tmp_path: pathlib.Path) -> None:
    metric_dir = tmp_path / "metrics"
    metric_dir.mkdir()
    path = metric_dir / "render.jsonl"
    path.write_text('{"timestamp":"2026-06-05T12:00:00Z"}\n', encoding="utf-8")

    with pytest.raises(InvalidArgument) as exc:
        metrics.list_metric_files(data_dir=tmp_path)

    assert exc.value.exit_code == 2
    assert "timestamp, command, inputs, and metrics fields" in exc.value.accepted


@pytest.mark.parametrize(
    ("kwargs", "flag"),
    [
        ({"command": ""}, "command"),
        ({"inputs": ["not", "a", "dict"]}, "inputs"),
        ({"metrics": {"bad": object()}}, "metrics"),
        ({"wall_time": -0.1}, "wall_time"),
        ({"tokens": 1.5}, "tokens"),
        ({"cost": -1.0}, "cost"),
    ],
)
def test_append_record_validates_basic_types(
    tmp_path: pathlib.Path,
    kwargs: dict[str, object],
    flag: str,
) -> None:
    params: dict[str, object] = {
        "command": "render",
        "inputs": {},
        "metrics": {},
        "data_dir": tmp_path,
    }
    params.update(kwargs)

    with pytest.raises(InvalidArgument) as exc:
        metrics.append_record(**params)  # type: ignore[arg-type]
    assert exc.value.flag == flag


def test_metrics_command_list_and_show_use_xdg_data_home(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    data_dir = tmp_path / "3d-cli"
    metrics.append_record(
        command="render",
        inputs={"file": "a.scad"},
        metrics={"IoU": 1.0},
        timestamp="2026-06-05T12:00:01Z",
        data_dir=data_dir,
    )
    metrics.append_record(
        command="check",
        inputs={"file": "b.scad"},
        metrics={"pass": True},
        timestamp="2026-06-05T12:00:02Z",
        data_dir=data_dir,
    )

    from commands.metrics import run

    assert run(["list"]) == 0
    out = capsys.readouterr().out
    assert "COMMAND" in out
    assert "check" in out
    assert "render" in out

    assert run(["show", "--limit", "1", "--command", "render"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    shown = json.loads(lines[0])
    assert shown["command"] == "render"
    assert shown["metrics"] == {"IoU": 1.0}


def test_metrics_command_help_and_no_args(capsys: pytest.CaptureFixture[str]) -> None:
    from commands.metrics import run

    assert run(["--help"]) == 0
    assert "3d metrics <subcommand>" in capsys.readouterr().out
    assert run([]) == 1
    assert "3d metrics <subcommand>" in capsys.readouterr().out


def test_metrics_command_rejects_bad_limit() -> None:
    from commands.metrics import run

    with pytest.raises(UsageError):
        run(["show", "--limit", "zero"])
