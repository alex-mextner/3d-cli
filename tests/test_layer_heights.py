"""Tests for lib/slicing/layer_heights.py: Orca layer simulation, capped profiles, 3MF profile writing."""

from __future__ import annotations

import random
import zipfile
from pathlib import Path

import pytest

from slicing import layer_heights as lh

H = 40.0


def test_simulate_uniform_matches_uniform_layers() -> None:
    height = 10.19
    got = lh.simulate_layers([0, 0.12, height, 0.12], height)
    want = lh.uniform_layers(height, 0.12)
    assert len(got) == len(want)
    assert got == pytest.approx(want, abs=1e-9)
    assert got[0] == lh.FIRST_LAYER_MM


def test_simulate_ramp_monotone_and_lands() -> None:
    tops = lh.simulate_layers([0, 0.08, 20, 0.32], 20.0)
    assert all(b > a for a, b in zip(tops, tops[1:], strict=False))
    assert abs(tops[-1] - 20.0) <= 0.5 * lh.HMAX_MM
    th = lh.thicknesses(tops)
    assert th[1] < th[-1]


def test_layers_in_zone_uses_mid() -> None:
    tops = [0.2, 0.4, 0.6]  # mids 0.1, 0.3, 0.5
    assert lh.layers_in_zone(tops, 0.25, 0.35) == [1]
    assert lh.layers_in_zone(tops, 0.0, 0.2) == [0]
    assert lh.layers_in_zone(tops, 0.6, 0.9) == []


def test_cap_profile_zone_capped_only_there() -> None:
    cap = 0.12
    prof = lh.cap_profile(H, [(10, 20)], cap=cap)
    assert lh.check_profile(prof, H) is None
    tops = lh.simulate_layers(prof, H)
    th = lh.thicknesses(tops)
    inside = lh.layers_in_zone(tops, 10, 20)
    assert inside
    assert max(th[i] for i in inside) <= cap + 1e-6
    free_th = lh.thicknesses(lh.simulate_layers(lh.cap_profile(H, []), H))
    assert max(free_th) > 0.3
    assert max(th) > 0.3  # the cap is not applied everywhere
    for a, b in zip(th[1:], th[2:], strict=False):
        assert abs(a - b) <= lh.DEFAULT_SLOPE * (a + b) / 2 + 1e-6
    assert prof[-2] == round(H, 4)
    assert abs(tops[-1] - H) <= 0.16


def test_cap_profile_edge_cases() -> None:
    with pytest.raises(ValueError):
        lh.cap_profile(H, [(10, 20)], cap=0.4)
    with pytest.raises(ValueError):
        lh.cap_profile(H, [(10, 20)], cap=0.05)
    with pytest.raises(ValueError):
        lh.cap_profile(H, [(20, 10)])
    clipped = lh.cap_profile(H, [(-5, 3), (38, 90)])
    assert lh.check_profile(clipped, H) is None


def test_cap_profile_with_base() -> None:
    prof = lh.cap_profile(H, [(5, 15)], base=[0, 0.2, H, 0.2])
    th = lh.thicknesses(lh.simulate_layers(prof, H))
    assert max(th) <= 0.2 + 0.0021 + 1e-6


def test_check_profile_flags() -> None:
    assert lh.check_profile([0, 0.2, 10, 0.2], 10) is None
    assert lh.check_profile([0, 0.2, 9, 0.2], 10)
    assert lh.check_profile([0, 0.2, 10], 10)
    assert lh.check_profile([5, 0.2, 3, 0.2, 10, 0.2], 10)
    assert lh.check_profile([0, 0.0, 10, 0.2], 10)


def test_parse_and_line_round_trip() -> None:
    p1 = [0.0, 0.12, 10.0, 0.2]
    p2 = [0.0, 0.3, 5.5, 0.3]
    text = lh.profile_line(1, p1) + "\n" + lh.profile_line(2, p2)
    assert text.startswith("object_id=1|0.000000;0.120000;")
    assert lh.parse_profiles(text) == {1: p1, 2: p2}
    with pytest.raises(ValueError, match="line 2"):
        lh.parse_profiles("object_id=1|0;0.1\nnonsense\n")
    with pytest.raises(ValueError):
        lh.profile_line(1, [0, 0.1, 5])


def test_write_profiles_3mf(tmp_path: Path) -> None:
    src = tmp_path / "a.3mf"
    entries = {
        "[Content_Types].xml": (b"<x/>", zipfile.ZIP_DEFLATED),
        lh.PROFILE_ENTRY: (
            lh.profile_line(2, [0, 0.3, 5, 0.3]).encode(),
            zipfile.ZIP_STORED,
        ),
        "3D/3dmodel.model": (b"model" * 50, zipfile.ZIP_DEFLATED),
    }
    with zipfile.ZipFile(src, "w") as z:
        for name, (data, ct) in entries.items():
            z.writestr(name, data, compress_type=ct)
    dst = tmp_path / "b.3mf"
    lh.write_profiles_3mf(src, dst, {1: [0, 0.12, 10, 0.12]})
    with zipfile.ZipFile(dst) as z, zipfile.ZipFile(src) as zs:
        assert z.namelist() == zs.namelist()
        for name in z.namelist():
            if name != lh.PROFILE_ENTRY:
                assert z.read(name) == zs.read(name)
            assert z.getinfo(name).compress_type == zs.getinfo(name).compress_type
        got = lh.parse_profiles(z.read(lh.PROFILE_ENTRY).decode())
    assert got == {2: [0, 0.3, 5, 0.3], 1: [0, 0.12, 10, 0.12]}
    assert not list(tmp_path.glob("*.tmp*"))
    with pytest.raises(ValueError):
        lh.write_profiles_3mf(src, src, {})
    bare = tmp_path / "bare.3mf"
    with zipfile.ZipFile(bare, "w") as z:
        z.writestr("x.txt", b"1")
    out = tmp_path / "out.3mf"
    lh.write_profiles_3mf(bare, out, {1: [0, 0.1, 3, 0.1]})
    with zipfile.ZipFile(out) as z:
        assert z.namelist() == ["x.txt", lh.PROFILE_ENTRY]


def _zone_excess(
    prof: list[float], height: float, zones: list[tuple[float, float]], cap: float
) -> float:
    """Largest amount by which a non-first layer with its mid inside a zone exceeds cap (<= 0 when fine)."""
    tops = lh.simulate_layers(prof, height)
    th = lh.thicknesses(tops)
    worst = -1.0
    for z_lo, z_hi in zones:
        for i in lh.layers_in_zone(tops, z_lo, z_hi):
            if i > 0:
                worst = max(worst, th[i] - cap)
    return worst


@pytest.mark.parametrize(
    ("height", "zone", "base_h", "cap"),
    [(20.0, (9.468, 16.375), 0.2, 0.16), (20.0, (6.919, 10.544), 0.32, 0.2)],
)
def test_cap_holds_at_zone_entry_for_regression_cases(
    height: float, zone: tuple[float, float], base_h: float, cap: float
) -> None:
    prof = lh.cap_profile(height, [zone], cap=cap, base=[0, base_h, height, base_h])
    assert _zone_excess(prof, height, [zone], cap) <= 1e-6
    assert lh.check_profile(prof, height) is None
    assert abs(lh.simulate_layers(prof, height)[-1] - height) <= 0.16


def test_cap_holds_for_random_zones_and_caps() -> None:
    rng = random.Random(116)
    for _ in range(60):
        height = rng.uniform(10.0, 18.0)
        z_lo = rng.uniform(1.0, height - 6.0)
        zone = (z_lo, z_lo + rng.uniform(1.0, 5.0))
        cap = rng.choice([0.10, 0.12, 0.16, 0.2, 0.24, 0.3])
        base_h = rng.choice([0.2, 0.32])
        prof = lh.cap_profile(height, [zone], cap=cap, base=[0, base_h, height, base_h])
        assert _zone_excess(prof, height, [zone], cap) <= 1e-6, (
            height,
            zone,
            cap,
            base_h,
        )
        assert lh.check_profile(prof, height) is None
        assert abs(lh.simulate_layers(prof, height)[-1] - height) <= 0.16
