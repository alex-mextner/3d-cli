"""The empty-bed references keep themselves in order (issue #723, item 3): per printer, plate type and light
state with their age (lib/bed_refs.py), a stale or other-plate reference never makes `occupied`, the other
light state is a marked fallback, `bed reference --refresh` re-captures from the best view with the light on
(lib/bed_refresh.py), and `watch.bed.auto_reference` does it after a confident clear check.

Printers are the in-process fakes of test_bed_view / test_bed_light (nothing touches a real printer or camera).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import bed_refs as R
from errors import InvalidArgument
from printer_ops import bed as pbed
from printer_ops import bed_light as BL
from test_bed_light import PIN, LightPrinter
from test_bed_state import DATA, K1_ROI, device as state_device, frame, k1_ref
from test_bed_view import FakePrinter, ZCamera, device

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")
import bed_refresh as RF  # noqa: E402  (needs numpy + OpenCV)
import bed_state as B  # noqa: E402

CFG = pbed.BedConfig(enabled=True, auto_view=True)
NOW = 1_800_000_000.0  # a wall-clock time: references are dated against it
DAY = 86400.0
CLEAR_AT_70 = {200.0: "u1_occupied_1400.jpg", 70.0: "u1_z10_clear_ref.jpg"}


@pytest.fixture(autouse=True)
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var, sub in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
    ):
        monkeypatch.setenv(var, str(tmp_path / sub))
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture()
def occupied(xdg: Path) -> dict[float, str]:
    """The camera frames of a bed with a part on it: at Z 70 the empty-plate frame with a green block pasted on."""
    img = cv2.imread(str(DATA / "u1_z10_clear_ref.jpg"))
    h, w = img.shape[:2]
    img[int(h * 0.55) : int(h * 0.55) + 90, int(w * 0.45) : int(w * 0.45) + 90] = (30, 90, 30)
    path = xdg / "part-at-70.jpg"
    cv2.imwrite(str(path), img)
    return {200.0: "u1_occupied_1400.jpg", 70.0: str(path)}


def capture(
    z: float,
    name: str,
    *,
    now: float = NOW,
    light: str = "unknown",
    cfg: pbed.BedConfig = CFG,
) -> dict[str, Any]:
    return B.capture_reference(
        device(),
        cfg,
        plate=True,
        frame=(DATA / name).read_bytes(),
        frame_z=z,
        now=now,
        light_state=lambda: light,
    )


def old_refs(now: float = NOW, light: str = "unknown") -> None:
    capture(200.0, "u1_z200_clear_ref.jpg", now=now, light=light)
    capture(70.0, "u1_z10_clear_ref.jpg", now=now, light=light)


def ctx(**kw: Any) -> R.JudgeContext:
    return R.JudgeContext(**{"now": NOW, **kw})


def journal(printer: str = "u1") -> list[dict[str, Any]]:
    path = R.journal_path(printer)
    return (
        [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
        if path.is_file()
        else []
    )


# ---- the identity of a reference (pure) -------------------------------------------------------


def test_light_state_of_a_verdict_report() -> None:
    assert R.light_state({"on_for_frame": True}) == "on"
    assert R.light_state({"on_for_frame": False}) == "off"
    assert R.light_state({"on_for_frame": None}) == "unknown"
    assert R.light_state(None) == "unknown"


def test_file_names_keep_the_legacy_one_for_the_default_plate_and_an_unknown_light() -> (
    None
):
    assert R.stem("plate", 200.0) == "plate-z200.0"
    assert R.stem("plate", 70.0, "default", "on") == "plate-z70.0-on"
    assert R.stem("noplate", 70.0, "textured", "off") == "noplate-z70.0-textured-off"


def test_a_plate_type_outranks_a_light_state_and_an_unknown_light_beats_the_other_one() -> (
    None
):
    assert R.rank("default", "on", "default", "on") == (0, 0)
    assert R.rank("default", "unknown", "default", "on") == (0, 1)
    assert R.rank("default", "off", "default", "on") == (0, 2)
    assert R.rank("smooth", "on", "default", "on") == (1, 0)
    assert R.rank("default", "off", None, None) == (0, 0)  # no preference asked
    assert R.light_differs("on", "off") and not R.light_differs("on", "unknown")


def test_age_is_unknown_for_a_time_that_was_never_recorded() -> None:
    assert R.age_days(0.0, NOW) is None and R.age_days(1.0, NOW) is None
    assert R.age_days(NOW - 3 * DAY, NOW) == pytest.approx(3.0)


@pytest.mark.parametrize(
    ("verdict", "fragment"),
    [
        ({"bed": "occupied", "plate": "present"}, "not clear with the plate on"),
        ({"bed": "clear", "plate": "unknown"}, "not clear with the plate on"),
        (
            {
                "bed": "clear",
                "plate": "present",
                "metrics": {"bed_in_view": 0.15},
                "light": {"on_for_frame": True},
            },
            "15%",
        ),
        (
            {
                "bed": "clear",
                "plate": "present",
                "light": {"on_for_frame": False, "found": True, "text": "light x"},
            },
            "not taken with the light on",
        ),
        (
            {
                "bed": "clear",
                "plate": "present",
                "light": {"found": True, "on_for_frame": None, "error": "boom"},
            },
            "not taken with the light on",
        ),
    ],
)
def test_a_frame_that_is_not_a_confident_clear_in_a_good_view_with_the_light_on_is_refused(
    verdict: dict[str, Any], fragment: str
) -> None:
    assert fragment in str(R.refresh_refusal(verdict, 0.85))


@pytest.mark.parametrize(
    "light",
    [
        {"on_for_frame": True},
        {
            "found": False,
            "skipped": None,
            "error": None,
        },  # the printer has no light to switch
        {"found": False, "skipped": "off by config (watch.bed.light.enabled: false)"},
    ],
)
def test_a_lit_frame_or_a_printer_without_a_light_may_become_the_reference(
    light: dict[str, Any],
) -> None:
    v = {
        "bed": "clear",
        "plate": "present",
        "metrics": {"bed_in_view": 1.0},
        "light": light,
    }
    assert R.refresh_refusal(v, 0.85) is None


# ---- the verdict against stale, other-plate and other-light references ---------------------------


def k1_part(size: int = 24) -> Any:
    img = frame("k1_z125_clear_ref.jpg").copy()
    img[180 : 180 + size, 300 : 300 + size] = (20, 20, 20)
    return img


def k1_with(**kw: Any) -> Any:
    ref = k1_ref()
    for k, v in kw.items():
        setattr(ref, k, v)
    return ref


def test_a_part_is_occupied_against_a_young_reference_and_unknown_against_a_stale_one() -> (
    None
):
    img = k1_part()
    young = B.classify(
        img, [k1_with(ts=NOW - 5 * DAY)], 125.0, "k1se", ctx=ctx(max_age_days=30)
    )
    assert young.bed == "occupied" and young.info["reference"]["stale"] is False
    old = B.classify(
        img, [k1_with(ts=NOW - 40 * DAY)], 125.0, "k1se", ctx=ctx(max_age_days=30)
    )
    assert old.bed == "unknown", old.reason
    assert (
        "40 days old" in old.reason
        and "3d printer bed reference k1se --refresh" in old.reason
    )
    assert (
        old.info["reference"]["stale"] is True
        and old.info["reference"]["age_days"] == 40.0
    )
    assert not B.occupied_confident(old, B.TH, 0.85)
    # no context (a caller that does not know the age) judges as before
    assert (
        B.classify(img, [k1_with(ts=NOW - 400 * DAY)], 125.0, "k1se").bed == "occupied"
    )


def test_the_age_limit_is_the_printers() -> None:
    img = k1_part()
    ref = [k1_with(ts=NOW - 40 * DAY)]
    assert (
        B.classify(img, ref, 125.0, "k1se", ctx=ctx(max_age_days=90)).bed == "occupied"
    )
    assert B.classify(img, ref, 125.0, "k1se", ctx=ctx(max_age_days=7)).bed == "unknown"


def test_a_clear_bed_against_a_stale_reference_stays_clear_and_says_how_old_it_is() -> (
    None
):
    v = B.classify(
        frame("k1_z125_clear_later.jpg"),
        [k1_with(ts=NOW - 90 * DAY)],
        125.0,
        "k1se",
        ctx=ctx(max_age_days=30),
    )
    assert v.bed == "clear" and v.info["reference"]["age_days"] == 90.0


def test_a_reference_of_another_plate_type_never_makes_occupied() -> None:
    img = k1_part()
    v = B.classify(
        img, [k1_with(plate="smooth")], 125.0, "k1se", ctx=ctx(plate="textured")
    )
    assert (
        v.bed == "unknown"
        and "plate type 'smooth'" in v.reason
        and "'textured'" in v.reason
    )
    assert v.info["reference"]["other_plate"] is True
    assert (
        B.classify(
            img, [k1_with(plate="textured")], 125.0, "k1se", ctx=ctx(plate="textured")
        ).bed
        == "occupied"
    )


def test_the_reference_of_the_same_plate_type_and_light_state_is_the_one_judged_against() -> (
    None
):
    other_plate = k1_with(plate="smooth", ts=NOW)
    on, off = k1_with(light="on", ts=NOW - DAY), k1_with(light="off", ts=NOW - 2 * DAY)
    unknown = k1_with(light="unknown", ts=NOW)
    img = frame("k1_z125_clear_ref.jpg")
    refs = [other_plate, unknown, on, off]
    for light, want in (("on", "on"), ("off", "off"), ("unknown", "unknown")):
        v = B.classify(img, refs, 125.0, "k1se", ctx=ctx(light=light))
        assert (
            v.info["reference"]["light"] == want
            and v.info["reference"]["plate"] == "default"
        )
        assert v.info["reference_light_mismatch"] is False


def test_another_light_state_is_a_fallback_marked_reference_light_mismatch_and_judged_by_texture() -> (
    None
):
    """The reference was taken lit, this frame dark: the empty bed is clear, the verdict says which reference it
    used and that the light differed."""
    v = B.classify(
        frame("k1_z125_clear_later.jpg"),
        [k1_with(light="on")],
        125.0,
        "k1se",
        ctx=ctx(light="off"),
    )
    assert v.bed == "clear", v.reason
    assert (
        v.info["reference_light_mismatch"] is True
        and "reference_light_mismatch" in v.reason
    )
    assert v.metrics["light_mismatch"] == 1.0
    # a real part is still found through the fallback
    part = B.classify(
        k1_part(), [k1_with(light="on")], 125.0, "k1se", ctx=ctx(light="off")
    )
    assert part.bed == "occupied" and part.info["reference_light_mismatch"] is True


def test_a_known_light_difference_alone_switches_the_normalised_compare_on() -> None:
    """The same pixels: with both light states known and different the texture channel is on."""
    img = frame("k1_z125_clear_ref.jpg")
    same = B.classify(img, [k1_with(light="on")], 125.0, "k1se", ctx=ctx(light="on"))
    differs = B.classify(
        img, [k1_with(light="on")], 125.0, "k1se", ctx=ctx(light="off")
    )
    assert (
        "light_mismatch" not in same.metrics
        and differs.metrics["light_mismatch"] == 1.0
    )


# ---- storing references: per plate type and light state --------------------------------------------


def k1_store(
    light: str, plate: str = "default", now: float = NOW, z: float = 125.0
) -> dict[str, Any]:
    cfg = pbed.BedConfig(enabled=True, roi=K1_ROI, plate=plate)
    return B.capture_reference(
        state_device("k1se", model=None),
        cfg,
        plate=True,
        frame=(DATA / "k1_z125_clear_ref.jpg").read_bytes(),
        frame_z=z,
        now=now,
        light_state=lambda: light,
    )


def test_a_reference_records_its_plate_type_light_state_and_time() -> None:
    ref = k1_store("on", "textured")
    assert (ref["plate"], ref["light"], ref["ts"]) == ("textured", "on", NOW)
    assert Path(ref["path"]).name == "plate-z125.0-textured-on.jpg"
    [listed] = B.list_references("k1se", pbed.BedConfig(enabled=True), NOW + 3 * DAY)
    assert (listed["plate"], listed["light"], listed["age_days"], listed["stale"]) == (
        "textured",
        "on",
        3.0,
        False,
    )


def test_a_capture_replaces_only_the_reference_of_its_own_plate_type_and_light_state() -> (
    None
):
    k1_store("on", now=NOW - 40 * DAY)
    k1_store("off", now=NOW - 40 * DAY)
    k1_store("on", "smooth", now=NOW - 40 * DAY)
    assert len(B.list_references("k1se")) == 3
    fresh = k1_store("on", now=NOW)
    refs = {(r["plate"], r["light"]): r["ts"] for r in B.list_references("k1se")}
    assert refs == {
        ("default", "on"): NOW,
        ("default", "off"): NOW - 40 * DAY,
        ("smooth", "on"): NOW - 40 * DAY,
    }
    assert Path(fresh["path"]).is_file()


def test_the_listing_flags_a_stale_reference_and_a_legacy_one_has_default_plate_and_unknown_light() -> (
    None
):
    k1_store("unknown", now=NOW - 45 * DAY)
    [r] = B.list_references("k1se", pbed.BedConfig(enabled=True), NOW)
    assert (r["plate"], r["light"], r["stale"], r["age_days"]) == (
        "default",
        "unknown",
        True,
        45.0,
    )
    assert Path(r["path"]).name == "plate-z125.0.jpg"
    # a meta file written before this version has neither key
    meta = next((Path(r["path"]).parent).glob("plate-z125.0.json"))
    data = json.loads(meta.read_text(encoding="utf-8"))
    for key in ("plate", "light"):
        data.pop(key)
    meta.write_text(json.dumps(data), encoding="utf-8")
    [legacy] = B.list_references("k1se")
    assert (legacy["plate"], legacy["light"]) == ("default", "unknown")
    assert B.RefStore("k1se").load()[0].plate == "default"


# ---- config -------------------------------------------------------------------------------------------


def test_the_reference_keys_default_and_validate() -> None:
    cfg = pbed.bed_config(state_device(watch={"bed": {"enabled": False}}))
    assert (cfg.plate, cfg.reference_max_age_days, cfg.auto_reference) == (
        "default",
        30.0,
        False,
    )
    cfg = pbed.bed_config(
        state_device(
            watch={
                "bed": {
                    "plate": "textured-pei",
                    "reference_max_age_days": 7,
                    "auto_reference": True,
                }
            }
        )
    )
    assert (cfg.plate, cfg.reference_max_age_days, cfg.auto_reference) == (
        "textured-pei",
        7.0,
        True,
    )
    for bad in (
        {"plate": "a b"},
        {"plate": ""},
        {"plate": 3},
        {"reference_max_age_days": -1},
        {"auto_reference": "yes"},
    ):
        with pytest.raises(InvalidArgument):
            pbed.bed_config(state_device(watch={"bed": bad}))


# ---- the verdict through the bed check ----------------------------------------------------------------


def judge(
    printer: FakePrinter,
    frames: dict[float, str],
    cfg: pbed.BedConfig = CFG,
    now: float = NOW,
) -> dict[str, Any]:
    return B.status_view(
        device(),
        cfg,
        mode="force",
        max_age=None,
        part_height=20.0,
        adapter=printer,
        camera=ZCamera(printer, frames),
        sleep=lambda s: None,
        settle_s=0.0,
        now=now,
    )


def test_the_bed_check_names_the_reference_it_used_and_degrades_occupied_against_a_stale_one(occupied: dict[float, str]) -> None:
    old_refs(now=NOW - 40 * DAY)
    v = judge(FakePrinter("complete", 200.0), occupied, now=NOW)
    assert v["bed"] == "unknown", v["reason"]
    assert v["reference"]["stale"] is True and "--refresh" in v["reason"]
    assert v["occupied_confident"] is False
    fresh = judge(FakePrinter("complete", 200.0), occupied, now=NOW - 40 * DAY)
    assert fresh["bed"] == "occupied" and fresh["reference"]["stale"] is False


def test_the_bed_check_prefers_the_reference_taken_with_the_same_light_state() -> None:
    capture(70.0, "u1_z10_clear_ref.jpg", light="off")
    capture(70.0, "u1_z10_clear_ref.jpg", light="on")
    capture(200.0, "u1_z200_clear_ref.jpg")
    lit = judge(
        LightPrinter("complete", 200.0, lit=False), CLEAR_AT_70
    )  # the check switches the light on
    assert (
        lit["reference"]["light"] == "on" and lit["reference_light_mismatch"] is False
    )
    dark = judge(
        FakePrinter("complete", 200.0), CLEAR_AT_70
    )  # no light on this printer: unknown state
    assert dark["reference_light_mismatch"] is False


def test_a_dark_reference_only_is_a_marked_light_mismatch_for_a_lit_frame() -> None:
    capture(70.0, "u1_z10_clear_ref.jpg", light="off")
    capture(200.0, "u1_z200_clear_ref.jpg", light="off")
    v = judge(LightPrinter("complete", 200.0, lit=False), CLEAR_AT_70)
    assert (
        v["reference_light_mismatch"] is True
        and "reference_light_mismatch" in v["reason"]
    )
    assert v["bed"] == "clear" and v["reference"]["light"] == "off"


# ---- bed reference --refresh -----------------------------------------------------------------------------


def refresh(
    printer: FakePrinter,
    frames: dict[float, str],
    *,
    assume: str | None = None,
    cfg: pbed.BedConfig = CFG,
    cam: ZCamera | None = None,
) -> dict[str, Any]:
    return RF.refresh_reference(
        device(),
        cfg,
        assume_empty=assume,
        part_height=20.0,
        adapter=printer,
        camera=cam or ZCamera(printer, frames),
        sleep=lambda s: None,
        settle_s=0.0,
        now=NOW,
    )


def moves(printer: FakePrinter) -> list[str]:
    return [
        ln
        for s in printer.scripts
        for ln in s.splitlines()
        if ln.startswith(("G0", "G1"))
    ]


def test_refresh_after_a_confident_clear_stores_the_lit_frame_from_the_best_view_and_puts_everything_back() -> (
    None
):
    old_refs(now=NOW - 60 * DAY)  # stale, legacy: plate default, light unknown
    p = LightPrinter("complete", 200.0, lit=False)
    res = refresh(p, CLEAR_AT_70)
    assert res["refreshed"] is True, res.get("refused")
    new = res["new_reference"]
    assert (new["z"], new["light"], new["plate"], new["ts"]) == (
        70.0,
        "on",
        "default",
        NOW,
    )
    assert (
        Path(new["path"]).name == "plate-z70.0-on.jpg" and Path(new["path"]).is_file()
    )
    assert res["view"]["action"] == "moved" and res["view"]["restored"] is True
    assert p.z == 200.0 and p.lit is False  # the bed and the light are back as found
    refs = {(r["z"], r["light"]) for r in B.list_references("u1")}
    assert refs == {(200.0, "unknown"), (70.0, "unknown"), (70.0, "on")}
    [line] = journal()
    assert (
        line["mode"] == "manual"
        and line["z"] == 70.0
        and line["light"] == "on"
        and line["verdict"] == "clear/present"
    )


def test_refresh_replaces_the_stale_reference_of_the_same_light_state_and_view() -> (
    None
):
    capture(70.0, "u1_z10_clear_ref.jpg", now=NOW - 60 * DAY, light="on")
    capture(200.0, "u1_z200_clear_ref.jpg", now=NOW - 60 * DAY, light="on")
    res = refresh(LightPrinter("complete", 200.0, lit=False), CLEAR_AT_70)
    assert res["refreshed"] is True
    fresh = {
        (r["z"], r["light"]): r["age_days"] for r in B.list_references("u1", CFG, NOW)
    }
    assert fresh == {(70.0, "on"): 0.0, (200.0, "on"): 60.0}


def test_refresh_refuses_a_frame_that_is_not_a_confident_clear_and_stores_nothing(occupied: dict[float, str]) -> None:
    old_refs()
    before = sorted(p.name for p in pbed.refs_dir("u1").iterdir())
    res = refresh(LightPrinter("complete", 200.0), occupied)
    assert res["refreshed"] is False and "not clear with the plate on" in res["refused"]
    assert sorted(p.name for p in pbed.refs_dir("u1").iterdir()) == before
    assert journal() == []


def test_refresh_refuses_when_the_light_could_not_be_switched_on() -> None:
    old_refs()
    p = LightPrinter("complete", 200.0, lit=False)
    p.fail_scripts = {BL.on_script(PIN, BL.LightConfig())}
    res = refresh(p, CLEAR_AT_70)
    assert res["refreshed"] is False and "not taken with the light on" in res["refused"]
    assert len(B.list_references("u1")) == 2


def test_refresh_on_a_printer_without_a_light_stores_the_frame_with_an_unknown_light() -> (
    None
):
    old_refs(now=NOW - 60 * DAY)
    res = refresh(FakePrinter("complete", 200.0), CLEAR_AT_70)
    assert res["refreshed"] is True and res["new_reference"]["light"] == "unknown"
    assert [r["ts"] for r in B.list_references("u1") if r["z"] == 70.0] == [
        NOW
    ]  # replaced the legacy one


def test_refresh_never_touches_a_printing_printer() -> None:
    old_refs()
    p = LightPrinter("printing", 3.0)
    cam = ZCamera(p, CLEAR_AT_70)
    res = refresh(p, CLEAR_AT_70, cam=cam)
    assert res["refreshed"] is False and "is printing" in res["refused"]
    assert cam.grabs == 0 and p.scripts == []
    assert len(B.list_references("u1")) == 2


def test_assume_empty_stores_the_frame_on_the_owners_word_and_keeps_the_reason(occupied: dict[float, str]) -> None:
    old_refs()
    p = LightPrinter("complete", 200.0, lit=False)
    res = refresh(p, occupied, assume="plate wiped, I looked at it")
    assert res["refreshed"] is True
    new = res["new_reference"]
    assert (
        new["assumed_empty"] == "plate wiped, I looked at it" and new["light"] == "on"
    )
    meta = json.loads(
        Path(new["path"]).with_suffix(".json").read_text(encoding="utf-8")
    )
    assert meta["assumed_empty"] == "plate wiped, I looked at it"
    [line] = journal()
    assert line["mode"] == "assumed" and line["why"] == "plate wiped, I looked at it"
    assert p.z == 200.0 and p.lit is False


def test_assume_empty_needs_no_reference_at_the_view_and_still_refuses_a_printing_printer() -> (
    None
):
    res = refresh(
        LightPrinter("complete", 200.0), CLEAR_AT_70, assume="empty"
    )  # no reference at all yet
    assert res["refreshed"] is True and len(B.list_references("u1")) == 1
    p = LightPrinter("printing", 3.0)
    assert (
        refresh(p, CLEAR_AT_70, assume="empty")["refreshed"] is False
        and p.scripts == []
    )


def test_refresh_without_any_reference_cannot_judge_and_names_the_capture() -> None:
    res = refresh(LightPrinter("complete", 200.0), CLEAR_AT_70)
    assert res["refreshed"] is False and B.list_references("u1") == []


# ---- watch.bed.auto_reference ---------------------------------------------------------------------------


AUTO = pbed.BedConfig(enabled=True, auto_view=True, auto_reference=True)


def test_auto_reference_refreshes_after_a_confident_clear_and_journals_it() -> None:
    old_refs(now=NOW - 60 * DAY)
    p = LightPrinter("complete", 200.0, lit=False)
    v = judge(p, CLEAR_AT_70, cfg=AUTO)
    assert v["bed"] == "clear" and v["auto_reference"]["refreshed"] is True
    assert {(r["z"], r["light"]) for r in B.list_references("u1")} == {
        (200.0, "unknown"),
        (70.0, "unknown"),
        (70.0, "on"),
    }
    [line] = journal()
    assert (
        line["mode"] == "auto"
        and line["z"] == 70.0
        and line["light"] == "on"
        and line["verdict"] == "clear/present"
    )
    assert set(line) == {"ts", "mode", "why", "z", "light", "verdict"}
    assert p.lit is False and p.z == 200.0


def test_auto_reference_is_off_by_default() -> None:
    old_refs()
    v = judge(LightPrinter("complete", 200.0), CLEAR_AT_70)
    assert (
        "auto_reference" not in v
        and journal() == []
        and len(B.list_references("u1")) == 2
    )


def test_auto_reference_never_stores_a_frame_that_is_not_a_confident_clear(occupied: dict[float, str]) -> None:
    old_refs()
    v = judge(LightPrinter("complete", 200.0), occupied, cfg=AUTO)
    assert (
        v["auto_reference"]["refreshed"] is False
        and "not clear" in v["auto_reference"]["reason"]
    )
    assert journal() == [] and len(B.list_references("u1")) == 2


def test_auto_reference_never_touches_a_printing_printer() -> None:
    old_refs()
    v = judge(LightPrinter("printing", 3.0), CLEAR_AT_70, cfg=AUTO)
    assert v["bed"] == "printing" and v["auto_reference"]["refreshed"] is False
    assert journal() == [] and len(B.list_references("u1")) == 2


def test_auto_reference_refuses_a_poor_view_and_a_frame_taken_dark() -> None:
    old_refs()
    p = LightPrinter("complete", 200.0, lit=False)
    p.fail_scripts = {BL.on_script(PIN, BL.LightConfig())}
    v = judge(p, CLEAR_AT_70, cfg=AUTO)
    assert (
        v["auto_reference"]["refreshed"] is False
        and "light" in v["auto_reference"]["reason"]
    )
    low = {
        "bed": "clear",
        "plate": "present",
        "source": "camera now",
        "raw_frame": "x.jpg",
        "z": 200.0,
        "metrics": {"bed_in_view": 0.15},
        "light": {"on_for_frame": True},
    }
    assert "15%" in str(RF.auto_refresh(device(), AUTO, low)["reason"])
    watch = {**low, "source": "watch", "metrics": {}}
    assert "not from a fresh frame" in str(
        RF.auto_refresh(device(), AUTO, watch)["reason"]
    )
