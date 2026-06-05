---
name: fdm-printability
description: Design-for-printability rules for FDM parts. Use when designing or reviewing any 3D-printable part — checking wall thickness, overhangs, bridges, holes, clearances, text, orientation, and warping before exporting an STL. Run the checklist before finalizing any part.
allowed-tools:
  - Read
  - Glob
---

# FDM Printability

Run this before exporting any STL. Imperative checklist first, numbers below, instant-fail list last.

The numbers assume the **project's printer + material** (see `3d.yaml` →
`project.printer` / `project.material`, resolved against `printers.yaml` / `materials.yaml`).
The defaults below are tuned for a **Bambu Lab A1** (0.4mm nozzle, textured PEI, open frame,
256³ build) in **PETG** — adjust per your own printer/material if they differ.

---

## Pre-export checklist

Walk every item. Each maps to a number in the tables below.

- [ ] **Walls ≥ 1.2mm** (3 perimeters @ 0.42 line). Structural/loaded walls ≥ 1.5mm. Absolute floor 0.8mm (2 perimeters) — only for non-loaded cosmetic walls.
- [ ] **Min feature / rib / boss ≥ 1.0mm**, ribs ≥ 1.5mm. Nothing thinner than a single 0.42mm line survives.
- [ ] **Overhangs ≤ 45°** from vertical, or chamfer/support them. PETG sags worse than PLA — for PETG treat 45° as a hard limit, add support beyond ~40°.
- [ ] **Bridges ≤ 10mm** unsupported (universal). **For PETG keep ≤ 5mm** or add support — PETG bridges poorly.
- [ ] **Holes ≥ 1.0mm** diameter. Vertical (axis = Z) print round; **horizontal holes print undersized and oval** — oversize them by ~0.1–0.2mm or ream/drill after.
- [ ] **Elephant foot**: add a 0.4–0.6mm × 45° chamfer on every edge touching the bed, OR rely on slicer elephant-foot compensation (~0.1–0.2mm). First-layer edges bulge outward.
- [ ] **Clearances** (per side, see table): sliding/loose **0.2mm**, press fit **0.1mm**, snap-fit / mating **0.3mm**. Tune by ±0.05 after a test print.
- [ ] **Text/embossing ≥ 1.0mm** stroke width and ≥ 1.0mm height/depth. Smaller blurs into the line width.
- [ ] **Orientation set for load**: Z layer adhesion is the weak axis. Orient so working/tension loads run **in-plane (X/Y)**, not across layers. Levers, hooks, snap arms: lay the load path flat.
- [ ] **Warping**: large flat footprints lift at corners on an open-frame printer. Add a brim or bed glue; keep big flat bottoms off long thin spans.
- [ ] **PETG release, not adhesion**: PETG sticks TOO hard to smooth PEI and can rip the plate. Use the **textured** PEI plate, or glue stick as a **release** layer on smooth PEI. Never bare smooth PEI with PETG.
- [ ] **Dry the PETG** if it has sat out (hygroscopic): bed-dry 80°C/12h or oven 65°C/8h. Wet PETG = stringing, popping, weak layers.
- [ ] **Manifold**: closed solids, no zero-thickness shells, no negative walls. Run `3d check` (or `3d export`, which validates geometry on the way out).

---

## Universal FDM numbers

| Constraint | Value | Note |
|---|---|---|
| Min wall (structural) | **1.2mm** | 3 perimeters @ 0.42 line ≈ 1.26mm |
| Min wall (recommended for loaded) | 1.5mm | safety margin |
| Min wall (absolute floor) | 0.8mm | 2 perimeters, cosmetic only |
| Min feature / detail | 1.0mm | ≈ 2–3 line widths |
| Min rib | 1.5mm | |
| Overhang limit | 45° from vertical | beyond → support/chamfer |
| Max bridge (reliable) | 10mm | universal |
| Min hole diameter | 1.0mm | below this loses round shape |
| Vertical hole | prints round | axis along Z |
| Horizontal hole | undersized + oval | oversize ~0.1–0.2mm or ream |
| Elephant-foot chamfer | 0.4–0.6mm @ 45° | on bed-touching edges |
| Clearance — sliding/loose | **0.2mm per side** | moving parts |
| Clearance — press fit | **0.1mm per side** | tight, permanent |
| Clearance — snap-fit / mating | **0.3mm per side** | assemble/disassemble |
| Snap-fit min thickness | 1.0mm | at thinnest point |
| Text/emboss min stroke | 1.0mm width, 1.0mm height/depth | |
| Corner/edge radius (inner) | = nozzle (0.4mm) | nozzle can't make sharp inside corners |
| Weak axis | Z (between layers) | orient loads in X/Y |

## Bambu A1 specifics (default printer)

| Parameter | Value |
|---|---|
| Default nozzle | 0.4mm (options: 0.2 / 0.6 / 0.8mm) |
| Build volume | 256 × 256 × 256 mm |
| Default line width | 0.42mm outer / 0.45mm inner (0.4 nozzle) |
| Recommended layer height | 0.2mm |
| Max layer height (0.4 nozzle) | ~0.28mm (≈ 70% of nozzle) |
| Build plate | textured PEI (stock) |
| Frame | open / bed-slinger, not enclosed |
| Enclosure impact | PLA/PETG fine; ABS/ASA warp from drafts |
| AMS | not required (single-spool OK) |

## PETG specifics (default material)

| Parameter | Value |
|---|---|
| Nozzle temp | 230–250°C (A1 generic ~255°C runs well) |
| Bed temp | 70–80°C |
| Part cooling fan | low (~30–50%), NOT full like PLA |
| Recommended layer | 0.15–0.3mm |
| Min wall | 1.2mm (3 perimeters); ≥1.5mm structural |
| Bridging / overhang | worse than PLA → ≤5mm bridge, support past ~40° |
| Layer adhesion | excellent (tough, strong Z for FDM) |
| Stringing/oozing | high → enable retraction, dry filament |
| Hygroscopic | yes → bed-dry 80°C/12h or oven 65°C/8h |
| Bed adhesion | sticks TOO well to smooth PEI → textured plate or glue as **release** |
| Flow ratio | ~0.95 (drop to 0.93–0.94 if clogging) |
| Print speed | ~40–90 mm/s (slow on open-frame A1) |

---

## Quick reject (instant fail — fix before export)

- Wall < 0.8mm anywhere → won't print solid.
- Loaded wall < 1.2mm → too weak.
- Feature / rib / pin < 1.0mm → snaps or doesn't form.
- Unsupported overhang > 45° (PETG: > ~40°) with no support/chamfer → droops.
- Unsupported bridge > 10mm (PETG > 5mm) → sags.
- Hole < 1.0mm → not round / closes up.
- Mating clearance = 0 (parts modeled touching) → fused, won't assemble.
- Snap arm / lever with load across Z layers → delaminates. Re-orient.
- Non-manifold / zero-thickness / negative wall → slicer chokes.

---

## Strength gate (load-bearing features)

Printability ≠ strength. For **every load-bearing region of every part**, prove
required-vs-allowable stress before export. PETG is anisotropic — strong in-plane
(~30 MPa), weak across layers (~14 MPa) — so print orientation is part of the check.

- [ ] Each loaded feature has a documented load + direction.
- [ ] Required stress computed (bending `M·c/I`, bearing `F/dt`, shear `F/A`, hoop `pr/t`).
- [ ] Allowable picked by load direction vs layers (XY 30 / Z 14 / shear 18 MPa).
- [ ] SF ≥ 2 static, **≥ 3 impact/cyclic**, ≥ 4 pressure.
- [ ] Print orientation set so the tensile load path is in-plane (XY), not across layers.

Theory + formulas + the anisotropy/orientation reasoning: `references/print-strength.md`.

---

## References

- `references/universal-fdm.md` — universal rules, full detail + sources.
- `references/bambu-a1-petg.md` — A1 + PETG specifics, slicer settings + sources.
- `references/print-strength.md` — anisotropic strength theory + per-feature workflow.
