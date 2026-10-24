"""calibration_plates/fit.py — the quick fit coupon as a plate: `3d calibrate gen fit` (G-code) and `3d calibrate fit gen`.

WHAT (tests, ids for --only/--skip = the ladders of lib/fit_coupon.py): sliding_round, tight_round, press_round,
  sliding_slot, snap_slot, press_slot — each a block of 7 holes/slots stepped around the fit table's value with a
  digit on a lip in front of every step — plus the spares (two Ø4 pins, two 2 mm tabs) printed whenever any
  ladder is. The whole coupon is centred on the bed of the printer; it prints by layer in about 10 minutes.
WHY: one short print ends with numbers in the fit table (`3d calibrate fit record`), instead of the dense plates'
  fit pins that are read by eye (issue #573).
INVARIANTS: geometry and layout come from `fit_coupon` (the same objects feed `3d calibrate fit gen`'s STL/3MF); OpenSCAD
  renders each object (`dense.openscad_stl`); every object is lower than the K1 gantry rod, so all print by layer.
"""

from __future__ import annotations

import fit_coupon
from slicing.calibration_gen import GenError
from slicing.calibration_plate import Item, Plate, PlateContext, PlateObject
from slicing.calibration_plates.dense import openscad_stl

PLATE_ID = "fit"
TITLE = "fit coupon: 7-step ladders of holes and slots"
TESTS = {
    f"{fit}_{shape}": f"{fit} fit, {'pin in round holes' if shape == 'round' else 'tab in slots'}"
    for fit, shape in fit_coupon.LADDER_SPECS
}


def build(ctx: PlateContext) -> Plate:
    chosen = tuple(
        ladder for ladder in fit_coupon.ladders() if ctx.wants(ladder.id)
    )
    if not chosen:
        raise GenError("the fit coupon needs at least one ladder")
    bed = tuple(ctx.p.bed)
    size = fit_coupon.layout(chosen).size
    if not fit_coupon.fits_bed(size, bed):
        raise GenError(
            f"the fit coupon ({size[0]:g}x{size[1]:g} mm) does not fit the {bed[0]:g}x{bed[1]:g} bed with a "
            f"{fit_coupon.MARGIN_MM:g} mm margin: print fewer ladders (--only)"
        )
    lay = fit_coupon.layout(chosen, fit_coupon.centred(size, (bed[0], bed[1])))
    objects: list[PlateObject] = []
    for placed in lay.objects:
        ladder = placed.ladder
        x0, y0, z0, x1, y1, z1 = placed.bbox
        if ladder is None:
            objects.append(
                PlateObject(
                    placed.name,
                    "fit_clearance",
                    stl=openscad_stl(placed.scad),
                    stl_bbox=placed.bbox,
                    params={"pin_mm": fit_coupon.PIN_D, "tab_mm": fit_coupon.TAB_T},
                    read=f"Spares: two Ø{fit_coupon.PIN_D:g} mm pins and two {fit_coupon.TAB_T:g} mm tabs to push into the "
                    "ladders' holes and slots.",
                )
            )
            continue
        half = (
            fit_coupon.ROUND_PITCH if ladder.shape == "round" else fit_coupon.SLOT_PITCH
        ) / 2
        objects.append(
            PlateObject(
                placed.name,
                "fit_clearance",
                stl=openscad_stl(placed.scad),
                stl_bbox=placed.bbox,
                params={
                    "fit_type": ladder.fit_type,
                    "shape": ladder.shape,
                    "ladder": ladder.number,
                    "gaps_mm": ",".join(f"{g:g}" for g in ladder.gaps),
                },
                items=tuple(
                    Item(
                        str(st.step),
                        (x - half, y0, z0, x + half, y1, z1),
                        {"gap_mm": st.gap_mm, "hole_mm": st.hole_mm},
                    )
                    for x, st in zip(placed.step_x, ladder.steps, strict=True)
                ),
                read=_read(ladder),
                record=f"3d calibrate fit record --fit {ladder.fit_type} --shape {ladder.shape} --result step=N",
            )
        )
    return Plate(
        PLATE_ID,
        TITLE,
        tuple(objects),
        layer_process={
            "top_shell_layers": 3,
            "bottom_shell_layers": 2,
            "sparse_infill_density": "15%",
            "brim_type": "no_brim",
        },
        notes=(
            "Read the ladders with `3d calibrate fit plan` (the exact instruction per ladder), then "
            "`3d calibrate fit record --printer P --material M --fit F --shape S --result step=N --yes`.",
        ),
    )


def _read(ladder: fit_coupon.Ladder) -> str:
    from calibrate_fit import readout_text

    return readout_text(ladder)
