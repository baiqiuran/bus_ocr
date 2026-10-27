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
from buspipe.templates import apply_template_profile
from buspipe.templates import recovery_profile
from buspipe.recovery import classify_non_route, recover_route_layout, locate_recovered_stop_band
from buspipe.landscape import (
    UnsupportedLandscapeError,
    extract_landscape_entry,
    locate_landscape,
    locate_wide_landscape,
    locate_stacked_landscape,
    route_from_filename,
)
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
        current_stop = station if station is not None else settings.current_stop
        rgb = np.asarray(Image.open(path).convert("RGB"))
        height, width, _ = rgb.shape
        # Template profiles are opt-in and default to legacy settings.  They
        # must be resolved from the actual image dimensions, not the CLI
        # defaults, because a single run can contain several template families.
        settings, _profile_key = apply_template_profile(settings, width, height, path.stem)
        endpoints_only = settings.extract_mode == "endpoints"
        if endpoints_only and width > height:
            try:
                return self._extract_landscape(path, rgb, current_stop, only_blocks, settings)
            except UnsupportedTemplateError:
                # Cropped multi-route sheets can also be wider than tall.
                # Only dispatch on a matching single-route template signature.
                pass
        red = make_red_mask(rgb)
        recovered = None
        try:
            blocks = detect_route_blocks(rgb, settings.layout)
        except RuntimeError:
            if width > height:
                raise UnsupportedTemplateError(
                    f"unsupported landscape next-stop board ({width}x{height})"
                ) from None
            if not endpoints_only or recovery_profile(width, height, path.stem) is None:
                raise
            classification = classify_non_route(rgb, path.stem, self.backend, settings)
            if classification:
                direction = poster_direction(path.stem)
                station_name = poster_station(path.stem, current_stop)
                return {
                    "poster": path.stem, "path": str(path), "size": [width, height],
                    "station": station_name, "direction": direction,
                    "station_key": f"{station_name}-{direction}", "extract_mode": "endpoints",
                    "entries": [], "skipped": True, **classification,
                }
            if recovery_profile(width, height, path.stem).name == "route_recovery_stacked":
                return self._extract_stacked_landscape(path, rgb, current_stop, only_blocks, settings)
            recovered = recover_route_layout(rgb, settings, path.stem)
            if recovered is None:
                raise
            settings, blocks, red = recovered.settings, recovered.blocks, recovered.red_mask
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
                    if recovered:
                        row_y0, row_y1, columns, row_debug = locate_recovered_stop_band(
                            rgb, route_y1, next_start, settings, recovery=recovered,
                        )
                    else:
                        row_y0, row_y1, columns, row_debug = locate_stop_band(
                            rgb, route_y1, next_start, settings.layout, adaptive=endpoints_only,
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

            hints = settings.route_hints.get(poster_name, [])
            filename_route = (
                route_from_filename(poster_name)
                if len(blocks) == 1 and not hints
                else ""
            )
            if endpoints_only:
                # Keep actual OCR separate from an optional known-poster hint.
                route_ocr = pick_route_number(number_items, {}, poster_name, index)
                route = hints[index] if index < len(hints) else filename_route or route_ocr
                header = {
                    "route": route,
                    "route_ocr": route_ocr,
                    "route_filename": filename_route,
                }
            else:
                header = parse_header(
                    header_items, number_items, time_items, date_items,
                    settings.route_hints, poster_name, index, row_y0,
                )
                if filename_route:
                    header["route"] = filename_route
                    header["route_filename"] = filename_route
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
                if recovered:
                    entry["recovery_profile"] = recovered.profile
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
            **({"recovery": {"profile": recovered.profile}} if recovered else {}),
        }

    def _extract_landscape(
        self, path: Path, rgb: np.ndarray, current_stop: str, only_blocks: set[int] | None,
        settings: PipelineSettings,
    ) -> dict[str, Any]:
        height, width = rgb.shape[:2]
        poster = path.stem
        station, direction = poster_station(poster, current_stop), poster_direction(poster)
        try:
            layout = locate_landscape(rgb, settings.landscape)
        except UnsupportedLandscapeError as regular_error:
            try:
                layout = locate_wide_landscape(rgb, settings.landscape)
            except UnsupportedLandscapeError as wide_error:
                raise UnsupportedTemplateError(
                    f"unsupported landscape next-stop board ({width}x{height}): "
                    f"{regular_error}; wide: {wide_error}"
                ) from wide_error
        result = {
            "poster": poster, "path": str(path), "size": [width, height],
            "extract_mode": "endpoints", "template": layout.template,
            "station": station, "direction": direction, "station_key": f"{station}-{direction}",
            "route_blocks": [[layout.route_box[1], layout.route_box[3] - 1]], "entries": [],
        }
        if only_blocks is not None and 0 not in only_blocks:
            return result
        self._emit("block_start", poster=poster, block_index=0, block_count=1,
                   route_y0=layout.route_box[1], route_y1=layout.route_box[3] - 1)
        cached = self.checkpoint.load(0) if self.checkpoint is not None else None
        if cached is not None:
            entry = copy.deepcopy(cached)
            entry["cache_hit"] = True
            result["entries"].append(entry)
            self._emit("block_cached", poster=poster, block_index=0, route=entry.get("route", ""),
                       stop_count=0, endpoint_count=2, extract_mode="endpoints")
            return result
        try:
            entry = extract_landscape_entry(rgb, layout, poster, station, direction,
                                            self.backend, settings, self.stats)
        except Exception as exc:
            self._emit("block_failed", poster=poster, block_index=0, error=str(exc))
            result["entries"].append({"poster": poster, "direction": direction, "block_index": 0,
                                      "route": "", "error": str(exc)})
            return result
        result["entries"].append(entry)
        if self.checkpoint is not None:
            self.checkpoint.store(0, entry)
        self._emit("block_done", poster=poster, block_index=0, route=entry["route"], stop_count=0,
                   endpoint_count=2, extract_mode="endpoints", column_count=len(entry["columns"]), row=entry["row"])
        if self.debug_dir is not None:
            self._draw_debug(rgb, poster, 0, layout.route_box[1], entry["row"][1], entry["endpoints"])
        return result

    def _extract_stacked_landscape(self, path, rgb, current_stop, only_blocks, settings):
        layouts = locate_stacked_landscape(rgb, settings.landscape)
        station = poster_station(path.stem, current_stop)
        direction = poster_direction(path.stem)
        profile = recovery_profile(rgb.shape[1], rgb.shape[0], path.stem).cache_key
        result = {
            "poster":path.stem, "path":str(path), "size":[rgb.shape[1],rgb.shape[0]],
            "station":station, "direction":direction, "station_key":f"{station}-{direction}",
            "extract_mode":"endpoints", "recovery":{"profile":profile},
            "route_blocks":[[layout.route_box[1],layout.route_box[3]-1] for layout in layouts],
            "entries":[],
        }
        for index,layout in enumerate(layouts):
            if only_blocks is not None and index not in only_blocks:
                continue
            self._emit("block_start",poster=path.stem,block_index=index,block_count=len(layouts),
                       route_y0=layout.route_box[1],route_y1=layout.route_box[3]-1)
            cached=self.checkpoint.load(index) if self.checkpoint is not None else None
            if cached is not None:
                entry=copy.deepcopy(cached)
                entry["cache_hit"]=True
                self._emit("block_cached",block_index=index,route=entry.get("route",""))
            else:
                try:
                    entry=extract_landscape_entry(rgb,layout,path.stem,station,direction,
                                                 self.backend,settings,self.stats)
                    entry.update(block_index=index,recovery_profile=profile)
                    if self.checkpoint is not None:
                        self.checkpoint.store(index,entry)
                    self._emit("block_done",block_index=index,route=entry["route"],endpoint_count=2)
                except Exception as exc:
                    entry={"poster":path.stem,"direction":direction,"block_index":index,
                           "route":"","error":str(exc)}
                    self._emit("block_failed",block_index=index,error=str(exc))
            result["entries"].append(entry)
        return result

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
