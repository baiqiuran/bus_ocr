"""Business parsing and validation for one route block.

Pure functions over OCR items: header fields, stop assembly, current stop and
next-stop agreement.  No image access and no globals.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

import numpy as np

CHINESE_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def normalize_for_compare(value: str) -> str:
    return re.sub(r"\s+", "", value).replace("（", "(").replace("）", ")")


def normalize_stop(text: str) -> str:
    text = re.sub(r"\s+", "", text)
    text = text.replace("立占", "站")
    text = text.replace("（", "(").replace("）", ")")
    text = re.sub(r"[，,、;；:：]+$", "", text)
    return text.strip("()")


def pick_route_number(
    items: Sequence[dict[str, Any]],
    route_hints: Mapping[str, Sequence[str]],
    poster_name: str,
    block_index: int,
) -> str:
    # Group boxes by overlapping baselines first: a detector may split K1 into
    # K and 1 and return them with slightly different top coordinates.
    lines: list[list[dict[str, Any]]] = []
    for item in sorted(items, key=lambda it: (it["y0"], it["x0"])):
        for line in lines:
            anchor = line[0]
            overlap = min(item["y1"], anchor["y1"]) - max(item["y0"], anchor["y0"])
            min_height = max(1, min(item["y1"] - item["y0"], anchor["y1"] - anchor["y0"]))
            if overlap >= min_height * 0.5:
                line.append(item)
                break
        else:
            lines.append([item])
    readings = [
        ("".join(it["text"] for it in sorted(line, key=lambda it: it["x0"])),
         min(float(it["score"]) for it in line))
        for line in lines
    ]
    readings.extend((item["text"], float(item["score"])) for item in items)
    candidates = []
    for raw, score in readings:
        text = re.sub(r"\s+", "", raw).replace("（", "(").split("(", 1)[0]
        match = re.fullmatch(r"([A-Za-z\u4e00-\u9fff]*?)([0-9OoIl]+)(?:路)?", text)
        if match:
            prefix, number = match.groups()
            number = number.translate(str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1"}))
            candidates.append((len(number), len(prefix), score, prefix.upper() + number))
        elif re.fullmatch(r"[\u4e00-\u9fff]+(?:专线|环线)", text):
            candidates.append((0, len(text), score, text))
    if candidates:
        return max(candidates, key=lambda z: z[:3])[3]
    known = route_hints.get(poster_name)
    if known and block_index < len(known):
        return known[block_index]
    return ""


def parse_fare(text: str) -> tuple[str, int | None]:
    compact = re.sub(r"\s+", "", text)
    m = re.search(r"一票制([一二三四五12345])元", compact)
    if not m:
        m = re.search(r"([一二三四五12345])元一票制", compact)
    if not m:
        return "", None
    raw = m.group(1)
    value = int(raw) if raw.isdigit() else CHINESE_DIGITS.get(raw)
    return m.group(0), value


def parse_operator(text: str) -> str:
    compact = re.sub(r"\s+", "", text)
    for name in ["福州公交集团", "福建港口", "康驰", "闽运", "公交集团", "开发区公交", "福州公交"]:
        if name in compact:
            return "福州公交集团" if name == "福州公交" else name
    return ""


def parse_effective_date(items: Sequence[dict[str, Any]], all_text: str) -> str:
    candidates = [item["text"] for item in items]
    candidates.append(all_text)
    for text in candidates:
        m = re.search(r"(20\d{2})\s*[.\-/年]\s*(\d{1,2})\s*[.\-/月]\s*(\d{1,2})", text)
        if m:
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return ""


def _normalize_time(value: str) -> str:
    h, m = value.replace(" ", "").split(":")
    return f"{int(h):02d}:{int(m):02d}"


def _minutes(value: str) -> int:
    h, m = value.split(":")
    return int(h) * 60 + int(m)


def parse_times(time_items: Sequence[dict[str, Any]], header_text: str) -> tuple[str, str, list[str]]:
    times: list[tuple[float, str]] = []
    for item in time_items:
        for t in re.findall(r"\d{1,2}\s*:\s*\d{2}", item["text"]):
            times.append((item["y0"], _normalize_time(t)))
    if not times:
        for t in re.findall(r"\d{1,2}\s*:\s*\d{2}", header_text):
            times.append((0.0, _normalize_time(t)))
    unique: list[str] = []
    for _, t in sorted(times, key=lambda z: z[0]):
        if t not in unique:
            unique.append(t)
    first_bus = min(unique, key=_minutes) if unique else ""
    last_bus = max(unique, key=_minutes) if unique else ""
    return first_bus, last_bus, unique


def _clean_label(value: str) -> str:
    for token in [
        "开往",
        "下站",
        "一票制",
        "一元一票制",
        "Fuzhou",
        "绿色",
        "巴士",
        "营运时间",
        "末班",
        "首班",
        "福州公交集团",
        "康驰",
        "angChi",
    ]:
        value = value.split(token)[0]
    return value.strip()


def parse_header(
    header_items: Sequence[dict[str, Any]],
    number_items: Sequence[dict[str, Any]],
    time_items: Sequence[dict[str, Any]],
    date_items: Sequence[dict[str, Any]],
    route_hints: Mapping[str, Sequence[str]],
    poster_name: str,
    block_index: int,
    row_y0: int,
) -> dict[str, Any]:
    """Turn header OCR items into the route-block header fields."""
    ordered = sorted(header_items, key=lambda z: (z["x0"], z["y0"]))
    header_text = "".join(item["text"] for item in ordered)
    main_header = [it for it in ordered if 700 <= it["x0"] <= 3350 and it["y0"] < row_y0]
    if not main_header:
        main_header = [it for it in ordered if 700 <= it["x0"] <= 3350]
    main_header.sort(key=lambda z: z["x0"])
    compact_header = "".join(item["text"] for item in main_header)
    # Widening the crop can clip the last digit of a 2/3-digit route number.
    compact_header = re.sub(r"^[0-9A-Za-z]+", "", compact_header)

    route_ocr = pick_route_number(number_items, route_hints, poster_name, block_index)
    known_routes = route_hints.get(poster_name, [])
    route = known_routes[block_index] if block_index < len(known_routes) else route_ocr

    next_stop = ""
    m = re.search(r"下站(.+?)(?=开往|一票制|$)", compact_header)
    if m:
        next_stop = _clean_label(m.group(1))
    destination = ""
    m = re.search(r"开往(.+?)(?=一票制|一元一票制|$)", compact_header)
    if m:
        destination = _clean_label(m.group(1))
    fare_raw, fare_yuan = parse_fare(compact_header)
    operator = parse_operator(header_text)
    first_bus, last_bus, times_all = parse_times(time_items, header_text)
    effective_date = parse_effective_date(list(date_items) + list(ordered) + list(time_items), header_text)
    return {
        "route": route,
        "route_ocr": route_ocr,
        "next_stop": next_stop,
        "destination": destination,
        "fare_raw": fare_raw,
        "fare_yuan": fare_yuan,
        "operator": operator,
        "first_bus": first_bus,
        "last_bus": last_bus,
        "times_all": times_all,
        "effective_date": effective_date,
        "header_text": header_text,
    }


def assemble_stops(
    recognized: Sequence[dict[str, Any]],
    current_stop: str,
    next_stop: str = "",
) -> tuple[list[dict[str, Any]], int | None]:
    """Assemble stop columns (including note columns) into ordered stops."""
    columns = sorted(recognized, key=lambda c: c["x0"])
    widths = [c["x1"] - c["x0"] + 1 for c in columns]
    median_width = float(np.median(widths)) if widths else 0.0
    gaps = [columns[i]["x0"] - columns[i - 1]["x1"] for i in range(1, len(columns))]
    median_gap = float(np.median(gaps)) if gaps else 0.0

    stops: list[dict[str, Any]] = []
    for ci, col in enumerate(columns):
        text = col.get("text", "")
        if not text:
            continue
        if re.search(r"20\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2}", text) or "执行" in text:
            continue
        width = col["x1"] - col["x0"] + 1
        gap_to_prev = (col["x0"] - columns[ci - 1]["x1"]) if ci > 0 else 10_000
        is_note = text.startswith(("(", "（")) or text.endswith((")", "）"))
        # A note is a narrow column placed unusually close to the stop it
        # annotates.  Width alone is unsafe: a short stop can be narrow too.
        if not is_note and median_width and width < median_width * 0.85 and median_gap and gap_to_prev < median_gap * 0.70:
            is_note = True
        if is_note and stops:
            note = text.strip("()（）")
            note = re.sub(r"[，,、;；:：)\）]+$", "", note).strip("()（） ")
            if note:
                stops[-1]["note"] = (stops[-1]["note"] + "；" + note) if stops[-1]["note"] else note
            continue
        stops.append(
            {
                "name": normalize_stop(text),
                "raw": text,
                "x0": col["x0"],
                "x1": col["x1"],
                "y0": col.get("y0", 0),
                "y1": col.get("y1", 0),
                "red_ratio": col.get("red_ratio", 0.0),
                "score": col.get("score", 0.0),
                "note": "",
                "source": "ocr",
                "ocr_flags": list(col.get("fallback_flags", [])),
            }
        )
    for stop in stops:
        base = stop["name"].strip("()（）")
        stop["display_name"] = f"{base}（{stop['note']}）" if stop["note"] else base

    # Identify the current stop (highlighted red on the poster), then validate
    # it against the header's next stop.
    current_index = None
    if current_stop:
        for i, stop in enumerate(stops):
            compact = re.sub(r"\s+", "", stop["name"])
            if current_stop in compact or (len(current_stop) == 2 and compact in set(current_stop)):
                current_index = i
                break
    if current_index is None:
        for i, stop in enumerate(stops):
            if stop["red_ratio"] > 0.25 and len(stop["name"]) <= 4:
                current_index = i
                break
    if current_index is None and next_stop:
        for i, stop in enumerate(stops[:-1]):
            if normalize_for_compare(stop["display_name"]) == normalize_for_compare(next_stop):
                current_index = max(0, i - 1)
                break
    for i, stop in enumerate(stops):
        stop["is_current"] = i == current_index
        stop["seq"] = i + 1
    return stops, current_index


def entry_targets(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Review/override targets; endpoint slots are separate from full stops."""
    return entry.get("endpoints", []) if entry.get("extract_mode") == "endpoints" else entry.get("stops", [])


def refresh_entry_validation(entry: dict[str, Any]) -> None:
    """Recompute next-stop agreement after an override changed stop names."""
    if entry.get("extract_mode") == "endpoints":
        terminals = {stop["role"]: stop.get("display_name", "") for stop in entry.get("endpoints", [])}
        entry["first_stop"] = terminals.get("first", "")
        entry["last_stop"] = terminals.get("last", "")
        entry["route_direction"] = (
            f"{entry['first_stop']}→{entry['last_stop']}"
            if entry["first_stop"] and entry["last_stop"] else ""
        )
        return
    stops = entry.get("stops", [])
    current_index = entry.get("current_index")
    expected_next = ""
    if isinstance(current_index, int) and 0 <= current_index + 1 < len(stops):
        expected_next = stops[current_index + 1].get("display_name", "")
    next_stop = entry.get("next_stop", "")
    entry["expected_next"] = expected_next
    entry["next_stop_matches"] = bool(next_stop) and normalize_for_compare(expected_next) == normalize_for_compare(next_stop)
    entry["stop_count"] = len(stops)
    for i, stop in enumerate(stops):
        stop["is_current"] = i == current_index
        stop["seq"] = i + 1
