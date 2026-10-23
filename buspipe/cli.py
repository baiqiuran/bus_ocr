"""Command line for the batch pipeline.

    python -m buspipe run SOURCE --out OUT --dry-run
    python -m buspipe run SOURCE --out OUT
    python -m buspipe export MANIFEST --out OUT
    python -m buspipe override MANIFEST HASH ROUTE DIRECTION SEQ NAME --note ...
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from buspipe.batch import BatchConfig, run_batch
from buspipe.config import DEFAULT_ROUTE_HINTS, PipelineSettings
from buspipe.ocr import FallbackOcrBackend, LocalRapidOcrBackend, OcrStats
from buspipe.remote import RemoteOcrBackend, RemoteOcrConfig
from buspipe.report import export_manifest
from buspipe.store import STATUS_DONE, STATUS_FAILED, STATUS_VALIDATED, STATUS_VLM_REVIEW, ManifestStore
from buspipe.vlm import HttpVlmReviewer, VlmConfig


def _run(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    manifest = Path(args.manifest) if args.manifest else None
    stats = OcrStats()
    settings = PipelineSettings(current_stop=args.station, route_hints=DEFAULT_ROUTE_HINTS, extract_mode=args.extract_mode)
    backend = None
    reviewer = None
    if not args.dry_run:
        if args.remote_ocr:
            remote = RemoteOcrBackend(
                RemoteOcrConfig(
                    url=args.remote_ocr,
                    model=args.remote_model,
                    timeout=args.remote_timeout,
                    max_retries=args.remote_retries,
                    backoff=args.remote_backoff,
                    batch_size=args.remote_batch_size,
                    disable_proxy=args.disable_proxy,
                ),
                stats=stats,
            )
            if args.no_local_fallback:
                backend = remote
            else:
                backend = FallbackOcrBackend(
                    remote,
                    LocalRapidOcrBackend(
                        threads=args.threads,
                        model_version=settings.model_version,
                        stats=stats,
                    ),
                    stats=stats,
                )
        else:
            backend = LocalRapidOcrBackend(
                threads=args.threads, model_version=settings.model_version, stats=stats
            )

        if args.vlm_url and not args.no_vlm:
            reviewer = HttpVlmReviewer(
                VlmConfig(
                    url=args.vlm_url,
                    model=args.vlm_model,
                    timeout=args.vlm_timeout,
                    max_retries=args.vlm_max_retries,
                    backoff=args.vlm_backoff,
                    disable_proxy=args.disable_proxy,
                )
            )

    config = BatchConfig(
        out_dir=out_dir,
        work_dir=Path(args.work_dir) if args.work_dir else None,
        manifest_path=manifest,
        current_stop=args.station,
        route_hints=DEFAULT_ROUTE_HINTS,
        pipeline=settings,
        workers=args.workers,
        threads=args.threads,
        dry_run=args.dry_run,
        retry_failed=not args.no_retry_failed,
        max_attempts=args.max_attempts,
        use_cache=not args.no_cache,
        enable_review=not args.no_review,
        enable_vlm=reviewer is not None,
        vlm_max_calls_per_poster=args.vlm_max_calls,
        vlm_min_confidence=args.vlm_min_confidence,
        export=not args.no_export,
        limit=args.limit,
        only_hashes=args.only_hash or None,
        only_stations=args.only_station or None,
        ocr_mode=args.ocr_mode,
        canvas_max_columns=args.max_columns,
        canvas_max_width=args.max_width,
    )
    result = run_batch(args.source, config, ocr_backend=backend, vlm_reviewer=reviewer)
    summary = {
        "dry_run": result.dry_run,
        "extract_mode": settings.extract_mode,
        "manifest": str(result.manifest_path),
        "counts": result.counts,
        "review": result.review_counts,
        "elapsed_seconds": result.elapsed,
        "cache": result.cache,
        "ocr": result.ocr,
        "vlm": result.vlm,
        "errors": result.errors,
        "files": result.report_files,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _export(args: argparse.Namespace) -> int:
    files = export_manifest(args.manifest, args.out)
    print(json.dumps(files, ensure_ascii=False, indent=2))
    return 0


def _override(args: argparse.Namespace) -> int:
    store = ManifestStore(args.manifest)
    poster = store.poster(args.hash)
    if poster is None:
        print(f"poster not found: {args.hash}", file=sys.stderr)
        return 2
    store.set_override(args.hash, args.route, args.direction, args.seq, args.name, args.note)
    if poster["status"] in {STATUS_DONE, STATUS_VALIDATED, STATUS_VLM_REVIEW}:
        # Keep the extracted JSON, but rerun validation/review so the override applies.
        store.set_status(args.hash, STATUS_VALIDATED)
    print(
        f"override saved: {args.hash[:8]} {args.route} {args.direction} #{args.seq} -> {args.name}"
    )
    return 0


def _list(args: argparse.Namespace) -> int:
    store = ManifestStore(args.manifest)
    for row in store.posters():
        if args.status and row["status"] != args.status:
            continue
        print(
            f"{row['image_hash'][:8]}  {row['status']:<12} {row['attempts']:>2}  "
            f"{row['station']} {row['direction']}  {Path(row['path']).name}  {row['error']}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="buspipe", description="公交站牌批量 OCR / VLM 复核流水线")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="扫描并批量处理一个目录或 manifest")
    run.add_argument("source", help="JPG 目录或已有 manifest.sqlite")
    run.add_argument("--out", required=True, help="输出目录")
    run.add_argument("--work-dir", default="", help="缓存/manifest 目录，默认 OUT/.batch")
    run.add_argument("--manifest", default="", help="显式指定 manifest 路径")
    run.add_argument("--station", default="", help="文件名无法给出站点时的兜底站名")
    run.add_argument("--workers", type=int, default=1, help="海报级并发，本地 OCR 建议 1")
    run.add_argument("--threads", type=int, default=4, help="RapidOCR 线程数")
    run.add_argument("--ocr-mode", choices=["batch", "columns"], default="batch")
    run.add_argument("--extract-mode", choices=["full", "endpoints"], default="full",
                     help="endpoints 只提取文件名站点/方位、线路号、首末站；full 提取完整站序")
    run.add_argument("--max-columns", type=int, default=None)
    run.add_argument("--max-width", type=int, default=None)
    run.add_argument("--limit", type=int, default=None, help="本次最多处理多少张")
    run.add_argument("--only-hash", action="append", default=[], help="只处理指定图片哈希（可重复）")
    run.add_argument("--only-station", action="append", default=[], help="只处理指定站点（可重复）")
    run.add_argument("--max-attempts", type=int, default=3)
    run.add_argument("--no-retry-failed", action="store_true")
    run.add_argument("--no-cache", action="store_true")
    run.add_argument("--no-review", action="store_true")
    run.add_argument("--no-export", action="store_true")
    run.add_argument("--dry-run", action="store_true", help="只扫描并生成 manifest")

    run.add_argument("--remote-ocr", default="", help="远程 OCR 服务 URL；密钥用 BUS_OCR_API_KEY")
    run.add_argument("--remote-model", default="rapidocr-remote")
    run.add_argument("--remote-timeout", type=float, default=30.0)
    run.add_argument("--remote-retries", type=int, default=2)
    run.add_argument("--remote-backoff", type=float, default=0.5)
    run.add_argument("--remote-batch-size", type=int, default=6)
    run.add_argument("--no-local-fallback", action="store_true")

    run.add_argument("--vlm-url", default="", help="VLM 服务 URL；密钥用 BUS_VLM_API_KEY")
    run.add_argument("--vlm-model", default="vlm-review")
    run.add_argument("--vlm-timeout", type=float, default=60.0)
    run.add_argument("--vlm-max-retries", type=int, default=1)
    run.add_argument("--vlm-backoff", type=float, default=1.0)
    run.add_argument("--vlm-max-calls", type=int, default=8, help="每张海报最多 VLM 调用数")
    run.add_argument("--vlm-min-confidence", type=float, default=0.6)
    run.add_argument("--no-vlm", action="store_true")
    run.add_argument("--disable-proxy", action="store_true", help="直连远程服务")
    run.set_defaults(func=_run)

    exp = sub.add_parser("export", help="从已有 manifest 重新导出 all_raw.json / 报告 / 复核 CSV")
    exp.add_argument("manifest")
    exp.add_argument("--out", required=True)
    exp.set_defaults(func=_export)

    override = sub.add_parser("override", help="写入人工修正，下一次 run 时应用")
    override.add_argument("manifest")
    override.add_argument("hash")
    override.add_argument("route")
    override.add_argument("direction")
    override.add_argument("seq", type=int)
    override.add_argument("name")
    override.add_argument("--note", default="")
    override.set_defaults(func=_override)

    listing = sub.add_parser("list", help="查看 manifest 状态")
    listing.add_argument("manifest")
    listing.add_argument("--status", default="")
    listing.set_defaults(func=_list)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
