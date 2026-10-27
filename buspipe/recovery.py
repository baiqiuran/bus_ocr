"""Scoped recovery for failed vertical layouts; legacy success never enters here."""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import Any

import numpy as np
from PIL import Image

from buspipe.config import LayoutSettings, PipelineSettings
from buspipe.layout import (
    detect_route_blocks,
    detect_stop_columns,
    group_indices,
    make_mask,
    make_red_mask,
)
from buspipe.ocr import OcrBackend, OcrParams, OcrStats
from buspipe.templates import recovery_profile


def content_bounds(rgb: np.ndarray) -> tuple[int, int]:
    """Find the inside edges of long red side borders, not isolated red glyphs."""
    height, width = rgb.shape[:2]
    red = make_red_mask(rgb)
    long_columns = red.mean(axis=0) >= 0.40
    left = np.where(long_columns[: round(width * 0.30)])[0]
    right = np.where(long_columns[round(width * 0.70) :])[0] + round(width * 0.70)
    if left.size and right.size:
        x0, x1 = int(left.max()) + 1, int(right.min())
        if x1 - x0 >= width * 0.45:
            return x0, x1
    return 0, width


def _caption(
    rgb: np.ndarray,
    box: tuple[int, int, int, int],
    backend: OcrBackend,
    settings: PipelineSettings,
) -> tuple[str, float]:
    x0, y0, x1, y1 = box
    crop = Image.fromarray(np.ascontiguousarray(rgb[y0:y1, x0:x1]))
    crop.thumbnail((1400, 1400))
    stats = getattr(backend, "stats", None)
    if isinstance(stats, OcrStats):
        stats.inc("images")
    items = backend.recognize(
        [np.asarray(crop)], OcrParams.from_settings(settings.ocr)
    )[0]
    parts = [re.sub(r"\s+", "", str(item.get("text", ""))) for item in items]
    text = "".join(parts)
    meaningful = [
        i for i in items if re.search(r"[\u4e00-\u9fff]", str(i.get("text", "")))
    ]
    for phrase in ("本站未启用", "本站暂停使用", "公交保障"):
        start = text.find(phrase)
        if start < 0:
            continue
        end = start + len(phrase)
        contributors = []
        offset = 0
        for item, part in zip(items, parts):
            if offset < end and offset + len(part) > start:
                contributors.append(item)
            offset += len(part)
        meaningful = contributors
        break
    return re.sub(r"\s+", "", text), min(
        (float(i.get("score", 0)) for i in meaningful), default=0
    )


def classify_non_route(
    rgb: np.ndarray,
    stem: str,
    backend: OcrBackend,
    settings: PipelineSettings,
) -> dict[str, Any] | None:
    """Conservative geometry + explicit captions; never skip on filename alone."""
    height, width = rgb.shape[:2]
    x0, x1 = content_bounds(rgb)
    r, g, b = np.moveaxis(rgb, 2, 0)
    if (
        "车道指示" in stem
        and np.mean((b > 120) & (r < 80) & (g < 100)) > 0.25
        and make_red_mask(rgb).mean() < 0.001
    ):
        return {
            "board_type": "wayfinding",
            "skip_reason": "车道指示牌：没有逐线路首末站",
            "method": "filename_and_blue_arrow_layout",
        }
    if "火车站北广场背胶" in stem:
        text, score = _caption(
            rgb, (0, 0, width, round(height * 0.4)), backend, settings
        )
        if "公交保障" in text and score >= 0.75:
            return {
                "board_type": "notice",
                "skip_reason": "公交保障通知，无逐线路首末站",
                "caption": text,
                "confidence": score,
                "method": "caption_and_family",
            }
    # Exclude the page frame and the separate QR/footer region. A real route
    # row contains dark glyphs, so its caption must not turn it into inactive.
    pad = max(3, round(width * 0.006))
    roi = rgb[: round(height * 0.76), x0 + pad : x1 - pad].copy()
    if not roi.size:
        return None
    # JPEG antialiasing may turn a red horizontal frame edge dark/brown. A
    # nearly page-wide flat rule is still a frame, not a caption character.
    roi[(roi.min(axis=2) < 200).mean(axis=1) >= 0.90] = 255
    white = np.all(roi > 235, axis=2)
    dark = np.all(roi < 120, axis=2)
    if white.mean() < 0.80 or dark.sum() > max(
        40, roi.shape[0] * roi.shape[1] * 0.00001
    ):
        return None
    ink = (roi.min(axis=2) < 180) & ~dark
    ys, xs = np.where(ink)
    if not ys.size or ink.sum() < max(20, ink.size * 0.00001):
        return {
            "board_type": "blank",
            "skip_reason": "空白底板：线路内容区无文字",
            "method": "white_content_geometry",
        }
    if xs.max() - xs.min() < roi.shape[1] * 0.35:
        return None
    box = (
        x0 + pad + max(0, int(xs.min()) - 10),
        max(0, int(ys.min()) - 10),
        x0 + pad + min(roi.shape[1], int(xs.max()) + 11),
        min(roi.shape[0], int(ys.max()) + 11),
    )
    text, score = _caption(rgb, box, backend, settings)
    if score >= 0.80 and any(word in text for word in ("本站未启用", "本站暂停使用")):
        return {
            "board_type": "inactive",
            "skip_reason": "本站未启用" if "未启用" in text else "本站暂停使用",
            "caption": text,
            "confidence": score,
            "method": "explicit_caption_no_route_ink",
        }
    return None


@dataclass
class RecoveredLayout:
    settings: PipelineSettings
    blocks: list[tuple[int, int]]
    red_mask: np.ndarray
    profile: str
    geometry: tuple[np.ndarray, LayoutSettings, float] | None = None


def recover_route_layout(
    rgb: np.ndarray,
    settings: PipelineSettings,
    stem: str,
) -> RecoveredLayout | None:
    height, width = rgb.shape[:2]
    profile = recovery_profile(width, height, stem)
    if profile is None or profile.layout_transform is None:
        return None
    effective = profile.apply(settings, width, height)
    x0, x1 = content_bounds(rgb)
    cw = x1 - x0
    layout = replace(
        effective.layout,
        route_x0=x0 + max(2, round(cw * 0.008)),
        route_x1=min(x1, x0 + round(cw * 0.28)),
        route_scan_x1=min(x1, x0 + round(cw * 0.50)),
        stop_x0=max(0, x0 - round(cw * 0.025)),
        stop_x1=min(width, x1 + round(cw * 0.012)),
        row_project_x0=x0 + round(cw * 0.08),
        row_project_x1=x1 - round(cw * 0.01),
    )
    red = make_red_mask(rgb)
    red[:, :x0] = False
    red[:, x1:] = False
    # Horizontal border strokes must not connect a number to a side frame.
    red[red[:, x0:x1].mean(axis=1) >= 0.80] = False
    blocks = detect_route_blocks(rgb, layout, red_mask=red)
    # A vertical current-stop name is several small characters, not one large
    # number. Check glyph-scale continuity inside each x component as well as
    # the combined row projection; lowering the minimum alone admits names.
    kept = []
    for a, b in blocks:
        strip = red[a : b + 1, layout.route_x0 : layout.route_scan_x1]
        tallest = 0
        for c, d in group_indices(np.where(strip.any(axis=0))[0], max_gap=2):
            if d - c + 1 < 3:
                continue
            runs = group_indices(
                np.where(strip[:, c : d + 1].any(axis=1))[0], max_gap=2
            )
            tallest = max(tallest, max((e - s + 1 for s, e in runs), default=0))
        if tallest >= layout.route_min_height:
            kept.append((a, b))
    if not kept:
        raise RuntimeError("No route-number glyphs found in recovery profile")
    return RecoveredLayout(
        replace(effective, layout=layout), kept, red, profile.cache_key
    )


def _geometry_image(rgb: np.ndarray, x0: int, x1: int) -> np.ndarray:
    """Normalize white-on-color rows for geometry only; OCR keeps original pixels."""
    result = rgb.copy()
    result[:, :x0] = 255
    result[:, x1:] = 255
    red = make_red_mask(rgb)
    # Only erase red frame pixels. Some lower route bands extend a little
    # beyond the main white inset; keep their dark/green terminal glyphs.
    result[red & (red.mean(axis=0) >= 0.40)[None, :]] = 255
    r, g, b = np.moveaxis(rgb.astype(np.int16), 2, 0)
    green_glyph = (g > r + 30) & (g > b + 30) & (g < 200)
    result[:, x0:x1][green_glyph[:, x0:x1]] = 0
    roi = rgb[:, x0:x1]
    saturated = (roi.max(axis=2).astype(np.int16) - roi.min(axis=2)) > 80
    non_red = saturated & ~make_red_mask(roi)
    rows = np.where(non_red.mean(axis=1) > 0.45)[0]
    for y in rows:
        xs = np.where(non_red[y])[0]
        a, b = int(xs.min()), int(xs.max()) + 1
        band = roi[y, a:b]
        # Yellow backgrounds contain dark text already; white glyph rows are
        # the darker blue/purple/green backgrounds.
        if np.median(band.mean(axis=1)) >= 205:
            continue
        bright = np.all(band > 165, axis=1)
        dark = np.all(band < 120, axis=1)
        red_glyph = (band[:, 0] > 150) & (band[:, 1] < 90) & (band[:, 2] < 90)
        result[y, x0 + a : x0 + b] = 255
        result[y, x0 + a : x0 + b][bright | dark | red_glyph] = 0
    return result


def locate_recovered_stop_band(
    rgb: np.ndarray,
    route_end: int,
    next_start: int | None,
    settings: PipelineSettings,
    *,
    recovery: RecoveredLayout | None = None,
) -> tuple[int, int, list[dict[str, Any]], dict[str, Any]]:
    x0, x1 = settings.layout.stop_x0, settings.layout.stop_x1
    geom = None
    # Keep the legacy column splitter in its validated pixel range on very
    # wide recovery boards; coordinates are restored to the source image.
    scale = min(1.0, 3200 / max(1, x1 - x0))
    layout = settings.layout
    if recovery is not None and recovery.geometry is not None:
        geom, layout, scale = recovery.geometry
    else:
        geom = _geometry_image(rgb, x0, x1)
        if scale < 1:
            im = Image.fromarray(geom)
            geom = np.array(
                im.resize((round(im.width * scale), round(im.height * scale)))
            )
            layout = replace(
                layout,
                stop_x0=round(layout.stop_x0 * scale),
                stop_x1=round(layout.stop_x1 * scale),
                row_project_x0=round(layout.row_project_x0 * scale),
                row_project_x1=round(layout.row_project_x1 * scale),
            )
        if recovery is not None:
            recovery.geometry = (geom, layout, scale)
    cw = layout.stop_x1 - layout.stop_x0
    start = round(route_end * scale) + max(3, round(cw * 0.002))
    end = min(
        geom.shape[0],
        round(next_start * scale) - 8
        if next_start is not None
        else start + max(800, round(cw * 0.60)),
    )
    mask = make_mask(geom[start:end, layout.stop_x0 : layout.stop_x1])
    if not mask.size:
        raise RuntimeError("empty recovered stop search")
    # Horizontal rules, route-line diagrams, ads and QR squares cannot serve
    # as a row of vertical station-name columns.
    rules = mask.mean(axis=1) > 0.70
    geom[start:end][rules] = 255
    counts = mask.sum(axis=1)
    counts[rules] = 0
    active = counts[counts > 0]
    threshold = max(8, int(np.percentile(active, 90) * 0.12)) if active.size else 8
    large_title = settings.layout.route_min_height >= rgb.shape[1] * 0.09
    row_gap_ratio = 0.016 if large_title else 0.006
    runs = group_indices(
        np.where(counts > threshold)[0], max_gap=max(10, round(cw * row_gap_ratio))
    )
    found = None
    for a, b in runs:
        top, bottom = start + a, start + b
        if bottom - top < max(30, round(cw * 0.014)):
            continue
        projected = detect_stop_columns(geom, top, bottom, layout, split_wide=False)
        columns = [
            c
            for c in projected
            if (c["y1"] - c["y0"] + 1) / (c["x1"] - c["x0"] + 1) >= 1.6
            and c["x1"] - c["x0"] + 1 <= cw * 0.08
        ]
        min_span = 0.30 if len(columns) >= 4 else 0.45
        if len(columns) < 2 or columns[-1]["x1"] - columns[0]["x0"] < cw * min_span:
            continue
        found = (top, bottom, columns)
        break
    if found is None:
        raise RuntimeError("no verified vertical endpoint row in recovered block")
    y0, y1, columns = found
    debug = {
        "search": [start, end],
        "runs": [[start + a, start + b] for a, b in runs],
        "chosen": [y0, y1],
        "raw_column_count": len(columns),
        "raw_column_median_aspect": float(
            np.median(
                [(c["y1"] - c["y0"] + 1) / (c["x1"] - c["x0"] + 1) for c in columns]
            )
        ),
    }
    if scale < 1:
        for col in columns:
            for key in ("x0", "y0"):
                col[key] = int(np.floor(col[key] / scale))
            for key in ("x1", "y1"):
                col[key] = int(np.ceil(col[key] / scale))
        y0, y1 = int(np.floor(y0 / scale)), int(np.ceil(y1 / scale))
    debug["recovery_geometry_scale"] = scale
    return y0, y1, columns, debug
