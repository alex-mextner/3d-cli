"""printer_ops/quality.py — the decision half of the print-quality monitor, on plain numbers (no pixels).

Numbers are what lib/print_quality.py reports for a frame: `fleck_frac` (white debris share) and `order` (bead
pattern regularity). The calibration frames are in tests/test_print_quality.py; here the STATE MACHINE is proved:
baseline from the job's own first layers, persistence, one alert per issue and level, never CRITICAL, clear,
trend, the first-layer floor, the report.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from errors import InvalidArgument
from printer_ops import quality as Q
from printer_ops.quality_report import render_markdown, summary_line, write_report

CLEAN = {
    "status": "ok",
    "fleck_frac": 0.014,
    "fleck_per10k": 9.0,
    "order": 0.84,
    "luma": 140.0,
}


def nums(fleck: float = 0.014, order: float = 0.84) -> dict[str, Any]:
    return {**CLEAN, "fleck_frac": fleck, "order": order}


class Clock:
    def __init__(self, step: float = 60.0) -> None:
        self.t = 0.0
        self.step = step

    def next(self) -> float:
        self.t += self.step
        return self.t


def judge_with_baseline(
    cfg: Q.QualityConfig | None = None,
) -> tuple[Q.QualityJudge, Clock]:
    judge, clock = (
        Q.QualityJudge(cfg or Q.QualityConfig(), printer="u1", job="sheet.gcode"),
        Clock(),
    )
    for _ in range(4):
        v = judge.feed(Q.QualityReading(clock.next(), nums(), layer=1))
        assert v.state == Q.LEARNING
    assert judge.base is not None
    return judge, clock


def feed(
    judge: Q.QualityJudge, clock: Clock, n: int, **kw: float
) -> list[Q.QualityVerdict]:
    return [
        judge.feed(Q.QualityReading(clock.next(), nums(**kw), layer=5, z=1.0))
        for _ in range(n)
    ]


def test_the_first_readings_with_the_part_on_the_plate_are_the_baseline_and_are_never_judged() -> (
    None
):
    judge = Q.QualityJudge(Q.QualityConfig())
    for _ in range(3):
        assert (
            judge.feed(Q.QualityReading(0, nums(0.09, 0.84), layer=1)).state
            == Q.LEARNING
        )
    assert judge.base is None
    v = judge.feed(Q.QualityReading(1, nums(0.09, 0.84), layer=1))
    assert (
        v.state == Q.LEARNING
        and judge.base is not None
        and judge.base.fleck == pytest.approx(0.09)
    )


def test_blind_and_absent_readings_are_counted_but_never_judged_and_never_clean() -> (
    None
):
    judge = Q.QualityJudge()
    assert (
        judge.feed(
            Q.QualityReading(0, {"status": "blind", "reason": "plate out of view"})
        ).state
        == Q.BLIND
    )
    assert judge.feed(Q.QualityReading(1, nums(), present=False)).state == Q.ABSENT
    assert (
        judge.report.skipped == {"blind": 1, "absent": 1}
        and sum(judge.report.per_level.values()) == 0
    )
    assert judge.report.max_level == Q.OK


def test_debris_is_stringing_and_a_single_bad_frame_is_not_a_finding() -> None:
    judge, clock = judge_with_baseline()
    (one,) = feed(judge, clock, 1, fleck=0.08, order=0.80)
    assert (
        one.raw == Q.SEVERE
        and one.issue == Q.STRINGING
        and one.level == Q.OK
        and one.new == []
    )
    (clean,) = feed(judge, clock, 1)
    assert clean.new == [] and clean.level == Q.OK


def test_two_bad_readings_warn_once_then_severe_once_and_never_again() -> None:
    judge, clock = judge_with_baseline()
    cfg = judge.cfg
    verdicts = feed(judge, clock, 2, fleck=0.045, order=0.83)  # warning level twice
    assert [f.kind for v in verdicts for f in v.new] == ["quality_stringing_warning"]
    sev = feed(judge, clock, 3, fleck=0.09, order=0.70)  # severe three times
    assert [f.kind for v in sev for f in v.new] == ["quality_stringing_severe"]
    assert judge.level == Q.SEVERE
    again = feed(judge, clock, 5, fleck=0.09, order=0.70)
    assert [f for v in again for f in v.new] == [], "one alert per issue and level"
    assert cfg.severe_confirm == 3 and judge.report.max_level == Q.SEVERE
    assert [a["kind"] for a in judge.report.alerts] == [
        "quality_stringing_warning",
        "quality_stringing_severe",
    ]


def test_a_quality_finding_is_never_critical_and_maps_to_watch_severities() -> None:
    assert set(Q.WATCH_SEVERITY.values()) == {"info", "warning"}
    judge, clock = judge_with_baseline()
    out = [f for v in feed(judge, clock, 6, fleck=0.1, order=0.6) for f in v.new]
    assert out and all(f.severity in ("info", "warning") for f in out)
    assert all("critical" not in f.severity for f in out)


def test_the_level_clears_after_clean_readings_and_may_warn_again_only_for_a_new_level() -> (
    None
):
    judge, clock = judge_with_baseline()
    feed(judge, clock, 3, fleck=0.045)
    assert judge.level == Q.WARNING
    cleared = feed(judge, clock, judge.cfg.clear_after)
    assert cleared[-1].level == Q.OK and "back to normal" in " ".join(cleared[-1].notes)
    again = feed(judge, clock, 3, fleck=0.045)
    assert [f for v in again for f in v.new] == [], (
        "already told once for this issue and level"
    )


def test_pattern_loss_without_debris_is_texture() -> None:
    judge, clock = judge_with_baseline()
    verdicts = feed(judge, clock, 3, fleck=0.015, order=0.66)  # order dropped 0.18
    kinds = [f.kind for v in verdicts for f in v.new]
    assert kinds and kinds[0].startswith("quality_texture_")
    assert "полосы" in [f.text for v in verdicts for f in v.new][0]


def test_the_first_layer_floor_needs_no_baseline_and_a_bad_first_layer_never_makes_the_baseline() -> (
    None
):
    judge = Q.QualityJudge(Q.QualityConfig())
    verdicts = [
        judge.feed(Q.QualityReading(float(i), nums(0.02, 0.40), layer=1))
        for i in range(2)
    ]
    assert verdicts[0].issue == Q.FIRST_LAYER and verdicts[0].raw == Q.SEVERE
    assert judge.base is None and judge.report.baseline is None
    kinds = [f.kind for v in verdicts for f in v.new]
    assert (
        kinds == ["quality_first_layer_warning"]
        or kinds == ["quality_first_layer_severe"]
        or kinds[0].startswith("quality_first_layer")
    )
    assert "первый слой" in [f.text for v in verdicts for f in v.new][0]


def test_without_a_layer_number_z_decides_what_a_first_layer_is() -> None:
    judge = Q.QualityJudge(Q.QualityConfig())
    assert (
        judge.feed(Q.QualityReading(0, nums(0.02, 0.5), z=0.3)).issue == Q.FIRST_LAYER
    )
    assert judge.feed(Q.QualityReading(1, nums(0.02, 0.5), z=5.0)).issue is None


def test_a_rising_debris_trend_lifts_a_notice_to_a_warning() -> None:
    judge, clock = judge_with_baseline(
        Q.QualityConfig(notice_abs=0.02, warning_abs=0.09, severe_abs=0.2)
    )
    verdicts = []
    for i in range(8):  # 1.9x .. 2.5x the baseline: notice level by ratio, rising fast
        v = judge.feed(
            Q.QualityReading(
                clock.next(), nums(fleck=0.021 + 0.003 * i), layer=5, z=1.0
            )
        )
        verdicts.append(v)
    assert any(
        v.trend is not None and v.trend >= judge.cfg.trend_rise for v in verdicts
    )
    assert any("rising" in " ".join(v.notes) for v in verdicts)


def test_a_clean_reading_moves_the_baseline_a_debris_reading_does_not() -> None:
    judge, clock = judge_with_baseline()
    before = judge.base.fleck  # type: ignore[union-attr]
    feed(judge, clock, 3, fleck=0.016)
    assert judge.base.fleck > before  # type: ignore[union-attr]
    grown = judge.base.fleck  # type: ignore[union-attr]
    feed(judge, clock, 3, fleck=0.09)
    assert judge.base.fleck == grown  # type: ignore[union-attr]


def test_the_notice_level_is_info_only_and_a_spotless_baseline_has_a_floor() -> None:
    judge = Q.QualityJudge(Q.QualityConfig())
    for _ in range(4):
        judge.feed(Q.QualityReading(0, nums(0.001), layer=1))
    clock = Clock()
    # 0.031 is 31x a spotless baseline but only 2.6x its floor, and under the warning's absolute level
    new = [
        f
        for _ in range(3)
        for f in judge.feed(
            Q.QualityReading(clock.next(), nums(0.031), layer=5, z=1.0)
        ).new
    ]
    assert [f.kind for f in new] == ["quality_stringing_notice"]
    assert new[0].severity == "info" and judge.level == Q.NOTICE


def test_a_slowly_growing_debris_is_not_absorbed_into_the_baseline() -> None:
    judge, clock = judge_with_baseline()
    for i in range(60):
        judge.feed(
            Q.QualityReading(
                clock.next(), nums(fleck=0.014 + 0.0008 * i), layer=5, z=1.0
            )
        )
    assert (
        judge.base is not None
        and judge.base.fleck <= 0.014 * judge.cfg.adapt_fleck_cap + 1e-9
    )
    assert judge.level != Q.OK, (
        "0.062 after an hour of creeping is stringing, however slowly it came"
    )


def test_absolute_verdict_is_what_a_single_frame_can_say() -> None:
    cfg = Q.QualityConfig()
    assert Q.absolute_verdict(nums(0.014), 1.2, cfg) == (None, Q.OK)
    assert Q.absolute_verdict(nums(0.04), 1.2, cfg) == (Q.STRINGING, Q.WARNING)
    assert Q.absolute_verdict(nums(0.08), 1.2, cfg) == (Q.STRINGING, Q.SEVERE)
    assert Q.absolute_verdict(nums(0.014, 0.5), 0.3, cfg) == (Q.FIRST_LAYER, Q.WARNING)
    assert Q.absolute_verdict(nums(0.014, 0.5), 5.0, cfg) == (None, Q.OK), (
        "the floor is for first layers only"
    )
    assert Q.absolute_verdict({"status": "blind"}, 1.0, cfg) == (None, Q.OK)
    assert Q.absolute_verdict(nums(0.09, 0.84), 0.3, cfg) == (None, Q.OK), (
        "a bare first-layer plate sparkles"
    )


def test_owner_text_is_russian_short_and_says_what_to_change() -> None:
    v = Q.QualityVerdict(Q.WARNING, fleck_ratio=3.2)
    text = Q.owner_text(Q.STRINGING, Q.WARNING, v)
    assert (
        "ниточки" in text and "3.2" in text and "Что менять" in text and len(text) < 330
    )
    assert Q.owner_text(
        Q.TEXTURE, Q.SEVERE, Q.QualityVerdict(Q.SEVERE, order_drop=0.2)
    ).startswith("усиливается")


def test_quality_config_from_devices_yaml() -> None:
    assert (
        Q.quality_config(None) == Q.QualityConfig()
        and Q.quality_config(False).enabled is False
    )
    assert Q.quality_config({"baseline_n": 6, "warning_ratio": 2}).baseline_n == 6
    for bad in ({"nope": 1}, {"baseline_n": "x"}, {"enabled": 1.5}, [1]):
        with pytest.raises(InvalidArgument):
            Q.quality_config(bad)


def test_the_report_counts_everything_and_renders_for_a_human(tmp_path: Path) -> None:
    judge, clock = judge_with_baseline()
    feed(judge, clock, 2)
    feed(judge, clock, 4, fleck=0.09, order=0.62)
    judge.feed(Q.QualityReading(clock.next(), {"status": "blind", "reason": "x"}))
    rep = judge.report
    assert rep.readings == 11 and rep.skipped == {"learning": 4, "blind": 1}
    assert rep.per_level["ok"] == 2 and rep.per_level["severe"] == 4
    assert rep.worst is not None and rep.worst["level"] == "severe"
    assert "11 readings (6 judged, 5 not" in summary_line(rep)
    md = render_markdown(rep)
    assert (
        "| severe | 4 |" in md
        and "Baseline (the job's own first layers, 4 readings)" in md
    )
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"jpeg")
    rep.worst["frame"] = str(frame)
    folder = write_report(rep, tmp_path / "runs", "u1")
    assert folder.name.endswith("-u1-monitor-report")
    assert json.loads((folder / "report.json").read_text())["max_level"] == "severe"
    assert (folder / "worst-frame.jpg").read_bytes() == b"jpeg"


def test_a_job_with_nothing_wrong_still_gets_an_honest_report() -> None:
    judge, clock = judge_with_baseline()
    feed(judge, clock, 5)
    md = render_markdown(judge.report)
    assert "Alerts sent: none." in md and "Worst reading: none" in md
