"""OCR backends and recognition helpers.

The backend contract is deliberately small: a list of RGB arrays in, a list of
item lists out.  Items use pixel coordinates relative to their own image.  This
is the seam where local RapidOCR, a remote HTTP service or a test fake plug in.
"""

from __future__ import annotations

import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

import numpy as np

from buspipe.config import CanvasSettings, OcrSettings
from buspipe.layout import (
    assign_canvas_items,
    build_canvases,
    make_mask,
    make_red_mask,
)


class OcrError(RuntimeError):
    """A non-retryable OCR contract/configuration failure."""


class OcrUnavailable(OcrError):
    """The backend cannot serve the request; callers may fall back locally."""


@dataclass(frozen=True)
class OcrParams:
    box_thresh: float = 0.3
    text_score: float = 0.1
    unclip_ratio: float = 1.5

    @classmethod
    def from_settings(cls, settings: OcrSettings) -> "OcrParams":
        return cls(
            box_thresh=settings.box_thresh,
            text_score=settings.text_score,
            unclip_ratio=settings.unclip_ratio,
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "box_thresh": self.box_thresh,
            "text_score": self.text_score,
            "unclip_ratio": self.unclip_ratio,
        }


class OcrBackend(Protocol):
    model_version: str

    def recognize(self, images: Sequence[np.ndarray], params: OcrParams) -> list[list[dict[str, Any]]]:
        ...


@dataclass
class OcrStats:
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    local_calls: int = 0
    remote_calls: int = 0
    remote_retries: int = 0
    remote_failures: int = 0
    fallbacks: int = 0
    images: int = 0
    canvas_batches: int = 0
    canvas_fallbacks: int = 0
    column_calls: int = 0

    def inc(self, name: str, amount: int = 1) -> None:
        with self.lock:
            setattr(self, name, getattr(self, name) + amount)

    def snapshot(self) -> dict[str, int]:
        with self.lock:
            return {
                "local_calls": self.local_calls,
                "remote_calls": self.remote_calls,
                "remote_retries": self.remote_retries,
                "remote_failures": self.remote_failures,
                "fallbacks": self.fallbacks,
                "images": self.images,
                "canvas_batches": self.canvas_batches,
                "canvas_fallbacks": self.canvas_fallbacks,
                "column_calls": self.column_calls,
            }


class LocalRapidOcrBackend:
    """RapidOCR on CPU, wrapped to the backend contract."""

    def __init__(
        self,
        threads: int = 4,
        model_version: str = "rapidocr-default",
        stats: OcrStats | None = None,
    ) -> None:
        from rapidocr import RapidOCR  # imported lazily: keeps tests fast

        self.model_version = model_version
        self.stats = stats or OcrStats()
        self._engine = RapidOCR(
            params={
                "EngineConfig.onnxruntime.intra_op_num_threads": max(1, int(threads)),
                "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                "Global.log_level": "error",
            }
        )

    def recognize(self, images: Sequence[np.ndarray], params: OcrParams) -> list[list[dict[str, Any]]]:
        self.stats.inc("local_calls")
        results: list[list[dict[str, Any]]] = []
        for image in images:
            result = self._engine(image, **params.as_dict())
            results.append(_items_from_rapidocr(result))
        return results


class FallbackOcrBackend:
    """Use the primary backend, falling back to a local one on unavailability."""

    def __init__(
        self,
        primary: OcrBackend,
        fallback: OcrBackend,
        stats: OcrStats | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.stats = stats or OcrStats()
        self.model_version = f"{primary.model_version}|fallback:{fallback.model_version}"

    def recognize(self, images: Sequence[np.ndarray], params: OcrParams) -> list[list[dict[str, Any]]]:
        try:
            return self.primary.recognize(images, params)
        except OcrError:
            self.stats.inc("fallbacks")
            return self.fallback.recognize(images, params)


def _items_from_rapidocr(result: Any) -> list[dict[str, Any]]:
    txts = result.txts if getattr(result, "txts", None) is not None else []
    boxes = result.boxes if getattr(result, "boxes", None) is not None else []
    scores = result.scores if getattr(result, "scores", None) is not None else []
    items: list[dict[str, Any]] = []
    for text, box, score in zip(txts, boxes, scores):
        box = np.asarray(box)
        items.append(
            {
                "text": text,
                "score": float(score),
                "x0": float(box[:, 0].min()),
                "x1": float(box[:, 0].max()),
                "y0": float(box[:, 1].min()),
                "y1": float(box[:, 1].max()),
            }
        )
    return normalize_items(items)


def normalize_items(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items:
        text = re.sub(r"\s+", "", str(item.get("text", ""))).strip()
        if not text:
            continue
        out.append(
            {
                "text": text,
                "score": float(item.get("score", 0.0)),
                "x0": float(item.get("x0", 0.0)),
                "x1": float(item.get("x1", 0.0)),
                "y0": float(item.get("y0", 0.0)),
                "y1": float(item.get("y1", 0.0)),
            }
        )
    return out


def offset_items(items: Sequence[dict[str, Any]], dx: float, dy: float) -> list[dict[str, Any]]:
    return [
        {**item, "x0": item["x0"] + dx, "x1": item["x1"] + dx, "y0": item["y0"] + dy, "y1": item["y1"] + dy}
        for item in items
    ]


def ocr_regions(
    img: np.ndarray,
    boxes: Sequence[tuple[int, int, int, int]],
    backend: OcrBackend,
    params: OcrParams,
    stats: OcrStats | None = None,
) -> list[list[dict[str, Any]]]:
    """OCR arbitrary regions, sending all non-empty crops in one backend call."""
    crops: list[np.ndarray] = []
    offsets: list[tuple[int, int, int]] = []  # (box position, x, y)
    results: list[list[dict[str, Any]]] = [[] for _ in boxes]
    for position, (x0, y0, x1, y1) in enumerate(boxes):
        crop = img[max(0, y0):max(0, y1), max(0, x0):max(0, x1)]
        if crop.size == 0:
            continue
        crops.append(np.ascontiguousarray(crop))
        offsets.append((position, x0, y0))
    if not crops:
        return results
    raw = backend.recognize(crops, params)
    if len(raw) != len(crops):
        raise OcrError(f"backend returned {len(raw)} results for {len(crops)} images")
    if stats:
        stats.inc("images", len(crops))
    for (position, dx, dy), items in zip(offsets, raw):
        results[position] = offset_items(normalize_items(items), dx, dy)
    return results


def dedup_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = sorted(items, key=lambda z: (z["y0"], z["x0"]))
    kept: list[dict[str, Any]] = []
    for item in items:
        duplicate = False
        for other in list(kept):
            overlap = min(item["y1"], other["y1"]) - max(item["y0"], other["y0"])
            min_height = max(1.0, min(item["y1"] - item["y0"], other["y1"] - other["y0"]))
            if overlap > 0.45 * min_height:
                if item["text"] == other["text"]:
                    if item["score"] > other["score"]:
                        other.update(item)
                    duplicate = True
                    break
                if item["text"] in other["text"]:
                    duplicate = True
                    break
                if other["text"] in item["text"]:
                    other.update(item)
                    duplicate = True
                    break
        if not duplicate:
            kept.append(item)
    kept.sort(key=lambda z: z["y0"])
    return kept


def join_items(items: list[dict[str, Any]]) -> tuple[str, float]:
    if not items:
        return "", 0.0
    text = "".join(item["text"] for item in sorted(items, key=lambda z: z["y0"]))
    weights = [max(1.0, item["y1"] - item["y0"]) for item in items]
    score = sum(item["score"] * w for item, w in zip(items, weights)) / sum(weights)
    return text, float(score)


# RapidOCR's detector is sensitive to the margin around a single huge glyph.
# Tight crops read some numbers and padded crops read others, so the route
# number is recognised at three paddings and the results vote.
ROUTE_NUMBER_PADDINGS: tuple[tuple[int, int], ...] = ((0, 0), (15, 5), (25, 10))


def route_number_boxes(
    block: tuple[int, int],
    bounds: tuple[int, int],
    image_shape: tuple[int, ...],
) -> list[tuple[int, int, int, int]]:
    y0, y1 = block
    x0, x1 = bounds
    height, width = image_shape[:2]
    return [
        (
            max(0, x0 - pad_x),
            max(0, y0 - pad_y),
            min(width, x1 + 1 + pad_x),
            min(height, y1 + 1 + pad_y),
        )
        for pad_y, pad_x in ROUTE_NUMBER_PADDINGS
    ]


def vote_route_items(
    candidates: Sequence[Sequence[dict[str, Any]]],
    min_score: float = 0.8,
) -> list[dict[str, Any]]:
    """Pick the most consistent high-confidence reading across crop paddings."""
    scored: list[tuple[str, float, list[dict[str, Any]]]] = []
    for items in candidates:
        text, score = join_items(list(items))
        if text:
            scored.append((text, score, list(items)))
    if not scored:
        return []
    confident = [candidate for candidate in scored if candidate[1] >= min_score]
    pool = confident or scored
    # Route identifiers are numeric or alphanumeric (K1, 通学专线2).  A crop
    # padding that misses the glyphs can instead read a neighbouring stop name,
    # so numeric readings win whenever any are present.
    numeric = [candidate for candidate in pool if re.search(r"[0-9A-Za-z]", candidate[0])]
    if numeric:
        pool = numeric
    counts = Counter(text for text, _, _ in pool)
    best = max(
        counts,
        key=lambda text: (
            counts[text],
            max(score for candidate, score, _ in pool if candidate == text),
        ),
    )
    for text, _, items in pool:
        if text == best:
            return items
    return []


def _red_ratio(img: np.ndarray, col: dict[str, Any], settings: OcrSettings) -> float:
    x0 = max(0, col["x0"] - 2)
    x1 = min(img.shape[1], col["x1"] + 3)
    y0 = max(0, col["y0"] - 2)
    y1 = min(img.shape[0], col["y1"] + 5)
    red_mask = make_red_mask(img[y0:y1, x0:x1])
    text_mask = make_mask(img[y0:y1, x0:x1])
    return float(red_mask.sum()) / max(1, int(text_mask.sum()))


def recognize_column(
    img: np.ndarray,
    col: dict[str, Any],
    backend: OcrBackend,
    settings: OcrSettings,
    pad_left: int | None = None,
    pad_right: int | None = None,
    flags: Sequence[str] = (),
    stats: OcrStats | None = None,
) -> dict[str, Any]:
    """OCR one vertical stop column, including dropped-character recovery."""
    pad_left = settings.pad_left if pad_left is None else pad_left
    pad_right = settings.pad_right if pad_right is None else pad_right
    x0 = max(0, col["x0"] - pad_left)
    x1 = min(img.shape[1], col["x1"] + pad_right)
    y0 = max(0, col["y0"] - settings.pad_top)
    y1 = min(img.shape[0], col["y1"] + settings.pad_bottom)
    crop = np.ascontiguousarray(img[y0:y1, x0:x1])
    items: list[dict[str, Any]] = []
    if crop.size:
        raw = backend.recognize([crop], OcrParams.from_settings(settings))
        if len(raw) != 1:
            raise OcrError(f"backend returned {len(raw)} results for one column")
        if stats:
            stats.inc("column_calls")
            stats.inc("images")
        items = dedup_items(offset_items(normalize_items(raw[0]), x0, y0))

    # Recover a character the detector dropped between two recognised characters.
    if items and not settings.fast:
        heights = [max(1.0, item["y1"] - item["y0"]) for item in items]
        median_height = float(np.median(heights))
        extra: list[dict[str, Any]] = []
        for prev, nxt in zip(items, items[1:]):
            gap = nxt["y0"] - prev["y1"]
            if gap <= settings.gap_height_ratio * median_height:
                continue
            gy0 = max(y0, int(prev["y1"]) - 2)
            gy1 = min(y1, int(nxt["y0"]) + 2)
            if gy1 - gy0 < 8:
                continue
            gap_crop = np.ascontiguousarray(img[gy0:gy1, x0:x1])
            gap_mask = make_mask(gap_crop)
            if int(gap_mask.sum()) < settings.gap_min_pixels:
                continue
            raw_gap = backend.recognize(
                [gap_crop],
                OcrParams(box_thresh=0.25, text_score=0.05, unclip_ratio=1.6),
            )
            if len(raw_gap) != 1:
                raise OcrError(f"backend returned {len(raw_gap)} results for gap crop")
            gap_items = dedup_items(offset_items(normalize_items(raw_gap[0]), x0, gy0))
            gap_text, gap_score = join_items(gap_items)
            if gap_text and gap_score >= settings.gap_score and 1 <= len(gap_text) <= 4:
                extra.append(
                    {
                        "text": gap_text,
                        "score": gap_score,
                        "x0": x0,
                        "x1": x1,
                        "y0": float(prev["y1"]),
                        "y1": float(nxt["y0"]),
                    }
                )
        if extra:
            items = dedup_items(items + extra)

    text, score = join_items(items)
    out = dict(col)
    out.update(
        {
            "text": text,
            "score": score,
            "red_ratio": _red_ratio(img, col, settings),
            "items": items,
            "fallback_flags": list(flags),
        }
    )
    return out


def _choose_column_result(
    img: np.ndarray,
    col: dict[str, Any],
    assigned: list[dict[str, Any]],
    row_height: int,
    settings: OcrSettings,
    backend: OcrBackend,
    flags: list[str],
    stats: OcrStats | None,
) -> dict[str, Any]:
    boxes = dedup_items(assigned)
    if boxes:
        full_boxes = [b for b in boxes if (b["y1"] - b["y0"]) >= settings.full_box_height_ratio * row_height]
        if full_boxes:
            best = max(full_boxes, key=lambda b: (len(b["text"]), b["score"]))
            text, score = best["text"], best["score"]
            chosen = [best]
        else:
            text, score = join_items(boxes)
            chosen = boxes
        coverage = max(b["y1"] for b in boxes) - min(b["y0"] for b in boxes)
    else:
        text, score, chosen, coverage = "", 0.0, [], 0.0
    if not text or score < settings.fallback_score or coverage < settings.fallback_coverage * row_height:
        if not text:
            flags.append("empty")
        elif score < settings.fallback_score:
            flags.append("low_score")
        else:
            flags.append("low_coverage")
        return recognize_column(img, col, backend, settings, flags=flags, stats=stats)
    out = dict(col)
    out.update(
        {
            "text": text,
            "score": float(score),
            "red_ratio": _red_ratio(img, col, settings),
            "items": chosen,
            "fallback_flags": flags,
        }
    )
    return out


def recognize_columns(
    img: np.ndarray,
    columns: Sequence[dict[str, Any]],
    row_y0: int,
    row_y1: int,
    backend: OcrBackend,
    settings: OcrSettings,
    canvas_settings: CanvasSettings,
    stats: OcrStats | None = None,
) -> list[dict[str, Any]]:
    """Recognize every stop column, batching canvases or one-by-one."""
    if not columns:
        return []
    row_height = row_y1 - row_y0 + 1
    if settings.mode == "batch":
        canvases = build_canvases(img, columns, row_y0, row_y1, canvas_settings)
        if not canvases:
            return []
        try:
            raw = backend.recognize([c.image for c in canvases], OcrParams.from_settings(settings))
            if len(raw) != len(canvases):
                raise OcrError(f"backend returned {len(raw)} results for {len(canvases)} canvases")
        except OcrError:
            # A whole-canvas failure must not lose the block: re-run each column
            # independently before giving up.
            if stats:
                stats.inc("canvas_fallbacks")
            return sorted(
                (
                    recognize_column(
                        img, col, backend, settings, flags=["canvas_fallback"], stats=stats
                    )
                    for col in columns
                ),
                key=lambda c: c["x0"],
            )
        if stats:
            stats.inc("canvas_batches", len(canvases))
            stats.inc("images", len(canvases))
        assigned_by_column: dict[int, list[dict[str, Any]]] = {i: [] for i in range(len(columns))}
        fallback_by_column: dict[int, set[str]] = {i: set() for i in range(len(columns))}
        for canvas, items in zip(canvases, raw):
            assigned, needs_fallback, flags = assign_canvas_items(
                normalize_items(items), canvas, columns, settings.fallback_box_width_ratio
            )
            for index, values in assigned.items():
                assigned_by_column[index].extend(values)
            for index in needs_fallback:
                fallback_by_column[index].update(flags.get(index, []) or ["cross_column"])
            # A column may sit on only one canvas, but keep the union anyway.
            for index, flag_list in flags.items():
                if index in needs_fallback:
                    fallback_by_column[index].update(flag_list)
        results: list[dict[str, Any]] = []
        for index, col in enumerate(columns):
            flags = sorted(fallback_by_column[index])
            if index in fallback_by_column and fallback_by_column[index]:
                results.append(recognize_column(img, col, backend, settings, flags=flags, stats=stats))
            else:
                results.append(
                    _choose_column_result(
                        img, col, assigned_by_column[index], row_height, settings, backend, flags, stats
                    )
                )
        return sorted(results, key=lambda c: c["x0"])

    # Column-by-column mode: preserve the legacy padding/retry behaviour.
    results = []
    for ci, col in enumerate(columns):
        gap_left = (col["x0"] - columns[ci - 1]["x1"]) if ci > 0 else 30
        gap_right = (columns[ci + 1]["x0"] - col["x1"]) if ci + 1 < len(columns) else 30
        pad_left = min(10, max(1, (gap_left - 2) // 2))
        pad_right = min(10, max(1, (gap_right - 2) // 2))
        best = recognize_column(img, col, backend, settings, pad_left=pad_left, pad_right=pad_right, stats=stats)
        if not settings.fast and (not best["text"] or best["score"] < settings.retry_score):
            wide_left = min(22, max(pad_left, (gap_left - 2) // 2))
            wide_right = min(22, max(pad_right, (gap_right - 2) // 2))
            if wide_left > pad_left or wide_right > pad_right:
                retry = recognize_column(
                    img, col, backend, settings, pad_left=wide_left, pad_right=wide_right, stats=stats
                )
                if retry["text"] and retry["score"] > best["score"] + settings.retry_gain:
                    retry["fallback_flags"] = sorted(set(retry.get("fallback_flags", []) + ["wide_padding_retry"]))
                    best = retry
        results.append(best)
    return sorted(results, key=lambda c: c["x0"])
