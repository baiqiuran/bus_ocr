"""Batch entry point: scan, extract, validate, review, export.

``run_batch`` is the highest acceptance seam.  It receives a source directory
or an existing SQLite manifest plus a configuration, and returns a batch
result describing what happened.  OCR and VLM are injected adapters.
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from buspipe.cache import BlockCache, CacheStats, FileCache, PosterCache, atomic_write_text, key_for
from buspipe.config import (
    DEFAULT_ROUTE_HINTS,
    PipelineSettings,
    poster_station,
)
from buspipe.core import PosterExtractor
from buspipe.ocr import LocalRapidOcrBackend, OcrBackend, OcrStats
from PIL import Image

from buspipe.report import write_outputs
from buspipe.parsing import normalize_for_compare
from buspipe.review import PosterSummary, ReviewEngine, apply_overrides, summaries_from_result
from buspipe.store import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_LAYOUT_DONE,
    STATUS_OCR_DONE,
    STATUS_PENDING,
    STATUS_VALIDATED,
    STATUS_VLM_REVIEW,
    ManifestStore,
    poster_json_path,
    review_sort_key,
    scan_directory,
    utc_now,
)
from buspipe.vlm import VlmCache, VlmOpinion, VlmRequest, VlmStats

VLM_REASONS = {
    "low_confidence",
    "cross_column",
    "wide_box",
    "low_coverage",
    "empty_ocr",
    "next_stop_mismatch",
}

logger = logging.getLogger(__name__)


class BatchError(RuntimeError):
    pass


@dataclass
class BatchConfig:
    out_dir: Path = Path("output")
    work_dir: Path | None = None
    manifest_path: Path | None = None
    current_stop: str = ""
    route_hints: Mapping[str, Sequence[str]] = field(default_factory=lambda: DEFAULT_ROUTE_HINTS)
    pipeline: PipelineSettings | None = None
    workers: int = 1
    threads: int = 4
    dry_run: bool = False
    retry_failed: bool = True
    max_attempts: int = 3
    rescan: bool = True
    use_cache: bool = True
    enable_review: bool = True
    enable_vlm: bool = False
    vlm_max_calls_per_poster: int = 8
    vlm_min_confidence: float = 0.6
    vlm_prompt_version: str = "vlm-1"
    export: bool = True
    limit: int | None = None
    only_hashes: Sequence[str] | None = None
    only_stations: Sequence[str] | None = None
    ocr_mode: str | None = None
    canvas_max_columns: int | None = None
    canvas_max_width: int | None = None
    on_event: Callable[[str, dict[str, Any]], None] | None = field(default=None, repr=False)

    def resolved_work_dir(self) -> Path:
        return self.work_dir or (self.out_dir / ".batch")

    def resolved_manifest(self) -> Path:
        return self.manifest_path or (self.resolved_work_dir() / "manifest.sqlite")

    def resolved_pipeline(self) -> PipelineSettings:
        base = self.pipeline or PipelineSettings(
            current_stop=self.current_stop,
            route_hints=self.route_hints,
        )
        ocr = base.ocr
        if self.ocr_mode is not None:
            ocr = replace(ocr, mode=self.ocr_mode)
        canvas = base.canvas
        if self.canvas_max_columns is not None:
            canvas = replace(canvas, max_columns=self.canvas_max_columns)
        if self.canvas_max_width is not None:
            canvas = replace(canvas, max_width=self.canvas_max_width)
        return replace(base, ocr=ocr, canvas=canvas)


@dataclass
class BatchResult:
    manifest_path: Path
    out_dir: Path
    dry_run: bool
    started_at: str
    elapsed: float
    scan: dict[str, Any]
    counts: dict[str, int]
    review_counts: dict[str, int]
    cache: dict[str, int]
    ocr: dict[str, int]
    vlm: dict[str, int] = field(default_factory=dict)
    poster_files: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    stage_seconds: dict[str, float] = field(default_factory=dict)
    review_csv: str = ""
    report_files: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "manifest_path": str(self.manifest_path),
            "out_dir": str(self.out_dir),
            "dry_run": self.dry_run,
            "started_at": self.started_at,
            "elapsed": self.elapsed,
            "scan": self.scan,
            "counts": self.counts,
            "review_counts": self.review_counts,
            "cache": self.cache,
            "ocr": self.ocr,
            "vlm": self.vlm,
            "poster_files": self.poster_files,
            "errors": self.errors,
            "stage_seconds": self.stage_seconds,
            "review_csv": self.review_csv,
            "report_files": self.report_files,
        }


def _write_result(path: Path, result: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(result, ensure_ascii=False, indent=2, default=str))


def _write_summaries(path: Path, summaries: Sequence[PosterSummary]) -> None:
    payload = [
        {
            "image_hash": item.image_hash,
            "station": item.station,
            "route": item.route,
            "direction": item.direction,
            "names": item.names,
            "stop_count": item.stop_count,
            "next_stop_matches": item.next_stop_matches,
        }
        for item in summaries
    ]
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _find_entry(result: dict[str, Any], item: Mapping[str, Any]) -> dict[str, Any] | None:
    for entry in result.get("entries", []):
        if str(entry.get("route", "")) != str(item.get("route", "")):
            continue
        if str(entry.get("direction", "")) != str(item.get("direction", "")):
            continue
        return entry
    return None


def _item_geometry(entry: dict[str, Any], item: Mapping[str, Any]) -> dict[str, Any] | None:
    seq = int(item.get("seq", 0))
    if seq > 0:
        stops = entry.get("stops", [])
        if seq <= len(stops):
            return stops[seq - 1]
        return None
    column_index = -seq - 1
    columns = entry.get("columns", [])
    if 0 <= column_index < len(columns):
        return columns[column_index]
    return None


def _crop_png(image: Image.Image, geometry: Mapping[str, Any], pad: int = 6) -> bytes:
    from io import BytesIO

    left = max(0, int(geometry.get("x0", 0)) - pad)
    top = max(0, int(geometry.get("y0", 0)) - 5)
    right = min(image.width, int(geometry.get("x1", 0)) + pad + 1)
    bottom = min(image.height, int(geometry.get("y1", 0)) + 6)
    buffer = BytesIO()
    image.crop((left, top, right, bottom)).save(buffer, format="PNG")
    return buffer.getvalue()


def _context_pngs(
    image: Image.Image | None,
    entry: dict[str, Any],
    item: Mapping[str, Any],
) -> list[bytes]:
    if image is None:
        return []
    seq = int(item.get("seq", 0))
    contexts: list[bytes] = []
    if seq > 0:
        stops = entry.get("stops", [])
        for offset in (-1, 1):
            index = seq - 1 + offset
            if 0 <= index < len(stops):
                contexts.append(_crop_png(image, stops[index]))
    else:
        column_index = -seq - 1
        columns = entry.get("columns", [])
        for offset in (-1, 1):
            index = column_index + offset
            if 0 <= index < len(columns):
                contexts.append(_crop_png(image, columns[index]))
    return contexts


def _update_stop_source(result: dict[str, Any], item: Mapping[str, Any], source: str) -> None:
    entry = _find_entry(result, item)
    if entry is None:
        return
    seq = int(item.get("seq", 0))
    stops = entry.get("stops", [])
    if 1 <= seq <= len(stops) and stops[seq - 1].get("source") != "manual":
        stops[seq - 1]["source"] = source


def _append_candidate(result: dict[str, Any], key: str, candidate: dict[str, Any]) -> None:
    items = result.setdefault(key, [])
    identity = (candidate.get("route"), candidate.get("direction"), candidate.get("seq"))
    result[key] = [
        existing
        for existing in items
        if (existing.get("route"), existing.get("direction"), existing.get("seq")) != identity
    ]
    result[key].append(candidate)


def _apply_vlm_opinion(
    store: ManifestStore,
    result: dict[str, Any],
    item: dict[str, Any],
    opinion: VlmOpinion,
    min_confidence: float,
    stats: VlmStats,
) -> None:
    if opinion.confidence < min_confidence:
        stats.inc("ignored")
        return
    note = f"（{opinion.note}）" if opinion.note else ""
    candidate = f"{opinion.text}{note}"
    ocr_text = str(item.get("ocr_text", "")).strip()
    payload = {
        "route": item.get("route", ""),
        "direction": item.get("direction", ""),
        "seq": item.get("seq", 0),
        "text": opinion.text,
        "note": opinion.note,
        "display_name": candidate,
        "agree": opinion.agree,
        "confidence": opinion.confidence,
        "reason": opinion.reason,
        "model_version": opinion.model_version,
    }
    if not ocr_text:
        store.set_review_status(item["id"], "vlm_only", vlm_text=candidate, source="vlm_only")
        _append_candidate(result, "vlm_candidates", {**payload, "source": "vlm_only"})
        stats.inc("vlm_only")
        return
    if opinion.agree and normalize_for_compare(candidate) == normalize_for_compare(ocr_text):
        store.set_review_status(item["id"], "vlm_confirmed", vlm_text=candidate, source="ocr+vlm")
        _update_stop_source(result, item, "ocr+vlm")
        stats.inc("confirmed")
        return
    store.set_review_status(item["id"], "needs_human", vlm_text=candidate, source="ocr")
    _append_candidate(result, "vlm_opinions", {**payload, "ocr_text": ocr_text, "source": "ocr"})
    stats.inc("conflicts")


def _run_vlm_pass(
    store: ManifestStore,
    config: BatchConfig,
    reviewer: Any,
    out_dir: Path,
    work_dir: Path,
    stats: VlmStats,
) -> None:
    cache = VlmCache(work_dir / "cache", stats)
    model_version = str(getattr(reviewer, "model_version", "vlm"))
    touched: set[str] = set()
    for poster in store.posters():
        if poster["status"] == STATUS_FAILED:
            continue
        image_hash = poster["image_hash"]
        all_open = store.review_items(image_hash=image_hash, statuses=["open"])
        if not all_open:
            continue
        touched.add(image_hash)
        open_items = [item for item in all_open if item["reason"] in VLM_REASONS]
        if not open_items:
            continue
        result_path = poster_json_path(out_dir, poster)
        if not result_path.exists():
            continue
        result = json.loads(result_path.read_text(encoding="utf-8"))
        selected = sorted(open_items, key=review_sort_key)[: max(0, config.vlm_max_calls_per_poster)]
        if not selected:
            continue
        image: Image.Image | None = None
        try:
            image = Image.open(poster["path"]).convert("RGB")
        except OSError:
            image = None
        changed = False
        for item in selected:
            entry = _find_entry(result, item)
            if entry is None:
                continue
            geometry = _item_geometry(entry, item)
            target_png: bytes | None = None
            crop_path = item.get("crop_path", "")
            if crop_path:
                try:
                    target_png = Path(crop_path).read_bytes()
                except OSError:
                    target_png = None
            if target_png is None and image is not None and geometry is not None:
                target_png = _crop_png(image, geometry)
            if not target_png:
                continue
            request = VlmRequest(
                target_png=target_png,
                context_pngs=_context_pngs(image, entry, item),
                route=str(item.get("route", "")),
                direction=str(item.get("direction", "")),
                header_next_stop=str(entry.get("next_stop", "")),
                destination=str(entry.get("destination", "")),
                ocr_text=str(item.get("ocr_text", "")),
                prompt_version=config.vlm_prompt_version,
            )
            opinion = cache.get(request, model_version)
            if opinion is None:
                try:
                    opinion = reviewer.review(request)
                    stats.inc("calls")
                    cache.put(request, model_version, opinion)
                except Exception:
                    stats.inc("errors")
                    continue
            _apply_vlm_opinion(store, result, item, opinion, config.vlm_min_confidence, stats)
            changed = True
        if image is not None:
            image.close()
        if changed:
            _write_result(result_path, result)
    unresolved = set()
    for image_hash in touched:
        if any(
            item["status"] in {"open", "needs_human", "vlm_only"}
            for item in store.review_items(image_hash=image_hash)
        ):
            unresolved.add(image_hash)
    return unresolved


def _read_summaries(path: Path) -> list[PosterSummary]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [
        PosterSummary(
            image_hash=str(item.get("image_hash", "")),
            station=str(item.get("station", "")),
            route=str(item.get("route", "")),
            direction=str(item.get("direction", "")),
            names=list(item.get("names", [])),
            stop_count=int(item.get("stop_count", 0)),
            next_stop_matches=bool(item.get("next_stop_matches")),
        )
        for item in payload
    ]


def _block_failure(result: dict[str, Any]) -> str:
    entries = result.get("entries", [])
    if not entries:
        return "no route blocks extracted"
    failed = [entry for entry in entries if "error" in entry]
    if failed:
        return str(failed[0].get("error") or "block failed")
    return ""


def run_batch(
    source: Path | str,
    config: BatchConfig,
    *,
    ocr_backend: OcrBackend | None = None,
    vlm_reviewer: Any | None = None,
    extractor: Callable[[Path, PipelineSettings, Callable[[str, dict[str, Any]], None] | None], dict[str, Any]]
    | None = None,
) -> BatchResult:
    started_at = utc_now()
    wall_start = time.perf_counter()
    source = Path(source)
    out_dir = Path(config.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir = config.resolved_work_dir()
    work_dir.mkdir(parents=True, exist_ok=True)

    if source.is_dir():
        manifest_path = config.resolved_manifest()
        source_dir: Path | None = source
    elif source.is_file() and source.suffix.lower() in {".sqlite", ".db", ".sqlite3"}:
        manifest_path = source
        source_dir = None
    else:
        raise FileNotFoundError(f"batch source not found: {source}")

    store = ManifestStore(manifest_path)
    scan: dict[str, Any] = {"discovered": 0, "new": 0, "duplicates": 0, "unchanged": 0, "failed": 0}
    if source.is_dir():
        scan = scan_directory(store, source, station_fallback=config.current_stop)
        store.set_meta("source_dir", str(source.resolve()))
    else:
        source_dir = Path(store.get_meta("source_dir", "")) if store.get_meta("source_dir") else None
        if config.rescan and source_dir and source_dir.is_dir():
            scan = scan_directory(store, source_dir, station_fallback=config.current_stop)

    stage_seconds = {"scan": round(time.perf_counter() - wall_start, 3)}
    counts = store.counts()
    cache_stats = CacheStats()
    ocr_stats = OcrStats()
    vlm_stats = VlmStats()

    if config.dry_run:
        return BatchResult(
            manifest_path=manifest_path,
            out_dir=out_dir,
            dry_run=True,
            started_at=started_at,
            elapsed=round(time.perf_counter() - wall_start, 3),
            scan=scan,
            counts=counts,
            review_counts=store.review_counts(),
            cache=cache_stats.snapshot(),
            ocr=ocr_stats.snapshot(),
            vlm={},
            stage_seconds=stage_seconds,
        )

    rows = store.processable(
        retry_failed=config.retry_failed,
        max_attempts=config.max_attempts,
        only_hashes=config.only_hashes,
    )
    if config.only_stations:
        wanted_stations = set(config.only_stations)
        rows = [row for row in rows if row["station"] in wanted_stations]
    if config.limit is not None:
        rows = rows[: max(0, config.limit)]
    base_settings = config.resolved_pipeline()
    backend = ocr_backend
    if backend is None and extractor is None:
        backend = LocalRapidOcrBackend(
            threads=config.threads, model_version=base_settings.model_version, stats=ocr_stats
        )
    if isinstance(getattr(backend, "stats", None), OcrStats):
        ocr_stats = backend.stats
    if backend is not None:
        # The cache key must follow the actual adapter/model, not just the
        # pipeline default; otherwise switching remote models reuses stale OCR.
        base_settings = replace(base_settings, model_version=str(backend.model_version))

    review_engine = ReviewEngine(base_settings)
    summary_dir = work_dir / "summaries"
    summary_dir.mkdir(parents=True, exist_ok=True)
    crop_dir = out_dir / "review" / "crops"
    extraction_start = time.perf_counter()
    errors: list[str] = []
    poster_files: dict[str, str] = {}

    def process(row: dict[str, Any]) -> tuple[str, dict[str, str], str | None]:
        image_hash = row["image_hash"]
        station = row["station"] or poster_station(Path(row["path"]).stem, config.current_stop)
        settings = replace(base_settings, current_stop=station)
        file_path = poster_json_path(out_dir, row)
        timings: dict[str, str] = {}
        local_start = time.perf_counter()
        try:
            status = row["status"]
            if status in {STATUS_PENDING, STATUS_LAYOUT_DONE, STATUS_OCR_DONE, STATUS_FAILED}:
                store.set_status(image_hash, STATUS_LAYOUT_DONE, attempt=True)
                base_key = settings.fingerprint()
                result: dict[str, Any] | None = None
                if config.use_cache:
                    poster_cache = PosterCache(
                        FileCache(work_dir / "cache", "posters", cache_stats),
                        key_for({"kind": "poster", "image_hash": image_hash, "base": base_key}),
                    )
                    result = poster_cache.load()
                if result is None:
                    checkpoint = (
                        BlockCache(FileCache(work_dir / "cache", "blocks", cache_stats), image_hash, base_key)
                        if config.use_cache
                        else None
                    )

                    def event_sink(event: str, payload: dict[str, Any]) -> None:
                        if event == "block_start":
                            store.mark_block(image_hash, payload["block_index"], "started")
                        elif event == "block_cached":
                            store.mark_block(
                                image_hash,
                                payload["block_index"],
                                "cached",
                                route=str(payload.get("route", "")),
                            )
                        elif event == "block_done":
                            store.mark_block(
                                image_hash,
                                payload["block_index"],
                                "done",
                                route=str(payload.get("route", "")),
                            )
                        elif event == "block_failed":
                            store.mark_block(
                                image_hash,
                                payload["block_index"],
                                "failed",
                                error=str(payload.get("error", "")),
                            )
                        if config.on_event:
                            config.on_event(event, payload)

                    if extractor is not None:
                        result = extractor(Path(row["path"]), settings, event_sink)
                    else:
                        poster_extractor = PosterExtractor(
                            backend,
                            settings,
                            stats=ocr_stats,
                            checkpoint=checkpoint,
                            event_sink=event_sink,
                        )
                        result = poster_extractor.extract(Path(row["path"]), station=station)
                    failure = _block_failure(result)
                    if failure:
                        raise BatchError(failure)
                    if config.use_cache:
                        PosterCache(
                            FileCache(work_dir / "cache", "posters", cache_stats),
                            key_for({"kind": "poster", "image_hash": image_hash, "base": base_key}),
                        ).store(result)
                timings["extract"] = f"{time.perf_counter() - local_start:.3f}"
                store.set_status(image_hash, STATUS_OCR_DONE)
                _write_result(file_path, result)
            else:
                # Later stages (validated/vlm_review) resume from the per-poster file.
                if file_path.exists():
                    result = json.loads(file_path.read_text(encoding="utf-8"))
                elif config.use_cache:
                    base_key = settings.fingerprint()
                    result = PosterCache(
                        FileCache(work_dir / "cache", "posters", cache_stats),
                        key_for({"kind": "poster", "image_hash": image_hash, "base": base_key}),
                    ).load()
                else:
                    result = None
                if result is None:
                    store.set_status(image_hash, STATUS_PENDING)
                    raise BatchError("result missing for resumed poster; rerun will extract")

            override_rows = store.overrides(image_hash)
            if override_rows:
                apply_overrides(result, override_rows)
            if config.enable_review:
                review_start = time.perf_counter()
                items = review_engine.collect(
                    result,
                    image_hash,
                    image_path=row["path"],
                    crop_dir=crop_dir,
                )
                store.replace_review_items(image_hash, items)
                timings["review"] = f"{time.perf_counter() - review_start:.3f}"
            summaries = summaries_from_result(result, image_hash, station)
            _write_summaries(summary_dir / f"{image_hash}.json", summaries)
            _write_result(file_path, result)
            poster_files[image_hash] = str(file_path)
            store.set_status(image_hash, STATUS_VALIDATED)
            store.set_status(image_hash, STATUS_DONE, error="")
            return image_hash, timings, None
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            store.set_status(image_hash, STATUS_FAILED, error=message, attempt=False)
            logger.warning("poster failed: %s (%s)", row["path"], message)
            return image_hash, timings, message

    if rows:
        workers = max(1, config.workers)
        window = max(1, workers * 2)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            iterator = iter(rows)
            pending = set()
            for _ in range(min(len(rows), window)):
                pending.add(pool.submit(process, next(iterator)))
            while pending:
                completed, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    image_hash, timings, error = future.result()
                    if error:
                        errors.append(f"{image_hash[:8]}: {error}")
                while len(pending) < window:
                    try:
                        row = next(iterator)
                    except StopIteration:
                        break
                    pending.add(pool.submit(process, row))
    stage_seconds["extract"] = round(time.perf_counter() - extraction_start, 3)

    review_start = time.perf_counter()
    if config.enable_review:
        summaries: list[PosterSummary] = []
        for poster in store.posters():
            if poster["status"] not in {STATUS_VALIDATED, STATUS_VLM_REVIEW, STATUS_DONE}:
                continue
            summary_path = summary_dir / f"{poster['image_hash']}.json"
            loaded = _read_summaries(summary_path)
            if not loaded:
                file_path = poster_json_path(out_dir, poster)
                if file_path.exists():
                    try:
                        payload = json.loads(file_path.read_text(encoding="utf-8"))
                    except ValueError:
                        payload = {}
                    station = poster["station"] or poster_station(Path(poster["path"]).stem)
                    loaded = summaries_from_result(payload, poster["image_hash"], station)
                    if loaded:
                        _write_summaries(summary_path, loaded)
            summaries.extend(loaded)
        bidirectional = review_engine.bidirectional_items(summaries)
        store.delete_review_items_by_reason("bidirectional_mismatch")
        for item in bidirectional:
            store.add_review_item(item)
    stage_seconds["review"] = round(time.perf_counter() - review_start, 3)

    if vlm_reviewer is not None and config.enable_vlm:
        vlm_start = time.perf_counter()
        unresolved = _run_vlm_pass(store, config, vlm_reviewer, out_dir, work_dir, vlm_stats)
        for image_hash in unresolved:
            store.set_status(image_hash, STATUS_VLM_REVIEW)
        stage_seconds["vlm"] = round(time.perf_counter() - vlm_start, 3)

    review_csv = ""
    if config.export:
        csv_path = out_dir / "review" / "待人工复核.csv"
        store.export_review_csv(csv_path)
        review_csv = str(csv_path)

    counts = store.counts()
    result = BatchResult(
        manifest_path=manifest_path,
        out_dir=out_dir,
        dry_run=False,
        started_at=started_at,
        elapsed=round(time.perf_counter() - wall_start, 3),
        scan=scan,
        counts=counts,
        review_counts=store.review_counts(),
        cache=cache_stats.snapshot(),
        ocr=ocr_stats.snapshot(),
        vlm=vlm_stats.snapshot(),
        poster_files=poster_files,
        errors=errors,
        stage_seconds=stage_seconds,
        review_csv=review_csv,
    )
    if config.export:
        result.report_files = write_outputs(store, result, out_dir)
    store.set_meta("last_run_at", started_at)
    return result
