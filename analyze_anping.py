#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate, correct and export the extracted Andan bus data."""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "output" / "anping"
RAW_PATH = OUT_DIR / "all_raw.json"
STATION = "安平小区"
D1, D2 = "东", "西"

MANUAL_CORRECTIONS = {
    # OCR character confusions / dropped characters
    "南三环": "南二环",
    "用二环": "南二环",
    "文山日里": "文山里",
    "山里": "文山里",
    "磨屠洋河": "磨洋河",
    "磨石洋河": "磨洋河",
    "干达休所": "干休所",
    "干体休所": "干休所",
    "干体休FF所": "干休所",
    "达休F所": "干休所",
    "林成地板": "林茂地板厂",
    "校区V2": "福建理工大学鼓山校区",
    "理工大学鼓山校区": "福建理工大学鼓山校区",
    "双浦": "双浦头",
    "双浦油头": "双浦头",
    "双浦用头": "双浦头",
    "双浦m头": "双浦头",
    "十1四桥": "十四桥",
    "十1四上桥": "十四桥",
    "十1四口桥": "十四桥",
    "十四一桥": "十四桥",
    "十1锦祠": "十锦祠",
    "打.铁挡": "打铁垱",
    "打一生铁饮挡": "打铁垱",
    "打.铁业垱": "打铁垱",
    "西禅件寺": "西禅寺",
    "西四禅+寺": "西禅寺",
    "西禅7寺": "西禅寺",
    "西禅。寺": "西禅寺",
    "凤风凰国池": "凤凰池",
    "凤风凰油池": "凤凰池",
    "风凰池": "凤凰池",
    "福大北北门": "福大北门",
    "福大2F门": "福大北门",
    "大北门": "福大北门",
    "同峰杆": "高峰桥",
    "东园山": "茶园山",
    "家古营": "蒙古营",
    "古营": "蒙古营",
    "文化化宫": "文化宫",
    "东街“口": "东街口",
    "高同佳1苑": "高佳苑",
    "屏山7": "屏山",
    "脉温泉": "源脉温泉",
    "福四一中": "福四中",
    "福四四中": "福四中",
    "化H河": "化工河",
    "化1工河": "化工河",
    "工河": "化工河",
    "福建海事局局": "福建海事局",
    "大人名1城": "大名城",
    "象园四头": "象园头",
    "象园2头": "象园头",
    "象。园头": "象园头",
    "元明桥": "光明桥",
    "光明": "光明桥",
    "三1十中": "三十中",
    "一+": "三十中",
    "南用井苻村": "南井村",
    "用井材": "南井村",
    "曷屿村": "葛屿村",
    "屿村": "葛屿村",
    "横堤": "横堤坝",
    "南前前村": "南前村",
    "元峰路": "元峰路口",
    "物构": "物构所",
    "南禅": "南禅山",
    "交通路": "交通路东",
    "太池路西": "斗池路西",
    "家园头": "象园头",
    "山村": "秀山村",
    "满洋": "满洋路",
    "华园区": "佳华园区",
    "齐1安路": "齐安路",
    "国艺花乌市场": "国艺花鸟市场",
    "讲堂王前": "讲堂前",
    "滨洲路F北": "滨洲路北",
    "鼓四四村": "鼓四村",
    "鼓四1村": "鼓四村",
    "洋2头口": "洋头口",
    "洋。头口": "洋头口",
    "洋头": "洋头口",
    "南公": "南公园",
    "金挡": "金垱",
    "福道L号口": "福道6号口",
    "1角池": "三角池",
    "建平。村": "建平村",
    "同佳苑": "高佳苑",
    "东街": "东街口",
    "状元": "状元境",
    "桔园路": "桔园一路",
    "凤区网路": "凤冈路",
    "凤区-路": "凤冈路",
    "横江江渡": "横江渡",
    "中下": "中下村",
    "五孔": "五孔闸",
    "鱼兴路": "鳌兴路",
    "鳌w兴路": "鳌兴路",
    "鼓E中学环": "鼓山中学（三环）",
    "鼓山出苑": "鼓山苑",
    "山": "鼓山苑",
    "园": "象园头",
    "工海新村": "上海新村",
    "上海新村1": "上海新村",
    "太池": "斗池",
    "盘河": "铜盘河",
    "三一角井": "三角井",
    "宗酒岭": "祭酒岭",
    "祭酒酒岭": "祭酒岭",
    "洪山妖": "洪山桥",
    "得1贵购路": "得贵路",
    "早仙T桥": "古仙桥",
    "子军路": "学军路",
    "四桥": "十四桥",
    "南用公公园": "南公园",
    "南八公二园": "南公园",
    "南公一园": "南公园",
    "H一渡": "上渡",
    "华侨中木学": "华侨中学",
    "肺秤医院": "肺科医院",
    "羌城市广场": "宝龙城市广场",
    "龙城市广场": "宝龙城市广场",
    "龙·城市广场": "宝龙城市广场",
    "龙1城市厂场": "宝龙城市广场",
    "羌城帝人场": "宝龙城市广场",
    "羌一城带厂场": "宝龙城市广场",
    "宝龙·城市广场": "宝龙城市广场",
    "宝龙城市广场": "宝龙城市广场",
    "福建海事局局": "福建海事局",
    "省而血液中心": "省血液中心",
    "省血液中心7": "省血液中心",
    "仓山万达广场1": "仓山万达广场",
    "仓山万1达广场": "仓山万达广场",
    "仓山出方达广场": "仓山万达广场",
    "浦上大桥西1": "浦上大桥西",
    "两园安置房一1区": "两园安置房一区",
    "江二龙好大桥": "江口龙好大桥",
    "无龙洲四西套会站": "尤溪洲（西）公交站",
    "无洲九西公站安": "尤溪洲（西）公交站",
    "尤溪洲(西)公交站": "尤溪洲（西）公交站",
    "尤溪洲（西）公交站": "尤溪洲（西）公交站",
    "乐西东": "乐西（东）",
    "乐西1东": "乐西（东）",
    "乐西(东)": "乐西（东）",
    "古田六一路口": "古田六一路口",
    "臺六一，路": "古田六一路口",
    "台六一，路": "古田六一路口",
    "富六一路": "古田六一路口",
    "古六一1路": "古田六一路口",
    "古六一，路": "古田六一路口",
    "富六一路口": "古田六一路口",
    "福建理工大学鼓山校区K": "福建理工大学鼓山校区",
    "尤九溪洲桥实": "尤溪洲桥头",
    "窗红江路口": "杨周红江路口",
    "金港路1口": "金港路口",
    "台江广1场": "台江广场",
    "江日口一公交总早": "江口公交总站",
    "打铁挡": "打铁垱",
    # 安平小区-specific OCR artefacts
    "长r寿,社": "长寿社",
    "青年人场": "青年广场",
    "三。十“中": "三十中",
    "安平小一区": "安平小区",
    "世欧人场": "世欧广场",
    "洋头1口": "洋头口",
    "打2铁当": "打铁垱",
    "打铁当": "打铁垱",
    "双浦n头": "双浦头",
    "X四桥": "十四桥",
    "红L峰": "红峰",
    "物初构F所": "物构所",
    "化工1河": "化工河",
    "听堂前": "讲堂前",
    "象1园头": "象园头",
    "打铁以当": "打铁垱",
    "十“四一桥": "十四桥",
    "高峰44桥": "高峰桥",
    "福1中": "福二中",
    "白1马河": "白马河",
    "洪梁厝路": "洪山梁厝路",
    "新东路": "福新东路",
    "人名城": "大名城",
    "江路": "瀛江路",
    "洲红星": "瀛洲红星",
    "平小区": "安平小区",
    "平小一区": "安平小区",
    "省产电中心": "省广电中心",
    "文山山里": "文山里",
    "三日叉街": "三叉街",
    "早Ⅲ一座": "古三座",
    "古1一座": "古三座",
    "仓山1镇": "仓山镇",
    "北园1西": "北园西",
    "则徐人场": "则徐广场",
    "白一湖亭": "白湖亭",
    "西四禅仕寺": "西禅寺",
    "福祥社": "福祥社区",
    "南公河口7": "南公河口",
    "闽合江江步之行心街": "闽江之心",
    "仓福高前": "仓前",
    "龙爱潭情岛角": "龙潭角",
    "光九龙寺桥": "光桥",
}

# Rare split/merge artefacts that cannot be fixed by a name rename alone.
# (route, direction, anchor stop) -> stops to insert after the anchor.
MANUAL_INSERT_AFTER = {
    ("93", "北", "长乐北路口"): ["乐西（东）"],
    ("93", "南", "世欧王庄"): ["乐西（东）"],
}

# Header fields that OCR could not recover from the crop at all.
MANUAL_NEXT_STOP = {
    ("6", "南"): "打铁垱",
}

# 179's two terminal timetables sit left of the regular time crop, so the OCR
# missed the 7:00 / 13:00 / 14:00 departures.  Both directions share the table.
MANUAL_TIMES = {
    ("179", "北"): ["07:00", "07:35", "08:10", "09:00", "09:30", "10:15", "11:05",
                    "12:00", "13:00", "14:00", "14:45", "15:30", "16:15", "17:00",
                    "17:45", "18:30"],
    ("179", "南"): ["07:00", "07:35", "08:10", "09:00", "09:30", "10:15", "11:05",
                    "12:00", "13:00", "14:00", "14:45", "15:30", "16:15", "17:00",
                    "17:45", "18:30"],
}

# After splitting a merged stop out of a note, the anchor's note sometimes still
# contains a truncated copy of it.  Force the intended note here.
MANUAL_ANCHOR_NOTE = {
    ("93", "北", "长乐北路口"): "美可普口腔医院",
    ("93", "南", "世欧王庄"): "",
}

# Names that cannot be fixed by a global rename because the same short string
# means different stops on different routes.
MANUAL_SEQ_FIXES = {
    ("77", "北", 25): "福二中",
    ("109", "北", 23): "福二中",
    ("77", "南", 17): "福二中",
    ("109", "南", 14): "福二中",
    ("6", "南", 14): "南二环",
}

# 安平小区 uses 东/西 directions and different route blocks; the 安淡-specific
# sequence overrides above must not leak into it.
MANUAL_INSERT_AFTER = {}
MANUAL_NEXT_STOP = {}
MANUAL_TIMES = {}
MANUAL_ANCHOR_NOTE = {}
MANUAL_SEQ_FIXES = {
    ("6", "东", 11): "南二环",
    ("309", "西", 4): "河下路",
}

# The stop name and its small parenthetical annotation were merged into one
# OCR column and need to be split back apart: (route, direction, seq) ->
# (name, note).
MANUAL_SPLIT_NOTE = {
    ("50", "西", 18): ("闽江之心", "台江步行街"),
    ("50", "西", 21): ("仓前", "福高"),
    ("50", "西", 22): ("龙潭角", "爱情岛"),
    ("50", "西", 33): ("光桥", "九龙寺"),
}

# Two adjacent OCR columns are really one stop plus its annotation:
# (route, direction, seq of the first column) -> (name, note of the next column)
MANUAL_MERGE_NEXT = {
    ("320", "西", 26): ("华侨中学", "红旗村"),
}

# Small parenthetical annotations misread inside an otherwise correct stop.
NOTE_FIXES = {
    "一桥北": "桥北",
    "一轮工": "轮工",
    "一轮工二": "轮工",
    "宁化路(东": "宁化路东",
    "一附一医院院": "附一医院",
    "1品市1场": "林产品市场",
    "一江旗寸一": "红旗村",
    "一幅弯斗门": "福湾斗门",
    "1": "西门",
    "一西门": "西门",
    "长乐路只": "长乐路口",
    "鼓岭古道（东；鼓岭古道（西": "鼓岭古道东、鼓岭古道西",
    "囤风园C": "闽风园",
    "1闽水园": "闽水园",
    "一闽水园": "闽水园",
    "一闽风园C": "闽风园",
    "C长乐路口": "长乐路口",
    "7长乐路口1": "长乐路口",
    "1阵坂": "阵坂",
}


def compact(text: str) -> str:
    text = str(text or "")
    text = re.sub(r"\s+", "", text)
    text = text.replace("（", "(").replace("）", ")")
    return text


def strip_note(text: str) -> str:
    text = compact(text)
    text = re.split(r"[()]", text, maxsplit=1)[0]
    return text.strip("·.,，、;；:：")


def clean_ocr_name(text: str) -> str:
    text = compact(text)
    # Drop trailing OCR fragments such as "广场1", "校区K", "路口2F".
    text = re.sub(r"(?<=[\u4e00-\u9fff])[0-9A-Za-z]+\s*$", "", text)
    text = re.sub(r"[·•]+$", "", text)
    text = text.strip("()（）·.,，、;；:：")
    return text


def normalize_for_compare(text: str) -> str:
    text = strip_note(text)
    text = text.replace("臺", "台")
    text = re.sub(r"[0-9A-Za-z]", "", text)
    return text


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def similarity(a: str, b: str) -> float:
    a, b = normalize_for_compare(a), normalize_for_compare(b)
    if not a and not b:
        return 1.0
    return 1.0 - levenshtein(a, b) / max(len(a), len(b))


def align_sequences(north: list[str], south_reversed: list[str]) -> list[tuple[int | None, int | None]]:
    """Needleman-Wunsch alignment allowing similar OCR variants to substitute."""
    n, m = len(north), len(south_reversed)
    gap = 0.65
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    back = [[None] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0] = dp[i - 1][0] + gap
        back[i][0] = "up"
    for j in range(1, m + 1):
        dp[0][j] = dp[0][j - 1] + gap
        back[0][j] = "left"
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            sim = similarity(north[i - 1], south_reversed[j - 1])
            sub_cost = 1.0 - sim if sim >= 0.58 else 2.0
            choices = [
                (dp[i - 1][j - 1] + sub_cost, "diag"),
                (dp[i - 1][j] + gap, "up"),
                (dp[i][j - 1] + gap, "left"),
            ]
            cost, direction = min(choices)
            dp[i][j] = cost
            back[i][j] = direction
    pairs: list[tuple[int | None, int | None]] = []
    i, j = n, m
    while i > 0 or j > 0:
        direction = back[i][j]
        if direction == "diag":
            pairs.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif direction == "up":
            pairs.append((i - 1, None))
            i -= 1
        else:
            pairs.append((None, j - 1))
            j -= 1
    pairs.reverse()
    return pairs


def choose_variant(a: str, b: str, freq: Counter) -> tuple[str, bool]:
    a, b = clean_ocr_name(a), clean_ocr_name(b)
    if not a:
        return b, True
    if not b:
        return a, True
    if a == b:
        return a, False
    # A duplicated final character is a common detector artefact.
    if len(a) == len(b) + 1 and a.startswith(b) and a[-1] == b[-1]:
        return b, True
    if len(b) == len(a) + 1 and b.startswith(a) and b[-1] == a[-1]:
        return a, True
    fa, fb = freq.get(a, 0), freq.get(b, 0)
    if fa != fb:
        return (a, True) if fa > fb else (b, True)
    return (a if len(a) >= len(b) else b), True


_MANUAL_NORMALIZED = {clean_ocr_name(key): value for key, value in MANUAL_CORRECTIONS.items()}


def apply_manual(name: str) -> str:
    name = clean_ocr_name(name)
    return _MANUAL_NORMALIZED.get(name, name)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw", default=str(RAW_PATH))
    parser.add_argument("--xlsx", default="", help="alternate workbook path when the main file is open in Excel")
    parser.add_argument("--out-dir", default="", help="alternate directory for CSV/Excel/report output")
    args = parser.parse_args()
    global OUT_DIR
    if args.out_dir:
        OUT_DIR = Path(args.out_dir)
    raw = json.loads(Path(args.raw).read_text(encoding="utf-8"))
    posters = raw["posters"]
    entries: list[dict[str, Any]] = []
    for poster in posters:
        for entry in poster.get("entries", []):
            if "error" in entry:
                continue
            entry = dict(entry)
            entry["poster"] = poster["poster"]
            entry["direction"] = poster["direction"]
            header_text = entry.get("header_text", "")
            operator = entry.get("operator", "")
            if operator == "福州公交":
                operator = "福州公交集团"
            if not operator and ("福建港" in header_text or "FJP" in header_text):
                operator = "福建港口"
            entry["operator"] = operator
            destination = compact(entry.get("destination", "")).replace("廈", "厦")
            destination = re.sub(r"[£¥]?\s*[0-9A-Za-z]+\s*$", "", destination)
            destination = destination.removesuffix("占").strip("·.,，、;；:：")
            # Keep a trailing parenthesis (e.g. 金融街万达广场（曙光路）); the
            # generic cleaner strips it, so only use it for known corrections.
            entry["destination"] = _MANUAL_NORMALIZED.get(clean_ocr_name(destination), destination)
            raw_next = compact(entry.get("next_stop", ""))
            next_stop = _MANUAL_NORMALIZED.get(raw_next, raw_next)
            if not next_stop:
                next_stop = MANUAL_NEXT_STOP.get((str(entry.get("route")), entry["direction"]), "")
            entry["next_stop"] = next_stop
            effective = entry.get("effective_date") or ""
            if effective:
                try:
                    pd.Timestamp(effective)
                except ValueError:
                    effective = ""
            entry["effective_date"] = effective
            time_key = (str(entry.get("route")), entry["direction"])
            if time_key in MANUAL_TIMES:
                times = MANUAL_TIMES[time_key]
                entry["times_all"] = list(times)
                entry["first_bus"] = min(times)
                entry["last_bus"] = max(times)
            for stop in entry.get("stops", []):
                stop["name"] = apply_manual(stop.get("name", ""))
                if stop.get("note"):
                    note = NOTE_FIXES.get(stop["note"].strip(), stop["note"])
                    note = re.sub(r"[）)]?\s*[0-9A-Za-z]+\s*$", "", note)
                    note = re.sub(r"[，,、;；:：)\）]+$", "", note)
                    stop["note"] = note.strip("()（） ")
                split = MANUAL_SPLIT_NOTE.get((str(entry.get("route")), entry["direction"], stop.get("seq")))
                if split:
                    stop["name"], stop["note"] = split
                stop["display_name"] = (f"{stop['name']}（{stop['note']}）" if stop.get("note") else stop["name"])
            entries.append(entry)

    # Frequency of exact cleaned names, used as a prior when two directions disagree.
    name_freq: Counter = Counter()
    for entry in entries:
        for stop in entry["stops"]:
            name_freq[apply_manual(stop["name"])] += 1

    by_route: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        by_route[str(entry["route"])].append(entry)

    pair_reports: list[dict[str, Any]] = []
    correction_log: list[dict[str, Any]] = []

    for route, route_entries in sorted(by_route.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 999):
        north = next((e for e in route_entries if e["direction"] == D1), None)
        south = next((e for e in route_entries if e["direction"] == D2), None)
        if not north or not south:
            pair_reports.append({"route": route, "status": "missing_direction",
                                 "north_count": len(north["stops"]) if north else 0,
                                 "south_count": len(south["stops"]) if south else 0})
            continue
        north_names = [clean_ocr_name(s["name"]) for s in north["stops"]]
        south_names = [clean_ocr_name(s["name"]) for s in south["stops"]][::-1]
        pairs = align_sequences(north_names, south_names)
        matched_sims: list[float] = []
        for ni, si in pairs:
            if ni is not None and si is not None:
                sim = similarity(north_names[ni], south_names[si])
                matched_sims.append(sim)
                # Only auto-correct near-identical variants.  North/south lists
                # can legitimately contain different one-way stops, so a looser
                # threshold would overwrite real data.
                if north_names[ni] != south_names[si] and sim >= 0.80:
                    chosen, changed = choose_variant(north_names[ni], south_names[si], name_freq)
                    if changed:
                        correction_log.append({"route": route, "north": north_names[ni],
                                               "south": south_names[si], "chosen": chosen})
                    north["stops"][ni]["name"] = chosen
                    north["stops"][ni]["display_name"] = (f"{chosen}（{north['stops'][ni]['note']}）"
                                                          if north["stops"][ni].get("note") else chosen)
                    south["stops"][len(south["stops"]) - 1 - si]["name"] = chosen
                    south["stops"][len(south["stops"]) - 1 - si]["display_name"] = (
                        f"{chosen}（{south['stops'][len(south['stops']) - 1 - si]['note']}）"
                        if south["stops"][len(south["stops"]) - 1 - si].get("note") else chosen)
        pair_reports.append({
            "route": route,
            "status": "ok",
            "north_count": len(north_names),
            "south_count": len(south_names),
            "mean_similarity": round(sum(matched_sims) / len(matched_sims), 3) if matched_sims else 0,
            "unmatched_north": [north_names[i] for i, j in pairs if i is not None and j is None],
            "unmatched_south": [south_names[j] for i, j in pairs if j is not None and i is None],
        })

    # Second manual pass after cross-direction voting.
    for entry in entries:
        for stop in entry["stops"]:
            stop["name"] = MANUAL_SEQ_FIXES.get(
                (str(entry["route"]), entry["direction"], stop.get("seq")), apply_manual(stop["name"]))
            if stop.get("note"):
                stop["note"] = NOTE_FIXES.get(stop["note"], stop["note"])
            stop["display_name"] = (f"{stop['name']}（{stop['note']}）" if stop.get("note") else stop["name"])

    # Join a stop whose parenthetical annotation got detected as its own column.
    for entry in entries:
        route = str(entry["route"])
        for (r, d, seq), (name, note) in MANUAL_MERGE_NEXT.items():
            if r != route or d != entry["direction"]:
                continue
            for i, stop in enumerate(entry["stops"]):
                if stop.get("seq") != seq:
                    continue
                stop["name"] = name
                stop["note"] = note
                stop["display_name"] = f"{name}（{note}）" if note else name
                if i + 1 < len(entry["stops"]):
                    del entry["stops"][i + 1]
                break

    # Insert stops that the batched OCR merged into a neighbouring note.
    for entry in entries:
        route = str(entry["route"])
        direction = entry["direction"]
        for (r, d, anchor), inserts in MANUAL_INSERT_AFTER.items():
            if r != route or d != direction:
                continue
            existing = {compact(s["name"]) for s in entry["stops"]}
            for i, stop in enumerate(list(entry["stops"])):
                if compact(stop["name"]) != compact(anchor):
                    continue
                note = stop.get("note", "")
                for inserted in inserts:
                    note = note.replace(inserted, "")
                    if compact(inserted) not in existing:
                        entry["stops"].insert(i + 1, {
                            "name": inserted, "raw": inserted, "x0": 0, "x1": 0,
                            "red_ratio": 0.0, "score": 1.0, "note": "",
                            "display_name": inserted, "is_current": False,
                        })
                        existing.add(compact(inserted))
                        i += 1
                if (r, d, anchor) in MANUAL_ANCHOR_NOTE:
                    note = MANUAL_ANCHOR_NOTE[(r, d, anchor)]
                note = re.sub(r"[；;]\s*$", "", note.strip("；; "))
                stop["note"] = note
                stop["display_name"] = f"{stop['name']}（{note}）" if note else stop["name"]
                break

    # Recompute the direction comparison with final names, otherwise the sheet
    # still quotes the pre-correction OCR variants.
    final_reports: list[dict[str, Any]] = []
    for route, route_entries in sorted(by_route.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 999):
        north = next((e for e in route_entries if e["direction"] == D1), None)
        south = next((e for e in route_entries if e["direction"] == D2), None)
        if not north or not south:
            final_reports.append({"route": route, "status": "missing_direction",
                                  "north_count": len(north["stops"]) if north else 0,
                                  "south_count": len(south["stops"]) if south else 0,
                                  "mean_similarity": 0, "unmatched_north": [], "unmatched_south": []})
            continue
        north_names = [clean_ocr_name(s["name"]) for s in north["stops"]]
        south_names = [clean_ocr_name(s["name"]) for s in south["stops"]][::-1]
        pairs = align_sequences(north_names, south_names)
        sims = [similarity(north_names[i], south_names[j])
                for i, j in pairs if i is not None and j is not None]
        final_reports.append({
            "route": route, "status": "ok",
            "north_count": len(north["stops"]), "south_count": len(south["stops"]),
            "mean_similarity": round(sum(sims) / len(sims), 3) if sims else 0,
            "unmatched_north": [north_names[i] for i, j in pairs if i is not None and j is None],
            "unmatched_south": [south_names[j] for i, j in pairs if j is not None and i is None],
        })
    pair_reports = final_reports

    # Build stops after correction and recompute current / next-stop validation.
    for entry in entries:
        current = None
        for i, stop in enumerate(entry["stops"]):
            if STATION in compact(stop["name"]):
                current = i
                break
        if current is None:
            for i, stop in enumerate(entry["stops"]):
                if stop.get("red_ratio", 0) > 0.25 and len(stop["name"]) <= 4:
                    current = i
                    break
        entry["current_index"] = current
        for i, stop in enumerate(entry["stops"]):
            stop["is_current"] = (i == current)
            stop["seq"] = i + 1
        expected = (entry["stops"][current + 1]["display_name"]
                    if current is not None and current + 1 < len(entry["stops"]) else "")
        entry["expected_next"] = expected
        # Parenthetical notes may appear on only one side (the header often
        # omits them), so compare the base names.
        entry["next_stop_matches"] = (bool(entry.get("next_stop"))
                                      and normalize_for_compare(expected) == normalize_for_compare(entry["next_stop"]))

    # ---------- DataFrames ----------
    route_rows = []
    stop_rows = []
    review_rows = []
    for entry in entries:
        stops = entry["stops"]
        current_name = stops[entry["current_index"]]["name"] if entry.get("current_index") is not None else ""
        route_rows.append({
            "海报": entry["poster"],
            "方向": entry["direction"],
            "线路": entry["route"],
            "下站": entry["next_stop"],
            "开往": entry["destination"],
            "票价(元)": entry["fare_yuan"],
            "运营公司": entry["operator"],
            "首班(最早)": entry["first_bus"],
            "末班(最晚)": entry["last_bus"],
            "全部时间": " / ".join(sorted(set(entry.get("times_all") or []))),
            "执行日期": entry["effective_date"],
            "站点数": len(stops),
            "当前站": current_name,
            "下站校验": "通过" if entry["next_stop_matches"] else "待复核",
            "站序": " → ".join(s["display_name"] for s in stops),
            "OCR下站原文": entry["next_stop"],
            "OCR表头": entry.get("header_text", ""),
        })
        for stop in stops:
            stop_rows.append({
                "海报": entry["poster"],
                "方向": entry["direction"],
                "线路": entry["route"],
                "序号": stop["seq"],
                "站点": stop["name"],
                "备注": stop.get("note", ""),
                "完整标注": stop["display_name"],
                "是否当前站": "是" if stop.get("is_current") else "",
                "地铁接驳": "是" if "地铁" in stop["display_name"] else "",
                "置信度": round(float(stop.get("score", 0)), 3),
            })
            code_noise = bool(re.search(r"[0-9A-Za-z]", stop["name"])) and not bool(
                re.search(r"(?:地铁站[A-F]口|[0-9]+号口)$", stop["name"]))
            punctuation_noise = bool(re.search(r"[。，、“”]", stop["name"]))
            needs_review = float(stop.get("score", 0)) < 0.7 or code_noise or punctuation_noise
            if needs_review:
                review_rows.append({
                    "类型": "站名",
                    "海报": entry["poster"], "方向": entry["direction"], "线路": entry["route"],
                    "序号": stop["seq"], "内容": stop["display_name"],
                    "置信度": round(float(stop.get("score", 0)), 3),
                    "原因": ("低置信度" if float(stop.get("score", 0)) < 0.7
                             else ("含数字/字母残留" if code_noise else "含异常标点")),
                })
        if not entry["next_stop_matches"]:
            review_rows.append({
                "类型": "下站校验", "海报": entry["poster"], "方向": entry["direction"], "线路": entry["route"],
                "序号": "", "内容": f"表头下站={entry['next_stop']}，站序下一站={entry['expected_next']}",
                "置信度": "", "原因": "双向/站序不一致",
            })
        if not entry.get("next_stop"):
            review_rows.append({
                "类型": "表头", "海报": entry["poster"], "方向": entry["direction"], "线路": entry["route"],
                "序号": "", "内容": entry.get("header_text", "")[:80], "置信度": "", "原因": "未解析出下站",
            })

    # A large count gap is the strongest signal of a merged or dropped column;
    # ordinary one-way stop differences stay in the 双向校验 sheet instead of
    # inflating the review list.
    for pair in pair_reports:
        route = str(pair.get("route"))
        if pair.get("status") != "ok":
            continue
        if abs(int(pair.get("north_count") or 0) - int(pair.get("south_count") or 0)) >= 3:
            review_rows.append({
                "类型": "双向校验", "海报": "", "方向": "", "线路": route, "序号": "",
                "内容": f"{D1}{pair.get('north_count')}站 / {D2}{pair.get('south_count')}站",
                "置信度": "", "原因": f"{D1}{D2}站数差≥3，需确认是单行站还是并站/漏站",
            })

    routes_df = pd.DataFrame(route_rows)
    stops_df = pd.DataFrame(stop_rows)
    review_df = pd.DataFrame(review_rows, columns=["类型", "海报", "方向", "线路", "序号", "内容", "置信度", "原因"])
    pairs_df = pd.DataFrame(pair_reports, columns=["route", "status", "north_count", "south_count",
                                                   "mean_similarity", "unmatched_north", "unmatched_south"])
    pairs_df = pairs_df.rename(columns={
        "north_count": f"{D1}向站数", "south_count": f"{D2}向站数",
        "unmatched_north": f"{D1}向独有站", "unmatched_south": f"{D2}向独有站",
    })
    corrections_df = pd.DataFrame(correction_log, columns=["route", "north", "south", "chosen"])
    corrections_df = corrections_df.rename(columns={"north": f"{D1}向识别", "south": f"{D2}向识别"})

    # ---------- Analysis ----------
    unique_routes = sorted({str(e["route"]) for e in entries},
                           key=lambda x: (0, int(x)) if x.isdigit() else (1, x))
    operator_counter = Counter(e["operator"] or "未识别" for e in entries)
    durations = []
    for e in entries:
        if e.get("first_bus") and e.get("last_bus"):
            try:
                fh, fm = map(int, e["first_bus"].split(":"))
                lh, lm = map(int, e["last_bus"].split(":"))
                durations.append((lh * 60 + lm) - (fh * 60 + fm))
            except Exception:
                pass
    stop_counts = [len(e["stops"]) for e in entries]
    earliest_by_route: dict[str, dict[str, Any]] = {}
    for e in sorted([x for x in entries if x.get("first_bus")], key=lambda x: x["first_bus"]):
        earliest_by_route.setdefault(str(e["route"]), e)
    latest_by_route: dict[str, dict[str, Any]] = {}
    for e in sorted([x for x in entries if x.get("last_bus")], key=lambda x: x["last_bus"], reverse=True):
        latest_by_route.setdefault(str(e["route"]), e)
    earliest = list(earliest_by_route.values())[:5]
    latest = list(latest_by_route.values())[:5]

    # One sequence per route for stop-frequency analysis (prefer north).
    route_best: dict[str, dict[str, Any]] = {}
    for e in entries:
        route = str(e["route"])
        if route not in route_best or e["direction"] == D1:
            route_best[route] = e
    stop_frequency: Counter = Counter()
    for e in route_best.values():
        seen = set()
        for stop in e["stops"]:
            name = stop["name"]
            if name and name not in seen:
                stop_frequency[name] += 1
                seen.add(name)
    metro_stops = sorted({s["display_name"] for e in entries for s in e["stops"] if "地铁" in s["display_name"]})
    seasonal = [e for e in entries if len(e.get("times_all") or []) > 2]
    direction_counter = Counter(e["direction"] for e in entries)
    fare_counter = Counter(e.get("fare_yuan") for e in entries if e.get("fare_yuan") is not None)
    dest_counter = Counter(e.get("destination") for e in entries if e.get("destination"))
    longest = sorted(entries, key=lambda e: len(e["stops"]), reverse=True)[:5]
    shortest = sorted(entries, key=lambda e: len(e["stops"]))[:5]
    longest_unique: list[dict[str, Any]] = []
    shortest_unique: list[dict[str, Any]] = []
    seen_long: set[str] = set()
    for e in sorted(entries, key=lambda x: len(x["stops"]), reverse=True):
        if str(e["route"]) not in seen_long:
            seen_long.add(str(e["route"]))
            longest_unique.append(e)
    seen_short: set[str] = set()
    for e in sorted(entries, key=lambda x: len(x["stops"])):
        if str(e["route"]) not in seen_short:
            seen_short.add(str(e["route"]))
            shortest_unique.append(e)
    metro_routes = sorted({str(e["route"]) for e in entries
                           for s in e["stops"] if "地铁" in s["display_name"]},
                          key=lambda x: int(x) if x.isdigit() else 999)
    fare2_routes = sorted({str(e["route"]) for e in entries if e.get("fare_yuan") == 2},
                          key=lambda x: int(x) if x.isdigit() else 999)
    first_values = [e["first_bus"] for e in entries if e.get("first_bus")]
    last_values = [e["last_bus"] for e in entries if e.get("last_bus")]
    earliest_time = min(first_values) if first_values else ""
    latest_time = max(last_values) if last_values else ""
    early_routes = sorted({str(e["route"]) for e in entries if e.get("first_bus") == earliest_time},
                          key=lambda x: int(x) if x.isdigit() else 999)
    late_routes = sorted({str(e["route"]) for e in entries if e.get("last_bus") == latest_time},
                         key=lambda x: int(x) if x.isdigit() else 999)
    early_close = sorted(longest_unique, key=lambda e: e.get("last_bus") or "99:99")[:3]
    r179 = next((e for e in entries if str(e.get("route")) == "179"), None)
    r179_span = (f"{r179.get('first_bus')}、末班 {r179.get('last_bus')}" if r179 else "")
    dates = []
    for e in entries:
        value = e.get("effective_date")
        if not value:
            continue
        try:
            parsed = pd.Timestamp(value).date()
        except ValueError:
            continue
        if 2020 <= parsed.year <= pd.Timestamp.today().year:
            dates.append(value)
    dates = sorted(set(dates))
    missing_dir_routes = [p["route"] for p in pair_reports if p.get("status") == "missing_direction"]
    diff_routes = [p for p in pair_reports if p.get("status") == "ok"
                   and abs(int(p.get("north_count") or 0) - int(p.get("south_count") or 0)) >= 3]
    routes_d1 = sorted({str(e["route"]) for e in entries if e["direction"] == D1},
                       key=lambda x: (0, int(x)) if x.isdigit() else (1, x))
    routes_d2 = sorted({str(e["route"]) for e in entries if e["direction"] == D2},
                       key=lambda x: (0, int(x)) if x.isdigit() else (1, x))
    routes_both = sorted(set(routes_d1) & set(routes_d2),
                         key=lambda x: (0, int(x)) if x.isdigit() else (1, x))

    summary_rows = [
        ("覆盖线路", f"{len(unique_routes)} 条（{'、'.join(map(str, unique_routes))}）"),
        ("方向记录", f"{len(entries)} 条（{D1} {direction_counter.get(D1, 0)} / {D2} {direction_counter.get(D2, 0)}）"),
        ("运营公司", "；".join(f"{k}: {v} 条" for k, v in operator_counter.most_common())),
        ("票价分布", "；".join(f"{k} 元: {v} 条" for k, v in sorted(fare_counter.items()))),
        ("站点数范围", f"{min(stop_counts)}–{max(stop_counts)} 站，中位数 {int(pd.Series(stop_counts).median())} 站"),
        ("站数最多", "；".join(f"{e['route']}路({e['direction']}) {len(e['stops'])} 站" for e in longest)),
        ("站数最少", "；".join(f"{e['route']}路({e['direction']}) {len(e['stops'])} 站" for e in shortest)),
        ("最早首班", "；".join(f"{e['route']}路 {e['first_bus']}" for e in earliest)),
        ("最晚末班", "；".join(f"{e['route']}路 {e['last_bus']}" for e in latest)),
        ("主要终点 Top 8", "；".join(f"{name}({count})" for name, count in dest_counter.most_common(8))),
        ("地铁接驳站", "；".join(metro_stops) if metro_stops else "未识别"),
        ("地铁接驳线路", "、".join(f"{r}路" for r in metro_routes) if metro_routes else "无"),
        (f"{D1}{D2}站数差≥3", "；".join(
            f"{p['route']}路({D1}{p['north_count']}/{D2}{p['south_count']})" for p in diff_routes
        ) if diff_routes else "无"),
        ("多时段线路", "；".join(
            f"{route}路({'/'.join(sorted({e['direction'] for e in seasonal if str(e['route']) == route}))})"
            for route in sorted({str(e["route"]) for e in seasonal},
                                key=lambda x: (0, int(x)) if x.isdigit() else (1, x))
        ) if seasonal else "无"),
        ("已识别执行日期", (f"{dates[0]} 至 {dates[-1]}（{len(dates)}/{len(entries)} 条可识别）"
                       if dates else f"未识别（{D1}/{D2}向牌日期栏多处模糊）")),
        ("待人工复核", f"{len(review_df)} 条；自动跨方向修正 {len(corrections_df)} 条"),
        ("高频站点 Top 15", "；".join(f"{name}({count})" for name, count in stop_frequency.most_common(15))),
    ]
    summary_df = pd.DataFrame(summary_rows, columns=["指标", "结果"])

    # ---------- Excel / CSV ----------
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    routes_df.to_csv(OUT_DIR / "线路汇总.csv", index=False, encoding="utf-8-sig")
    stops_df.to_csv(OUT_DIR / "站点明细.csv", index=False, encoding="utf-8-sig")
    review_df.to_csv(OUT_DIR / "待人工复核.csv", index=False, encoding="utf-8-sig")
    pairs_df.to_csv(OUT_DIR / "双向校验.csv", index=False, encoding="utf-8-sig")
    corrections_df.to_csv(OUT_DIR / "修正记录.csv", index=False, encoding="utf-8-sig")

    notes_df = pd.DataFrame([
        ("数据源", str(raw.get("source_dir", ""))),
        ("识别时间", pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")),
        ("提取方法", "版面色块/竖排投影切块 + RapidOCR 条带批量识别（疑似并字时逐列复核）+ 双向站序校验 + 人工规则修正"),
        ("方向说明", f"“{D1}/{D2}”对应站牌文件名中的方向；每条记录从起点站排到终点站，当前站为{STATION}。"),
        ("首末班口径", f"取站牌“营运时间”栏的最早首班与最晚末班。部分线路列出两端总站时刻，不代表{STATION}站实际到站时间。"),
        ("站点数", f"包含当前站{STATION}；括号备注不计为独立站点。"),
        ("复核建议", "优先检查“待人工复核”表和置信度低于 0.7 的站名；少数形近字已用规则修正，原始 OCR 可在输出 JSON 中追溯。"),
    ], columns=["项目", "说明"])

    xlsx_path = Path(args.xlsx) if args.xlsx else OUT_DIR / f"{STATION}公交数据.xlsx"
    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        notes_df.to_excel(writer, sheet_name="说明", index=False)
        summary_df.to_excel(writer, sheet_name="分析摘要", index=False)
        routes_df.to_excel(writer, sheet_name="线路汇总", index=False)
        stops_df.to_excel(writer, sheet_name="站点明细", index=False)
        pairs_df.to_excel(writer, sheet_name="双向校验", index=False)
        review_df.to_excel(writer, sheet_name="待人工复核", index=False)
        corrections_df.to_excel(writer, sheet_name="修正记录", index=False)
        from openpyxl.styles import Alignment, Font, PatternFill
        for sheet in writer.sheets.values():
            for column_cells in sheet.columns:
                width = min(70, max(10, max(len(str(c.value)) if c.value is not None else 0 for c in column_cells) + 2))
                sheet.column_dimensions[column_cells[0].column_letter].width = width
            for cell in sheet[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="2F5597")
                cell.alignment = Alignment(horizontal="center", vertical="center")
            sheet.freeze_panes = "A2"
            if sheet.max_row >= 2:
                sheet.auto_filter.ref = sheet.dimensions

    # ---------- Markdown report ----------
    top3_text = "、".join(f"“{n}”{c}次" for n, c in stop_frequency.most_common(3)) or "无"
    early_close_text = "、".join(
        f"{e['route']}路（{e.get('last_bus')}）" for e in early_close if e.get("last_bus")
    ) or "无"
    fare2_text = "、".join(f"{r}路" for r in fare2_routes)
    lines = [
        f"# {STATION}公交站牌数据提取与分析",
        "",
        f"数据源：`{raw.get('source_dir', '')}`，共 {len(posters)} 张站牌，"
        f"{len(entries)} 条方向记录，覆盖 {len(unique_routes)} 条线路。",
        "",
        "## 关键结果",
        "",
    ]
    for metric, value in summary_rows:
        lines.append(f"- **{metric}**：{value}")
    lines += [
        "",
        "> 首末班是站牌“营运时间”栏的汇总。部分线路列出两端总站的时刻，本表取最早/最晚值，"
        f"不代表{STATION}站的实际到站时刻；如需精确到站时间，需要线路时刻表或实时公交数据。",
        "",
        "## 数据观察",
        "",
        f"- {STATION}站共 {len(unique_routes)} 条线路、{len(entries)} 个方向记录，"
        f"其中 {len(metro_routes)} 条线路可接驳地铁；按线路去重后，共线最多的站是 {top3_text}。",
        f"- 票价以 1 元为主（{fare_counter.get(1, 0)} 条）"
        + (f"，2 元线路为 {fare2_text}" if fare2_text else "，全部为 1 元")
        + (f"；其中 179 路是定时班次（首班 {r179_span}）" if r179 else "") + "。",
        f"- 早班最早为 {'、'.join(f'{r}路' for r in early_routes) or '无'} {earliest_time}；"
        f"{'、'.join(f'{r}路' for r in late_routes) or '无'} 末班 {latest_time}；"
        f"收班最早的是 {early_close_text}。",
        (f"- {D1}{D2}站数差≥3 的 "
         f"{'、'.join(f'{p['route']}路' for p in diff_routes)} 呈单行/环线特征，"
         "差异站保留而非强行对齐；其余差异多为同名不同侧的成对站。"
         if diff_routes else f"- {D1}{D2}双向站数差异较小，未发现明显的整列漏识别。"),
        f"- 线路长度从 {min(len(e['stops']) for e in entries)} 到 {max(len(e['stops']) for e in entries)} 站；"
        f"{longest_unique[0]['route']} 路（{len(longest_unique[0]['stops'])} 站）和 "
        f"{longest_unique[1]['route']} 路（{len(longest_unique[1]['stops'])} 站）最长，"
        f"{shortest_unique[0]['route']} 路（{len(shortest_unique[0]['stops'])} 站）最短。",
        "",
        "## 数据口径",
        "",
        f"每条线路牌都包含完整站序（从起点到终点）、当前站“{STATION}”、下一站、终点、票价、运营公司、首末班时间和执行日期。",
        f"{D1}向牌覆盖 {len(routes_d1)} 条线路（{'、'.join(f'{r}路' for r in routes_d1)}），"
        f"{D2}向牌覆盖 {len(routes_d2)} 条线路。只有同时具备{D1}{D2}向牌的 "
        f"{'、'.join(f'{r}路' for r in routes_both)} 做了反向站序校验；"
        "其余线路没有反向牌，站序无法交叉核对，差异站按原牌保留。",
        "",
        "## 准确率与复核",
        "",
        f"- 自动提取的 {len(stop_rows)} 条站点记录中，{len(review_df)} 条进入待复核清单："
        f"{int((review_df['类型'] == '站名').sum())} 条为低置信度站名（核对后仍保留），"
        f"{int((review_df['类型'] == '双向校验').sum())} 条为{D1}{D2}站数差提醒。",
        "- 主要误差来源：极窄的竖排文字、字符粘连、站点旁的括号备注，以及形近字。",
        "- 已对双向能互相校正的站名自动投票；仍建议按《待人工复核》抽查低置信度条目。",
        "",
        "## 文件",
        "",
        f"- `{xlsx_path.name}`：分析摘要、线路汇总、站点明细、双向校验、待人工复核、修正记录",
        "- `线路汇总.csv` / `站点明细.csv`：便于程序读取",
        "- `待人工复核.csv`：优先检查清单",
        "",
    ]
    (OUT_DIR / "分析报告.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"entries={len(entries)} routes={len(unique_routes)} stops={len(stop_rows)} review={len(review_df)}")
    print(f"written {xlsx_path}")


if __name__ == "__main__":
    main()
