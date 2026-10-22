#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Merge block-level extraction parts into one all_raw.json."""

import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"
PARTS = OUT / "parts"
ORDER = ["安淡-北-01", "安淡-北-02", "安淡-南-01", "安淡-南-02"]
EXPECTED = {"安淡-北-01": 10, "安淡-北-02": 10, "安淡-南-01": 9, "安淡-南-02": 11}


def main() -> None:
    poster_meta = {}
    entries = defaultdict(dict)
    for path in sorted(PARTS.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for poster in payload.get("posters", []):
            name = poster["poster"]
            poster_meta.setdefault(name, {k: v for k, v in poster.items() if k != "entries"})
            for entry in poster.get("entries", []):
                index = entry.get("block_index")
                if index in entries[name]:
                    old = entries[name][index]
                    if len(entry.get("stops", [])) <= len(old.get("stops", [])):
                        continue
                entries[name][index] = entry

    merged_posters = []
    for name in ORDER:
        meta = poster_meta.get(name)
        if not meta:
            print(f"missing poster {name}")
            continue
        ordered_entries = [entries[name][i] for i in sorted(entries[name])]
        meta = dict(meta)
        meta["entries"] = ordered_entries
        merged_posters.append(meta)
        missing = [i for i in range(EXPECTED[name]) if i not in entries[name]]
        print(f"{name}: {len(ordered_entries)} entries" + (f", missing blocks {missing}" if missing else ""))

    target = OUT / "all_raw.json"
    target.write_text(json.dumps({"source_dir": r"E:/BaiduNetdiskDownload/底板/安淡",
                                  "posters": merged_posters}, ensure_ascii=False, indent=2, default=str),
                      encoding="utf-8")
    print(f"written {target}")


if __name__ == "__main__":
    main()
