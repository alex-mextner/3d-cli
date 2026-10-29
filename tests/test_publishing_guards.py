"""Regression tests for the publish planning/offer guards (PR #620 review): source identity, make targets and
browser sessions, and what the offer reports and replays. No site is contacted."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

import pytest

from errors import InvalidArgument, UsageError
from library import db as librarydb
from library import records
from publishing import execute, flow, offer_run, plan
from publishing.source import SourceRef, load_source
from publish_fixtures import YAML, make_project

PRINTABLES = "https://www.printables.com/model/311253-spoon-drawer-pull"
CULTS = "https://cults3d.com/en/3d-model/home/spoon-pull"


@pytest.fixture(autouse=True)
def hermetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var, sub in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
    ):
        monkeypatch.setenv(var, str(tmp_path / sub))
    monkeypatch.setenv("THREED_LIBRARY_DB", str(tmp_path / "library.db"))
    monkeypatch.setenv("THREED_KEYCHAIN", "off")
    monkeypatch.delenv("THREED_PUBLISH_OFFER", raising=False)


def no_network(url: str) -> str:
    raise AssertionError(f"the marketplace must not be asked for {url}")


def write_model_json(
    directory: Path,
    *,
    site: str = "printables",
    model_id: str = "311253",
    url: str = PRINTABLES,
    license: str = "CC BY 4.0",
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "model.json"
    path.write_text(
        json.dumps(
            {
                "source": {"site": site, "id": model_id, "url": url},
                "title": "Spoon pull",
                "author": {"name": "ann"},
                "license": license,
            }
        ),
        encoding="utf-8",
    )
    return path


def source(url: str, site: str = "printables", model_id: str = "311253") -> SourceRef:
    return SourceRef(url, site, model_id, "", "", "", "url")


def photo(tmp_path: Path, name: str = "table.jpg") -> plan.Photo:
    path = tmp_path / name
    path.write_bytes(b"jpg")
    return plan.Photo(path, "table")


def make_eval(project: Path, platforms: str = "all", **opts: Any) -> plan.Evaluation:
    return plan.evaluate(
        project,
        plan.platform_list(platforms),
        no_network,
        plan.Options(kind="make", platform_all=platforms == "all", **opts),
    )


# ------------------------------------------------------------------------------------------ source identity
@pytest.mark.parametrize(
    "fields",
    [
        {"url": "https://attacker.example/upload"},
        {"url": "https://www.printables.com/model/999-other"},
        {"site": "thingiverse"},
        {"url": "http://www.printables.com/model/311253-spoon"},
        {"url": "https://www.printables.com.attacker.example/model/311253"},
    ],
)
def test_a_model_json_whose_declared_site_or_id_the_url_does_not_back_is_refused(
    tmp_path: Path, fields: dict[str, str]
) -> None:
    path = write_model_json(tmp_path / "dl", **fields)
    with pytest.raises(InvalidArgument):
        load_source(str(path))


def test_a_model_json_source_takes_site_and_id_from_its_url(tmp_path: Path) -> None:
    path = write_model_json(tmp_path / "dl")
    src = load_source(str(path))
    assert (src.site, src.model_id, src.url) == ("printables", "311253", PRINTABLES)


# ------------------------------------------------------------------------------------------ make planning
def test_makes_of_different_models_do_not_share_a_browser_session(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    pic = photo(tmp_path)
    first = make_eval(project, "printables", of=source(PRINTABLES), photos=(pic,))
    other = "https://www.printables.com/model/42-other"
    second = make_eval(
        project, "printables", of=source(other, model_id="42"), photos=(pic,)
    )
    assert execute._session_name(first.plans[0]) != execute._session_name(
        second.plans[0]
    )
    again = make_eval(project, "printables", of=source(PRINTABLES), photos=(pic,))
    assert execute._session_name(first.plans[0]) == execute._session_name(
        again.plans[0]
    )


def test_an_explicit_of_beats_the_recorded_publication(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    old = "https://www.printables.com/model/1-old-publication"
    ev = make_eval(
        project,
        "printables",
        of=source(PRINTABLES),
        targets=(("printables", old),),
        photos=(photo(tmp_path),),
    )
    (item,) = ev.plans
    assert item.fields["model_url"] == PRINTABLES
    assert item.browser_steps[0] == ("open", PRINTABLES)


def test_all_covers_a_cults_make_target(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    ev = make_eval(
        project,
        "all",
        targets=(("cults", CULTS),),
        photos=(photo(tmp_path),),
    )
    assert ev.all_problems() == ()
    assert [p.platform for p in ev.plans] == ["cults"]
    assert ev.plans[0].fields["model_url"] == CULTS


# ------------------------------------------------------------------------------------------ the offer
def library_with_two_makes(tmp_path: Path, project: Path) -> tuple[Path, str, str]:
    db = tmp_path / "library.db"
    c = librarydb.open_library(db)
    try:
        uids = []
        for model_id in ("101", "202"):
            row, _ = records.ensure_source(
                c,
                site="printables",
                model_id=model_id,
                url=f"https://www.printables.com/model/{model_id}-m",
                license="CC BY 4.0",
                author="ann",
            )
            made, _ = records.add_make(
                c,
                "proj",
                printer="k1se",
                status="done",
                source_uid=str(row["uid"]),
                project_dir=str(project),
            )
            uids.append(str(made["uid"]))
    finally:
        c.close()
    return db, uids[0], uids[1]


def test_a_selected_older_make_keeps_its_own_source(tmp_path: Path) -> None:
    project = make_project(tmp_path, "category: Household\n")
    db, older, newer = library_with_two_makes(tmp_path, project)
    for uid, model_id in ((older, "101"), (newer, "202")):
        built = offer_run.build(project, trigger="print", make=uid, db_path=str(db))
        url = f"https://www.printables.com/model/{model_id}-m"
        assert built["source"] == url
        assert built["plans"][-1].ev.source is not None
        assert built["plans"][-1].ev.source.url == url


def test_modifying_a_downloaded_model_is_a_remix_under_its_source_rules(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path, "category: Household\n")
    write_model_json(project, license="CC BY-ND 4.0")
    built = offer_run.build(
        project, trigger="modify", db_path=str(tmp_path / "no-library.db")
    )
    (item,) = built["plans"]
    assert item.kind == "remix" and item.ev.remix is not None
    assert built["source"] == PRINTABLES
    assert any(
        "permission" in p.lower() or "derivative" in p.lower()
        for p in item.ev.all_problems()
    )


def test_the_confirmation_names_and_defaults_by_the_effective_licence(
    tmp_path: Path,
) -> None:
    project = make_project(
        tmp_path, YAML.replace("license: cc-by-nc", "license: cc-by-nc-nd")
    )
    prompts: list[str] = []
    shown: list[str] = []
    sent: list[str] = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return ""

    def send(prep: flow.Prepared) -> list[flow.Outcome]:
        sent.append(prep.kind)
        return []

    code = offer_run.run_offer(
        project,
        trigger="modify",
        mode="ask",
        yes=False,
        as_json=False,
        db_path=str(tmp_path / "no-library.db"),
        interactive=lambda: True,
        ask=ask,
        out=shown.append,
        send=send,
    )
    assert code == 0 and sent == []
    assert "cc-by-nc-nd" in "\n".join(shown)
    assert prompts and "[y/N]" in prompts[0]


def test_the_replay_command_carries_every_effective_input(tmp_path: Path) -> None:
    project = make_project(tmp_path, "category: Household\n")
    built = offer_run.build(
        project,
        trigger="modify",
        design="named-design",
        db_path=str(tmp_path / "no-library.db"),
        drop=True,
    )
    (argv,) = offer_run._command(built, "modify", "ask")
    assert argv[argv.index("--design") + 1] == "named-design"
    assert "--drop-nonprintable" in argv and argv[-1] == "--yes"
    argv = offer_run._command(
        {**built, "allow": ("probe_pcb", "fan"), "reason": "a stand-in"},
        "modify",
        "ask",
    )[0]
    assert argv[argv.index("--allow-nonprintable") + 1] == "probe_pcb,fan"
    assert argv[argv.index("--reason") + 1] == "a stand-in"


# ------------------------------------------------------------------------------------------ the offer's inputs
def library_with_sources(tmp_path: Path, **licences: str) -> Path:
    """A library holding the model_source rows `licences` (model id -> licence text)."""
    db = tmp_path / "library.db"
    c = librarydb.open_library(db)
    try:
        for model_id, lic in licences.items():
            records.ensure_source(
                c,
                site="printables",
                model_id=model_id,
                url=f"https://www.printables.com/model/{model_id}-m",
                license=lic,
                author="ann",
            )
    finally:
        c.close()
    return db


def test_the_confirmation_command_is_shell_quoted(tmp_path: Path) -> None:
    hostile = "holder; printf REVIEW_INJECTED; #"
    project = make_project(tmp_path / "my projects")
    shown: list[str] = []
    code = offer_run.run_offer(
        project,
        trigger="modify",
        mode="ask",
        yes=False,
        as_json=False,
        design=hostile,
        db_path=str(tmp_path / "no-library.db"),
        interactive=lambda: False,
        out=shown.append,
    )
    assert code == 0
    argv = shlex.split(shown[-1])
    assert argv[argv.index("--design") + 1] == hostile
    assert argv[argv.index("offer") + 1] == str(project)
    assert ";" not in shown[-1].replace(shlex.quote(hostile), "")


def test_the_source_publish_yaml_declares_beats_the_librarys_provenance(
    tmp_path: Path,
) -> None:
    declared = "https://www.printables.com/model/999-m"
    project = make_project(tmp_path, YAML + f"remix_of: {declared}\n")
    db = library_with_sources(tmp_path, **{"101": "CC BY 4.0", "999": "CC BY-ND 4.0"})
    c = librarydb.open_library(db)
    try:
        design = records.ensure_design(c, "proj", str(project))
        old = c.execute(
            "SELECT uid FROM model_source WHERE model_id = '101'"
        ).fetchone()
        records.relate(
            c, "remix_of", "design", str(design["uid"]), "model_source", str(old["uid"])
        )
    finally:
        c.close()
    built = offer_run.build(project, trigger="modify", db_path=str(db))
    (item,) = built["plans"]
    assert item.ev.spec.remix_of == declared and built["source"] == declared
    assert any("derivative" in p.lower() for p in item.ev.all_problems())


def test_a_yaml_only_remix_defaults_to_the_licence_its_source_requires(
    tmp_path: Path,
) -> None:
    declared = "https://www.printables.com/model/555-m"
    project = make_project(
        tmp_path,
        f"title: Wall plug\ndescription: A spiral plug.\ncategory: Household\nremix_of: {declared}\n",
    )
    db = library_with_sources(tmp_path, **{"555": "CC BY-SA 4.0"})
    built = offer_run.build(project, trigger="modify", db_path=str(db))
    (item,) = built["plans"]
    assert item.ev.spec.license.id == "cc-by-sa"
    assert built["license"]["id"] == "cc-by-sa"
    assert item.ev.all_problems() == ()


def test_a_design_that_contradicts_the_make_is_refused_before_the_offer_defaults(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    db = tmp_path / "library.db"
    c = librarydb.open_library(db)
    try:
        records.ensure_design(c, "B")
        made, _ = records.add_make(c, "A", status="done")
        uid = str(made["uid"])
    finally:
        c.close()
    with pytest.raises(UsageError, match="B.*A|A.*B"):
        offer_run.build(project, trigger="print", design="B", make=uid, db_path=str(db))
