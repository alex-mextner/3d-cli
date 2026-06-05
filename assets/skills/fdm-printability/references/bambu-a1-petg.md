# Bambu Lab A1 + PETG specifics

Printer- and material-specific detail for the default setup. Universal rules: `universal-fdm.md`.
If your project's `3d.yaml` names a different printer/material, treat these as a starting point.

---

## Bambu Lab A1 (the machine)

| Spec | Value |
|---|---|
| Build volume | 256 × 256 × 256 mm |
| Default nozzle | 0.4mm stainless |
| Nozzle options | 0.2mm (fine), 0.6mm, 0.8mm (hardened steel) |
| Default line width | 0.42mm outer wall, 0.45mm inner wall (0.4 nozzle) |
| Recommended layer height | 0.2mm |
| Max layer height (0.4 nozzle) | ~0.28mm (≈ 70% of nozzle diameter) |
| Min layer height | ~0.08mm |
| Build plate (stock) | textured PEI |
| Frame | open / bed-slinger, no heated enclosure |
| AMS | optional — single-spool printing works without it |

**Open-frame consequences:**
- Chamber is at ambient — drafts cool the part. PLA and **PETG are fine**; ABS/ASA warp and crack
  without an enclosure.
- Slow perimeters on tall/thin parts (the A1 mini guide suggests ~40 mm/s perimeters) because the
  open frame loses heat and thin walls don't get time to bond at speed.

**Line width drives min wall.** At 0.42mm line: 2 perimeters = 0.84mm (floor), 3 = 1.26mm
(structural target), 4 = 1.68mm. The 1.2mm min wall = 3 perimeters with a hair of margin.

---

## PETG (the material)

### Temperatures

| Setting | Value |
|---|---|
| Nozzle | 230–250°C typical (A1 generic profile runs ~255°C and works) |
| Bed | 70–80°C |
| Part cooling fan | LOW — ~30–50%, never full. PETG needs heat to bond layers; too much fan = weak, stringy layers |

### Flow / speed (Bambu Studio generic PETG)

| Setting | Value |
|---|---|
| Flow ratio | ~0.95 (drop to 0.93–0.94 if clogging/over-extruding) |
| Print speed | ~40–90 mm/s; transparent PETG ~20 mm/s; HF profile ~16 mm³/s volumetric |
| Wall loops | keep ≤ 6 (more increases warp risk) |
| Infill | ≤ 50% density (more increases warp risk) |

### Behavior vs PLA — the things that bite

- **Stringing / oozing:** PETG oozes badly. Enable retraction, dry the filament, and accept some
  z-seam/blob cleanup. This is the #1 PETG annoyance.
- **Bridging / overhangs: WORSE than PLA.** PETG droops sooner. Keep bridges ≤ 5mm (vs 10mm
  universal) and add support past ~40° rather than relying on the 45° rule.
- **Layer adhesion: EXCELLENT.** Once bonded, PETG is tough and impact-resistant with strong
  interlayer adhesion — better than PLA. The Z axis is still the weak axis relatively, but in
  absolute terms PETG's layer bond is one of its strengths. Good for living-hinge-ish flex and
  pressure parts.
- **Hygroscopic:** absorbs water from air (worse at 50–60% RH). Wet PETG pops, strings, and prints
  weak. Dry it: **heat bed 80°C / 12h**, or **convection oven 60–65°C / 8h** (flip spool every
  6h), or AMS dryer 65°C / 8h.

### Bed adhesion — PETG sticks TOO well (release, not adhesion)

This is inverted from the usual "how do I make it stick" problem. PETG fuses to **smooth** PEI so
hard it can tear chunks out of the plate when you remove the part.

- **Use the textured PEI plate** (A1 stock) — PETG releases cleanly from it.
- On smooth PEI, apply **glue stick as a RELEASE agent** (a barrier so PETG does NOT weld to the
  plate), not as adhesion.
- Never run PETG on bare smooth PEI.
- For genuinely warp-prone large flat parts, a **brim** still helps hold corners down.

---

## Sources

- Bambu Lab Wiki — *PETG Usage Guide*: https://wiki.bambulab.com/en/filament/petg
- Bambu Lab Wiki — *Filament guide / material table*:
  https://wiki.bambulab.com/en/general/filament-guide-material-table
- Bambu Lab — *A1 Technical Specifications*: https://bambulab.com/en/a1/tech-specs
- Bambu Lab Wiki — *A1 FAQ*: https://wiki.bambulab.com/en/a1/manual/faq
- Bambu Lab Community Forum — PETG settings threads:
  https://forum.bambulab.com/t/looking-for-the-best-settings-for-printing-with-petg/89482
- Stanford Lab64 — *FDM Rules of Thumb*:
  https://lab64.stanford.edu/hiddenoutdated-items/fdm-rules-of-thumb
- Protolabs Network (Hubs) — *How to design parts for FDM 3D printing*:
  https://www.hubs.com/knowledge-base/how-design-parts-fdm-3d-printing/
