"""Reusable single-poster extraction core.

``PosterExtractor.extract`` performs layout projection, header OCR, column OCR
and business assembly.  It has no printing, environment reads or module-level
mutable state; the CLI passes an event sink for progress output and the batch
runner passes a block checkpoint for resumability.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

import numpy as np
from PIL import Image, ImageDraw

from buspipe.config import PipelineSettings, poster_direction, poster_station
from buspipe.endpoints import recognize_endpoints
from buspipe.layout import (
    detect_route_blocks,
    locate_stop_band,
    make_red_mask,
    route_number_bounds,
)
from buspipe.ocr import (
    OcrBackend,
    OcrParams,
    OcrStats,
    dedup_items,
    ocr_regions,
    recognize_columns,
    route_number_boxes,
    vote_route_items,
)
from buspipe.parsing import assemble_stops, normalize_for_compare, parse_header, pick_route_number, refresh_entry_validation


class BlockCheckpoint(Protocol):
    def load(self, block_index: int) -> dict[str, Any] | None:
        ...

    def store(self, block_index: int, entry: dict[str, Any]) -> None:
        ...


EventSink = Callable[[str, dict[str, Any]], None]


class UnsupportedTemplateError(RuntimeError):
    """The poster is a different board type than the multi-route stop sheet."""


class PosterExtractor:
    def __init__(
        self,
        backend: OcrBackend,
        settings: PipelineSettings,
        stats: OcrStats | None = None,
        checkpoint: BlockCheckpoint | None = None,
        event_sink: EventSink | None = None,
        debug_dir: Path | None = None,
    ) -> None:
        self.backend = backend
        self.settings = settings
        self.stats = stats
        self.checkpoint = checkpoint
        self.event_sink = event_sink
        self.debug_dir = debug_dir

    def _emit(self, event: str, **payload: Any) -> None:
        if self.event_sink:
            self.event_sink(event, payload)

    def extract(
        self,
        path: Path | str,
        station: str | None = None,
        only_blocks: set[int] | None = None,
    ) -> dict[str, Any]:
        path = Path(path)
        settings = self.settings
        endpoints_only = settings.extract_mode == "endpoints"
        current_stop = station if station is not None else settings.current_stop
        rgb = np.asarray(Image.open(path).convert("RGB"))
        height, width, _ = rgb.shape
        red = make_red_mask(rgb)
        try:
            blocks = detect_route_blocks(rgb, settings.layout)
        except RuntimeError:
            if width > height:
                raise UnsupportedTemplateError(
                    f"unsupported landscape next-stop board ({width}x{height})"
                ) from None
            raise
        poster_name = path.stem
        direction = poster_direction(poster_name)
        if endpoints_only:
            current_stop = poster_station(poster_name, current_stop)
        entries: list[dict[str, Any]] = []

        for index, (route_y0, route_y1) in enumerate(blocks):
            if only_blocks is not None and index not in only_blocks:
                continue
            self._emit(
                "block_start",
                poster=poster_name,
                block_index=index,
                block_count=len(blocks),
                route_y0=int(route_y0),
                route_y1=int(route_y1),
            )
            if self.checkpoint is not None:
                cached = self.checkpoint.load(index)
                if cached is not None:
                    entry = copy.deepcopy(cached)
                    entry.setdefault("cache_hit", True)
                    entries.append(entry)
                    self._emit(
                        "block_cached",
                        poster=poster_name,
                        block_index=index,
                        route=entry.get("route", ""),
                        stop_count=entry.get("stop_count", 0),
                        endpoint_count=len(entry.get("endpoints", [])),
                        extract_mode=settings.extract_mode,
                    )
                    continue

            next_start = blocks[index + 1][0] if index + 1 < len(blocks) else None
            number_x0, number_x1 = route_number_bounds(
                red, (route_y0, route_y1), settings.layout
            )
            # Start the header to the right of the number's real extent.  The
            # old fixed 690px edge clipped wide numbers into the header OCR.
            header_x0 = min(3350, max(690, number_x1 + 20))
            try:
                try:
                    row_y0, row_y1, columns, row_debug = locate_stop_band(
                        rgb, route_y1, next_start, settings.layout
                    )
                except RuntimeError:
                    if width > height:
                        raise UnsupportedTemplateError(
                            f"unsupported landscape next-stop board ({width}x{height})"
                        ) from None
                    raise
                # Next-stop boards carry one large column or a horizontal name,
                # rather than a row of tall vertical stop-name columns.
                # Count projected columns before wide-column recovery: one
                # large next-stop name must not turn into several fake stops.
                if width > height and (
                    row_debug["raw_column_count"] <= 1
                    or row_debug["raw_column_median_aspect"] < settings.layout.landscape_min_column_aspect
                ):
                    raise UnsupportedTemplateError(
                        f"unsupported landscape next-stop board ({width}x{height})"
                    )
                boxes = [] if endpoints_only else [
                    (header_x0, max(0, route_y0 - 30), 3350, route_y1 + 70),
                    (3300, max(0, route_y0 - 30), 4080, route_y1 + 80),
                    (3200, max(0, row_y1 - 120), 4080, min(height, row_y1 + 100)),
                ]
                boxes.extend(route_number_boxes(
                    (route_y0, route_y1), (number_x0, number_x1), rgb.shape
                ))
                region_items = [
                    dedup_items(items)
                    for items in ocr_regions(
                        rgb,
                        boxes,
                        self.backend,
                        OcrParams.from_settings(settings.ocr),
                        self.stats,
                    )
                ]
                if endpoints_only:
                    header_items, time_items, date_items = [], [], []
                    number_candidates = region_items
                else:
                    header_items, time_items, date_items, *number_candidates = region_items
                number_items = vote_route_items(number_candidates)
            except UnsupportedTemplateError:
                raise
            except Exception as exc:  # keep going and report the block
                self._emit("block_failed", poster=poster_name, block_index=index, error=str(exc))
                entries.append(
                    {
                        "poster": poster_name,
                        "direction": direction,
                        "block_index": index,
                        "route": "",
                        "error": str(exc),
                    }
                )
                continue

            if endpoints_only:
                # Keep actual OCR separate from an optional known-poster hint.
                route_ocr = pick_route_number(number_items, {}, poster_name, index)
                hints = settings.route_hints.get(poster_name, [])
                header = {"route": hints[index] if index < len(hints) else route_ocr, "route_ocr": route_ocr}
            else:
                header = parse_header(
                    header_items, number_items, time_items, date_items,
                    settings.route_hints, poster_name, index, row_y0,
                )
            try:
                if endpoints_only:
                    endpoints, recognized = recognize_endpoints(
                        rgb, columns, row_y0, row_y1, self.backend, settings, self.stats,
                    )
                else:
                    recognized = recognize_columns(
                        rgb, columns, row_y0, row_y1, self.backend,
                        settings.ocr, settings.canvas, self.stats,
                    )
            except Exception as exc:
                self._emit("block_failed", poster=poster_name, block_index=index, error=str(exc))
                entries.append(
                    {
                        "poster": poster_name,
                        "direction": direction,
                        "block_index": index,
                        "route": header.get("route", ""),
                        "error": str(exc),
                    }
                )
                continue

            entry = {
                "poster": poster_name,
                "direction": direction,
                "block_index": index,
                **header,
                "number_items": number_items,
                "row": [int(row_y0), int(row_y1)],
                "row_debug": row_debug,
                "columns": [{k: v for k, v in col.items() if k != "items"} for col in recognized],
            }
            if endpoints_only:
                entry.update(
                    extract_mode="endpoints", station=current_stop,
                    station_key=f"{current_stop}-{direction}", endpoints=endpoints, stops=[],
                )
                refresh_entry_validation(entry)
            else:
                stops, current_index = assemble_stops(recognized, current_stop, header["next_stop"])
                expected_next = ""
                if current_index is not None and current_index + 1 < len(stops):
                    expected_next = stops[current_index + 1]["display_name"]
                next_matches = bool(header["next_stop"]) and normalize_for_compare(
                    expected_next
                ) == normalize_for_compare(header["next_stop"])
                entry.update(
                    stops=stops, stop_count=len(stops), current_index=current_index,
                    expected_next=expected_next, next_stop_matches=next_matches,
                    header_items=header_items, time_items=time_items, date_items=date_items,
                )
            entries.append(entry)
            if self.checkpoint is not None:
                self.checkpoint.store(index, entry)

            self._emit(
                "block_done",
                poster=poster_name,
                block_index=index,
                route=entry["route"],
                stop_count=entry.get("stop_count", 0),
                endpoint_count=len(endpoints) if endpoints_only else 0,
                extract_mode=settings.extract_mode,
                column_count=len(recognized),
                row=[int(row_y0), int(row_y1)],
            )
            if self.debug_dir is not None:
                self._draw_debug(rgb, poster_name, index, route_y0, row_y1, endpoints if endpoints_only else stops)

        return {
            "poster": poster_name,
            "extract_mode": settings.extract_mode,
            "station": current_stop,
            "station_key": f"{current_stop}-{direction}",
            "direction": direction,
            "path": str(path),
            "size": [int(width), int(height)],
            "route_blocks": [[int(a), int(b)] for a, b in blocks],
            "entries": entries,
        }

    def _draw_debug(
        self,
        rgb: np.ndarray,
        poster_name: str,
        index: int,
        route_y0: int,
        row_y1: int,
        stops: Sequence[dict[str, Any]],
    ) -> None:
        assert self.debug_dir is not None
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        height = rgb.shape[0]
        crop = Image.fromarray(rgb[max(0, route_y0 - 40):min(height, row_y1 + 60), :])
        draw = ImageDraw.Draw(crop)
        for stop in stops:
            x = (stop["x0"] + stop["x1"]) // 2
            draw.text((x - 20, stop["seq"] * 2), stop["display_name"], fill=(200, 0, 0))
        crop.save(self.debug_dir / f"{poster_name}_b{index:02d}.jpg", quality=90)
