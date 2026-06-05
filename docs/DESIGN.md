# Illustration Design System — `3d-cli`

A minimal design system for the technical illustrations that appear in the glossary,
README, and command docs. Every illustration is generated from a text prompt via
`draw` (`~/.files/bin/draw`) and stored under `docs/img/`. They are committed to git
so docs are self-contained (no external CDN).

## Purpose

- Make abstract 3D / CAD / geometry concepts concrete for first-time readers.
- Keep the visual style consistent across 50+ glossary entries.
- Avoid stock-photo genericity — every image must be recognisably "this project's docs".

## Style

- **Flat technical illustration** — no photorealism, no gradients, no 3D-rendered gloss.
- **Clean line art** with solid fills and a single accent color.
- **White or very light grey background** (`#F8F5F0` — same as the README light theme).
- **Consistent palette** (see below) so images feel like a set.
- **No text inside the image** — captions live in Markdown; images are language-agnostic.
- **Square aspect ratio** (1024×1024) unless the concept genuinely needs landscape.
- **PNG format** — lossless enough, universally supported.

## Palette

| Role | Hex | Usage |
|---|---|---|
| Primary accent | `#A3B59A` | the "good / positive / target" element (e.g., the matched silhouette, the correct part) |
| Secondary accent | `#E6A57E` | the "bad / error / contrast" element (e.g., the difference overlay, the failed gate) |
| Neutral base | `#6B8E9B` | structural lines, axes, grid, camera frustum |
| Dark | `#2A2A2A` | outlines, text-like labels (if any) |
| Background | `#F8F5F0` | page background, should blend with GitHub / docs rendering |
| Highlight | `#D4C4A8` | cut faces, section planes, reference-photo ghost |

## Prompt conventions

All prompts passed to `draw` should follow this template:

```
Minimal flat technical illustration of <concept>, clean line art, solid fills, no gradients, no text, no photorealism, sage green (#A3B59A) and warm terracotta (#E6A57E) accents on off-white background (#F8F5F0), thin dark outlines (#2A2A2A), square 1024x1024, vector-like style
```

Replace `<concept>` with the concrete subject. Examples:

- **Silhouette IoU** — "two overlapping geometric shapes, one sage green, one terracotta, intersection area highlighted, thin outlines, minimal flat technical illustration"
- **Manifold check** — "a closed watertight 3D cube with all edges sealed, sage green, next to a broken cube with a hole, terracotta, flat technical illustration"
- **Camera fit** — "a 3D camera frustum aiming at a reference object, with azimuth/elevation arrows, sage green and steel blue, flat technical illustration"
- **Section plane** — "a 3D cube cut by a transparent plane, cut face highlighted in warm beige, flat technical illustration"
- **FDM anisotropy** — "a 3D-printed part showing layer lines, with stress arrows pointing across and along layers, sage green and terracotta, flat technical illustration"

## File naming

```
docs/img/<term>.svg
```

where `<term>` is the glossary anchor slug (e.g., `iou`, `manifold`, `fit-camera`).

## Regeneration policy

- If a term is added to the glossary, generate the image, add it to `docs/img/`, and link it from the glossary entry.
- If the design system changes (palette, style), regenerate ALL images in one batch via a script.
- Never commit prompts without the generated image — the doc must be renderable offline.

## Usage in Markdown

```markdown
![Silhouette IoU — two overlapping masks, intersection in sage green, difference in terracotta](docs/img/iou.svg)
```

Keep the alt text descriptive — it doubles as the caption.

## Generation command

```bash
# single image (AI-generated via HF)
draw "Minimal flat technical illustration of a watertight 3D cube..." -o docs/img/manifold.png

# batch (from a script)
cat terms.txt | while read term prompt; do
  draw "$prompt" -o "docs/img/${term}.png"
done

# vector fallback (deterministic, no API needed)
python docs/img/generate.py
```

## Design rationale

- **Why flat?** Photoreal renders compete with the actual tool output (OpenSCAD / Blender renders). Illustrations must be clearly "diagrams", not "renders".
- **Why no text?** Text in images is hard to localise, hard to search, and breaks at small sizes.
- **Why the palette?** The three main colours (`#A3B59A`, `#E6A57E`, `#6B8E9B`) are from the same muted, earth-adjacent family — they feel technical but not cold, and they print well in greyscale.
