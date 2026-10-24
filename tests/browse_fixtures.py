"""Shared fixtures for the model browser tests (#578): a REAL ranking pipeline run offline (a scripted catalogue and a
scripted model backend, the S6 mock backend), its recorded events, fake slicer / browser / agent executables, and a
fake `actions.invoke` that records what a key asked for. Nothing here touches the network or starts a real program."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from llm_fixtures import FakeCatalog, ScriptedBackend, model, verdicts_json

from library import actions, rankflow
from library.browse_run import PipelineSource
from library import search as searchcore
from library.llm import LlmConfig

QUERY = "soil moisture sensor probe case"
SITES = ("printables", "thingiverse")

PLAN = {
    "schema": "3d.models.plan/1",
    "summary": "a descriptive phrase became part names",
    "queries": [
        {"query": "sensor enclosure", "kind": "wide", "rationale": "the category"},
        {
            "query": "capacitive probe case",
            "kind": "narrow",
            "rationale": "the exact part",
        },
    ],
    "followups": [
        {"query": "plant stake sensor holder", "rationale": "a stake variant"},
        {"query": "greenhouse sensor mount", "rationale": "a mount instead"},
    ],
    "questions": [
        {
            "key": "need_cad",
            "type": "single",
            "prompt": "Do you need CAD files?",
            "choices": ["yes", "no"],
        },
        {
            "key": "extras",
            "type": "multi",
            "prompt": "Which extras?",
            "choices": ["lid", "stake", "cable gland"],
        },
        {"key": "printer", "type": "text", "prompt": "Which printer?"},
    ],
}


def verdict_for(card: dict[str, Any]) -> tuple[str, float, str]:
    title = str(card["title"]).lower()
    if "keychain" in title:
        return "no", 0.05, "a keychain, not a case"
    if "probe" in title and card["n"] % 2 == 1:
        return "fits", 0.9 - card["n"] / 100, "a case for a probe"
    return "maybe", 0.5, "a generic case"


def handler(role: str, _model: str, user: str) -> str:
    if role == "classify":
        return verdicts_json(user, verdict_for)
    return json.dumps(PLAN)


def catalog() -> FakeCatalog:
    sensor = {
        "printables": [
            model("printables", 100 + i, f"Sensor enclosure {i}") for i in range(1, 3)
        ],
        "thingiverse": [],
    }
    stake = {
        "printables": [model("printables", 200, "Plant stake sensor holder")],
        "thingiverse": [],
    }
    return FakeCatalog(
        {
            QUERY: {
                "printables": [
                    model("printables", i, f"Soil probe case {i}") for i in range(1, 5)
                ],
                "thingiverse": [
                    model("thingiverse", i, f"Keychain {i}") for i in range(1, 3)
                ],
            },
            "sensor enclosure": sensor,
            "plant stake sensor holder": stake,
        }
    )


def backend() -> ScriptedBackend:
    return ScriptedBackend(handler)


def make_source(
    monkeypatch: pytest.MonkeyPatch,
    db_path: str,
    *,
    llm: bool = True,
    scripted: ScriptedBackend | None = None,
) -> PipelineSource:
    """A `PipelineSource` over the real `rankflow.Pipeline` with the scripted catalogue and model."""
    monkeypatch.setattr("marketplaces.catalog.search", catalog())
    rank = rankflow.RankOptions(LlmConfig(), enabled=llm, backend=scripted or backend())
    return PipelineSource(
        rank, searchcore.SearchOptions(sites=SITES, limit=50, db_path=db_path)
    )


def seeded(
    monkeypatch: pytest.MonkeyPatch, db_path: str
) -> tuple[list[dict[str, Any]], str]:
    """Run one ranked search into `db_path`; returns its events and the root session uid."""
    source = make_source(monkeypatch, db_path)
    events = list(source.search(QUERY))
    return events, str(events[-1]["search"])


class FakeActions:
    """A stand-in for `actions.invoke` that records `(name, kwargs)` and answers with a canned ok result, so a test
    asserts which action a key called with which selection without starting anything. With `download_root` a
    download really creates `<root>/<site>-<id>/part.stl`, so the slicer step finds a file."""

    def __init__(self, download_root: Path | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.download_root = download_root

    def __call__(self, name: str, **kwargs: Any) -> actions.ActionResult:
        self.calls.append((name, kwargs))
        data: dict[str, Any] = {"fake": True}
        if name == "download":
            tail = kwargs["url"].replace("https://", "").replace(".example/model/", "-")
            folder = (self.download_root or Path("/nonexistent")) / tail
            if self.download_root is not None:
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "part.stl").write_bytes(b"solid")
            data = {
                "source": None,
                "dir": str(folder),
                "files": [{"name": "part.stl", "size": 5, "sha256": "x"}],
                "license": None,
                "role": None,
            }
        elif name == "preview":
            card = kwargs["card"]
            data = {
                "site": card.get("site"),
                "id": card.get("id"),
                "title": card.get("title"),
                "url": card.get("url"),
                "image": {"url": None, "protocol": None, "sequence": None},
                "files": card.get("files", []),
                "analytics": {},
            }
        elif name == "open_slicer":
            data = {
                "slicer": "fake",
                "command": ["fake-slicer"],
                "paths": kwargs["path"],
                "started": not kwargs["dry_run"],
                "pid": 1,
            }
        elif name == "launch_agent":
            data = {
                "backend": kwargs["backend"] or "omp",
                "command": ["fake-agent"],
                "cwd": kwargs["cwd"],
                "mode": kwargs["mode"],
                "brief": "/brief.md",
                "started": not kwargs["dry_run"],
            }
        elif name == "open_browser":
            data = {
                "urls": kwargs["urls"],
                "commands": [],
                "started": not kwargs["dry_run"],
            }
        return actions.ok_result(name, data)

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]
