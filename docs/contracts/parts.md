# Part roles, print guardrail and palette (umbrella #570, S0 #571)

`lib/parts.py` is the shared contract for what a part IS.

- **Role** `printed | vitamin | reference | stand_in`; `printable` is derived (`role == "printed"`), never stored
  separately (a parts file that contradicts it is rejected).
- **`reference_source`** `{url, license}` is mandatory for `vitamin` and `reference` parts: non-printable models
  come from existing reference models (find them with `3d models search` / `3d library`), never invented.
- **Guardrail.** `assert_printable(parts, include_non_printable=False, command=...)` raises
  `NonPrintableParts` (a `GateFailure`, exit 1) for any non-printable part. `--include-non-printable` is the
  explicit override; it is logged (`3d.parts` WARNING) and returned in `PrintGuard.overridden`.
  `filter_printable(parts)` is the drop-and-report variant for commands that build a set (`arrange`).
- **3MF tag.** `Part.to_metadata()` -> `{"3d:role": ..., "3d:printable": "true|false"}`;
  `part_from_metadata(name, meta)` reads it back (an untagged object is `printed`; `3d:printable=false` alone
  reads as `stand_in`, never as printed).
- **Palette.** `assign_styles(parts)` gives every part name a distinct `#RRGGBB`, stable across runs and
  independent of input order; stand-ins are always the muted `ghost` style (alpha 0.35). `legend(parts)` /
  `legend_text(parts)` produce the legend rows.

Wiring at each print boundary (`arrange`, `prep`, `export`, `printer start/upload`, publish) and of the palette
into render/dimension/usdz/3mf export is done per slice; see the follow-ups in the S0 pull request.
