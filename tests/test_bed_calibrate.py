"""`3d printer bed calibrate NAME` (lib/bed_calibrate.py, issue #723): how much of the plate the camera sees at
each Z, learnt on an idle printer by moving the bed in Z only, and used by the bed check for a printer without a
camera calibration.

The camera is `SynthCamera`: it renders a textured plate that slides down the frame (and shrinks a little) as
the fake printer's bed Z rises, so the true visible share per Z is known. Real frames are used where they exist
(the K1 SE empty bed, shifted like a Z move). Nothing here touches a real printer or camera.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from errors import InvalidArgument, UsageError
from printer_ops import bed as pbed
from printer_ops.devices import Device
from test_bed_light import LightPrinter
from test_bed_state import K1_ROI, frame
from test_bed_view import RESUME, FakePrinter

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")
import bed_calibrate as C  # noqa: E402  (needs numpy + OpenCV)
import bed_state as B  # noqa: E402
import bed_view as V  # noqa: E402

SHAPE = (270, 480)
Z0 = 40.0  # the Z the plate is fully in view at
NOW = 1_800_000_000.0


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


def device(
    *, model: str | None = None, access: str = "full", resume: Any = RESUME
) -> Device:
    """A Moonraker printer with no camera calibration (model None) and a Z axis that goes to 275."""
    return Device(
        "k1",
        "moonraker",
        "127.0.0.1",
        7125,
        model,
        Path("test.yaml"),
        access=access,
        watch={},
        resume=resume,
    )


# ---- a plate that slides down the frame as the bed rises ------------------------------------------------------


def plate_texture(w: int = 360, h: int = 220, seed: int = 1) -> Any:
    rng = np.random.default_rng(seed)
    n = rng.normal(0, 1, (h, w)).astype(np.float32)
    t = (
        cv2.GaussianBlur(n, (0, 0), 2.0) * 40
        + cv2.GaussianBlur(n[::-1], (0, 0), 8.0) * 30
    )
    return cv2.cvtColor(np.clip(70 + t, 10, 200).astype(np.uint8), cv2.COLOR_GRAY2BGR)


def render(
    tex: Any, z: float, *, strip: bool = True
) -> tuple[Any, list[tuple[float, float]]]:
    """The frame at bed Z `z` and the plate's outline in it: 1 px down per mm of Z, 0.05 % smaller per mm."""
    h, w = tex.shape[:2]
    s = 1 - 0.0005 * (z - Z0)
    m = np.array([[s, 0, (SHAPE[1] - w * s) / 2], [0, s, 20 + (z - Z0)]], np.float32)
    out = np.full((*SHAPE, 3), (150, 170, 190), np.uint8)
    if strip:
        out[:, :40] = (30, 30, 30)  # a static dark machine part beside the plate
    warped = cv2.warpAffine(tex, m, (SHAPE[1], SHAPE[0]), borderValue=(0, 0, 0))
    inside = (
        cv2.warpAffine(
            np.full((h, w), 255, np.uint8),
            m,
            (SHAPE[1], SHAPE[0]),
            flags=cv2.INTER_NEAREST,
        )
        > 0
    )
    out[inside] = warped[inside]
    corners = ((0, 0), (w - 1, 0), (w - 1, h - 1), (0, h - 1))
    return out, [
        (float(m[0, 0] * x + m[0, 2]), float(m[1, 1] * y + m[1, 2])) for x, y in corners
    ]


def true_share(tex: Any, z: float) -> float:
    return C.visible_share(render(tex, z)[1], SHAPE)


class SynthCamera:
    """The camera of the fake printer: the frame depends on where its bed is."""

    def __init__(self, printer: FakePrinter, tex: Any, *, strip: bool = True) -> None:
        self.printer, self.tex, self.strip = printer, tex, strip
        self.grabs = 0
        self.fail: BaseException | None = None
        self.zs: list[float] = []

    def keepalive(self) -> bool:
        return False

    def grab(self) -> Any:
        self.grabs += 1
        if self.fail is not None and self.grabs > 2:
            raise self.fail
        self.zs.append(self.printer.z)
        img, _ = render(self.tex, self.printer.z, strip=self.strip)
        jpeg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])[1].tobytes()
        com = (
            b"\xff\xfe" + (2 + 8).to_bytes(2, "big") + self.grabs.to_bytes(8, "big")
        )  # never a frozen frame
        return SimpleNamespace(jpeg=jpeg[:2] + com + jpeg[2:])


def roi_cfg(tex: Any, **kw: Any) -> pbed.BedConfig:
    poly = C.to_norm(render(tex, Z0)[1], SHAPE)
    return pbed.BedConfig(enabled=True, roi=tuple((x, y) for x, y in poly), **kw)


def seeded_reference(tex: Any, dev: Device, cfg: pbed.BedConfig) -> None:
    """An empty-bed reference at Z0 (the plate fully in view): the seed of the calibration."""
    jpeg = cv2.imencode(".jpg", render(tex, Z0)[0])[1].tobytes()
    B.capture_reference(dev, cfg, plate=True, frame=jpeg, frame_z=Z0, now=NOW)


def run(
    printer: FakePrinter, cam: SynthCamera, cfg: pbed.BedConfig, **kw: Any
) -> dict[str, Any]:
    args: dict[str, Any] = {
        "part_height": 20.0,
        "z_from": 40.0,
        "z_to": 200.0,
        "z_step": 20.0,
    }
    args.update(kw)
    return C.calibrate(
        device(),
        cfg,
        adapter=printer,
        camera=cam,
        now=NOW,
        sleep=lambda s: None,
        settle_s=0.0,
        **args,
    )


# ---- geometry --------------------------------------------------------------------------------------------------


def test_the_visible_share_of_a_polygon_is_its_area_in_the_frame_over_its_whole_area() -> (
    None
):
    inside = [(10.0, 10.0), (110.0, 10.0), (110.0, 110.0), (10.0, 110.0)]
    assert C.visible_share(inside, SHAPE) == pytest.approx(1.0)
    half = [
        (10.0, 169.0),
        (110.0, 169.0),
        (110.0, 369.0),
        (10.0, 369.0),
    ]  # the frame ends at y 269
    assert C.visible_share(half, SHAPE) == pytest.approx(0.5, abs=0.01)
    assert C.visible_share([(10.0, 400.0), (50.0, 400.0), (50.0, 450.0)], SHAPE) == 0.0
    huge = [
        (-500.0, -500.0),
        (900.0, -500.0),
        (900.0, 900.0),
        (-500.0, 900.0),
    ]  # the frame is inside the plate
    assert C.visible_share(huge, SHAPE) == pytest.approx(479 * 269 / (1400 * 1400))
    assert (
        C.visible_share([(0.0, 0.0), (1.0, 1.0), (2.0, 2.0)], SHAPE) == 0.0
    )  # no area


def test_a_polygon_round_trips_between_pixels_and_the_unit_frame() -> None:
    px = C.to_px(K1_ROI, SHAPE)
    assert C.to_norm(px, SHAPE) == [[round(x, 4), round(y, 4)] for x, y in K1_ROI]


# ---- the Z grid ------------------------------------------------------------------------------------------------


def limits(floor: float = 35.0, ceiling: float = 270.0) -> V.Limits:
    return V.Limits(floor, ceiling, 20.0, "--part-height")


def test_the_default_grid_runs_from_the_clearance_floor_to_the_ceiling_in_20_mm_steps() -> (
    None
):
    grid = C.plan_grid(limits(), None, None, None)
    assert grid[0] == 35.0 and grid[-1] == 255.0 and len(grid) == 12
    assert C.plan_grid(limits(), 40.0, 100.0, 30.0) == [40.0, 70.0, 100.0]


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        (
            (30.0, None, None),
            "outside what may be moved to",
        ),  # under the clearance floor
        ((None, 280.0, None), "outside what may be moved to"),  # over the axis ceiling
        ((None, None, 0.0), "positive --z-step"),
        ((None, None, float("nan")), "finite"),
        ((100.0, 60.0, None), "below --z-from"),
        ((35.0, 270.0, 1.0), "too many"),
    ],
)
def test_a_grid_outside_the_limits_or_without_a_sane_step_is_refused(
    args: tuple[Any, ...], fragment: str
) -> None:
    with pytest.raises(UsageError, match=fragment):
        C.plan_grid(limits(), *args)


# ---- following the plate ----------------------------------------------------------------------------------------


def test_the_followed_plate_gives_the_true_visible_share_at_every_z() -> None:
    tex = plate_texture()
    zs = [Z0 + 20 * i for i in range(1, 10)]
    frames = {z: render(tex, z)[0] for z in zs}
    seed = C.Seed(render(tex, Z0)[0], Z0, render(tex, Z0)[1], "plate_mask", "test")
    tracked = C.analyse(frames, seed)
    got = {p.z: p.share for p in tracked.at}
    assert set(got) >= {z for z in zs if z <= 200}
    for z, share in got.items():
        assert share == pytest.approx(true_share(tex, z), abs=0.03), z
    assert (
        got[Z0] > 0.95 > got[160.0] > got[200.0]
    )  # the plate slides out of the frame with the bed's Z


def test_a_plate_that_cannot_be_followed_ends_the_chain_and_the_rest_is_listed_not_invented() -> (
    None
):
    tex = plate_texture()
    frames = {z: render(tex, z)[0] for z in (60.0, 80.0)}
    frames[100.0] = np.full((*SHAPE, 3), 128, np.uint8)  # a covered lens: no texture
    frames[120.0] = render(tex, 120.0)[0]
    seed = C.Seed(render(tex, Z0)[0], Z0, render(tex, Z0)[1], "plate_mask", "test")
    tracked = C.analyse(frames, seed)
    assert [p.z for p in tracked.at] == [Z0, 60.0, 80.0]
    assert [u["z"] for u in tracked.unfollowed] == [100.0, 120.0]
    assert "at Z 100" in str(tracked.unfollowed[0]["why"])


def test_a_real_frame_shifted_like_a_z_move_is_followed_with_the_k1_plate_polygon() -> (
    None
):
    """The K1 SE empty bed (tests/data/bed) with the plate polygon of its config, moved down like a bed that rises."""
    img = frame("k1_z125_clear_ref.jpg")
    shape = img.shape[:2]
    poly = C.to_px(K1_ROI, shape)
    frames, truth = {}, {}
    for z, ty in ((100.0, 10.0), (110.0, 25.0), (120.0, 40.0)):
        m = np.float32([[1, 0, 0], [0, 1, ty]])
        frames[z] = cv2.warpAffine(
            img, m, (shape[1], shape[0]), borderMode=cv2.BORDER_REPLICATE
        )
        truth[z] = C.transform(poly, m)
    tracked = C.analyse(frames, C.Seed(img, 90.0, poly, "plate_mask", "K1_ROI"))
    assert [p.z for p in tracked.at] == [90.0, 100.0, 110.0, 120.0]
    for p in tracked.at[1:]:
        assert p.share == pytest.approx(C.visible_share(truth[p.z], shape), abs=0.03)


def test_without_a_seed_the_largest_dark_region_is_the_plate_and_the_result_says_it_is_rough() -> (
    None
):
    tex = plate_texture()
    frames = {z: render(tex, z, strip=False)[0] for z in (40.0, 60.0, 80.0, 100.0)}
    poly = C.heuristic_polygon(frames[40.0])
    assert poly is not None and C.area(poly) > 0.5 * C.area(render(tex, Z0)[1])
    tracked = C.analyse(frames, None)
    assert tracked.method == "heuristic" and "largest dark" in tracked.seed_source
    shares = [p.share for p in tracked.at]
    assert len(shares) == 4 and shares == sorted(shares, reverse=True)


def test_no_plate_to_start_from_is_refused_with_the_way_out() -> None:
    blank = {z: np.full((*SHAPE, 3), 200, np.uint8) for z in (40.0, 60.0)}
    with pytest.raises(UsageError, match="no plate to start from"):
        C.analyse(blank, None)


# ---- the printer flow --------------------------------------------------------------------------------------------


def calibrated() -> tuple[LightPrinter, SynthCamera, dict[str, Any], Any]:
    """A calibration run from Z 200 over the grid 40..180, with the empty-bed reference as the seed."""
    tex = plate_texture()
    cfg = roi_cfg(tex)
    seeded_reference(tex, device(), cfg)
    p = LightPrinter("complete", 200.0, lit=False)
    cam = SynthCamera(p, tex)
    return p, cam, run(p, cam, cfg, z_to=180.0), tex


def test_calibrate_stores_the_true_visible_share_per_z_where_the_check_reads_it() -> (
    None
):
    _, cam, res, tex = calibrated()
    assert res["method"] == "plate_mask" and res["view"]["restored"] is True
    # one frame per Z (the check grabs twice: the second, newer one is judged)
    assert list(dict.fromkeys(cam.zs)) == [
        40.0,
        60.0,
        80.0,
        100.0,
        120.0,
        140.0,
        160.0,
        180.0,
    ]
    views = dict(res["views"])
    for z, share in views.items():
        assert share == pytest.approx(true_share(tex, z), abs=0.04), z
    stored = pbed.read_calibration("k1")
    assert stored is not None and stored.method == "plate_mask"
    assert len(stored.plate_mask or ()) == 4 and [z for z, _ in stored.views] == sorted(
        views
    )
    # the file lies beside the references and is not one
    assert [r["kind"] for r in B.list_references("k1")] == ["plate"]
    assert [r.kind for r in B.RefStore("k1").load()] == ["plate"]


def test_calibrate_moves_only_in_z_puts_the_bed_and_the_light_back_and_leaves_no_marker() -> (
    None
):
    p, _, res, _ = calibrated()
    moves = [
        ln
        for s in p.scripts
        for ln in s.splitlines()
        if ln.startswith(("G0", "G1", "G28"))
    ]
    assert not [
        m for m in moves if not m.startswith("G1 Z")
    ]  # no X/Y, no home, no macro
    assert (
        moves[0] == "G1 Z40.000 F600"
        and moves[-1] == "G1 Z200.000 F600"
        and len(moves) == 9
    )
    assert p.z == 200.0 and p.lit is False and V._read_pending("k1") is None
    assert res["light"]["switched"] is True and res["light"]["restored"] is True


def test_calibrate_refuses_everything_that_is_not_an_idle_homed_writable_printer_before_moving() -> (
    None
):
    tex = plate_texture()
    cfg = roi_cfg(tex)
    for printer, fragment in (
        (FakePrinter("printing", 3.0), "printing"),
        (FakePrinter("paused", 3.0), "paused"),
        (FakePrinter("complete", 200.0, homed=""), "not homed"),
    ):
        cam = SynthCamera(printer, tex)
        with pytest.raises(UsageError, match=fragment):
            run(printer, cam, cfg)
        assert printer.scripts == [] and cam.grabs == 0
    p = FakePrinter("complete", 200.0)
    with pytest.raises(UsageError, match="read-only|no writes"):
        C.calibrate(
            device(access="read"),
            cfg,
            adapter=p,
            camera=SynthCamera(p, tex),
            part_height=20.0,
        )
    assert p.scripts == []


def test_calibrate_needs_the_part_height_and_stays_within_the_clearance() -> None:
    tex = plate_texture()
    cfg = roi_cfg(tex)
    p = FakePrinter("complete", 200.0)
    cam = SynthCamera(p, tex)
    with pytest.raises(
        UsageError, match="height of what may stand on the bed is unknown"
    ):
        run(p, cam, cfg, part_height=None)
    with pytest.raises(UsageError, match="outside what may be moved to"):
        run(p, cam, cfg, z_from=20.0)  # the floor is the part height + 15 mm
    assert p.scripts == [] and cam.grabs == 0


def test_calibrate_puts_the_bed_back_when_the_camera_fails_half_way() -> None:
    tex = plate_texture()
    cfg = roi_cfg(tex)
    seeded_reference(tex, device(), cfg)
    p = FakePrinter("complete", 200.0)
    cam = SynthCamera(p, tex)
    cam.fail = OSError("camera went away")
    with pytest.raises(OSError, match="went away"):
        run(p, cam, cfg)
    assert p.z == 200.0 and V._read_pending("k1") is None
    assert pbed.read_calibration("k1") is None  # nothing half-stored


def test_calibrate_stops_when_a_job_starts_and_stores_nothing_wrong() -> None:
    tex = plate_texture()
    cfg = roi_cfg(tex)
    seeded_reference(tex, device(), cfg)
    p = FakePrinter("complete", 200.0)
    cam = SynthCamera(p, tex)

    def start_job(printer: FakePrinter) -> None:
        if printer.z > 80.0:
            printer.raw = "printing"

    p.on_script = start_job
    res = run(p, cam, cfg)  # the frames before the job are enough for a calibration
    assert (
        "stopped at Z" in res["view"]["refused"]
        and "printing" in res["view"]["refused"]
    )
    assert max(z for z, _ in res["views"]) <= 100.0
    assert not any(
        s.startswith("G1 Z200") for s in p.scripts
    )  # a job owns the bed now: no restore move is sent


def test_calibrate_without_a_reference_or_plate_mask_uses_the_dark_region_and_says_so() -> (
    None
):
    tex = plate_texture()
    p = FakePrinter("complete", 200.0)
    cam = SynthCamera(p, tex, strip=False)
    res = run(p, cam, pbed.BedConfig(enabled=True), z_to=100.0)
    assert res["method"] == "heuristic" and len(res["views"]) >= 3
    assert "check it" in C._text(device(), res)


def test_the_text_and_json_of_a_calibration() -> None:
    tex = plate_texture()
    cfg = roi_cfg(tex)
    seeded_reference(tex, device(), cfg)
    p = FakePrinter("complete", 200.0)
    res = run(p, SynthCamera(p, tex), cfg, z_to=100.0)
    text = C._text(device(), res)
    assert (
        "calibrated from watch.bed.plate_mask on the reference at Z 40" in text
        and "bed put back" in text
    )
    assert (
        "Z   40.0  100%" in text
        and "of the plate in the frame" in text
        and "calibration.json" in text
    )
    json.dumps(res, default=str)


# ---- what the check does with it ----------------------------------------------------------------------------------


def write_views(views: list[list[float]]) -> None:
    pbed.write_calibration(
        "k1", {"ts": NOW, "method": "plate_mask", "views": views, "plate_mask": None}
    )


def test_a_learnt_calibration_tells_the_check_how_much_of_the_plate_shows_at_a_z() -> (
    None
):
    cfg = pbed.BedConfig(enabled=True)
    assert (
        B.z_match(device(), cfg).views == ()
        and B.z_match(device(), cfg).in_view(100.0) is None
    )
    write_views([[40.0, 1.0], [100.0, 0.6], [200.0, 0.0]])
    match = B.z_match(device(), cfg)
    assert match.in_view(70.0) == pytest.approx(0.8) and match.in_view(
        150.0
    ) == pytest.approx(0.3)
    assert (
        match.in_view(30.0) is None and match.in_view(250.0) is None
    )  # nobody looked there
    # the view picker: unknown to be in view beyond the learnt Zs is 0, not "in the frame by definition"
    assert (
        match.picker_view(70.0) == pytest.approx(0.8)
        and match.picker_view(250.0) == 0.0
    )
    assert (
        B.ZMatch().picker_view(250.0) == 1.0
    )  # no views, no calibration: a roi polygon is in the frame
    # the bed check now knows that Z 190 is a poor view of the plate
    assert B.out_of_view(match, 199.0, B.thresholds_for(cfg)) is not None
    assert B.out_of_view(match, 70.0, B.thresholds_for(cfg)) is None


def test_the_declared_views_override_the_learnt_ones_and_a_calibrated_camera_or_a_roi_ignores_them() -> (
    None
):
    write_views([[40.0, 1.0], [200.0, 0.0]])
    declared = pbed.BedConfig(enabled=True, views=((50.0, 1.0), (150.0, 0.5)))
    assert B.z_match(device(), declared).in_view(100.0) == pytest.approx(0.75)
    with_roi = pbed.BedConfig(enabled=True, roi=((0.1, 0.1), (0.9, 0.1), (0.9, 0.9)))
    assert (
        B.z_match(device(), with_roi).views == ()
    )  # a roi polygon is in the frame by definition
    assert B.z_match(device(), pbed.BedConfig(enabled=True)).views != ()
    u1 = device(model="Snapmaker U1")
    assert (
        B.z_match(u1, pbed.BedConfig(enabled=True)).views == ()
    )  # the bundled calibration knows better
    assert B.z_match(u1, pbed.BedConfig(enabled=True)).in_view(200.0) is not None


def test_a_broken_calibration_file_is_ignored_not_fatal() -> None:
    path = pbed.calibration_path("k1")
    path.parent.mkdir(parents=True, exist_ok=True)
    for text in ("{", '{"views": [[1, 2]]}', '{"views": [[40, 1.5], [60, 0.2]]}', "[]"):
        path.write_text(text, encoding="utf-8")
        assert pbed.read_calibration("k1") is None
    assert B.z_match(device(), pbed.BedConfig(enabled=True)).views == ()


# ---- the config ---------------------------------------------------------------------------------------------------


def dev_with(bed: dict[str, Any]) -> Device:
    return Device(
        "k1",
        "moonraker",
        "127.0.0.1",
        7125,
        "Creality K1 SE",
        Path("test.yaml"),
        watch={"bed": bed},
    )


def test_views_camera_and_plate_mask_are_read_and_validated() -> None:
    cfg = pbed.bed_config(
        dev_with(
            {
                "views": {"200": 0.15, 70: 1.0},
                "camera": "http://127.0.0.1:8080/?action=snapshot",
            }
        )
    )
    assert (
        cfg.views == ((70.0, 1.0), (200.0, 0.15))
        and cfg.camera == "http://127.0.0.1:8080/?action=snapshot"
    )
    mask = [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9]]
    assert pbed.bed_config(dev_with({"plate_mask": mask})).roi == tuple(
        (x, y) for x, y in mask
    )
    assert (
        pbed.bed_config(dev_with({"roi": mask})).roi
        == pbed.bed_config(dev_with({"plate_mask": mask})).roi
    )
    assert (
        pbed.bed_config(
            dev_with({"enabled": True, "camera": "/server/files/camera/monitor.jpg"})
        ).enabled
        is True
    )
    bad_configs: list[dict[str, Any]] = [
        {"views": {}},
        {"views": [1, 2]},
        {"views": {"x": 1.0}},
        {"views": {70: 1.5}},
        {"views": {-5: 0.5}},
        {"camera": ""},
        {"camera": "http://a b"},
        {"camera": 7},
        {"plate_mask": mask, "roi": mask},
        {"plate_mask": [[0.1, 0.1]]},
    ]
    for bad in bad_configs:
        with pytest.raises(InvalidArgument):
            pbed.bed_config(dev_with(bad))
