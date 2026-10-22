#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Re-OCR one stop column from the merged extraction for manual verification.

Batched OCR occasionally joins two adjacent vertical names.  This tool crops a
single stored column and runs the slower, single-column recogniser on it, and
also writes the crop to output/recheck/ so the glyphs can be inspected visually.

Examples:
  python recheck.py --route 39 --direction 北 --name 双浦头
  python recheck.py --poster 安淡-南-01 --route 19 --seq 12 --save
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import numpy as np
from PIL import Image
from rapidocr import RapidOCR

from extract import OUT_DIR, SRC_DIR, recognize_column

RAW = OUT_DIR / "all_raw.json"
CROP_DIR = OUT_DIR / "recheck"


def compact(text: str) -> str:
    return "".join(str(text or "").split())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", default=str(RAW))
    parser.add_argument("--poster", default="")
    parser.add_argument("--route", required=True)
    parser.add_argument("--direction", required=True, choices=["北", "南", "东", "西"])
    parser.add_argument("--seq", type=int, default=0)
    parser.add_argument("--name", default="")
    parser.add_argument("--save", action="store_true", help="write the crop for visual inspection")
    args = parser.parse_args()

    raw = json.loads(Path(args.raw).read_text(encoding="utf-8"))
    entries = []
    for poster in raw["posters"]:
        if args.poster and poster["poster"] != args.poster:
            continue
        if poster["direction"] != args.direction:
            continue
        for entry in poster.get("entries", []):
            if str(entry.get("route")) == str(args.route):
                entries.append((poster["poster"], entry))
    if not entries:
        raise SystemExit("route/direction not found")
    poster_name, entry = entries[0]

    targets = []
    for stop in entry.get("stops", []):
        if args.seq and int(stop.get("seq", -1)) == args.seq:
            targets.append(stop)
        elif args.name and compact(stop.get("name")) == compact(args.name):
            targets.append(stop)
    if not targets:
        print("stops:", " → ".join(f"{s['seq']}:{s['display_name']}" for s in entry["stops"]))
        raise SystemExit("stop not found; pick --seq from the list above")

    path = SRC_DIR / f"{poster_name}.jpg"
    rgb = np.asarray(Image.open(path).convert("RGB"))
    threads = max(1, int(os.environ.get("OCR_THREADS", "8")))
    engine = RapidOCR(params={
        "EngineConfig.onnxruntime.intra_op_num_threads": threads,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        "Global.log_level": "error",
    })

    row_y0, row_y1 = entry["row"]
    all_columns = sorted(entry.get("columns", []), key=lambda c: c["x0"])
    for stop in targets:
        x0, x1 = int(stop["x0"]), int(stop["x1"])
        print(f"\n[{poster_name} 路{args.route}{args.direction}] seq={stop['seq']} "
              f"batch={stop['display_name']!r} score={stop.get('score')} x={x0}-{x1}")
        prev_x1 = max([c["x1"] for c in all_columns if c["x1"] < x0] or [x0 - 60])
        next_x0 = min([c["x0"] for c in all_columns if c["x0"] > x1] or [x1 + 60])
        gap_left = max(1, x0 - prev_x1)
        gap_right = max(1, next_x0 - x1)
        for label, pad_l, pad_r in (
            ("tight", min(10, max(1, (gap_left - 2) // 2)), min(10, max(1, (gap_right - 2) // 2))),
            ("wide", min(22, max(3, (gap_left - 2) // 2)), min(22, max(3, (gap_right - 2) // 2))),
        ):
            col = {"x0": x0, "x1": x1, "y0": int(row_y0), "y1": int(row_y1),
                   "red_ratio": stop.get("red_ratio", 0), "score": stop.get("score", 0)}
            out = recognize_column(rgb, col, engine, pad_left=pad_l, pad_right=pad_r)
            print(f"  {label}: {out['text']!r} score={out['score']:.3f}")
        if args.save:
            CROP_DIR.mkdir(parents=True, exist_ok=True)
            pad = 6
            crop = rgb[max(0, row_y0 - 10):min(rgb.shape[0], row_y1 + 10),
                       max(0, x0 - pad):min(rgb.shape[1], x1 + pad)]
            out_path = CROP_DIR / f"{poster_name}_{args.route}{args.direction}_{stop['seq']:02d}.png"
            Image.fromarray(crop).save(out_path)
            print(f"  crop: {out_path}")


if __name__ == "__main__":
    main()
