"""Readable e2e stories of the #579 loop with the real bin/3d: record a make, attach photos, publish a remix to a
loopback Thingiverse and read the chain back from the library; the table photo with a lifted bed against a
loopback Moonraker that simulates Z; the non-printable-parts guard; the publish offer. No real site, account or
printer is contacted and nothing is published without --yes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from photo_fixtures import ZMoonraker, write_devices
from publish_fixtures import FakeThingiverse, make_project, three_d_yaml

from .workflow_helper import run_cli

SRC_URL = "https://www.printables.com/model/311253-spoon-drawer-pull"
BLANK = {
    "THINGIVERSE_TOKEN": "",
    "THINGIVERSE_UPLOAD_TOKEN": "",
    "MAKERWORLD_TOKEN": "",
    "THREED_KEYCHAIN": "off",
}


def env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {**BLANK, "THREED_LIBRARY_DB": str(tmp_path / "library.db"), **extra}


def model_json(tmp_path: Path, license: str = "Creative Commons - Attribution") -> Path:
    d = tmp_path / "downloaded"
    d.mkdir(exist_ok=True)
    (d / "model.json").write_text(
        json.dumps(
            {
                "source": {"site": "printables", "id": "311253", "url": SRC_URL},
                "title": "Spoon pull",
                "author": {"name": "ann", "url": ""},
                "license": license,
                "files": [],
            }
        ),
        encoding="utf-8",
    )
    return d / "model.json"


def cli(tmp_path: Path, *argv: str, **extra: str) -> tuple[int, str, str]:
    r = run_cli(tmp_path, *argv, env_extra=env(tmp_path, **extra))
    return r.returncode, r.stdout, r.stderr


def jcli(tmp_path: Path, *argv: str, **extra: str) -> dict[str, Any]:
    code, out, err = cli(tmp_path, *argv, "--json", **extra)
    assert code == 0, out + err
    return json.loads(out)


def test_from_a_downloaded_model_to_a_published_remix_and_back_through_the_library(
    tmp_path: Path,
) -> None:
    """A user prints a downloaded model, photographs the print, modifies it, publishes the remix to Thingiverse
    and asks the library where everything came from."""
    project = make_project(tmp_path)
    src = model_json(tmp_path)
    made = jcli(
        tmp_path,
        "make",
        "add",
        "--design",
        "wall-plug",
        "--source-url",
        str(src),
        "--printer",
        "k1se",
        "--filament",
        "PLA",
        "--status",
        "printing",
        "--project-dir",
        str(project),
    )
    make_uid = str(made["make"])
    assert (
        len(make_uid) == 36 and made["created"] is True and made["status"] == "printing"
    )
    done = jcli(tmp_path, "make", "status", make_uid[:12], "done")
    assert done["status"] == "done" and done["finished_at"]
    photo = tmp_path / "table.jpg"
    photo.write_bytes(b"\xff\xd8table\xff\xd9")
    added = jcli(
        tmp_path,
        "photo",
        "add",
        str(photo),
        "--make",
        make_uid,
        "--kind",
        "table",
        "--z-mm",
        "70",
    )
    assert added["ok"] is True and added["data"]["kind"] == "table"

    # the plan (dry run) already carries the library photo and records nothing
    code, out, err = cli(
        tmp_path,
        "models",
        "publish",
        str(project),
        "--platform",
        "thingiverse",
        "--kind",
        "remix",
        "--of",
        str(src),
        "--make",
        make_uid,
    )
    assert code == 0, out + err
    assert (
        "remix of  " + SRC_URL in out
        and "photos from the library (1)" in out
        and "(bed at Z 70)" in out
    )
    assert jcli(tmp_path, "library", "publications", "list")["publications"] == []

    fake = FakeThingiverse()
    try:
        code, out, err = cli(
            tmp_path,
            "models",
            "publish",
            str(project),
            "--platform",
            "thingiverse",
            "--kind",
            "remix",
            "--of",
            str(src),
            "--make",
            make_uid,
            "--yes",
            THREED_THINGIVERSE_API=fake.base,
            THINGIVERSE_UPLOAD_TOKEN="tok-1",
        )
    finally:
        fake.close()
    assert code == 0, out + err
    assert "thingiverse: published https://www.thingiverse.com/thing:777" in out
    uploaded = b"".join(c[3] for c in fake.calls)
    assert b"table.jpg" in uploaded  # the photo of the make went up with the files

    pubs = jcli(tmp_path, "library", "publications", "list")["publications"]
    assert [(p["platform"], p["state"], p["url"]) for p in pubs] == [
        ("thingiverse", "published", "https://www.thingiverse.com/thing:777")
    ]
    rels = jcli(tmp_path, "library", "relations", "list")["relations"]
    chain = {(r["rel"], r["from"]["kind"], r["to"]["kind"]) for r in rels}
    assert chain == {
        ("remix_of", "design", "model_source"),
        ("make_of", "make", "design"),
        ("derived_from", "design", "model_source"),
    }
    graph = jcli(tmp_path, "library", "graph", "wall-plug")
    assert graph["schema"] == "3d.library.graph/1"
    code, text, _ = cli(tmp_path, "library", "graph", "wall-plug")
    assert (
        code == 0
        and f"source {SRC_URL}" in text
        and "<-- remix_of -- design wall-plug" in text
    )
    assert "<-- make_of -- make of wall-plug (done, k1se)" in text
    makes = jcli(tmp_path, "library", "makes", "list")["makes"]
    assert makes[0]["photos"] == 1 and makes[0]["status"] == "done"


def test_make_post_dry_run_plans_the_photos_for_the_source_page(tmp_path: Path) -> None:
    """A user prints somebody else's model, photographs it and asks for a make post to that model's page; the dry run
    plans the photos for the source page and publishes nothing."""
    project = make_project(tmp_path)
    src = model_json(tmp_path, "CC BY-NC-ND 4.0")
    made = jcli(
        tmp_path,
        "make",
        "add",
        "--design",
        "wall-plug",
        "--source-url",
        str(src),
        "--printer",
        "k1se",
        "--status",
        "done",
        "--project-dir",
        str(project),
    )
    photo = tmp_path / "table.jpg"
    photo.write_bytes(b"jpg")
    jcli(
        tmp_path,
        "photo",
        "add",
        str(photo),
        "--make",
        str(made["make"]),
        "--z-mm",
        "70",
    )
    doc = jcli(
        tmp_path,
        "models",
        "publish",
        str(project),
        "--platform",
        "all",
        "--kind",
        "make",
        "--make",
        str(made["make"]),
        "--of",
        str(src),
        "--dry-run",
    )
    assert (
        doc["schema"] == "3d.publish/1"
        and doc["kind"] == "make"
        and doc["refused"] is False
    )
    assert list(doc["platforms"]) == ["printables"]
    printables = doc["platforms"]["printables"]
    assert printables["fields"]["model_url"] == SRC_URL and printables["fields"][
        "photos"
    ] == ["table.jpg"]
    assert printables["steps"][-1].startswith("STOP before the final Post button")
    assert doc["photos"][0]["z_mm"] == 70.0


def test_a_pcb_reference_model_in_the_publish_set_is_refused_and_the_override_is_explicit_and_logged(
    tmp_path: Path,
) -> None:
    """A user puts a PCB reference model in the publish set: the publish is refused, and only an explicit override
    with a reason goes through and is logged."""
    project = make_project(tmp_path)
    three_d_yaml(
        project,
        "  wall_plug: {file: wall_plug.scad}\n  probe_pcb:\n    file: vitamins/REF_probe.scad\n    role: vitamin\n"
        "    reference_source: {url: https://www.printables.com/model/9001-soil-probe-pcb, license: CC BY 4.0}\n",
    )
    (project / "print" / "probe_pcb.stl").write_bytes(b"solid pcb\nendsolid pcb\n")
    code, out, err = cli(
        tmp_path, "models", "publish", str(project), "--platform", "thingiverse"
    )
    assert code == 1  # a gate failure, not a usage error
    assert (
        "stl   print/probe_pcb.stl" in out
        and "probe_pcb" in out
        and "NOT printable" in out
    )
    assert (
        "refusing to publish non-printable part(s): probe_pcb (vitamin)" in err
        and "--drop-nonprintable" in err
    )
    fake = FakeThingiverse()
    try:
        code, _, err = cli(
            tmp_path,
            "models",
            "publish",
            str(project),
            "--platform",
            "thingiverse",
            "--yes",
            THREED_THINGIVERSE_API=fake.base,
            THINGIVERSE_UPLOAD_TOKEN="tok",
        )
        assert code == 1 and fake.calls == []  # --yes does not lift a gate
        code, out, err = cli(
            tmp_path,
            "models",
            "publish",
            str(project),
            "--platform",
            "thingiverse",
            "--drop-nonprintable",
        )
        assert code == 0 and "warning: dropped probe_pcb.stl" in out
        doc = jcli(
            tmp_path,
            "models",
            "publish",
            str(project),
            "--platform",
            "thingiverse",
            "--drop-nonprintable",
        )
        assert (
            "- probe_pcb: https://www.printables.com/model/9001-soil-probe-pcb (CC BY 4.0)"
            in doc["platforms"]["thingiverse"]["fields"]["description"]
        )
        code, _, err = cli(
            tmp_path,
            "models",
            "publish",
            str(project),
            "--platform",
            "thingiverse",
            "--allow-nonprintable",
            "probe_pcb",
        )
        assert code == 2 and "--reason" in err
        code, out, err = cli(
            tmp_path,
            "models",
            "publish",
            str(project),
            "--platform",
            "thingiverse",
            "--allow-nonprintable",
            "probe_pcb",
            "--reason",
            "assembly kit",
            "--yes",
            THREED_THINGIVERSE_API=fake.base,
            THINGIVERSE_UPLOAD_TOKEN="tok",
        )
        assert code == 0, out + err
        assert b"probe_pcb.stl" in b"".join(c[3] for c in fake.calls)
    finally:
        fake.close()
    exported = jcli(tmp_path, "library", "export")  # export is always JSON
    events = exported["tables"]["publication"]
    assert [p["state"] for p in events] == ["published"]
    log = json.loads(
        run_cli(tmp_path, "library", "export", env_extra=env(tmp_path)).stdout
    )
    assert log["tables"]["design"][0]["name"] == "wall-plug"


def test_the_table_photo_lifts_the_bed_photographs_restores_and_records_the_height(
    tmp_path: Path,
) -> None:
    """After a print a user captures the table photo: the bed lifts, the camera shoots, the bed goes back and the
    library records the photo with its height."""
    moon = ZMoonraker(state="complete", z=200.0)
    with moon.serve():
        write_devices(tmp_path / "xdg-config", moon)
        made = jcli(
            tmp_path,
            "make",
            "add",
            "--design",
            "mole",
            "--printer",
            "k1u",
            "--status",
            "done",
        )
        make_uid = str(made["make"])
        # dry run (the default): reads the printer, plans the lift, moves and photographs nothing
        code, out, err = cli(
            tmp_path,
            "photo",
            "capture",
            "--make",
            make_uid,
            "--lift",
            "70",
            "--part-height",
            "20",
        )
        assert code == 0, out + err
        assert (
            "DRY RUN" in out
            and "from Z 200.0 to Z 70.0" in out
            and "allowed Z 35.0..270.0" in out
        )
        assert moon.scripts == [] and moon.snapshots == 0
        shot = tmp_path / "shots" / "table.jpg"
        code, out, err = cli(
            tmp_path,
            "photo",
            "capture",
            "--make",
            make_uid,
            "--lift",
            "70",
            "--part-height",
            "20",
            "--out",
            str(shot),
            "--yes",
        )
        assert code == 0, out + err
        assert (
            moon.moves() == ["G1 Z70.000 F600", "G1 Z200.000 F600"] and moon.z == 200.0
        )
        assert shot.read_bytes().startswith(b"\xff\xd8") and "bed put back" in out
    photos = jcli(tmp_path, "photo", "list", "--make", make_uid)["photos"]
    assert [(p["kind"], p["z_mm"], p["path"]) for p in photos] == [
        ("table", 70.0, str(shot))
    ]


def test_the_table_photo_is_refused_while_the_printer_prints_or_the_lift_is_out_of_range(
    tmp_path: Path,
) -> None:
    """A user asks for a table photo while the printer is printing or with a lift out of range: both are refused
    and the bed does not move."""
    busy = ZMoonraker(state="printing", z=10.0)
    with busy.serve():
        write_devices(tmp_path / "xdg-config", busy)
        made = jcli(tmp_path, "make", "add", "--design", "mole", "--printer", "k1u")
        code, out, err = cli(
            tmp_path,
            "photo",
            "capture",
            "--make",
            str(made["make"]),
            "--lift",
            "70",
            "--part-height",
            "20",
            "--yes",
        )
        assert code == 1 and "the bed belongs to the job" in err
        assert busy.scripts == [] and busy.snapshots == 0
    idle = ZMoonraker(state="complete", z=200.0)
    with idle.serve():
        write_devices(tmp_path / "xdg-config", idle)
        for lift in ("20", "272"):
            code, _, err = cli(
                tmp_path,
                "photo",
                "capture",
                "--make",
                str(made["make"]),
                "--lift",
                lift,
                "--part-height",
                "20",
                "--yes",
            )
            assert code == 1 and "outside the allowed Z 35..270" in err
        code, _, err = cli(
            tmp_path,
            "photo",
            "capture",
            "--make",
            str(made["make"]),
            "--lift",
            "70",
            "--yes",
        )
        assert code == 1 and "height of what may stand on the bed is unknown" in err
        assert idle.scripts == []


def test_register_the_project_renders_as_photos(tmp_path: Path) -> None:
    """A user registers the project's renders as photos of the design so a listing can use them."""
    project = make_project(tmp_path)
    jcli(
        tmp_path, "make", "add", "--design", "wall-plug", "--project-dir", str(project)
    )
    dry = jcli(tmp_path, "photo", "renders", str(project), "--design", "wall-plug")
    assert dry["dry_run"] is True and [
        Path(r["path"]).name for r in dry["renders"]
    ] == ["iso.png"]
    assert jcli(tmp_path, "photo", "list")["photos"] == []
    first = jcli(
        tmp_path, "photo", "renders", str(project), "--design", "wall-plug", "--yes"
    )
    again = jcli(
        tmp_path, "photo", "renders", str(project), "--design", "wall-plug", "--yes"
    )
    assert (
        first["renders"][0]["registered"] is True
        and again["renders"][0]["registered"] is False
    )
    assert [
        p["kind"]
        for p in jcli(tmp_path, "photo", "list", "--design", "wall-plug")["photos"]
    ] == ["render"]


def test_the_offer_is_silent_when_off_and_never_publishes_without_yes(
    tmp_path: Path,
) -> None:
    """After a print the user is offered to publish: silent with the offer off, a plan and a command without a
    terminal, and a publish only together with --yes."""
    project = make_project(tmp_path, "category: Household\n")
    jcli(
        tmp_path,
        "make",
        "add",
        "--design",
        "wall-plug",
        "--printer",
        "k1se",
        "--status",
        "printing",
        "--project-dir",
        str(project),
    )
    fake = FakeThingiverse()
    api = {"THREED_THINGIVERSE_API": fake.base, "THINGIVERSE_UPLOAD_TOKEN": "tok"}
    try:
        code, out, err = cli(
            tmp_path, "models", "offer", "--printer", "k1se", "--publish", "off", **api
        )
        assert (code, out, err) == (0, "", "")
        code, out, err = cli(
            tmp_path,
            "models",
            "offer",
            "--printer",
            "k1se",
            **{"THREED_PUBLISH_OFFER": "off", **api},
        )
        assert (code, out, err) == (0, "", "")
        # no terminal under test: `ask` prints the plan and the command, publishes nothing
        code, out, err = cli(tmp_path, "models", "offer", "--printer", "k1se", **api)
        assert (
            code == 0
            and "licence: cc-by" in out
            and "not published: confirm" in out
            and "3d models offer" in out
        )
        doc = jcli(
            tmp_path, "models", "offer", "--printer", "k1se", "--publish", "auto", **api
        )
        assert doc["status"] == "needs_confirmation" and doc["license"]["id"] == "cc-by"
        assert fake.calls == []
        done = jcli(
            tmp_path,
            "models",
            "offer",
            "--printer",
            "k1se",
            "--platform",
            "thingiverse",
            "--publish",
            "auto",
            "--yes",
            **api,
        )
        assert done["status"] == "published" and done["outcomes"][0]["url"].endswith(
            "thing:777"
        )
    finally:
        fake.close()
    assert (
        jcli(tmp_path, "library", "publications", "list")["publications"][0]["state"]
        == "published"
    )


def test_bad_invocations_are_structured_errors(tmp_path: Path) -> None:
    """A user mistypes a publish, make or photo command and gets a structured error, not a traceback."""
    code, _, err = cli(tmp_path, "make", "add")
    assert code == 2 and "--design" in err
    code, _, err = cli(tmp_path, "make", "add", "--design", "x", "--status", "burning")
    assert code == 2 and "accepted: queued, printing, done, failed" in err
    code, _, err = cli(
        tmp_path, "photo", "add", str(tmp_path / "nope.jpg"), "--make", "x"
    )
    assert code == 2 and "file not found" in err
    code, _, err = cli(tmp_path, "library", "graph", "ghost")
    assert code == 2 and "does not exist" in err
    code, _, err = cli(tmp_path, "models", "offer")
    assert code == 2 and "--printer" in err
    code, _, err = cli(
        tmp_path, "photo", "capture", "--make", "0199a1c2-7d3e", "--lift", "high"
    )
    assert code == 2 and "--lift" in err
    project = make_project(tmp_path)
    code, _, err = cli(
        tmp_path,
        "models",
        "publish",
        str(project),
        "--platform",
        "all",
        "--kind",
        "make",
    )
    assert code == 2 and "--make ID" in err
