"""The shelf planner (#420): which spools from stock to load on which head to reach a target colour."""

from __future__ import annotations

from slicing.mix_model import mix
from slicing.mix_shelf import MIN_SPOOL_G, Spool, common_material, plan

TARGET = mix(
    ["#D32F2F", "#F6C700"], [1, 1]
)  # what 1:1 red and yellow layers show: reachable only with red
BLACK = Spool(1, "#000000", "Black", "PLA", 10, 800.0, head=1)
WHITE = Spool(2, "#FFFFFF", "White", "PLA", 11, 800.0, head=3)
YELLOW = Spool(3, "#F6C700", "Yellow", "PLA", 12, 800.0, head=4)
LOADED = [BLACK, WHITE, YELLOW]


def shelf(spool_id: int, hex_: str, name: str, **kw: object) -> Spool:
    fields: dict[str, object] = {
        "material": "PLA",
        "remaining_g": 700.0,
        "filament_id": 100 + spool_id,
    }
    fields.update(kw)
    return Spool(spool_id, hex_, name, **fields)  # type: ignore[arg-type]


def test_a_target_reachable_only_with_an_unloaded_spool_names_the_spool_the_head_and_both_errors() -> (
    None
):
    red = shelf(20, "#D32F2F", "Red")
    got = plan(TARGET, LOADED, [red], heads=[1, 2, 3, 4])
    assert got.baseline.best.delta_e > 8
    (opt,) = got.options
    (load,) = opt.loads
    assert (load.spool_id, load.head) == (20, 2)  # the free head
    assert opt.unloads == () and opt.swaps == 1
    assert opt.best.delta_e < 1 < got.baseline.best.delta_e
    assert got.chosen is opt
    assert {s.head for s in opt.placed} == {1, 2, 3, 4}


def test_with_every_head_taken_the_spool_goes_where_a_loaded_one_is_taken_out() -> None:
    red = shelf(20, "#D32F2F", "Red")
    loaded = [*LOADED, Spool(4, "#1565C0", "Blue", "PLA", 13, 800.0, head=2)]
    got = plan(TARGET, loaded, [red], heads=[1, 2, 3, 4])
    opt = got.options[0]
    (out,) = opt.unloads
    assert opt.loads[0].head == out.head and out.spool_id != YELLOW.spool_id
    assert opt.best.delta_e < 1 < got.baseline.best.delta_e
    assert all(s.spool_id != out.spool_id for s in opt.placed)


def test_empty_and_other_material_spools_are_left_out_with_the_reason() -> None:
    empty = shelf(21, "#D32F2F", "Red (empty)", remaining_g=MIN_SPOOL_G - 1)
    petg = shelf(22, "#D32F2F", "Red PETG", material="PETG")
    nomat = shelf(23, "#D32F2F", "Red ?", material="")
    got = plan(TARGET, LOADED, [empty, petg, nomat], heads=[1, 2, 3, 4])
    assert got.options == [] and got.chosen is got.baseline
    why = {s.spool_id: reason for s, reason in got.excluded}
    assert (
        "empty" in why[21]
        and "PETG is not PLA" in why[22]
        and "material unknown" in why[23]
    )


def test_equal_results_prefer_fewer_swaps_and_a_useless_extra_spool_adds_nothing() -> (
    None
):
    red = shelf(20, "#D32F2F", "Red")
    grey = shelf(
        24, "#808080", "Grey"
    )  # does not beat what red + the loaded ones already reach
    got = plan(TARGET, LOADED, [grey, red], heads=[1, 2, 3, 4], max_swaps=2)
    assert got.options[0].swaps == 1 and got.options[0].loads[0].spool_id == 20
    # the 2-swap loadout red + grey reaches the same mix as red alone: it is not offered as another option
    first = got.options[0]
    assert not [
        o
        for o in got.options[1:]
        if o.swaps == 2 and o.best.pattern == first.best.pattern
    ]
    assert plan(TARGET, LOADED, [red], heads=[1, 2, 3, 4], max_swaps=0).options == []


def test_two_spools_are_loaded_together_when_neither_alone_gets_there() -> None:
    red = shelf(20, "#D32F2F", "Red")
    green = shelf(25, "#2E7D32", "Green")
    target = mix(["#D32F2F", "#2E7D32"], [1, 1])
    loaded = [
        Spool(1, "#000000", "Black", "PLA", 10, 800.0, head=1),
        Spool(2, "#FFFFFF", "White", "PLA", 11, 800.0, head=3),
    ]
    one = plan(target, loaded, [red, green], heads=[1, 2, 3, 4], max_swaps=1)
    two = plan(target, loaded, [red, green], heads=[1, 2, 3, 4], max_swaps=2)
    assert two.options[0].swaps == 2 and {s.spool_id for s in two.options[0].loads} == {
        20,
        25,
    }
    assert {s.head for s in two.options[0].loads} == {2, 4}  # the two free heads
    assert two.options[0].best.delta_e < 1 < one.options[0].best.delta_e


def test_the_material_can_be_given_and_filters_the_shelf() -> None:
    red_petg = shelf(20, "#D32F2F", "Red PETG", material="PETG")
    red_pla = shelf(21, "#D32F2F", "Red PLA")
    got = plan(TARGET, LOADED, [red_petg, red_pla], heads=[1, 2, 3, 4], material="petg")
    assert got.material == "PETG"
    assert [s.spool_id for s, _ in got.excluded] == [21]
    assert got.options[0].loads[0].spool_id == 20


def test_common_material_is_the_most_frequent_upper_cased() -> None:
    assert (
        common_material(
            [
                Spool(1, "#000000", material="pla"),
                Spool(2, "#FFFFFF", material="PETG"),
                Spool(3, "#808080", material="PLA"),
            ]
        )
        == "PLA"
    )
    assert common_material([Spool(1, "#000000")]) == ""


def test_plan_serialises_for_json() -> None:
    import json

    red = shelf(20, "#D32F2F", "Red")
    d = plan(TARGET, LOADED, [red], heads=[1, 2, 3, 4]).to_dict()
    assert json.loads(json.dumps(d))["options"][0]["load"][0]["head"] == 2


def test_no_head_to_load_on_is_no_option() -> None:
    red = shelf(20, "#D32F2F", "Red")
    assert plan(TARGET, LOADED, [red], heads=[]).options == []


def test_a_full_printer_evicts_exactly_one_loaded_spool_per_swap() -> None:
    red = shelf(20, "#D32F2F", "Red")
    got = plan(TARGET, LOADED, [red], heads=[1, 3, 4])  # every head taken
    assert got.options and all(len(o.unloads) == o.swaps == 1 for o in got.options)
