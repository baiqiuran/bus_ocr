"""Layout projection, stop-column detection and dynamic canvas packing.

Everything here is pure: it receives pixels plus settings and returns geometry.
No OCR engine, filesystem or environment access.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import numpy as np
from PIL import Image

from buspipe.config import CanvasSettings, LayoutSettings


def group_indices(indices: Iterable[int], max_gap: int) -> list[tuple[int, int]]:
    values = list(indices)
    if not values:
        return []
    runs: list[tuple[int, int]] = []
    start = prev = int(values[0])
    for value in values[1:]:
        value = int(value)
        if value - prev > max_gap:
            runs.append((start, prev))
            start = value
        prev = value
    runs.append((start, prev))
    return runs


def make_mask(rgb: np.ndarray) -> np.ndarray:
    """Black text plus the red current-station text. Background colours excluded."""
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    dark = (r < 120) & (g < 120) & (b < 120)
    red = (r > 150) & (g < 90) & (b < 90)
    return dark | red


def make_red_mask(rgb: np.ndarray) -> np.ndarray:
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    return (r > 150) & (g < 90) & (b < 90)


def detect_route_blocks(rgb: np.ndarray, settings: LayoutSettings) -> list[tuple[int, int]]:
    """Find the large red route numbers in the left margin.

    A route title can wrap onto two lines (e.g. a shuttle line plus its
    parenthetical campus name).  Nearby red runs are therefore merged before
    the height filter so the two lines are treated as one route block.
    """
    red = make_red_mask(rgb)
    col_count = red[:, settings.route_x0:settings.route_x1].sum(axis=1)
    rows = np.where(col_count > 5)[0]
    runs = group_indices(rows, max_gap=5)
    height, width = red.shape
    candidates: list[tuple[int, int]] = []
    for a, b in runs:
        if a <= settings.route_top_margin or b >= height - settings.route_bottom_margin:
            continue
        # Page borders can sit just above the first number. Identify their
        # page-wide horizontal strokes independently of the title's height.
        if red[a:b + 1].sum(axis=1).max() >= width * settings.route_frame_min_width_ratio:
            continue
        if b - a + 1 < settings.route_min_height:
            x0, x1 = route_number_bounds(red, (a, b), settings)
            fragment = red[a:b + 1, x0:x1 + 1]
            # Hollow stop symbols can be close to the title, but are much less
            # dense than red glyphs. Do not merge them into the route header.
            if not fragment.size or fragment.mean() < settings.route_fragment_min_density:
                continue
        candidates.append((a, b))
    merged: list[tuple[int, int]] = []
    for a, b in candidates:
        if merged and a - merged[-1][1] <= settings.route_merge_gap:
            previous_x0, previous_x1 = route_number_bounds(red, merged[-1], settings)
            x0, x1 = route_number_bounds(red, (a, b), settings)
            widths = (previous_x1 - previous_x0 + 1, x1 - x0 + 1)
            overlap = min(previous_x1, x1) - max(previous_x0, x0) + 1
            # A narrow red current-stop glyph is not a second title line.
            if (
                min(widths) >= max(widths) * settings.route_merge_min_width_ratio
                and overlap >= min(widths) * settings.route_merge_min_width_ratio
            ):
                merged[-1] = (merged[-1][0], b)
                continue
        merged.append((a, b))
    blocks = []
    for a, b in merged:
        block_height = b - a + 1
        if not settings.route_min_height <= block_height <= settings.route_max_height:
            continue
        has_full_height_line = any(
            a <= start <= end <= b and end - start + 1 >= settings.route_min_height
            for start, end in candidates
        )
        if not has_full_height_line:
            x0, x1 = route_number_bounds(red, (a, b), settings)
            # Stacked red current-stop characters also form nearby short runs.
            # A title made only of small lines must span horizontally instead.
            if x1 - x0 + 1 < block_height:
                continue
        blocks.append((a, b))
    if not blocks:
        raise RuntimeError("No route-number blocks found")
    return blocks


def route_number_bounds(
    red: np.ndarray,
    block: tuple[int, int],
    settings: LayoutSettings,
) -> tuple[int, int]:
    """Horizontal extent of the red glyphs inside one route row band.

    ``route_x0:route_x1`` is only a projection window: on wider templates the
    number continues past it.  Cropping at the window edge truncates digits
    (80 -> 8, 102 -> 10), so OCR uses the measured extent instead.
    """
    y0, y1 = block
    width = red.shape[1]
    limit = min(width, settings.route_scan_x1)
    columns = np.where(red[y0:y1 + 1, settings.route_x0:limit].sum(axis=0) > 2)[0]
    if columns.size == 0:
        return min(settings.route_x0, width), min(settings.route_x1, width)
    return int(columns.min()) + settings.route_x0, int(columns.max()) + settings.route_x0


def _stop_row_counts(
    rgb: np.ndarray, y0: int, y1: int, settings: LayoutSettings, *, remove_red_rules: bool = False,
) -> np.ndarray:
    band = rgb[y0:y1, settings.row_project_x0:settings.row_project_x1]
    mask = make_mask(band)
    if remove_red_rules and mask.size:
        # Page-wide red rules/backgrounds carry more ink than station glyphs.
        mask[make_red_mask(band).mean(axis=1) >= 0.98] = False
    return mask.sum(axis=1)


def detect_stop_row(
    rgb: np.ndarray,
    route_end: int,
    next_route_start: int | None,
    settings: LayoutSettings,
    *,
    adaptive: bool = False,
) -> tuple[int, int, dict[str, Any]]:
    """Locate the dense vertical-text row below the header."""
    height = rgb.shape[0]
    y0 = route_end + 15
    limit = height
    if next_route_start is not None:
        limit = min(limit, next_route_start - 60)
    y1 = min(limit, route_end + 500)
    row_count = _stop_row_counts(rgb, y0, y1, settings, remove_red_rules=adaptive)
    if row_count.size == 0 or row_count.max() < 10:
        raise RuntimeError(f"empty stop band at y={y0}:{y1}")
    threshold = max(20, int(row_count.max() * 0.15))
    rows = np.where(row_count > threshold)[0]
    runs = group_indices(rows, max_gap=20)
    if not runs:
        raise RuntimeError(f"no text row at y={y0}:{y1}")
    start, end = max(runs, key=lambda se: int(row_count[se[0]:se[1] + 1].sum()))
    # Follow only the row crossing the limit. Keeping its threshold and run
    # avoids switching to a denser advertisement below the station text.
    while adaptive and end == len(row_count) - 1 and y0 < y1 < limit:
        edge = rgb[y1 - 1:y1 + 1, settings.row_project_x0:settings.row_project_x1]
        if not edge.size or not ((edge < 120).all(axis=2).sum(axis=1) > 10).all():
            break
        extra = _stop_row_counts(rgb, y1, min(limit, y1 + 500), settings, remove_red_rules=True)
        continuations = group_indices(np.where(extra > threshold)[0], max_gap=20)
        if not continuations or continuations[0][0] > 20:
            break
        length = continuations[0][1] + 1
        row_count = np.concatenate((row_count, extra[:length]))
        y1 += length
        end = len(row_count) - 1
    runs = group_indices(np.where(row_count > threshold)[0], max_gap=20)
    # Include immediately adjacent fragments (e.g. a detached bottom stroke).
    merged_start, merged_end = start, end
    for s, e in runs:
        if s >= start and e <= end:
            continue
        if adaptive:
            adjacent = 0 <= s - merged_end <= 25 or 0 <= merged_start - e <= 25
        else:
            # Preserve historical full-mode geometry and its fixture snapshots.
            adjacent = s - merged_end <= 25 or merged_start - e <= 25
        if adjacent:
            merged_start = min(merged_start, s)
            merged_end = max(merged_end, e)
    row_y0, row_y1 = y0 + merged_start, y0 + merged_end
    debug = {
        "search": [int(y0), int(y1)],
        "threshold": int(threshold),
        "runs": [[int(y0 + s), int(y0 + e)] for s, e in runs],
        "chosen": [int(start + y0), int(end + y0)],
        "final": [int(row_y0), int(row_y1)],
    }
    return int(row_y0), int(row_y1), debug


def _column_candidate(mask: np.ndarray, x0: int, x1: int, row_height: int) -> dict[str, Any] | None:
    sub = mask[:, x0:x1 + 1]
    ys = np.where(sub.any(axis=1))[0]
    if len(ys) == 0:
        return None
    height = int(ys[-1] - ys[0] + 1)
    width = int(x1 - x0 + 1)
    if width < 15 or height < 0.5 * row_height:
        return None
    return {"x0": int(x0), "x1": int(x1), "y0": int(ys[0]), "y1": int(ys[-1]), "width": width, "height": height}


def detect_stop_columns(
    rgb: np.ndarray,
    row_y0: int,
    row_y1: int,
    settings: LayoutSettings,
    *,
    split_wide: bool = True,
) -> list[dict[str, Any]]:
    strip = rgb[row_y0:row_y1 + 1, settings.stop_x0:settings.stop_x1]
    if not strip.size:
        return []
    mask = make_mask(strip)
    # A page frame is nearly solid red and continues above/below the text row.
    # Inspect that context so red terminal glyphs (even dense ones) are kept.
    # This follows the pixels rather than assuming a fixed page-edge position.
    padding = max(10, strip.shape[0] // 10)
    context = rgb[
        max(0, row_y0 - padding):min(rgb.shape[0], row_y1 + padding + 1),
        settings.stop_x0:settings.stop_x1,
    ]
    frame_columns = make_red_mask(context).mean(axis=0) >= 0.98
    mask[:, frame_columns] = False
    col_count = mask.sum(axis=0)
    xs = np.where(col_count > 2)[0]
    if len(xs) == 0:
        return []
    runs = group_indices(xs, max_gap=2)
    columns: list[dict[str, Any]] = []
    for s, e in runs:
        candidate = _column_candidate(mask, s, e, mask.shape[0])
        if candidate:
            columns.append(candidate)
    if not columns:
        return []
    median_width = float(np.median([c["width"] for c in columns]))
    # Touching neighbouring columns: recursively split wide runs at the weakest
    # internal gap.  A single wide run can contain three or more stop columns.
    split_columns: list[dict[str, Any]] = []
    queue = list(columns)
    while queue:
        col = queue.pop(0)
        width = col["width"]
        if split_wide and width > max(90, median_width * 1.65):
            s, e = col["x0"], col["x1"]
            lo = s + int(width * 0.25)
            hi = s + int(width * 0.75)
            search = col_count[lo:hi + 1]
            if search.size:
                split = lo + int(np.argmin(search))
            else:
                split = s + width // 2
            left = _column_candidate(mask, s, max(s, split - 1), mask.shape[0])
            right = _column_candidate(mask, min(e, split), e, mask.shape[0])
            if left and right and left["width"] >= 15 and right["width"] >= 15:
                queue = [left, right] + queue
                continue
        split_columns.append(col)
    # Last-resort guard: never send a genuinely wide strip to OCR.  Such a
    # strip almost always contains several touching stop columns.
    guarded: list[dict[str, Any]] = []
    for col in split_columns:
        width = col["width"]
        if split_wide and width > 160:
            s, e = col["x0"], col["x1"]
            parts = max(2, min(12, int(round(width / 72))))
            step = width / parts
            pieces = []
            ok = True
            for k in range(parts):
                px0 = int(round(s + k * step))
                px1 = int(round(s + (k + 1) * step)) - 1
                piece = _column_candidate(mask, px0, min(e, px1), mask.shape[0])
                if not piece or piece["width"] < 12:
                    ok = False
                    break
                pieces.append(piece)
            if ok and len(pieces) > 1:
                guarded.extend(pieces)
                continue
        guarded.append(col)
    split_columns = guarded
    split_columns.sort(key=lambda c: c["x0"])
    return [
        {
            "x0": int(c["x0"] + settings.stop_x0),
            "x1": int(c["x1"] + settings.stop_x0),
            "y0": int(c["y0"] + row_y0),
            "y1": int(c["y1"] + row_y0),
        }
        for c in split_columns
    ]


def locate_stop_band(
    rgb: np.ndarray,
    route_end: int,
    next_route_start: int | None,
    settings: LayoutSettings,
    *,
    adaptive: bool = False,
) -> tuple[int, int, list[dict[str, Any]], dict[str, Any]]:
    """Locate/trim the row; adaptive mode follows clipped endpoint text."""
    row_y0, row_y1, row_debug = detect_stop_row(
        rgb, route_end, next_route_start, settings, adaptive=adaptive,
    )
    columns = detect_stop_columns(rgb, row_y0, row_y1, settings)
    # The row-projection top often includes the route line/diamonds above the
    # text.  Trim to where most stop columns actually start.
    if columns:
        tops = sorted(c["y0"] for c in columns)
        bottoms = sorted(c["y1"] for c in columns)
        trimmed_top = tops[min(len(tops) - 1, int(len(tops) * 0.55))]
        trimmed_bottom = bottoms[min(len(bottoms) - 1, int(len(bottoms) * 0.90))]
        if trimmed_bottom - trimmed_top >= 100:
            trimmed_columns = detect_stop_columns(rgb, trimmed_top, trimmed_bottom, settings)
            if trimmed_columns:
                row_y0, row_y1 = trimmed_top, trimmed_bottom
                columns = trimmed_columns
                row_debug["trimmed"] = [int(row_y0), int(row_y1)]
    raw_columns = detect_stop_columns(rgb, row_y0, row_y1, settings, split_wide=False)
    row_debug["raw_column_count"] = len(raw_columns)
    row_debug["raw_column_median_aspect"] = float(np.median([
        (c["y1"] - c["y0"] + 1) / (c["x1"] - c["x0"] + 1)
        for c in raw_columns
    ])) if raw_columns else 0.0
    return row_y0, row_y1, columns, row_debug


def _is_note_column(col: dict[str, Any], prev: dict[str, Any], median_width: float, median_gap: float) -> bool:
    text = str(col.get("text", ""))
    if text.startswith(("(", "（")) or text.endswith((")", "）")):
        return True
    width = col["x1"] - col["x0"] + 1
    gap = col["x0"] - prev["x1"]
    return bool(median_width and median_gap and width < median_width * 0.85 and gap < median_gap * 0.70)


def group_columns(columns: Sequence[dict[str, Any]]) -> list[list[int]]:
    """Group a stop with the note columns immediately after it.

    Geometry is used because packing happens before OCR.  The heuristic mirrors
    the post-OCR note rule, so a note stays on the same canvas as its stop.
    """
    if not columns:
        return []
    widths = [c["x1"] - c["x0"] + 1 for c in columns]
    gaps = [columns[i]["x0"] - columns[i - 1]["x1"] for i in range(1, len(columns))]
    median_width = float(np.median(widths)) if widths else 0.0
    median_gap = float(np.median(gaps)) if gaps else 0.0
    groups: list[list[int]] = []
    for index, col in enumerate(columns):
        if index > 0 and groups:
            prev_index = groups[-1][-1]
            prev = columns[prev_index]
            if _is_note_column(col, prev, median_width, median_gap):
                groups[-1].append(index)
                continue
        groups.append([index])
    return groups


def pack_columns(columns: Sequence[dict[str, Any]], settings: CanvasSettings) -> list[list[int]]:
    """Pack column indices into canvases under max-columns/max-width rules."""
    groups = group_columns(columns)
    chunks: list[list[int]] = []
    current: list[int] = []
    content_width = 0
    frame = settings.margin * 2

    def width_of(group: list[int]) -> int:
        return sum(columns[i]["x1"] - columns[i]["x0"] + 5 for i in group)

    for group in groups:
        group_width = width_of(group)
        extra = group_width + (settings.gap if current else 0)
        too_many = bool(current) and len(current) + len(group) > settings.max_columns
        too_wide = bool(current) and frame + content_width + extra > settings.max_width
        if too_many or too_wide:
            chunks.append(current)
            current, content_width = [], 0
            extra = group_width
        current.extend(group)
        content_width += extra
    if current:
        chunks.append(current)
    return chunks


@dataclass
class Canvas:
    index: int
    image: np.ndarray
    spans: list[tuple[int, int, int]]  # (x0, x1, column_index)
    column_indices: list[int]


def build_canvases(
    img: np.ndarray,
    columns: Sequence[dict[str, Any]],
    row_y0: int,
    row_y1: int,
    settings: CanvasSettings,
) -> list[Canvas]:
    chunks = pack_columns(columns, settings)
    canvases: list[Canvas] = []
    for canvas_index, chunk in enumerate(chunks):
        pieces: list[tuple[int, np.ndarray]] = []
        for column_index in chunk:
            col = columns[column_index]
            x0 = max(0, col["x0"] - settings.left_pad)
            x1 = min(img.shape[1], col["x1"] + settings.right_pad)
            y0 = max(0, row_y0 - settings.top_pad)
            y1 = min(img.shape[0], row_y1 + settings.bottom_pad)
            crop = np.ascontiguousarray(img[y0:y1, x0:x1])
            if crop.size == 0:
                continue
            pieces.append((column_index, crop))
        if not pieces:
            continue
        content = sum(crop.shape[1] for _, crop in pieces) + settings.gap * (len(pieces) - 1)
        width = max(2 * settings.margin + content, 1)
        source_height = max(crop.shape[0] for _, crop in pieces)
        height = source_height + settings.top_margin + settings.bottom_margin
        canvas = Image.new("RGB", (width, height), "white")
        spans: list[tuple[int, int, int]] = []
        cursor = settings.margin
        for column_index, crop in pieces:
            canvas.paste(Image.fromarray(crop), (cursor, settings.top_margin))
            spans.append((cursor, cursor + crop.shape[1], column_index))
            cursor += crop.shape[1] + settings.gap
        canvases.append(
            Canvas(
                index=canvas_index,
                image=np.asarray(canvas),
                spans=spans,
                column_indices=[i for i, _ in pieces],
            )
        )
    return canvases


def assign_canvas_items(
    items: Sequence[dict[str, Any]],
    canvas: Canvas,
    columns: Sequence[dict[str, Any]],
    box_width_ratio: float = 1.8,
) -> tuple[dict[int, list[dict[str, Any]]], set[int], dict[int, list[str]]]:
    """Map OCR boxes back to source columns and flag unreliable assignments."""
    assigned: dict[int, list[dict[str, Any]]] = {i: [] for i in canvas.column_indices}
    needs_fallback: set[int] = set()
    flags: dict[int, list[str]] = {i: [] for i in canvas.column_indices}
    for item in items:
        x0, x1 = float(item["x0"]), float(item["x1"])
        box_width = max(1.0, x1 - x0)
        overlaps: list[tuple[int, float]] = []
        for start, end, column_index in canvas.spans:
            overlap = min(x1, end) - max(x0, start)
            if overlap > max(12.0, 0.18 * box_width):
                overlaps.append((column_index, overlap))
        if len(overlaps) > 1:
            for column_index, _ in overlaps:
                needs_fallback.add(column_index)
                flags[column_index].append("cross_column")
            continue
        if not overlaps:
            continue
        owner = overlaps[0][0]
        col_width = columns[owner]["x1"] - columns[owner]["x0"] + 1
        if box_width > col_width * box_width_ratio:
            needs_fallback.add(owner)
            flags[owner].append("wide_box")
            continue
        assigned[owner].append(dict(item))
    return assigned, needs_fallback, flags

