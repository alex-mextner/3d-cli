"""Recognition of segmented instances: CLIP zero-shot class proposals (calibrated) + bank retrieval inside the proposed classes.

`Recogniser.classify(rgb, masks, categories)` returns per instance

  text_topk  the best category labels from CLIP text prompts. Raw CLIP is biased towards a few classes ("battery" fires on
             tapes, bit sets and calipers); subtracting the per-class mean similarity over all crops of the photo removes most
             of it (spike, 33 hand-labelled crops: top-1 0.42 -> 0.61).
  bank_topk  nearest bank entries, searched ONLY inside the categories the text stage proposed. Text proposes a class, the
             bank re-ranks inside it; retrieval never overrides the text class (unrestricted retrieval against an untextured
             bank is weak: top-3 0.30-0.45). When the bank holds nothing of the proposed classes the list is empty.

The caller (a UI) is expected to let the user confirm the class.

Category vocabularies are data, not code: a comma list, a text file (one per line, `label: synonym, synonym`), or a JSON list of
strings / `{"id": ..., "prompts": [...]}` objects. An object without `prompts` takes its prompts from `name` + `synonyms`
(a string, a list, or a per-language dict of which `en` is used: CLIP is an English model), so a tool-class table exported by
another system works as is.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from . import bank as bank_mod
from . import io
from .clip import ClipEncoder, DinoEncoder, crop_masked
from .errors import BadInput

DEFAULT_CATEGORIES = [
    "screwdriver", "pliers", "hammer", "tape measure", "wrench", "scissors", "drill bit case", "cordless drill", "clamp",
    "roll of tape", "glue", "multimeter", "caliper", "saw", "utility knife", "box", "battery", "hex key set", "bit set",
    "tin snips", "wire stripper", "power supply",
]  # fmt: skip
TEMPLATES = [
    "a photo of a {}.",
    "a top-down photo of a {} lying on a carpet.",
    "a {} tool.",
]
# Per-class mean subtraction needs several crops to estimate the class bias; below this the raw similarities are returned.
MIN_CALIBRATION_CROPS = 8
# Calibration pool (the spike's protocol, 60 largest crops above 2500 px of a 2000x1500 photo): the class means are taken over the
# larger objects only, since small fragments distort them. 2500 / (2000 * 1500) of the image area.
CALIBRATION_POOL = 60
CALIBRATION_MIN_AREA_FRAC = 0.00083
# Keys under which a JSON object may carry the category list (e.g. an index document that also holds its tool classes).
WRAPPER_KEYS = ("categories", "toolClasses")

MaskLike = Mapping[str, Any] | np.ndarray


@dataclass(frozen=True)
class Category:
    """A class label (what the result carries) and the English phrases CLIP is asked about."""

    label: str
    prompts: tuple[str, ...]


CategorySpec = str | Mapping[str, Any] | Category


def _english(value: Any) -> list[str]:
    """str | list | {lang: ...} -> English phrases."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, Mapping):
        return _english(value.get("en"))
    if isinstance(value, (list, tuple)):
        return [p for v in value for p in _english(v)]
    raise BadInput(f"cannot read category phrases from {value!r}")


def _dedupe(items: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def parse_categories(specs: Sequence[CategorySpec] | None) -> list[Category]:
    """Normalise category specs (strings or objects, see module doc) to `Category` records; labels must be unique."""
    if not specs:
        return [Category(c, (c,)) for c in DEFAULT_CATEGORIES]
    out: list[Category] = []
    for spec in specs:
        if isinstance(spec, Category):
            out.append(spec)
            continue
        if isinstance(spec, str):
            label = spec.strip()
            if not label:
                raise BadInput("empty category label")
            out.append(Category(label, (label,)))
            continue
        if not isinstance(spec, Mapping):
            raise BadInput(f"a category is a string or an object, got {spec!r}")
        label = str(spec.get("id") or spec.get("label") or "").strip()
        if not label:
            raise BadInput(f"category object without 'id': {dict(spec)!r}")
        prompts = _english(spec.get("prompts")) or _english(
            spec.get("name")
        ) + _english(spec.get("synonyms"))
        out.append(
            Category(
                label, _dedupe(prompts) or (label.replace("-", " ").replace("_", " "),)
            )
        )
    labels = [c.label for c in out]
    if len(set(labels)) != len(labels):
        dup = sorted({x for x in labels if labels.count(x) > 1})
        raise BadInput(f"duplicate category labels: {', '.join(dup)}")
    return out


def _text_file_categories(text: str) -> list[CategorySpec]:
    specs: list[CategorySpec] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        label, sep, rest = line.partition(":")
        if sep:
            specs.append(
                {
                    "id": label.strip(),
                    "prompts": [
                        label.strip(),
                        *[p.strip() for p in rest.split(",") if p.strip()],
                    ],
                }
            )
        else:
            specs.append(line)
    return specs


def load_categories(spec: str | Path | None) -> list[Category]:
    """`--categories`: None -> the default workshop vocabulary; an existing file (JSON list / JSON object wrapping a list under
    one of `WRAPPER_KEYS` / one label per line, optional `label: synonym, synonym`); otherwise a comma list "a,b,c"."""
    if spec is None or str(spec).strip() == "":
        return parse_categories(None)
    try:
        path = Path(spec)
        is_file = path.is_file()
    except OSError:  # a long comma list is not a valid file name
        is_file = False
    if not is_file:
        items = [c.strip() for c in str(spec).split(",") if c.strip()]
        if not items:
            raise BadInput("--categories is empty")
        if len(items) == 1 and (
            "/" in items[0] or items[0].endswith((".txt", ".json"))
        ):
            raise BadInput(
                f"categories file not found: {items[0]}",
                'a file, or a comma list like "screwdriver,pliers"',
            )
        return parse_categories(items)
    text = path.read_text(encoding="utf-8")
    if text.lstrip().startswith(("[", "{")):
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise BadInput(f"{path} is not valid JSON: {exc}") from exc
        if isinstance(data, dict):
            data = next((data[k] for k in WRAPPER_KEYS if k in data), None)
        if not isinstance(data, list):
            raise BadInput(
                f"{path}: expected a JSON list (or an object with a '{WRAPPER_KEYS[0]}' list)"
            )
        specs: list[CategorySpec] = data
    else:
        specs = _text_file_categories(text)
    if not specs:
        raise BadInput(f"{path} has no categories")
    return parse_categories(specs)


def as_items(
    masks: Sequence[MaskLike], shape: tuple[int, ...] | None = None
) -> list[tuple[int, np.ndarray]]:
    """Instances (dicts with 'mask' and optional 'id', e.g. `Segmenter.run()["instances"]`) or bare bool arrays -> (id, mask).

    With the image `shape`, every mask must be exactly as large as the image."""
    items: list[tuple[int, np.ndarray]] = []
    for k, m in enumerate(masks, 1):
        if isinstance(m, Mapping):
            if "mask" not in m:
                raise BadInput(
                    "instance dicts passed to classify() need a 'mask' entry"
                )
            try:
                iid = int(m.get("id", k))
            except (TypeError, ValueError) as exc:
                raise BadInput(
                    f"instance id {m.get('id')!r} is not an integer"
                ) from exc
            items.append((iid, np.asarray(m["mask"], bool)))
        else:
            items.append((k, np.asarray(m, bool)))
    ids = [i for i, _ in items]
    if len(set(ids)) != len(ids):
        raise BadInput("instance ids must be unique")
    for iid, mask in items:
        if mask.ndim != 2 or not mask.any():
            raise BadInput(
                f"instance {iid}: the mask must be a non-empty HxW boolean array"
            )
        if shape is not None:
            bank_mod.check_mask(mask, shape, f"instance {iid}: the mask")
    return items


def load_instances(
    instances_json: str | Path, image_shape: tuple[int, int]
) -> list[dict[str, Any]]:
    """The instances of a `3d segment` run (`instances.json`) with their masks loaded: [{"id", "mask": bool HxW, ...}].

    `image_shape` is (height, width) of the photo that was segmented; every mask must match it."""
    p = Path(instances_json)
    if not p.is_file():
        raise BadInput(
            f"instances file not found: {p}", "run `3d segment <image> -o DIR` first"
        )
    try:
        records = json.loads(p.read_text(encoding="utf-8"))["instances"]
        out = [{**rec, "mask": io.load_mask(p.parent / rec["mask"])} for rec in records]
    except (ValueError, KeyError, TypeError) as exc:
        raise BadInput(f"{p} is not a `3d segment` instances.json: {exc!r}") from exc
    for rec in out:
        mask = rec["mask"]
        if mask.shape != tuple(image_shape):
            raise BadInput(
                f"mask {rec.get('id')} is {mask.shape[1]}x{mask.shape[0]}, the image is {image_shape[1]}x{image_shape[0]}",
                "pass the photo that was segmented",
            )
    return out


def calibration_rows(
    items: Sequence[tuple[int, np.ndarray]], size: int, min_area_frac: float
) -> np.ndarray | None:
    """Row indices of the calibration pool: the `size` largest masks that cover at least `min_area_frac` of the image.

    The per-class mean is a property of the objects on the table, not of the small fragments a segmenter also returns (on the
    carpet photo, averaging over all 98 instances instead of the 60 largest costs 9 points of top-1: 0.64 -> 0.55). Returns None
    (= use every row) when `size` is 0 or fewer than `MIN_CALIBRATION_CROPS` masks qualify.
    """
    if size <= 0 or not items:
        return None
    areas = np.array([int(m.sum()) for _, m in items])
    keep = np.flatnonzero(areas >= min_area_frac * items[0][1].size)
    if len(keep) < MIN_CALIBRATION_CROPS:
        return None
    pool = np.asarray(keep[np.argsort(-areas[keep], kind="stable")[:size]])
    return pool if len(pool) >= MIN_CALIBRATION_CROPS else None


def calibrate_scores(
    sim: np.ndarray, prior: np.ndarray | None = None, rows: np.ndarray | None = None
) -> tuple[np.ndarray, bool]:
    """Subtract the per-class mean similarity: `prior` if given, else the mean over `rows` (default: every row).

    Returns (scores, was_calibrated); without a prior fewer than `MIN_CALIBRATION_CROPS` rows are not calibrated."""
    if prior is not None:
        if prior.shape != (sim.shape[1],):
            raise BadInput(
                f"prior must have one value per category ({sim.shape[1]}), got shape {prior.shape}"
            )
        return sim - prior[None, :], True
    pool = sim if rows is None else sim[rows]
    if pool.shape[0] < MIN_CALIBRATION_CROPS:
        return sim, False
    return sim - pool.mean(axis=0, keepdims=True), True


class Recogniser:
    """CLIP text classification (+ optional bank retrieval). Encoders may be shared: pass the service's resident ones."""

    vram_estimate_mb = ClipEncoder.vram_estimate_mb + DinoEncoder.vram_estimate_mb

    def __init__(
        self,
        device: str = "auto",
        clip: ClipEncoder | None = None,
        dino: DinoEncoder | None = None,
        templates: Sequence[str] = TEMPLATES,
    ) -> None:
        self.requested = device
        self.templates = list(templates)
        self._clip, self._dino = clip, dino
        self._owns_clip, self._owns_dino = clip is None, dino is None
        self._text_cache: dict[tuple[Category, ...], np.ndarray] = {}

    @property
    def loaded(self) -> bool:
        return self._clip is not None and self._clip.loaded

    @property
    def device(self) -> str:
        return self._clip.device if self._clip is not None else "cpu"

    def _get_clip(self) -> ClipEncoder:
        if self._clip is None:
            self._clip = ClipEncoder(self.requested)
        return self._clip

    def _get_dino(self) -> DinoEncoder:
        if self._dino is None:
            self._dino = DinoEncoder(self.requested)
        return self._dino

    def load(self) -> None:
        """Load CLIP (DINOv2 is loaded on the first bank query that needs it)."""
        self._get_clip().load()

    def unload(self) -> None:
        """Free the encoders this Recogniser created; injected ones stay with their owner."""
        if self._owns_clip and self._clip is not None:
            self._clip.unload()
            self._clip = None
        if self._owns_dino and self._dino is not None:
            self._dino.unload()
            self._dino = None
        self._text_cache.clear()

    def text_embeddings(self, categories: Sequence[Category]) -> np.ndarray:
        """(C, 512): per category the l2-normalised mean over its prompts x templates."""
        key = tuple(categories)
        if key not in self._text_cache:
            prompts = [
                t.format(p)
                for c in categories
                for p in c.prompts
                for t in self.templates
            ]
            emb = self._get_clip().texts(prompts)
            rows, at = [], 0
            for c in categories:
                n = len(c.prompts) * len(self.templates)
                rows.append(emb[at : at + n].mean(axis=0))
                at += n
            e = np.stack(rows)
            self._text_cache[key] = e / np.maximum(
                np.linalg.norm(e, axis=1, keepdims=True), 1e-12
            )
        return self._text_cache[key]

    def _similarity(
        self,
        rgb: np.ndarray,
        items: Sequence[tuple[int, np.ndarray]],
        cats: Sequence[Category],
    ) -> np.ndarray:
        crops = [crop_masked(rgb, m)[0] for _, m in items]
        return np.asarray(
            100.0 * self._get_clip().images(crops) @ self.text_embeddings(cats).T
        )

    def similarity(
        self,
        rgb: np.ndarray,
        masks: Sequence[MaskLike],
        categories: Sequence[CategorySpec] | None = None,
    ) -> np.ndarray:
        """(n_masks, n_categories) CLIP similarity x 100: the raw, uncalibrated scores."""
        return self._similarity(
            rgb, as_items(masks, rgb.shape), parse_categories(categories)
        )

    def _bank_scores(
        self,
        rgb: np.ndarray,
        items: Sequence[tuple[int, np.ndarray]],
        index: bank_mod.BankIndex,
        use: str,
    ) -> list[np.ndarray]:
        """Per instance the (N,) scores against every bank entry, embedded with this Recogniser's resident encoders."""
        parts = bank_mod.metric_parts(use)
        masks = [m for _, m in items]
        clip_q, dino_q = bank_mod.object_embeddings(
            rgb,
            masks,
            use,
            clip=self._get_clip() if "clip" in parts else None,
            dino=self._get_dino() if "dino" in parts else None,
        )
        return [
            bank_mod.score_object(
                index,
                use,
                mask,
                None if clip_q is None else clip_q[k],
                None if dino_q is None else dino_q[k],
            )
            for k, mask in enumerate(masks)
        ]

    def classify(
        self,
        rgb: np.ndarray,
        masks: Sequence[MaskLike],
        categories: Sequence[CategorySpec] | None = None,
        *,
        calibrate: bool = True,
        topk: int = 5,
        bank: bank_mod.BankIndex | str | Path | None = None,
        bank_topk: int | None = None,
        rerank_classes: int = 3,
        metric: str = "auto",
        prior: np.ndarray | None = None,
        pool_size: int = CALIBRATION_POOL,
        pool_min_area_frac: float = CALIBRATION_MIN_AREA_FRAC,
    ) -> list[dict[str, Any]]:
        """One record per mask: `{"id","text_topk":[{"label","score","raw"}],"bank_topk":[{"bank_id","score","metric",...}],"calibrated"}`.

        `categories`: strings or `{"id", "prompts"}` objects (see `parse_categories`); default the 22 workshop tools. Calibration
        subtracts the per-class mean similarity over a calibration pool (the `pool_size` largest masks that cover at least
        `pool_min_area_frac` of the image, see `calibration_rows`; needs >= 8, else `calibrated` is False and scores are raw)
        or an explicit `prior` (one value per category, e.g. kept from an earlier photo). `bank` (an index dir or a loaded
        `BankIndex`) adds retrieval restricted to the `rerank_classes` best text classes (`bank_restricted_to` lists them).
        """
        if rgb.ndim != 3 or rgb.shape[2] != 3:
            raise BadInput(
                f"classify expects an HxWx3 RGB array, got shape {rgb.shape}"
            )
        items = as_items(masks, rgb.shape)
        if not items:
            return []
        cats = parse_categories(categories)
        if topk < 1 or rerank_classes < 1 or (bank_topk is not None and bank_topk < 1):
            raise BadInput("topk, bank_topk and rerank_classes must be at least 1")
        index, use = None, ""
        if bank is not None:  # a bad index or metric fails before any model runs
            index = (
                bank
                if isinstance(bank, bank_mod.BankIndex)
                else bank_mod.load_index(bank)
            )
            use = bank_mod.resolve_metric(index, metric, True)
        raw = self._similarity(rgb, items, cats)
        pool = calibration_rows(items, pool_size, pool_min_area_frac)
        scores, calibrated = (
            calibrate_scores(raw, prior, pool) if calibrate else (raw, False)
        )
        bank_scores = (
            self._bank_scores(rgb, items, index, use) if index is not None else None
        )
        out: list[dict[str, Any]] = []
        for k, (iid, _) in enumerate(items):
            order = np.argsort(-scores[k], kind="stable")
            rec: dict[str, Any] = {
                "id": iid,
                "text_topk": [
                    {
                        "label": cats[j].label,
                        "score": round(float(scores[k, j]), 3),
                        "raw": round(float(raw[k, j]), 3),
                    }
                    for j in order[:topk]
                ],
                "bank_topk": [],
                "calibrated": calibrated,
            }
            if index is not None and bank_scores is not None:
                proposed = [cats[j].label for j in order[:rerank_classes]]
                rec["bank_restricted_to"] = proposed
                rec["bank_topk"] = bank_mod.rank(
                    index,
                    bank_scores[k],
                    topk if bank_topk is None else bank_topk,
                    use,
                    proposed,
                )
            out.append(rec)
        return out
