"""Behaviour of the prep step cache: reuse on equal key, miss on any change, propagate downstream."""

from __future__ import annotations

from pathlib import Path

from prep.cache import StepCache, make_key


def test_key_changes_with_params_and_upstream() -> None:
    base = make_key("repair", {"ring": 2})
    assert base == make_key("repair", {"ring": 2})
    assert base != make_key("repair", {"ring": 3})
    assert make_key("adapt", {}, upstream=base) != make_key(
        "adapt", {}, upstream="other"
    )
    assert make_key("adapt", {}) != make_key("repair", {})


def test_hit_requires_matching_key_and_surviving_artifacts(tmp_path: Path) -> None:
    cache = StepCache(tmp_path)
    art = tmp_path / "02_repaired.npz"
    art.write_bytes(b"x")
    cache.store("repair", "k1", 12.5, {"verdict": "PASS"}, [art])

    hit = cache.load("repair", "k1")
    assert hit is not None and hit.seconds == 12.5 and hit.result == {"verdict": "PASS"}
    assert cache.load("repair", "other-key") is None  # parameters changed -> recompute

    art.unlink()
    assert (
        cache.load("repair", "k1") is None
    )  # artifact deleted -> recompute, never a stale pass


def test_corrupt_record_is_a_miss_not_a_crash(tmp_path: Path) -> None:
    cache = StepCache(tmp_path)
    cache.dir.mkdir(parents=True)
    cache.path("repair").write_text("{not json", encoding="utf-8")
    assert cache.load("repair", "k") is None


def test_drop_from_forgets_that_step_and_everything_after(tmp_path: Path) -> None:
    cache = StepCache(tmp_path)
    steps = ("a", "b", "c")
    for s in steps:
        cache.store(s, "k", 1.0, {}, [])
    cache.drop_from(steps, "b")
    assert cache.load("a", "k") is not None
    assert cache.load("b", "k") is None and cache.load("c", "k") is None
