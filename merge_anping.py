#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Merge the four per-poster extraction files into all_raw.json."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output" / "anping"
SOURCE = Path(r"E:/BaiduNetdiskDownload/底板/安平小区")


def main() -> None:
    posters = []
    for name in ("east01.json", "west01.json", "west02.json"):
        payload = json.loads((OUT / name).read_text(encoding="utf-8"))
        posters.extend(payload.get("posters", []))
    if not posters:
        raise SystemExit("no poster JSON files found")
    merged = {"source_dir": str(SOURCE), "posters": posters}
    target = OUT / "all_raw.json"
    target.write_text(json.dumps(merged, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"merged {len(posters)} posters -> {target}")
    for poster in posters:
        entries = poster.get("entries", [])
        print(f"  {poster.get('poster')}: {len(entries)} entries")


if __name__ == "__main__":
    main()
