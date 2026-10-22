"""Pipeline settings.

All tunables live here so the extraction core never reads environment
variables or module-level mutable state.  The legacy CLI builds these values
from the same environment variables it has always used.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

# Route hints for posters whose route-number glyphs are not reliably readable.
# They are keyed by poster stem, exactly like the historical KNOWN_ROUTES map.
DEFAULT_ROUTE_HINTS: dict[str, list[str]] = {
    "安淡-北-01": ["6", "7", "14", "19", "39", "77", "82", "93", "109", "112"],
    "安淡-北-02": ["130", "169", "179", "302", "309", "313", "315", "316", "318", "351"],
    "安淡-南-01": ["6", "7", "14", "19", "39", "77", "82", "93", "109"],
    "安淡-南-02": ["112", "130", "169", "179", "302", "309", "313", "315", "316", "318", "351"],
    "安平小区-东-01": ["6", "29", "81"],
    "安平小区-西-01": ["1", "6", "14", "15", "29", "39", "50"],
    "安平小区-西-02": ["51", "71", "81", "309", "320", "K1"],
}

DIRECTION_TOKENS = ("北", "南", "东", "西")


@dataclass(frozen=True)
class LayoutSettings:
    route_x0: int = 300
    route_x1: int = 680
    stop_x0: int = 300
    stop_x1: int = 4080
    row_project_x0: int = 700
    row_project_x1: int = 4000
    route_min_height: int = 120
    route_max_height: int = 360
    route_top_margin: int = 100
    route_bottom_margin: int = 300


@dataclass(frozen=True)
class CanvasSettings:
    max_columns: int = 8
    max_width: int = 1000
    gap: int = 30
    margin: int = 20
    top_margin: int = 15
    bottom_margin: int = 15
    left_pad: int = 2
    right_pad: int = 3
    top_pad: int = 5
    bottom_pad: int = 6


@dataclass(frozen=True)
class OcrSettings:
    mode: str = "batch"  # batch | columns
    fast: bool = False
    box_thresh: float = 0.3
    text_score: float = 0.1
    unclip_ratio: float = 1.5
    pad_left: int = 6
    pad_right: int = 6
    pad_top: int = 2
    pad_bottom: int = 5
    # Batch-to-single-column fallback thresholds.
    fallback_score: float = 0.5
    fallback_coverage: float = 0.78
    fallback_box_width_ratio: float = 1.8
    full_box_height_ratio: float = 0.72
    # Column-mode retry thresholds.
    retry_score: float = 0.75
    retry_gain: float = 0.06
    # Gap-fill (dropped character) thresholds.
    gap_height_ratio: float = 1.25
    gap_min_pixels: int = 20
    gap_score: float = 0.45


@dataclass(frozen=True)
class PipelineSettings:
    current_stop: str = "安淡"
    route_hints: Mapping[str, Sequence[str]] = field(default_factory=lambda: DEFAULT_ROUTE_HINTS)
    layout: LayoutSettings = field(default_factory=LayoutSettings)
    canvas: CanvasSettings = field(default_factory=CanvasSettings)
    ocr: OcrSettings = field(default_factory=OcrSettings)
    pipeline_version: str = "2.0"
    model_version: str = "rapidocr-default"
    review_low_score: float = 0.75
    bidirectional_min_similarity: float = 0.9

    def fingerprint(self, extra: Mapping[str, Any] | None = None) -> str:
        """Stable hash of every setting that can change extracted data."""
        payload: dict[str, Any] = {
            "pipeline_version": self.pipeline_version,
            "model_version": self.model_version,
            "current_stop": self.current_stop,
            "route_hints": {k: list(v) for k, v in self.route_hints.items()},
            "layout": asdict(self.layout),
            "canvas": asdict(self.canvas),
            "ocr": asdict(self.ocr),
        }
        if extra:
            payload["extra"] = dict(extra)
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def poster_direction(name: str) -> str:
    for token in DIRECTION_TOKENS:
        if f"-{token}-" in name:
            return token
    parts = name.split("-")
    # Some files append artwork notes to the direction segment, e.g.
    # 安铺村-北，小灯箱80x130CM-01.jpg; the segment still starts with 北.
    for part in parts[1:]:
        for token in DIRECTION_TOKENS:
            if part.startswith(token):
                return token
    return parts[1] if len(parts) >= 3 else "未知"


def poster_station(name: str, fallback: str = "") -> str:
    """Station is the part before the direction token, e.g. 安平小区-东-01."""
    for token in DIRECTION_TOKENS:
        marker = f"-{token}-"
        if marker in name:
            return name.split(marker, 1)[0]
    parts = name.split("-")
    if len(parts) >= 3:
        return parts[0]
    return fallback or name


def settings_from_env(base: PipelineSettings | None = None) -> PipelineSettings:
    """Build settings for the legacy single/poster CLI.

    Only values that the historical CLI exposed are read from the environment;
    everything else keeps its dataclass default.
    """
    base = base or PipelineSettings()
    fast = os.environ.get("OCR_FAST", "0") == "1"
    mode = os.environ.get("OCR_MODE", "columns")
    route_max = int(os.environ.get("BUS_ROUTE_MAX_H", str(base.layout.route_max_height)))
    stop = os.environ.get("BUS_STOP", base.current_stop)
    return replace(
        base,
        current_stop=stop,
        layout=replace(base.layout, route_max_height=route_max),
        ocr=replace(base.ocr, fast=fast, mode=mode),
    )

