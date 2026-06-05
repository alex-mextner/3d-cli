# Print-Strength Verification (PETG, FDM, anisotropic)

Printability says a feature *forms*; strength says it *survives its load*. Every
load-bearing feature needs a documented **required-vs-allowable** stress check
before export.

## 1. Material allowables (PETG, as-printed)

Datasheet PETG ≈ 45–50 MPa tensile / 60–70 MPa flexural. Printed parts are
weaker (voids, imperfect fusion) and **anisotropic** — much weaker across layers.

| direction | allowable (yield-based, conservative) | when it applies |
|---|---|---|
| **XY in-plane** (load along layer lines) | **30 MPa** | tension/bending whose tensile fibre runs in the print plane |
| **Z inter-layer** (load across layers) | **14 MPa** | tension/bending pulling layers apart — AVOID for load paths |
| **shear** (in-plane) | **18 MPa** | pins, teeth, lugs in shear |

Knockdown vs datasheet ≈ 0.7 in-plane, ≈ 0.3–0.4 across layers. These already
include that. Tune with a test coupon if a margin is tight. For a different
material, substitute its as-printed allowables (PLA is stiffer but more brittle;
ABS/ASA lower in-plane but better impact when enclosed).

## 2. Safety factors

| load type | min SF |
|---|---|
| static, prototype | **2.0** |
| impact / shock / repeated | **3.0** |
| pressure / safety-critical | **≥4** + measured validation |

## 3. Formulas (classical mechanics of materials)

- **Bending** (cantilever tip load): `σ = M·c / I`, `M = F·L`, `I = b·h³/12`,
  `c = h/2`. `h` is the section depth in the load direction.
- **Bearing** (bolt in a hole): `σ = F / (d·t)` (bolt dia × wall thickness).
- **Shear** (tooth/pin): `τ = F / A` (sheared cross-section).
- **Hoop** (internal pressure): `σ = p·r / t`. Applies to any wall holding gas or
  liquid pressure — printability ≠ pressure safety, so prove this separately for
  any pressure-bearing wall. Keep SF ≥ 4 and validate with a measured burst test.

## 4. Per-feature workflow (run for EACH region of EACH part)

1. Identify the **load and its direction** (from the part's intended use).
2. Compute **required stress** with the matching formula.
3. Pick **allowable** by load direction vs layers (XY/Z/shear).
4. **SF = allow / σ**; compare to the target above.
5. Set **print orientation** so the tensile load path is in-plane (XY), never
   across layers. Document it.

A real FE solve (FEA) catches what the prismatic-beam formulas miss — stress
concentrations at fillets, multi-axial states, contact, buckling. Escalate from
the analytical `σ` to an FE solve (e.g. sfepy / FreeCAD-FEM / CalculiX) wherever
the simple beam model is insufficient, and use the governing (larger) stress for
the safety factor.
