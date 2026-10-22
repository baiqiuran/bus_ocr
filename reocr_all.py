#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Second-pass per-column OCR for every stored stop column.

Batched strip OCR is fast but sometimes joins a note or the next column into
one stop name.  This pass re-reads each column on its own and stores the
candidate next to the batch result so the two can be compared.

Usage:
  python reocr_all.py --posters 安淡-北-01,安淡-南-01 --out output/reocr_a.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path

import numpy as np
from PIL import Image
from rapidocr import RapidOCR

from extract import OUT_DIR, SRC_DIR, recognize_column

RAW = OUT_DIR / "all_raw.json"


def anomaly(text: str) -> bool:
    """Characters that should not appear inside a normal stop name."""
    if not text:
        return True
    if re.search(r"[0-9A-Za-z]", text):
        # Legitimate exit letters and numbered road entrances.
        if not re.search(r"(?:地铁站[A-F]口|[0-9]+号口)", text):
            return True
    if re.search(r"[。，、；：“”‘’？!！]", text):
        return True
    return False


def pick(tight: dict, wide: dict | None) -> dict:
    candidates = [tight] + ([wide] if wide else [])
    for cand in candidates:
        if cand["text"] and not anomaly(cand["text"]):
            return cand
    return max(candidates, key=lambda c: (bool(c["text"]), c["score"]))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--posters", default="", help="comma separated poster stems")
    parser.add_argument("--out", required=True)
    parser.add_argument("--raw", default=str(RAW))
    parser.add_argument("--wide-on-anomaly", action="store_true", default=True)
    args = parser.parse_args()

    raw = json.loads(Path(args.raw).read_text(encoding="utf-8"))
    wanted = {p.strip() for p in args.posters.split(",") if p.strip()}
    threads = max(1, int(os.environ.get("OCR_THREADS", "8")))
    engine = RapidOCR(params={
        "EngineConfig.onnxruntime.intra_op_num_threads": threads,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        "Global.log_level": "error",
    })

    results = []
    for poster in raw["posters"]:
        name = poster["poster"]
        if wanted and name not in wanted:
            continue
        print(f"processing {name} ...", flush=True)
        rgb = np.asarray(Image.open(SRC_DIR / f"{name}.jpg").convert("RGB"))
        started = time.time()
        done = 0
        for entry in poster["entries"]:
            if "error" in entry:
                continue
            cols = sorted(entry.get("columns", []), key=lambda c: c["x0"])
            row_y0, row_y1 = entry["row"]
            for stop in entry.get("stops", []):
                x0, x1 = int(stop.get("x0") or 0), int(stop.get("x1") or 0)
                if x1 <= x0:
                    continue
                prev_x1 = max([c["x1"] for c in cols if c["x1"] < x0] or [x0 - 60])
                next_x0 = min([c["x0"] for c in cols if c["x0"] > x1] or [x1 + 60])
                gap_l = max(1, x0 - prev_x1)
                gap_r = max(1, next_x0 - x1)
                col = {"x0": x0, "x1": x1, "y0": int(row_y0), "y1": int(row_y1),
                       "red_ratio": stop.get("red_ratio", 0), "score": stop.get("score", 0)}
                tight = recognize_column(
                    rgb, col, engine,
                    pad_left=min(10, max(1, (gap_l - 2) // 2)),
                    pad_right=min(10, max(1, (gap_r - 2) // 2)),
                )
                wide = None
                if args.wide_on_anomaly and (anomaly(tight["text"]) or tight["score"] < 0.85):
                    wide = recognize_column(
                        rgb, col, engine,
                        pad_left=min(22, max(3, (gap_l - 2) // 2)),
                        pad_right=min(22, max(3, (gap_r - 2) // 2)),
                    )
                    wide.update({"x0": x0, "x1": x1})
                chosen = pick(tight, wide)
                results.append({
                    "poster": name, "route": entry["route"], "direction": poster["direction"],
                    "seq": stop.get("seq"), "x0": x0, "x1": x1,
                    "batch": stop.get("name", ""), "batch_score": stop.get("score", 0),
                    "tight": tight["text"], "tight_score": tight["score"],
                    "wide": wide["text"] if wide else "", "wide_score": wide["score"] if wide else 0,
                    "chosen": chosen["text"], "chosen_score": chosen["score"],
                })
                done += 1
                if done % 20 == 0:
                    print(f"  {entry['route']}{poster['direction']} seq={stop.get('seq')} "
                          f"{done}/{sum(len(e.get('stops', [])) for e in poster['entries'])} "
                          f"{time.time() - started:.0f}s", flush=True)
        Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {args.out}: {len(results)} candidates {time.time() - started:.0f}s", flush=True)


if __name__ == "__main__":
    main()
