"""Anomaly collection, manual overrides and bidirectional consistency checks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Sequence

from PIL import Image

from buspipe.config import PipelineSettings
from buspipe.parsing import entry_targets, refresh_entry_validation
from buspipe.store import REASON_PRIORITY, review_sort_key

FLAG_REASONS = {
    "cross_column": ("cross_column", "批量画布检测框跨列"),
    "wide_box": ("wide_box", "检测框明显宽于列宽"),
    "low_coverage": ("low_coverage", "识别框未覆盖整列"),
    "empty": ("empty_ocr", "该列 OCR 为空"),
    "direction_uncertain": ("direction_uncertain", "横版箭头不明确，需核对首末站顺序"),
}


@dataclass
class PosterSummary:
    image_hash: str
    station: str
    route: str
    direction: str
    names: list[str]
    stop_count: int
    next_stop_matches: bool



def apply_overrides(result: dict[str, Any], overrides: Sequence[dict[str, Any]]) -> int:
    applied = 0
    for override in overrides:
        for entry in result.get("entries", []):
            if entry.get("direction") != override.get("direction"):
                continue
            if str(entry.get("route", "")) != str(override.get("route", "")):
                continue
            stops = entry_targets(entry)
            seq = int(override.get("seq", 0))
            if not 1 <= seq <= len(stops):
                continue
            name = str(override.get("name", "")).strip()
            if not name:
                continue
            note = str(override.get("note", "")).strip()
            stop = stops[seq - 1]
            stop["name"] = name
            stop["note"] = note
            stop["display_name"] = f"{name}（{note}）" if note else name
            stop["source"] = "manual"
            stop["score"] = 1.0
            stop["ocr_flags"] = []
            stop["manual"] = True
            applied += 1
    for entry in result.get("entries", []):
        refresh_entry_validation(entry)
    return applied


def summaries_from_result(
    result: dict[str, Any], image_hash: str, station: str
) -> list[PosterSummary]:
    summaries = []
    for entry in result.get("entries", []):
        if entry.get("error") or entry.get("extract_mode") == "endpoints":
            continue
        summaries.append(
            PosterSummary(
                image_hash=image_hash,
                station=station,
                route=str(entry.get("route", "")),
                direction=str(entry.get("direction", "")),
                names=[stop.get("display_name", "") for stop in entry.get("stops", [])],
                stop_count=len(entry.get("stops", [])),
                next_stop_matches=bool(entry.get("next_stop_matches")),
            )
        )
    return summaries


class ReviewEngine:
    def __init__(self, settings: PipelineSettings) -> None:
        self.settings = settings

    def collect(
        self,
        result: dict[str, Any],
        image_hash: str,
        *,
        image_path: Path | str | None = None,
        crop_dir: Path | str | None = None,
    ) -> list[dict[str, Any]]:
        items: dict[tuple[str, str, str, int], dict[str, Any]] = {}
        image: Image.Image | None = None
        crop_root = Path(crop_dir) if crop_dir else None
        if crop_root is not None:
            crop_root.mkdir(parents=True, exist_ok=True)
        saved: set[str] = set()

        def crop_path_for(x0, x1, y0, y1, route: str, direction: str, seq: int, reason: str) -> str:
            nonlocal image
            if crop_root is None or image_path is None:
                return ""
            name = f"{image_hash[:8]}_{route}_{direction}_{seq}_{reason}.png".replace(" ", "_")
            target = crop_root / name
            if name in saved:
                return str(target)
            try:
                if image is None:
                    image = Image.open(image_path).convert("RGB")
                left = max(0, int(x0) - 6)
                top = max(0, int(y0) - 5)
                right = min(image.width, int(x1) + 7)
                bottom = min(image.height, int(y1) + 6)
                image.crop((left, top, right, bottom)).save(target)
                saved.add(name)
            except (OSError, ValueError):
                return ""
            return str(target)

        def add(
            entry: dict[str, Any],
            reason: str,
            seq: int,
            detail: str,
            ocr_text: str = "",
            score: float | None = None,
            source: str = "ocr",
            crop: str = "",
        ) -> None:
            key = (str(entry.get("route", "")), str(entry.get("direction", "")), reason, int(seq))
            if key in items:
                return
            items[key] = {
                "image_hash": image_hash,
                "route": str(entry.get("route", "")),
                "direction": str(entry.get("direction", "")),
                "seq": int(seq),
                "reason": reason,
                "detail": detail,
                "ocr_text": ocr_text,
                "source": source,
                "score": score,
                "crop_path": crop,
                "status": "open",
            }

        for entry in result.get("entries", []):
            route = str(entry.get("route", ""))
            direction = str(entry.get("direction", ""))
            if entry.get("error"):
                add(entry, "layout_error", 0, f"版面/OCR 失败：{entry['error']}")
                continue
            endpoints_only = entry.get("extract_mode") == "endpoints"
            if endpoints_only:
                if entry.get("recovery_profile"):
                    add(entry, "recovered_layout", 0, f"使用线路号定位恢复分支 {entry['recovery_profile']}，需核对线路号及有序首末站")
                number_items = entry.get("number_items", [])
                if not route:
                    add(entry, "route_number_unknown", 0, "未识别出线路号")
                elif not entry.get("route_ocr") or entry.get("route_ocr") != route or (
                    number_items and min(float(item.get("score", 0.0)) for item in number_items) < self.settings.review_low_score
                ):
                    add(entry, "route_number_uncertain", 0, "线路号 OCR 置信度不足或与已知线路提示不一致", ocr_text=route)
            stops = entry_targets(entry)
            if endpoints_only:
                for stop in stops:
                    text = str(stop.get("name", ""))
                    # A/B/C/D suffixes in building/metro names are legitimate.
                    residual = re.sub(r"[A-Za-z](?=(?:区|口|座|栋|馆|出口))", "", text)
                    if re.search(r"[_#■\u3040-\u30ffA-Za-z]", residual):
                        seq = int(stop.get("seq", 0))
                        crop = crop_path_for(stop.get("x0",0),stop.get("x1",0),stop.get("y0",0),stop.get("y1",0),route,direction,seq,"suspicious_text")
                        add(entry,"suspicious_text",seq,"端点含异常字符，保留原文待复核",ocr_text=text,score=stop.get("score"),crop=crop)
            if not stops:
                add(entry, "unknown_template", 0, "未识别出任何站名列，可能是新模板")
                continue
            current_index = entry.get("current_index")
            next_stop = str(entry.get("next_stop", ""))
            expected_next = str(entry.get("expected_next", ""))
            if not endpoints_only and current_index is None and next_stop:
                add(
                    entry,
                    "current_stop_unknown",
                    0,
                    f"未能定位当前站；表头下站={next_stop}",
                    ocr_text=next_stop,
                )
            if not endpoints_only and next_stop and not entry.get("next_stop_matches"):
                # seq is 1-based; the expected next stop is current_index + 2.
                seq = int(current_index) + 2 if isinstance(current_index, int) else 0
                target_stop = stops[seq - 1] if 1 <= seq <= len(stops) else None
                crop = (
                    crop_path_for(
                        target_stop.get("x0", 0),
                        target_stop.get("x1", 0),
                        target_stop.get("y0", 0),
                        target_stop.get("y1", 0),
                        route,
                        direction,
                        seq,
                        "next_stop_mismatch",
                    )
                    if target_stop
                    else ""
                )
                add(
                    entry,
                    "next_stop_mismatch",
                    seq,
                    f"表头下站={next_stop}；站序下站={expected_next or '（无）'}",
                    ocr_text=expected_next or next_stop,
                    crop=crop,
                )
            for stop in stops:
                seq = int(stop.get("seq", 0))
                flags = list(stop.get("ocr_flags", []))
                x0, x1 = stop.get("x0", 0), stop.get("x1", 0)
                y0, y1 = stop.get("y0", 0), stop.get("y1", 0)
                for flag in flags:
                    mapped = FLAG_REASONS.get(flag)
                    if not mapped:
                        continue
                    reason, detail = mapped
                    crop = crop_path_for(x0, x1, y0, y1, route, direction, seq, reason)
                    add(
                        entry,
                        reason,
                        seq,
                        detail,
                        ocr_text=stop.get("display_name", ""),
                        score=stop.get("score"),
                        source=str(stop.get("source", "ocr")),
                        crop=crop,
                    )
                if float(stop.get("score", 0.0)) < self.settings.review_low_score:
                    crop = crop_path_for(x0, x1, y0, y1, route, direction, seq, "low_confidence")
                    add(
                        entry,
                        "low_confidence",
                        seq,
                        f"OCR 置信度 {float(stop.get('score', 0.0)):.3f} 低于 "
                        f"{self.settings.review_low_score:.2f}",
                        ocr_text=stop.get("display_name", ""),
                        score=stop.get("score"),
                        source=str(stop.get("source", "ocr")),
                        crop=crop,
                    )
            for column_index, column in enumerate(entry.get("columns", [])):
                if column.get("text"):
                    continue
                if endpoints_only and any(
                    stop.get("manual") and stop["x0"] <= column.get("x0", 0)
                    and stop["x1"] >= column.get("x1", 0) for stop in stops
                ):
                    continue
                # Negative seq means "column position", not a stop sequence.
                seq = -1 - column_index
                crop = crop_path_for(
                    column.get("x0", 0),
                    column.get("x1", 0),
                    column.get("y0", 0),
                    column.get("y1", 0),
                    route,
                    direction,
                    seq,
                    "empty_ocr",
                )
                add(
                    entry,
                    "empty_ocr",
                    seq,
                    f"列 {column_index + 1}（x={column.get('x0', 0)}）OCR 为空",
                    crop=crop,
                )
        if image is not None:
            image.close()
        return sorted(items.values(), key=review_sort_key)

    def bidirectional_items(
        self,
        summaries: Sequence[PosterSummary],
        *,
        threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        threshold = self.settings.bidirectional_min_similarity if threshold is None else threshold
        groups: dict[tuple[str, str], list[PosterSummary]] = {}
        for summary in summaries:
            if not summary.route or not summary.names:
                continue
            groups.setdefault((summary.station, summary.route), []).append(summary)
        items: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for (station, route), members in groups.items():
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    left, right = members[i], members[j]
                    if left.direction == right.direction:
                        continue
                    if len(left.names) != len(right.names):
                        continue
                    pair_key = tuple(sorted([left.image_hash, right.image_hash]))
                    if pair_key in seen:
                        continue
                    seen.add(pair_key)
                    ratio = SequenceMatcher(
                        None, left.names, list(reversed(right.names))
                    ).ratio()
                    if ratio >= threshold:
                        continue
                    detail = (
                        f"{left.direction}向与{right.direction}向站序反向相似度 {ratio:.2f} "
                        f"低于 {threshold:.2f}"
                    )
                    for summary, other in ((left, right), (right, left)):
                        items.append(
                            {
                                "image_hash": summary.image_hash,
                                "route": route,
                                "direction": summary.direction,
                                "seq": 0,
                                "reason": "bidirectional_mismatch",
                                "detail": detail,
                                "ocr_text": "",
                                "source": "ocr",
                                "score": None,
                                "crop_path": "",
                                "status": "open",
                            }
                        )
        return sorted(items, key=review_sort_key)

