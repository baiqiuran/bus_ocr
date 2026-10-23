"""Batch report and analyzer-compatible exports."""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from buspipe.cache import _replace_with_retry, atomic_write_text
from buspipe.config import poster_direction, poster_station
from buspipe.store import (
    STATUS_DONE,
    STATUS_VALIDATED,
    STATUS_VLM_REVIEW,
    ManifestStore,
    poster_json_path,
    utc_now,
)


def _cache_hit_rate(cache: dict[str, int]) -> float:
    hits = cache.get("block_hits", 0) + cache.get("poster_hits", 0)
    misses = cache.get("block_misses", 0) + cache.get("poster_misses", 0)
    total = hits + misses
    return round(hits / total, 4) if total else 0.0


def build_report(store: ManifestStore, result: Any) -> dict[str, Any]:
    counts = store.counts()
    review_counts = store.review_counts()
    total = counts.get("total", 0)
    done = counts.get(STATUS_DONE, 0)
    failed = counts.get("failed", 0)
    duplicates = counts.get("duplicate", 0)
    pending_review = sum(review_counts.get(key, 0) for key in ("open", "needs_human", "vlm_only"))
    failures = [
        {
            "image_hash": row["image_hash"],
            "path": row["path"],
            "station": row["station"],
            "direction": row["direction"],
            "error": row["error"],
            "attempts": row["attempts"],
        }
        for row in store.posters()
        if row["status"] == "failed"
    ]
    cache = result.cache
    return {
        "run_started_at": result.started_at,
        "generated_at": utc_now(),
        "source_dir": store.get_meta("source_dir", ""),
        "manifest_path": str(result.manifest_path),
        "out_dir": str(result.out_dir),
        "extract_mode": store.get_meta("extract_mode", "full"),
        "elapsed_seconds": result.elapsed,
        "stage_seconds": result.stage_seconds,
        "scan": result.scan,
        "counts": {
            "total": total,
            "done": done,
            "failed": failed,
            "incomplete": max(0, total - done - failed),
            "duplicates": duplicates,
            "by_status": {key: value for key, value in counts.items() if key not in {"total", "duplicate"}},
        },
        "review": {
            "total": sum(review_counts.values()),
            "pending": pending_review,
            "by_status": review_counts,
            "csv": result.review_csv,
        },
        "cache": {**cache, "hit_rate": _cache_hit_rate(cache)},
        "ocr": result.ocr,
        "vlm": result.vlm,
        "failures": failures,
        "posters_written_this_run": len(result.poster_files),
    }


def _markdown(report: dict[str, Any]) -> str:
    counts = report["counts"]
    review = report["review"]
    cache = report["cache"]
    ocr = report["ocr"]
    vlm = report["vlm"]
    lines = [
        "# 站牌批量处理报告",
        "",
        f"- 运行开始：{report['run_started_at']}",
        f"- 数据源：`{report['source_dir']}`",
        f"- manifest：`{report['manifest_path']}`",
        f"- 总耗时：{report['elapsed_seconds']:.1f} 秒",
        f"- 识别模式：{report['extract_mode']}",
        "",
        "## 总览",
        "",
        "| 指标 | 数量 |",
        "| --- | ---: |",
        f"| 唯一图片 | {counts['total']} |",
        f"| 完成 | {counts['done']} |",
        f"| 失败 | {counts['failed']} |",
        f"| 未完成 | {counts['incomplete']} |",
        f"| 重复图片（未处理） | {counts['duplicates']} |",
        f"| 复核项（待处理） | {review['pending']} |",
        f"| 复核项（总计） | {review['total']} |",
        "",
        "## 阶段耗时",
        "",
        "| 阶段 | 秒 |",
        "| --- | ---: |",
    ]
    for stage, seconds in report["stage_seconds"].items():
        lines.append(f"| {stage} | {seconds} |")
    lines += [
        "",
        "## 缓存与调用",
        "",
        "| 指标 | 数值 |",
        "| --- | ---: |",
        f"| 缓存命中率 | {cache['hit_rate']:.1%} |",
        f"| 块缓存命中/未命中 | {cache.get('block_hits', 0)} / {cache.get('block_misses', 0)} |",
        f"| 海报缓存命中/未命中 | {cache.get('poster_hits', 0)} / {cache.get('poster_misses', 0)} |",
        f"| OCR 处理图像 | {ocr.get('images', 0)} |",
        f"| 远程 OCR 请求 | {ocr.get('remote_calls', 0)} |",
        f"| 远程重试 | {ocr.get('remote_retries', 0)} |",
        f"| 本地回退 | {ocr.get('fallbacks', 0)} |",
        f"| VLM 调用 | {vlm.get('calls', 0)} |",
        f"| VLM 缓存命中 | {vlm.get('cache_hits', 0)} |",
        "",
    ]
    if report["failures"]:
        lines += ["## 失败项", "", "| 图片 | 站点 | 方向 | 错误 | 重试次数 |", "| --- | --- | --- | --- | ---: |"]
        for item in report["failures"]:
            error = str(item["error"]).replace("|", "\\|")
            lines.append(
                f"| `{Path(item['path']).name}` | {item['station']} | {item['direction']} | {error} | {item['attempts']} |"
            )
        lines.append("")
    if review["csv"]:
        lines += ["## 复核", "", f"复核队列：`{review['csv']}`", ""]
    return "\n".join(lines)


def _result_path(store: ManifestStore, out_dir: Path, poster: dict[str, Any]) -> Path:
    path = poster_json_path(out_dir, poster)
    if not path.exists() and store.get_meta("results_dir"):
        path = poster_json_path(store.get_meta("results_dir"), poster)
    return path


def write_station_routes(store: ManifestStore, out_dir: Path) -> dict[str, str]:
    """Flat station/direction -> ordered route-terminal mappings for either mode."""
    rows = []
    for poster in store.posters():
        if poster["status"] not in {STATUS_VALIDATED, STATUS_VLM_REVIEW, STATUS_DONE}:
            continue
        path = _result_path(store, out_dir, poster)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        unresolved = {
            (str(item["route"]), str(item["direction"]))
            for item in store.review_items(image_hash=poster["image_hash"])
            if item["status"] in {"open", "needs_human", "vlm_only"}
        }
        for entry in payload.get("entries", []):
            if entry.get("error"):
                continue
            stem = payload.get("poster", Path(poster["path"]).stem)
            station = poster_station(stem, poster["station"])
            direction = poster_direction(stem)
            route = str(entry.get("route", ""))
            if entry.get("extract_mode") == "endpoints":
                first, last = entry.get("first_stop", ""), entry.get("last_stop", "")
            else:
                stops = entry.get("stops", [])
                first = stops[0].get("display_name", "") if stops else ""
                last = stops[-1].get("display_name", "") if stops else ""
            rows.append({
                "station": station, "direction": direction, "station_key": f"{station}-{direction}",
                "route": route, "first_stop": first, "last_stop": last,
                "route_direction": f"{first}→{last}" if first and last else "",
                "needs_review": (route, direction) in unresolved or not all((route, first, last)) or direction == "未知",
                "poster": stem, "block_index": entry.get("block_index", 0),
            })
    fields = ["station", "direction", "station_key", "route", "first_stop", "last_stop", "route_direction", "needs_review", "poster", "block_index"]
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    csv_path, json_path = out_dir / "station_routes.csv", out_dir / "station_routes.json"
    atomic_write_text(csv_path, "\ufeff" + buffer.getvalue())
    atomic_write_text(json_path, json.dumps(rows, ensure_ascii=False, indent=2))
    return {"station_routes_csv": str(csv_path), "station_routes_json": str(json_path)}


def write_merged_raw(store: ManifestStore, result: Any, out_dir: Path) -> Path:
    """Stream all finished poster JSONs into the analyzer's all_raw.json shape."""
    path = out_dir / "all_raw.json"
    out_dir.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".all_raw.", suffix=".tmp", dir=str(out_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write('{"source_dir": ')
            handle.write(json.dumps(store.get_meta("source_dir", ""), ensure_ascii=False))
            handle.write(', "posters": [')
            first = True
            for poster in store.posters():
                if poster["status"] not in {STATUS_VALIDATED, STATUS_VLM_REVIEW, STATUS_DONE}:
                    continue
                file_path = _result_path(store, out_dir, poster)
                if not file_path.exists():
                    continue
                try:
                    payload = json.loads(file_path.read_text(encoding="utf-8"))
                except ValueError:
                    continue
                if not first:
                    handle.write(",\n")
                handle.write(json.dumps(payload, ensure_ascii=False, default=str))
                first = False
            handle.write("]}")
        _replace_with_retry(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return path


def write_outputs(store: ManifestStore, result: Any, out_dir: Path) -> dict[str, str]:
    merged = write_merged_raw(store, result, out_dir)
    report = build_report(store, result)
    json_path = out_dir / "batch_report.json"
    md_path = out_dir / "batch_report.md"
    atomic_write_text(json_path, json.dumps(report, ensure_ascii=False, indent=2, default=str))
    atomic_write_text(md_path, _markdown(report))
    files = {"all_raw": str(merged), "report_json": str(json_path), "report_md": str(md_path)}
    files.update(write_station_routes(store, out_dir))
    if result.review_csv:
        files["review_csv"] = str(result.review_csv)
    return files


def export_manifest(manifest_path: Path | str, out_dir: Path | str) -> dict[str, str]:
    """Export an existing manifest without touching OCR."""
    from types import SimpleNamespace

    manifest_path = Path(manifest_path)
    out_dir = Path(out_dir)
    store = ManifestStore(manifest_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    review_csv = out_dir / "review" / "待人工复核.csv"
    store.export_review_csv(review_csv)
    result = SimpleNamespace(
        started_at="",
        manifest_path=manifest_path,
        out_dir=out_dir,
        elapsed=0.0,
        stage_seconds={},
        scan={},
        cache={
            "block_hits": 0,
            "block_misses": 0,
            "poster_hits": 0,
            "poster_misses": 0,
            "invalid": 0,
            "writes": 0,
        },
        ocr={
            "local_calls": 0,
            "remote_calls": 0,
            "remote_retries": 0,
            "remote_failures": 0,
            "fallbacks": 0,
            "images": 0,
            "canvas_batches": 0,
            "canvas_fallbacks": 0,
            "column_calls": 0,
        },
        vlm={},
        poster_files={},
        review_csv=str(review_csv),
    )
    return write_outputs(store, result, out_dir)
