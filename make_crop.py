#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Render a readable crop of one route row for visual verification."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw

from extract import OUT_DIR, SRC_DIR

RAW = OUT_DIR / "all_raw.json"
CROP_DIR = OUT_DIR / "crops"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", default=str(RAW))
    parser.add_argument("--route", required=True)
    parser.add_argument("--direction", required=True, choices=["北", "南"])
    parser.add_argument("--chunks", type=int, default=4)
    parser.add_argument("--scale", type=float, default=2.0)
    parser.add_argument("--x0", type=int, default=280)
    parser.add_argument("--x1", type=int, default=4090)
    parser.add_argument("--label", action="store_true", help="draw stop sequence numbers")
    args = parser.parse_args()

    raw = json.loads(Path(args.raw).read_text(encoding="utf-8"))
    found = None
    for poster in raw["posters"]:
        if poster["direction"] != args.direction:
            continue
        for entry in poster.get("entries", []):
            if str(entry.get("route")) == str(args.route):
                found = (poster, entry)
                break
    if not found:
        raise SystemExit("route/direction not found")
    poster, entry = found
    img = Image.open(SRC_DIR / f"{poster['poster']}.jpg").convert("RGB")
    row_y0, row_y1 = entry["row"]
    top = max(0, row_y0 - (60 if args.label else 20))
    bottom = min(img.height, row_y1 + 40)
    strip = img.crop((args.x0, top, args.x1, bottom))
    draw = ImageDraw.Draw(strip)
    if args.label:
        for stop in entry.get("stops", []):
            cx = int((stop["x0"] + stop["x1"]) / 2) - args.x0
            draw.text((cx - 8, 4), str(stop["seq"]), fill=(200, 0, 0))
    if args.scale != 1:
        strip = strip.resize((int(strip.width * args.scale), int(strip.height * args.scale)),
                             Image.LANCZOS)
    CROP_DIR.mkdir(parents=True, exist_ok=True)
    width = strip.width
    step = (width + args.chunks - 1) // args.chunks
    paths = []
    for i in range(args.chunks):
        part = strip.crop((i * step, 0, min(width, (i + 1) * step), strip.height))
        path = CROP_DIR / f"{poster['poster']}_{args.route}{args.direction}_{i + 1}.png"
        part.save(path)
        paths.append(path)
    print(f"{poster['poster']} route {args.route}{args.direction} stops={entry['stop_count']} "
          f"row={row_y0}-{row_y1}")
    print(" → ".join(f"{s['seq']}:{s['display_name']}" for s in entry.get("stops", [])))
    for path in paths:
        print(path)


if __name__ == "__main__":
    main()
