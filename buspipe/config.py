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
    # Many templates place the number further right than the projection window.
    # The projection only detects the row; the actual glyph extent is re-measured
    # inside each block before OCR.
    route_scan_x1: int = 1600
    route_merge_gap: int = 60
    route_merge_min_width_ratio: float = 0.5
    route_fragment_min_density: float = 0.3
    route_frame_min_width_ratio: float = 0.8
    landscape_min_column_aspect: float = 2.5
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
class LandscapeSettings:
    min_aspect: float = 1.3
    max_aspect: float = 1.9
    route_right: float = 0.54
    route_bottom: float = 0.47
    caption_top: float = 0.70
    caption_bottom: float = 0.95
    divider_min_width: float = 0.65
    row_top_offset: float = 0.05
    row_bottom: float = 0.89
    min_column_height: float = 0.18
    foreground_delta: int = 70
    glyph_height: int = 64


@dataclass(frozen=True)
class PipelineSettings:
    current_stop: str = "安淡"
    extract_mode: str = "full"  # full | endpoints
    route_hints: Mapping[str, Sequence[str]] = field(default_factory=lambda: DEFAULT_ROUTE_HINTS)
    layout: LayoutSettings = field(default_factory=LayoutSettings)
    landscape: LandscapeSettings = field(default_factory=LandscapeSettings)
    canvas: CanvasSettings = field(default_factory=CanvasSettings)
    ocr: OcrSettings = field(default_factory=OcrSettings)
    pipeline_version: str = "2.4.1"
    model_version: str = "rapidocr-default"
    review_low_score: float = 0.75
    bidirectional_min_similarity: float = 0.9

    def __post_init__(self) -> None:
        if self.extract_mode not in {"full", "endpoints"}:
            raise ValueError(f"unknown extraction mode: {self.extract_mode}")

    def fingerprint(self, extra: Mapping[str, Any] | None = None) -> str:
        """Stable hash of every setting that can change extracted data."""
        payload: dict[str, Any] = {
            "pipeline_version": self.pipeline_version,
            "model_version": self.model_version,
            "current_stop": self.current_stop,
            "extract_mode": self.extract_mode,
            "route_hints": {k: list(v) for k, v in self.route_hints.items()},
            "layout": asdict(self.layout),
            "landscape": asdict(self.landscape),
            "canvas": asdict(self.canvas),
            "ocr": asdict(self.ocr),
        }
        if extra:
            payload["extra"] = dict(extra)
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


FACILITY_DIRECTION_TOKENS = (
    "场站",
    "出站",
    "进站",
    "上客点",
    "下客点",
    "上客区",
    "下客区",
    "首末站",
    "上车点",
    "下车点",
    "站台",
)


def poster_direction(name: str) -> str:
    for token in DIRECTION_TOKENS:
        if f"-{token}-" in name:
            return token
    parts = name.split("-")
    # Some files append artwork notes to the direction segment, e.g.
    # 安铺村-北，小灯箱80x130CM-01.jpg; the segment still starts with 北.
    for part in parts[1:]:
        part = part.strip()
        for token in DIRECTION_TOKENS:
            if part.startswith(token):
                return token
    if len(parts) >= 3:
        part = parts[1].strip()
        for token in FACILITY_DIRECTION_TOKENS:
            if part == token or part.startswith(token):
                return token
        return "未知"
    return "未知"


def poster_station(name: str, fallback: str = "") -> str:
    """Station is the part before the direction token, e.g. 安平小区-东-01."""
    for token in DIRECTION_TOKENS:
        marker = f"-{token}-"
        if marker in name:
            return name.split(marker, 1)[0].strip()
    parts = name.split("-")
    if len(parts) >= 3:
        return parts[0].strip()
    return (fallback or name).strip()


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

