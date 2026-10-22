#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Command-line adapter for the legacy single-directory extraction flow.

The layout/OCR/parse implementation lives in :mod:`buspipe`.  This module only
keeps the historical arguments, environment variables and printed progress
messages so existing runs behave the same.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any

from buspipe.config import settings_from_env
from buspipe.core import PosterExtractor
from buspipe.ocr import LocalRapidOcrBackend

logging.getLogger("RapidOCR").setLevel(logging.ERROR)

ROOT = Path(__file__).resolve().parent
SRC_DIR = Path(os.environ.get("BUS_SRC", r"E:/BaiduNetdiskDownload/底板/安淡"))
OUT_DIR = Path(os.environ.get("BUS_OUT", str(ROOT / "output")))
DEBUG_DIR = ROOT / "debug"


def _event_printer(event: str, payload: dict[str, Any]) -> None:
    if event == "block_start":
        print(
            f"  [{payload['poster']}] block {payload['block_index'] + 1}/{payload['block_count']} "
            f"y={payload['route_y0']}-{payload['route_y1']}",
            flush=True,
        )
    elif event == "block_cached":
        print(f"    route {payload.get('route', '')}: cached", flush=True)
    elif event == "block_done":
        print(
            f"    route {payload.get('route', '')}: stops={payload.get('stop_count', 0)} "
            f"cols={payload.get('column_count', 0)} row={payload.get('row', ['?', '?'])[0]}-"
            f"{payload.get('row', ['?', '?'])[1]}",
            flush=True,
        )
    elif event == "block_failed":
        print(f"    block {payload.get('block_index')} failed: {payload.get('error')}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--poster", help="poster stem, e.g. 安淡-北-01")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--block", type=int, action="append", help="block index to process (repeatable)")
    parser.add_argument("--out", default=str(OUT_DIR / "raw_extract.json"))
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    paths = sorted(SRC_DIR.glob("*.jpg"))
    if args.poster:
        paths = [p for p in paths if p.stem == args.poster]
    if not paths:
        raise SystemExit("no matching posters")

    settings = settings_from_env()
    threads = max(1, int(os.environ.get("OCR_THREADS", "4")))
    backend = LocalRapidOcrBackend(threads=threads)
    extractor = PosterExtractor(
        backend,
        settings,
        event_sink=_event_printer,
        debug_dir=DEBUG_DIR if args.debug else None,
    )

    posters = []
    for path in paths:
        print(f"processing {path.name} ...", flush=True)
        result = extractor.extract(
            path,
            station=settings.current_stop,
            only_blocks=set(args.block) if args.block else None,
        )
        posters.append(result)
        # Save after every full poster so a later failure cannot discard earlier work.
        if args.block is None:
            per_poster = OUT_DIR / f"{path.stem}_raw.json"
            per_poster.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            print(f"  wrote {per_poster}", flush=True)

    output = {"source_dir": str(SRC_DIR), "posters": posters}
    Path(args.out).write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"written {args.out}")


if __name__ == "__main__":
    main()
