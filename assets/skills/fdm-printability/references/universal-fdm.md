# Universal FDM design rules

Material- and printer-independent constraints for fused deposition. Numbers are conservative
defaults; tune by test print. For Bambu A1 + PETG specifics see `bambu-a1-petg.md`.

---

## Wall thickness

Walls are built from perimeters (outer + inner loops) at the line width. Min printable wall is
**2 perimeters** ≈ 0.8mm at a 0.4mm nozzle (line ~0.42mm). That is the absolute floor and is
cosmetic-only.

- **Structural / loaded walls: ≥ 1.2mm** (3 perimeters ≈ 1.26mm). This is the recommended target.
- **Recommended for functional/loaded: ≥ 1.5mm** for margin.
- Below 2 perimeters the slicer leaves gaps or under-extrudes — the wall is hollow and weak.

Hubs/Protolabs Network state 0.8mm is the minimum any of their machines will print, with 1.5mm
recommended for functional parts.

## Min feature size, ribs, pins

- **Min feature / detail: 1.0mm** (≈ 2–3 line widths). Anything one line wide is fragile.
- **Ribs: ≥ 1.5mm** thick.
- **Vertical pins: ≥ 5mm** diameter to carry load (below that there's no infill, just a stick
  that snaps). Smaller pins are fine as locating features, not load-bearing.

## Overhangs

A layer is supported by the layer below it. At **45° from vertical**, each new layer overlaps the
previous by ~50% — enough to stay put. Beyond 45° the layer hangs into air and droops.

- **≤ 45°: print as-is.**
- **> 45°: add support, or redesign as a chamfer.**
- Chamfer bed-touching and bridging transitions at 45° instead of leaving square overhangs.

## Bridging

A bridge spans a gap with nothing underneath; the filament is pulled taut between two anchors.

- **≤ 10mm: reliable** on most FDM.
- **10–30mm: prints with progressive sag**, cosmetic droop, possible support scars.
- **> 30mm: needs support.**
- Sources disagree on the "reliable" figure (some design guides say 5mm, Hubs says ~10mm). Treat
  **10mm as the universal reliable ceiling**, and tighten for sag-prone materials (PETG → 5mm).

## Holes and bores

- **Min diameter 1.0mm** to keep a round shape.
- **Vertical holes** (axis along Z, built layer by layer as circles) print round and close to
  nominal, though slightly undersized because the nozzle compresses the inner perimeter.
- **Horizontal holes** (axis in X/Y, printed as a bridge over the top arc) come out **undersized
  and oval / drooped at the top**. Design them oversize by ~0.1–0.2mm, or model nominal and
  ream/drill to final size after printing.
- Inner corners get a radius equal to the nozzle (0.4mm) — you cannot print a sharp inside corner.

## Elephant foot

The first few layers bulge outward because the nozzle is close to a hot bed and the part is
squished. This makes the base oversized and can break press fits / flatness at the bottom.

- **Chamfer 0.4–0.6mm @ 45°** on every edge touching the bed, OR
- Use slicer **elephant-foot compensation** (~0.1–0.2mm) to shrink the first layer inward.

## Clearance for mating / moving parts

Two printed surfaces modeled touching will fuse. Add a gap **per side** (total gap = 2× this):

| Fit | Clearance per side | Use |
|---|---|---|
| Sliding / loose | **0.2mm** | moving parts, channels, guides |
| Press fit | **0.1mm** | permanent tight assembly, pins in bores |
| Snap-fit / removable mating | **0.3mm** | parts that clip together and apart |

These match Hubs (0.2 loose, 0.1 tight, ~0.3 for assembled clearance). Always test-print and tune
±0.05 — exact fit depends on calibration and material shrink.

## Snap-fits

- **Min thickness 1.0mm** at the thinnest point of the cantilever.
- Orient the flexing arm so it bends **in-plane** (load not across Z layers) or it delaminates.

## Text and embossing

- **Min stroke width 1.0mm**, **min height/depth 1.0mm**, for both raised and engraved text.
- Thinner strokes collapse into the line width and become unreadable.

## Layer adhesion anisotropy (the Z-weakness)

FDM parts are **anisotropic**: strong in the layer plane (X/Y), weak across layers (Z). Adjacent
layers bond only where they touch; the layer boundary is a stress concentrator ("a stack of small
valleys"). A part can be 2× stronger in-plane than across layers.

**Design rule:** identify the main working load and orient the part so that load runs **in the
layer plane**, ideally with the force direction ~90° to the build (Z) axis. Levers, hooks, snap
arms, and anything in tension or bending must NOT have the load path crossing the layer lines.

## Warping

Thermal contraction as the part cools pulls corners up off the bed, especially on large flat
footprints and on open-frame / unheated-chamber printers.

- Add a **brim** or **bed adhesive** for large flat bottoms.
- Avoid long thin flat spans; break up large flat areas.
- Worse for high-shrink materials (ABS/ASA); PETG and PLA are tolerant but not immune.

## Tolerances / dimensional accuracy

FDM is less accurate than SLA/SLS. Typical FDM dimensional accuracy is roughly **±0.2mm** (or
±0.5% on larger dimensions) on a well-calibrated desktop machine, worse on tall parts. For
critical form-and-fit, test-print the interface, don't trust nominal dimensions.

---

## Sources

- Protolabs Network (Hubs) — *How to design parts for FDM 3D printing*:
  https://www.hubs.com/knowledge-base/how-design-parts-fdm-3d-printing/
- Protolabs Network (Hubs) — *Key design considerations for 3D printing*:
  https://www.hubs.com/knowledge-base/key-design-considerations-3d-printing/
- Protolabs Network (Hubs) — *DFM tips for thin walls*:
  https://www.hubs.com/knowledge-base/dfm-tips-for-3d-printed-parts-with-thin-walls/
- Stanford Lab64 — *FDM Rules of Thumb*:
  https://lab64.stanford.edu/hiddenoutdated-items/fdm-rules-of-thumb
