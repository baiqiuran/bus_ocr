#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Quick QA report for the merged extraction."""

import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "output" / "all_raw.json"


def main() -> None:
    data = json.loads(RAW.read_text(encoding="utf-8"))
    entries = []
    for poster in data["posters"]:
        entries.extend(poster.get("entries", []))
    print(f"posters={len(data['posters'])} entries={len(entries)}")
    issues = []
    for entry in sorted(entries, key=lambda e: (str(e.get("direction")), int(e.get("route") or 999),
                                                str(e.get("poster")))):
        names = [s["name"] for s in entry.get("stops", [])]
        if "安淡" not in names:
            issues.append((entry.get("poster"), entry.get("direction"), entry.get("route"), "缺少安淡"))
        if entry.get("next_stop") and entry.get("expected_next") and entry["next_stop"] != entry["expected_next"]:
            issues.append((entry.get("poster"), entry.get("direction"), entry.get("route"),
                           f"下站不一致: {entry['next_stop']} vs {entry['expected_next']}"))
        for stop in entry.get("stops", []):
            if re.search(r"[0-9A-Za-z]", stop["name"]):
                issues.append((entry.get("poster"), entry.get("direction"), entry.get("route"),
                               f"站名含数字/字母: {stop['display_name']}"))
            if "；" in (stop.get("note") or ""):
                issues.append((entry.get("poster"), entry.get("direction"), entry.get("route"),
                               f"备注含分号: {stop['display_name']}"))
        for a, b in zip(names, names[1:]):
            if a == b:
                issues.append((entry.get("poster"), entry.get("direction"), entry.get("route"),
                               f"连续重复站: {a}"))
        print(f"{entry.get('poster')} {entry.get('direction')} {entry.get('route')} "
              f"stops={len(names)} next={entry.get('next_stop')} dest={entry.get('destination')}")
        print("  " + " → ".join(s["display_name"] for s in entry.get("stops", [])))
    print("\nISSUES", len(issues))
    for row in issues:
        print(row)


if __name__ == "__main__":
    main()
