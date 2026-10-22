"""Synthetic posters and a deterministic OCR backend for integration tests.

The images contain the same geometry the real pipeline projects: red route
numbers, a header band and a row of vertical stop columns.  The fake OCR reads
coloured markers/bar geometry instead of glyphs, which keeps tests fast and
deterministic while still exercising layout, canvas packing and fallback.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Sequence

import numpy as np
from PIL import Image, ImageDraw

from buspipe.ocr import OcrUnavailable

WIDTH = 1600
BLOCK_SPACING = 500
ROUTE_Y0 = 150
ROUTE_HEIGHT = 150
BAR_WIDTH = 30
BAR_GAP = 22
BAR_X0 = 760
BAR_HEIGHT = 200
BAR_TOP_OFFSET = 40

GREEN = (0, 255, 0)
BLUE = (0, 0, 255)
YELLOW = (255, 255, 0)
MAGENTA = (255, 0, 255)
CYAN = (0, 255, 255)
RED = (220, 0, 0)


def make_poster(
    path: Path | str,
    *,
    blocks: int = 1,
    routes: Sequence[int] = (1,),
    stops: int = 8,
    current_index: int = 1,
    header_anomaly: bool = False,
    low_score_index: int | None = None,
    cross_index: int | None = None,
    skip_index: int | None = None,
    variant: int = 0,
) -> Path:
    path = Path(path)
    height = 200 + BLOCK_SPACING * blocks + 200
    image = Image.new("RGB", (WIDTH, height), "white")
    draw = ImageDraw.Draw(image)
    for block in range(blocks):
        route_y0 = ROUTE_Y0 + block * BLOCK_SPACING
        route_y1 = route_y0 + ROUTE_HEIGHT
        draw.rectangle([320, route_y0, 660, route_y1], fill=RED)
        route_number = routes[block % len(routes)]
        # Keep the fake route marker inside the red number block so it is still
        # visible when the extractor crops tightly to the glyph extent.
        draw.rectangle([330, route_y0 + 5, 330 + route_number * 4, route_y0 + 8], fill=BLUE)
        draw.rectangle(
            [700, route_y0 + 2, 700 + (10 if header_anomaly else 4), route_y0 + 5],
            fill=GREEN,
        )
        bar_top = route_y1 + BAR_TOP_OFFSET
        for index in range(stops):
            x0 = BAR_X0 + index * (BAR_WIDTH + BAR_GAP)
            color = RED if index == current_index else (0, 0, 0)
            draw.rectangle([x0, bar_top, x0 + BAR_WIDTH - 1, bar_top + BAR_HEIGHT - 1], fill=color)
            marker_x = x0 + 3
            marker_y = bar_top + 3
            if index == low_score_index:
                draw.rectangle([marker_x, marker_y, marker_x + 8, marker_y + 2], fill=YELLOW)
            if index == cross_index:
                draw.rectangle([marker_x, marker_y, marker_x + 8, marker_y + 2], fill=MAGENTA)
            if index == skip_index:
                draw.rectangle([marker_x, marker_y, marker_x + 8, marker_y + 2], fill=CYAN)
    # Unique pixels per poster so duplicate detection is meaningful.
    draw.point((10 + variant % 80, 10 + (variant // 80) % 40), fill=(128, 128, 128))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, quality=92)
    return path


def _color_mask(image: np.ndarray, color: tuple[int, int, int]) -> np.ndarray:
    """Tolerant masks: fixture JPEGs shift pure marker colours slightly."""
    r, g, b = image[:, :, 0], image[:, :, 1], image[:, :, 2]
    if color == GREEN:
        return (g > 180) & (r < 100) & (b < 100)
    if color == BLUE:
        return (b > 180) & (r < 100) & (g < 100)
    if color == YELLOW:
        return (r > 180) & (g > 180) & (b < 100)
    if color == MAGENTA:
        return (r > 180) & (b > 180) & (g < 100)
    if color == CYAN:
        return (g > 180) & (b > 180) & (r < 100)
    return np.all(image == np.array(color, dtype=np.uint8), axis=2)


def _has_color(image: np.ndarray, color: tuple[int, int, int]) -> bool:
    return bool(_color_mask(image, color).any())


def _color_count(image: np.ndarray, color: tuple[int, int, int]) -> int:
    return int(_color_mask(image, color).sum())


def _color_width(image: np.ndarray, color: tuple[int, int, int]) -> int:
    mask = _color_mask(image, color)
    xs = np.where(mask.any(axis=0))[0]
    return int(xs[-1] - xs[0] + 1) if len(xs) else 0


def _header_items(anomaly: bool) -> list[dict]:
    next_name = "别的站" if anomaly else "测试站"
    entries = [
        ("下站", 10),
        (next_name, 60),
        ("开往", 140),
        ("终点站", 200),
        ("一票制一元", 290),
        ("06:00", 390),
        ("20:30", 460),
        ("2026.10.01", 540),
        ("福州公交集团", 650),
    ]
    return [
        {"text": text, "score": 0.99, "x0": x, "x1": x + 40, "y0": 20, "y1": 42}
        for text, x in entries
    ]


def _bars(image: np.ndarray) -> list[tuple[int, int, int, int]]:
    mask = (image < 240).any(axis=2)
    counts = mask.sum(axis=0)
    xs = np.where(counts > 2)[0]
    if len(xs) == 0:
        return []
    runs: list[tuple[int, int]] = []
    start = prev = int(xs[0])
    for value in xs[1:]:
        value = int(value)
        if value - prev > 2:
            runs.append((start, prev))
            start = value
        prev = value
    runs.append((start, prev))
    bars = []
    for x0, x1 in runs:
        sub = mask[:, x0:x1 + 1]
        ys = np.where(sub.any(axis=1))[0]
        if len(ys) == 0:
            continue
        width = x1 - x0 + 1
        height = int(ys[-1] - ys[0] + 1)
        if width >= 10 and height >= 100:
            bars.append((x0, x1, int(ys[0]), int(ys[-1])))
    return bars


def _marker_present(image: np.ndarray, bar: tuple[int, int, int, int], color: tuple[int, int, int]) -> bool:
    x0, x1, y0, y1 = bar
    return _has_color(image[y0:y1 + 1, x0:x1 + 1], color)


class SyntheticOcrBackend:
    model_version = "synthetic-v1"

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.recognize_calls = 0
        self.images_seen = 0

    def detect_route_number(self, image: np.ndarray) -> int | None:
        width = _color_width(image, BLUE)
        if width <= 0:
            return None
        # JPEG shaves roughly one pixel from each end of the thin marker.
        return max(1, round((width + 1) / 4))

    def _one(self, image: np.ndarray) -> list[dict]:
        if _has_color(image, GREEN):
            return _header_items(anomaly=_color_width(image, GREEN) > 6)
        route = self.detect_route_number(image)
        if route is not None:
            return [{"text": str(route), "score": 0.99, "x0": 5, "y0": 10, "x1": 60, "y1": 34}]
        bars = _bars(image)
        merged_canvas = image.shape[1] > 250
        items: list[dict] = []
        index = 0
        while index < len(bars):
            bar = bars[index]
            x0, x1, y0, y1 = bar
            if _marker_present(image, bar, CYAN):
                index += 1
                continue
            if (
                merged_canvas
                and _marker_present(image, bar, MAGENTA)
                and index + 1 < len(bars)
            ):
                nxt = bars[index + 1]
                items.append(
                    {
                        "text": "测试站测试站",
                        "score": 0.99,
                        "x0": x0 - 1,
                        "x1": nxt[1] + 1,
                        "y0": min(y0, nxt[2]),
                        "y1": max(y1, nxt[3]),
                    }
                )
                index += 2
                continue
            low = _marker_present(image, bar, YELLOW)
            items.append(
                {
                    "text": "测试站",
                    "score": 0.4 if low else 0.99,
                    "x0": x0 - 1,
                    "x1": x1 + 1,
                    "y0": y0,
                    "y1": y1,
                }
            )
            index += 1
        return items

    def recognize(self, images: Sequence[np.ndarray], params) -> list[list[dict]]:
        with self.lock:
            self.recognize_calls += 1
            self.images_seen += len(images)
        return [self._one(np.asarray(image)) for image in images]


class FailOnceBackend:
    """Raises for the first request that contains a given route marker."""

    def __init__(self, inner: SyntheticOcrBackend, route: int = 2) -> None:
        self.inner = inner
        self.route = route
        self.remaining = 1
        self.model_version = inner.model_version + f"|fail-once-{route}"

    def recognize(self, images: Sequence[np.ndarray], params) -> list[list[dict]]:
        for image in images:
            image = np.asarray(image)
            if self.inner.detect_route_number(image) == self.route and self.remaining > 0:
                self.remaining -= 1
                raise OcrUnavailable(f"synthetic failure for route {self.route}")
        return self.inner.recognize(images, params)


class FlakyBackend:
    """Fails the first N calls, then delegates."""

    def __init__(self, inner, failures: int = 3) -> None:
        self.inner = inner
        self.remaining = failures
        self.model_version = inner.model_version + "|flaky"
        self.failed_calls = 0

    def recognize(self, images, params):
        if self.remaining > 0:
            self.remaining -= 1
            self.failed_calls += 1
            raise OcrUnavailable("synthetic transient failure")
        return self.inner.recognize(images, params)
