"""`3d judge --rubric / --min-dim / --max-mean`: a per-feature rubric from a file and gates on the mean of all reads."""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from commands.judge import run
from errors import GateFailure, InputNotFound, UsageError

RUBRIC = [
    {
        "key": "nostril_shape",
        "anchor_0": "no nostrils",
        "anchor_2": "blobs",
        "anchor_4": "tight comma slits",
    },
    {
        "key": "nostril_colour",
        "label": "Nostril colour",
        "anchor_0": "black",
        "anchor_2": "too dark",
        "anchor_4": "slightly darker than the nose",
    },
]


class _Scripted:
    """A sighted judge that answers each call from a script: canonical first, then the stability samples."""

    supports_images = True
    model = None
    name = "scripted"

    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = replies
        self.calls = 0
        self.systems: list[str] = []

    def complete(
        self,
        system: str,
        user: str,
        images: Any = None,
        timeout: float = 1200.0,
        temperature: float | None = None,
    ) -> str:
        self.systems.append(system)
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return json.dumps(reply)


def _setup(
    tmp_path: pathlib.Path, monkeypatch: Any, replies: list[dict[str, Any]]
) -> tuple[list[str], _Scripted]:
    images = []
    for name in ("render.png", "reference.png"):
        (tmp_path / name).write_bytes(b"\x89PNG\r\n\x1a\n")
        images.append(str(tmp_path / name))
    (tmp_path / "rubric.json").write_text(json.dumps(RUBRIC), encoding="utf-8")
    backend = _Scripted(replies)
    monkeypatch.setattr("commands.judge._resolve_backends", lambda opts: [backend])
    return images, backend


def _scores(shape: float, colour: float, **extra: Any) -> dict[str, Any]:
    return {"nostril_shape": shape, "nostril_colour": colour, **extra}


def test_rubric_file_replaces_the_builtin_dimensions(
    tmp_path: pathlib.Path, monkeypatch: Any, capsys: Any
) -> None:
    images, backend = _setup(tmp_path, monkeypatch, [_scores(3, 4)])
    assert (
        run([*images, "--rubric", str(tmp_path / "rubric.json"), "--stability-n", "0"])
        == 0
    )
    out = capsys.readouterr().out
    assert "DIM.nostril_shape=3.0" in out and "DIM.nostril_colour=4.0" in out
    assert "DIM.silhouette_proportion" not in out
    assert (
        "tight comma slits" in backend.systems[0]
        and "Nostril colour" in backend.systems[0]
    )  # the anchors reached the prompt


def test_runs_mean_averages_the_canonical_read_and_every_stability_sample(
    tmp_path: pathlib.Path, monkeypatch: Any, capsys: Any
) -> None:
    images, _ = _setup(
        tmp_path, monkeypatch, [_scores(4, 4), _scores(2, 3), _scores(3, 2)]
    )
    assert (
        run([*images, "--rubric", str(tmp_path / "rubric.json"), "--stability-n", "2"])
        == 0
    )
    out = capsys.readouterr().out
    assert "DIM.nostril_shape=4.0" in out  # the canonical read alone
    assert (
        "RUNS_DIM.nostril_shape=3.0" in out and "RUNS_DIM.nostril_colour=3.0" in out
    )  # (4 + 2 + 3) / 3, (4 + 3 + 2) / 3
    assert "RUNS_MEAN=3.0" in out and "RUNS=3" in out


def test_min_dim_gate_passes_on_the_mean_of_all_reads_and_fails_below_it(
    tmp_path: pathlib.Path, monkeypatch: Any, capsys: Any
) -> None:
    images, backend = _setup(
        tmp_path, monkeypatch, [_scores(4, 4), _scores(2, 3), _scores(3, 2)]
    )
    base = [*images, "--rubric", str(tmp_path / "rubric.json"), "--stability-n", "2"]
    assert run([*base, "--min-dim", "3"]) == 0
    assert "GATE=PASS" in capsys.readouterr().out
    backend.calls = 0  # the same three reads again
    with pytest.raises(
        GateFailure, match=r"nostril_shape 3\.0 < --min-dim 3\.5"
    ) as err:
        run([*base, "--min-dim", "3.5"])
    assert "nostril_colour 3.0 < --min-dim 3.5" in str(err.value)
    assert "GATE=FAIL" in capsys.readouterr().out


def test_one_good_canonical_read_does_not_pass_a_gate_the_samples_fail(
    tmp_path: pathlib.Path, monkeypatch: Any, capsys: Any
) -> None:
    images, _ = _setup(
        tmp_path, monkeypatch, [_scores(4, 4), _scores(1, 1), _scores(1, 1)]
    )
    with pytest.raises(GateFailure, match="--min-dim 3"):
        run(
            [
                *images,
                "--rubric",
                str(tmp_path / "rubric.json"),
                "--stability-n",
                "2",
                "--min-dim",
                "3",
            ]
        )
    assert (
        "DIM.nostril_shape=4.0" in capsys.readouterr().out
    )  # the canonical read alone would have passed


def test_max_mean_gate_is_a_negative_control(
    tmp_path: pathlib.Path, monkeypatch: Any, capsys: Any
) -> None:
    images, _ = _setup(tmp_path, monkeypatch, [_scores(1, 0)])
    base = [*images, "--rubric", str(tmp_path / "rubric.json"), "--stability-n", "0"]
    assert run([*base, "--max-mean", "2"]) == 0
    capsys.readouterr()
    images, _ = _setup(
        tmp_path, monkeypatch, [_scores(4, 4)]
    )  # a judge that rates unrelated views high is broken
    with pytest.raises(GateFailure, match=r"mean 4\.0 > --max-mean 2"):
        run([*base, "--max-mean", "2"])


def test_a_blind_verdict_cannot_satisfy_a_gate(
    tmp_path: pathlib.Path, monkeypatch: Any, capsys: Any
) -> None:
    for name in ("render.png", "reference.png"):
        (tmp_path / name).write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setenv(
        "THREED_AI_MOCK_RESPONSE",
        json.dumps(
            {
                "silhouette_proportion": 4,
                "feature_completeness": 4,
                "structural_correctness": 4,
                "detail_fidelity": 4,
            }
        ),
    )
    with pytest.raises(GateFailure, match="blind verdict"):
        run(
            [
                str(tmp_path / "render.png"),
                str(tmp_path / "reference.png"),
                "--stability-n",
                "0",
                "--min-dim",
                "3",
            ]
        )
    out = capsys.readouterr().out
    assert "RUNS_MEAN=NA" in out and "GATE=FAIL" in out


def test_without_a_gate_the_exit_is_zero_whatever_the_scores(
    tmp_path: pathlib.Path, monkeypatch: Any, capsys: Any
) -> None:
    images, _ = _setup(tmp_path, monkeypatch, [_scores(0, 0)])
    assert (
        run([*images, "--rubric", str(tmp_path / "rubric.json"), "--stability-n", "0"])
        == 0
    )
    assert "GATE=" not in capsys.readouterr().out


def test_rationale_is_surfaced_when_the_reply_has_one(
    tmp_path: pathlib.Path, monkeypatch: Any, capsys: Any
) -> None:
    images, _ = _setup(
        tmp_path, monkeypatch, [_scores(2, 3, rationale="slits are too long")]
    )
    assert (
        run([*images, "--rubric", str(tmp_path / "rubric.json"), "--stability-n", "0"])
        == 0
    )
    assert "RATIONALE=slits are too long" in capsys.readouterr().out


def test_json_output_carries_all_runs_rationale_and_gate(
    tmp_path: pathlib.Path, monkeypatch: Any, capsys: Any
) -> None:
    images, _ = _setup(
        tmp_path, monkeypatch, [_scores(4, 4, rationale="ok"), _scores(2, 2)]
    )
    assert (
        run(
            [
                *images,
                "--rubric",
                str(tmp_path / "rubric.json"),
                "--stability-n",
                "1",
                "--min-dim",
                "3",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["all_runs"] == {
        "runs": 2,
        "per_dim": {"nostril_shape": 3.0, "nostril_colour": 3.0},
        "mean": 3.0,
    }
    assert payload["rationale"] == "ok"
    assert payload["gate"] == {"status": "PASS", "failures": []}


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("not json", "not valid JSON"),
        ("{}", "non-empty JSON list"),
        ("[]", "non-empty JSON list"),
        (
            json.dumps([{"key": "a", "anchor_0": "x", "anchor_2": "y"}]),
            "dimension 0 needs",
        ),
        (
            json.dumps(
                [{"key": "", "anchor_0": "x", "anchor_2": "y", "anchor_4": "z"}]
            ),
            "dimension 0 needs",
        ),
        (json.dumps([RUBRIC[0], RUBRIC[0]]), "duplicate dimension keys"),
    ],
)
def test_bad_rubric_files_are_usage_errors(
    tmp_path: pathlib.Path, monkeypatch: Any, content: str, message: str
) -> None:
    images, _ = _setup(tmp_path, monkeypatch, [_scores(3, 3)])
    (tmp_path / "bad.json").write_text(content, encoding="utf-8")
    with pytest.raises(UsageError, match=message):
        run([*images, "--rubric", str(tmp_path / "bad.json")])


def test_missing_rubric_file_and_bad_gate_numbers_are_input_errors(
    tmp_path: pathlib.Path, monkeypatch: Any
) -> None:
    images, _ = _setup(tmp_path, monkeypatch, [_scores(3, 3)])
    with pytest.raises(InputNotFound):
        run([*images, "--rubric", str(tmp_path / "nope.json")])
    with pytest.raises(UsageError, match="--min-dim must be a number"):
        run([*images, "--min-dim", "high"])
    with pytest.raises(UsageError, match="--max-mean must be a number"):
        run([*images, "--max-mean=low"])
