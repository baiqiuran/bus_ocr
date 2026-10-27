"""Single-route horizontal boards: front captions and back terminal columns.

All boxes refer to the original image. Only the route and terminal regions are
sent to OCR; filename compass directions never determine route reading order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np
from PIL import Image

from buspipe.config import LandscapeSettings, PipelineSettings
from buspipe.endpoints import assemble_endpoint
from buspipe.layout import (
    LayoutSettings,
    detect_stop_columns,
    group_columns,
    group_indices,
    make_mask,
    make_red_mask,
)
from buspipe.ocr import OcrBackend, OcrError, OcrParams, OcrStats, dedup_items, normalize_items
from buspipe.parsing import pick_route_number, refresh_entry_validation


class UnsupportedLandscapeError(RuntimeError):
    pass


# Wide single-route boards put the route number and "下站/开往" caption above a
# full-width row of vertical stop names.  The regular landscape templates put
# their caption at the bottom, so these need a separate geometry probe.
# These are module constants rather than PipelineSettings fields on purpose:
# changing the settings fingerprint would invalidate the block cache for the
# already completed vertical retry.
_WIDE_MIN_ASPECT = 2.6
_WIDE_ROUTE_RIGHT = 0.25
_WIDE_ROUTE_BOTTOM = 0.34
_WIDE_ROW_MIN_HEIGHT = 0.10
_WIDE_ROW_MAX_HEIGHT = 0.62
_WIDE_ROW_MIN_TOP = 0.15
_WIDE_ROW_MAX_BOTTOM = 0.92
_WIDE_MIN_GROUPS = 5
_WIDE_MAX_GROUPS = 70
_WIDE_COLUMN_MAX_WIDTH = 0.09
_WIDE_COLUMN_MIN_HEIGHT = 0.10
_WIDE_COLUMN_MIN_ASPECT = 1.8
_WIDE_EDGE_MARGIN = 0.20


@dataclass
class LandscapeLayout:
    template: str
    route_box: tuple[int, int, int, int]
    groups: list[list[dict[str, Any]]]
    arrow: str = "right"


def _bbox(mask: np.ndarray, dx: int = 0, dy: int = 0) -> tuple[int, int, int, int]:
    ys, xs = np.where(mask)
    if not len(xs):
        raise UnsupportedLandscapeError("empty text region")
    return int(xs.min()) + dx, int(ys.min()) + dy, int(xs.max()) + dx + 1, int(ys.max()) + dy + 1


def _foreground(rgb: np.ndarray, delta: int) -> np.ndarray:
    background = np.median(rgb.reshape(-1, 3)[::4], axis=0)
    return np.abs(rgb.astype(np.int16) - background).max(axis=2) > delta


def _column(box: tuple[int, int, int, int], vertical: bool) -> dict[str, Any]:
    x0, y0, x1, y1 = box
    return {"x0": x0, "y0": y0, "x1": x1 - 1, "y1": y1 - 1, "vertical": vertical}


def _front_groups(rgb: np.ndarray, red: np.ndarray, cfg: LandscapeSettings) -> list[list[dict[str, Any]]]:
    height, width = rgb.shape[:2]
    y0, y1 = round(height * cfg.caption_top), round(height * cfg.caption_bottom)
    caption_red = red[y0:y1, :round(width * 0.9)]
    if caption_red.sum() < height * width * 0.0001:
        raise UnsupportedLandscapeError("no front direction separator")
    sx0, sy0, sx1, sy1 = _bbox(caption_red, dy=y0)
    if not width * 0.05 < sx0 < sx1 < width * 0.85 or sy1 - sy0 < height * 0.025:
        raise UnsupportedLandscapeError("invalid front direction separator")
    # The red word is excluded from OCR. This also handles 开往 -> 开住 OCR
    # mistakes without guessing or rewriting any station-name character.
    top, bottom = max(y0, sy0 - 8), min(y1, sy1 + 8)
    groups = []
    for left, right in ((0, sx0), (sx1, width)):
        region = rgb[top:bottom, left:right]
        mask = _foreground(region, cfg.foreground_delta)
        xs = np.flatnonzero(mask.sum(axis=0) > max(2, (bottom - top) * 0.06))
        runs = group_indices(xs, max_gap=max(2, round((sy1 - sy0) * 0.22)))
        runs = [(a, b) for a, b in runs if b - a > width * 0.012]
        if not runs:
            raise UnsupportedLandscapeError("cannot locate both sides of front caption")
        # Keep the caption nearest the separator, excluding the separate logo.
        a, b = runs[-1] if left == 0 else runs[0]
        box = _bbox(mask[:, a:b + 1], dx=left + a, dy=top)
        groups.append([_column(box, False)])
    return groups


def _back_groups(
    rgb: np.ndarray, divider: int, cfg: LandscapeSettings,
) -> list[list[dict[str, Any]]]:
    height, width = rgb.shape[:2]
    y0, y1 = divider + round(height * cfg.row_top_offset), round(height * cfg.row_bottom)
    if y1 <= y0:
        raise UnsupportedLandscapeError("invalid back station row")
    # Request-stop boards have a yellow explanatory footer beneath the white
    # stop row. Stop at that background transition instead of merging all
    # columns through the footer.
    backgrounds = np.median(rgb[y0:y1, ::4], axis=1)
    baseline = np.median(backgrounds[:max(1, round(height * 0.03))], axis=0)
    transitions = np.flatnonzero(np.abs(backgrounds - baseline).max(axis=1) > cfg.foreground_delta)
    transitions = transitions[transitions > height * cfg.min_column_height]
    if len(transitions):
        y1 = min(y1, y0 + int(transitions[0]) - 3)
    mask = _foreground(rgb[y0:y1], cfg.foreground_delta)

    def detect(mask: np.ndarray, top: int) -> list[dict[str, Any]]:
        xs = np.flatnonzero(mask.sum(axis=0) > max(3, mask.shape[0] * 0.03))
        columns = []
        for a, b in group_indices(xs, max_gap=max(2, round(width * 0.003))):
            if b - a < width * 0.004:
                continue
            box = _bbox(mask[:, a:b + 1], dx=a, dy=top)
            if box[3] - box[1] >= height * cfg.min_column_height:
                columns.append(_column(box, True))
        return columns

    columns = detect(mask, y0)
    if columns:
        # Amenity icons sit above the shared first-character baseline. They
        # must not join adjacent stop columns or become endpoint characters.
        top = max(y0, int(np.quantile([c["y0"] for c in columns], 0.60)) - 2)
        columns = detect(mask[top - y0:], top)
    groups = group_columns(columns)
    if len(groups) < 2:
        raise UnsupportedLandscapeError("cannot locate two back terminal columns")
    first, last = [columns[i] for i in groups[0]], [columns[i] for i in groups[-1]]
    if len(first) == 1 and len(groups) > 2:
        probe = columns[groups[1][0]]
        if probe["x1"] - probe["x0"] < 0.95 * (first[0]["x1"] - first[0]["x0"]):
            first.append({**probe, "note_probe": True})
    if len(last) == 1 and len(groups) > 2:
        probe = columns[groups[-2][0]]
        if last[0]["x1"] - last[0]["x0"] < 0.95 * (probe["x1"] - probe["x0"]):
            last.insert(0, {**probe, "terminal_probe": True})
    return [first, last]


def locate_landscape(rgb: np.ndarray, cfg: LandscapeSettings) -> LandscapeLayout:
    height, width = rgb.shape[:2]
    if not cfg.min_aspect <= width / height <= cfg.max_aspect:
        raise UnsupportedLandscapeError("unrecognized board aspect ratio")
    red = make_red_mask(rgb)
    route_mask = red[:round(height * cfg.route_bottom), :round(width * cfg.route_right)]
    if route_mask.sum() < width * height * 0.005:
        raise UnsupportedLandscapeError("no prominent red route title")
    route_box = _bbox(route_mask)
    if route_box[2] - route_box[0] < width * 0.08 or route_box[3] - route_box[1] < height * 0.08:
        raise UnsupportedLandscapeError("invalid route title")
    start, end = round(height * 0.35), round(height * 0.65)
    rows = np.flatnonzero(red[start:end].sum(axis=1) >= width * cfg.divider_min_width)
    if not len(rows):
        return LandscapeLayout("landscape_front", route_box, _front_groups(rgb, red, cfg))
    divider = int(rows[0]) + start
    head = red[max(start, divider - round(height * 0.035)):divider]
    left, right = int(head[:, :round(width * 0.08)].sum()), int(head[:, round(width * 0.92):].sum())
    arrow = "unknown"
    if max(left, right) > width * height * 0.00003 and max(left, right) > 2 * min(left, right):
        arrow = "left" if left > right else "right"
    return LandscapeLayout("landscape_back", route_box, _back_groups(rgb, divider, cfg), arrow)


def _wide_row_band(rgb: np.ndarray) -> tuple[int, int]:
    """Return the densest full-width band of tall vertical stop names."""
    height, width = rgb.shape[:2]
    mask = make_mask(rgb)
    counts = mask.sum(axis=1)
    threshold = max(5, round(width * 0.012))
    runs = group_indices(np.flatnonzero(counts > threshold), max_gap=12)
    candidates: list[tuple[int, int, int]] = []
    for y0, y1 in runs:
        band_height = y1 - y0 + 1
        if not height * _WIDE_ROW_MIN_HEIGHT <= band_height <= height * _WIDE_ROW_MAX_HEIGHT:
            continue
        if y0 < height * _WIDE_ROW_MIN_TOP or y1 > height * _WIDE_ROW_MAX_BOTTOM:
            continue
        candidates.append((int(counts[y0:y1 + 1].sum()), int(y0), int(y1)))
    if not candidates:
        raise UnsupportedLandscapeError("cannot locate wide stop row")
    _, y0, y1 = max(candidates)
    return y0, y1


def locate_wide_landscape(rgb: np.ndarray, cfg: LandscapeSettings) -> LandscapeLayout:
    """Locate the wide, single-route "full stop row" family.

    The route line is printed top-left, followed by a caption such as
    ``下站… 开往…``.  The first and last stops are the two ends of the vertical
    stop row below it; the caption's next stop is deliberately not used.
    """
    height, width = rgb.shape[:2]
    if width / height < _WIDE_MIN_ASPECT:
        raise UnsupportedLandscapeError("not a wide landscape board")

    red = make_red_mask(rgb)
    # Page frames and full-width rules can otherwise dominate the route box.
    frame_columns = red.mean(axis=0) >= 0.98
    frame_rows = red.mean(axis=1) >= 0.98
    red[:, frame_columns] = False
    red[frame_rows, :] = False
    route_region = red[
        : round(height * _WIDE_ROUTE_BOTTOM),
        : round(width * _WIDE_ROUTE_RIGHT),
    ]
    if route_region.sum() < width * height * 0.0008:
        raise UnsupportedLandscapeError("no wide route title")
    route_box = _bbox(route_region)
    route_width = route_box[2] - route_box[0]
    route_height = route_box[3] - route_box[1]
    if route_width < width * 0.02 or route_height < height * 0.04:
        raise UnsupportedLandscapeError("invalid wide route title")

    row_y0, row_y1 = _wide_row_band(rgb)
    layout_settings = LayoutSettings(stop_x0=0, stop_x1=width)
    columns = detect_stop_columns(rgb, row_y0, row_y1, layout_settings, split_wide=True)
    filtered: list[dict[str, Any]] = []
    for column in columns:
        column_width = column["x1"] - column["x0"] + 1
        column_height = column["y1"] - column["y0"] + 1
        if column_width > width * _WIDE_COLUMN_MAX_WIDTH:
            continue
        if column_height < height * _WIDE_COLUMN_MIN_HEIGHT:
            continue
        if column_height / max(1, column_width) < _WIDE_COLUMN_MIN_ASPECT:
            continue
        filtered.append({**column, "vertical": True})
    if not filtered:
        raise UnsupportedLandscapeError("no wide stop columns")

    groups = group_columns(filtered)
    if not _WIDE_MIN_GROUPS <= len(groups) <= _WIDE_MAX_GROUPS:
        raise UnsupportedLandscapeError(f"unexpected wide stop-column count: {len(groups)}")
    first = [filtered[index] for index in groups[0]]
    last = [filtered[index] for index in groups[-1]]
    if first[0]["x0"] > width * _WIDE_EDGE_MARGIN or last[-1]["x1"] < width * (1 - _WIDE_EDGE_MARGIN):
        raise UnsupportedLandscapeError("wide terminal columns are not at the row edges")
    return LandscapeLayout("landscape_wide", route_box, [first, last], "right")


def route_from_filename(poster: str) -> str:
    match = re.search(r"-[东西南北][^-]*-(.+)-\d+$", poster)
    if match:
        route = canonical_route(match[1])
        if route:
            return route
        # Artwork notes after the route number (e.g. 108-电子菲林) are common.
        return canonical_route(match[1].split("-", 1)[0])
    # Terminal/boarding-area artwork often has no compass token.  The route
    # follows a layout marker instead, e.g. 金山公交总站-出站-111-菲林-01.
    match = re.search(
        r"-(?:出站|上客点|下客点|上客区|下客区|场站|首末站|站外|站内|上车点|下车点|站台)-([^-]+)",
        poster,
    )
    return canonical_route(match[1]) if match else ""


def canonical_route(text: str) -> str:
    text = re.sub(r"\s+", "", text).replace("（", "(").split("(")[0]
    shuttle = re.fullmatch(r"(?:地铁)?接驳(\d+)(?:号)?(?:专线|路)?", text)
    if shuttle:
        return "接驳" + shuttle[1]
    return pick_route_number([{"text": text, "score": 1.0, "x0": 0, "x1": 100, "y0": 0, "y1": 100}], {}, "", 0)


def _ocr_image(mask: np.ndarray, target_height: int) -> tuple[np.ndarray, float]:
    """Scale glyphs into the detector's useful range, with clear white margins."""
    image = Image.fromarray(np.where(mask, 0, 255).astype(np.uint8)).convert("RGB")
    scale = min(1.0, target_height / image.height)
    image = image.resize((max(2, round(image.width * scale)), max(2, round(image.height * scale))))
    array = np.pad(np.asarray(image), ((20, 20), (20, 20), (0, 0)), constant_values=255)
    return array, scale


def _vertical_ocr_image(mask: np.ndarray, glyph_height: int) -> tuple[np.ndarray, bool]:
    """Reflow only clearly separated, similarly sized glyphs; otherwise keep vertical text."""
    runs = group_indices(np.flatnonzero(mask.sum(axis=1) > 1), max_gap=max(2, round(mask.shape[1] * 0.18)))
    heights = [b - a + 1 for a, b in runs]
    median = float(np.median(heights)) if heights else 0.0
    brackets = (len(runs) >= 4 and heights[0] < median * 0.6 and heights[-1] < median * 0.6)
    body = heights[1:-1] if brackets else heights
    if (len(body) < 2 or min(body) < median * 0.6 or max(body) > median * 1.6
            or max(body) > mask.shape[1] * 1.8):
        return _ocr_image(mask, mask.shape[0])[0], True
    pieces = []
    for index, (a, b) in enumerate(runs):
        part = mask[a:b + 1]
        xs = np.flatnonzero(part.any(axis=0))
        image = Image.fromarray(np.where(part[:, xs[0]:xs[-1] + 1], 0, 255).astype(np.uint8))
        if brackets and index in {0, len(runs) - 1}:
            # Vertical ︵ / ︶ become ordinary parentheses when reflowed.
            image = image.transpose(Image.Transpose.ROTATE_90)
        image = image.resize((max(4, round(image.width * glyph_height / image.height)), glyph_height))
        pieces.append(image)
    canvas = Image.new("RGB", (sum(p.width + 4 for p in pieces) + 36, glyph_height + 40), "white")
    cursor = 20
    for piece in pieces:
        canvas.paste(piece, (cursor, 20))
        cursor += piece.width + 4
    return np.asarray(canvas), False


def _ocr_targets(
    rgb: np.ndarray, columns: Sequence[dict[str, Any]], backend: OcrBackend,
    settings: PipelineSettings, stats: OcrStats | None,
) -> list[dict[str, Any]]:
    crops = []
    vertical_orders = []
    for col in columns:
        crop = rgb[col["y0"]:col["y1"] + 1, col["x0"]:col["x1"] + 1]
        # Include a background border when estimating the paper colour: tight
        # glyph bounds can otherwise contain more ink than background.
        x0, y0 = max(0, col["x0"] - 8), max(0, col["y0"] - 8)
        context = rgb[y0:min(rgb.shape[0], col["y1"] + 9), x0:min(rgb.shape[1], col["x1"] + 9)]
        border = np.concatenate((context[0], context[-1], context[:, 0], context[:, -1]))
        background = np.median(border, axis=0)
        mask = np.abs(crop.astype(np.int16) - background).max(axis=2) > settings.landscape.foreground_delta
        if col["vertical"]:
            image, vertical = _vertical_ocr_image(mask, settings.landscape.glyph_height)
        else:
            image, vertical = _ocr_image(mask, settings.landscape.glyph_height)[0], False
        crops.append(image)
        vertical_orders.append(vertical)
    raw = backend.recognize(crops, OcrParams.from_settings(settings.ocr))
    if len(raw) != len(crops):
        raise OcrError("backend result count differs from landscape target count")
    if stats:
        stats.inc("images", len(crops))
    values = []
    for col, items, vertical in zip(columns, raw, vertical_orders):
        items = dedup_items(normalize_items(items))
        items.sort(key=lambda item: item["y0"] if vertical else item["x0"])
        text = "".join(item["text"] for item in items)
        values.append({**col, "text": text, "score": min((item["score"] for item in items), default=0.0),
                       "fallback_flags": [] if text else ["empty"]})
    return values


def _ocr_route(
    rgb: np.ndarray, box: tuple[int, int, int, int], backend: OcrBackend,
    settings: PipelineSettings, stats: OcrStats | None,
) -> list[dict[str, Any]]:
    x0, y0, x1, y1 = box
    mask = make_red_mask(rgb[y0:y1, x0:x1])
    image, scale = _ocr_image(mask, settings.landscape.glyph_height)
    raw = backend.recognize([image], OcrParams.from_settings(settings.ocr))
    if len(raw) != 1:
        raise OcrError("backend result count differs from landscape route count")
    if stats:
        stats.inc("images")
    items = normalize_items(raw[0])
    for item in items:
        for key, offset in (("x0", x0), ("x1", x0), ("y0", y0), ("y1", y0)):
            item[key] = (item[key] - 20) / scale + offset
    return items


def extract_landscape_entry(
    rgb: np.ndarray, layout: LandscapeLayout, poster: str, station: str, direction: str,
    backend: OcrBackend, settings: PipelineSettings, stats: OcrStats | None,
) -> dict[str, Any]:
    number_items = _ocr_route(rgb, layout.route_box, backend, settings, stats)
    ordered = sorted(number_items, key=lambda item: item["x0"])
    route_ocr = canonical_route("".join(item["text"] for item in ordered))
    if not route_ocr:
        route_ocr = pick_route_number(number_items, {}, poster, 0)
    route_hint = route_from_filename(poster)
    route = route_hint or route_ocr
    groups = layout.groups
    columns = [col for group in groups for col in group]
    recognized = _ocr_targets(rgb, columns, backend, settings, stats)
    split = len(groups[0])
    first_group, last_group = recognized[:split], recognized[split:]
    first_group = [c for c in first_group if not c.get("note_probe") or c["text"].startswith(("(", "（"))]
    if last_group[0].get("terminal_probe") and not last_group[-1]["text"].startswith(("(", "（")):
        last_group = last_group[1:]
    if layout.arrow == "left":
        first_group, last_group = last_group, first_group
    endpoints = [assemble_endpoint(first_group, "first"), assemble_endpoint(last_group, "last")]
    if layout.arrow == "unknown":
        for endpoint in endpoints:
            endpoint["ocr_flags"].append("direction_uncertain")
    entry = {
        "poster": poster, "block_index": 0, "template": layout.template,
        "extract_mode": "endpoints", "station": station, "direction": direction,
        "station_key": f"{station}-{direction}", "route": route, "route_ocr": route_ocr,
        "route_filename": route_hint, "number_items": number_items,
        "endpoints": endpoints, "stops": [], "columns": recognized,
        "row": [min(c["y0"] for c in columns), max(c["y1"] for c in columns)],
        "row_debug": {"arrow": layout.arrow, "endpoint_columns": len(columns)},
    }
    refresh_entry_validation(entry)
    return entry
