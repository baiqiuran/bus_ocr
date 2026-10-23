"""Recognize route terminals without sending intermediate stops to OCR."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Sequence

import numpy as np

from buspipe.config import PipelineSettings
from buspipe.layout import group_columns
from buspipe.ocr import OcrBackend, OcrStats, recognize_columns
from buspipe.parsing import normalize_stop


def _metadata(text: str) -> bool:
    return bool(re.search(r"20\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2}", text) or "执行" in text)


def assemble_endpoint(group: Sequence[dict[str, Any]], role: str) -> dict[str, Any]:
    # Grouping uses the complete row's geometry. Recomputing medians from just
    # two distant endpoints can mistake a narrow terminal for a note.
    base = group[0]
    name = normalize_stop(str(base.get("text", "")), preserve_parentheses=True)
    notes = [str(c.get("text", "")).strip("()（） ，,、;；:：") for c in group[1:]]
    note = "；".join(text for text in notes if text and not _metadata(text))
    flags = sorted({flag for c in group for flag in c.get("fallback_flags", [])})
    if not name:
        flags = sorted(set(flags + ["empty"]))
    return {
        "role": role,
        # seq is an endpoint slot (1=first, 2=last), never a route stop index.
        "seq": 1 if role == "first" else 2,
        "name": name,
        "raw": str(base.get("text", "")),
        "note": note,
        "display_name": f"{name}（{note}）" if name and note else name,
        "x0": base["x0"],
        "x1": max(c["x1"] for c in group),
        "y0": min(c["y0"] for c in group),
        "y1": max(c["y1"] for c in group),
        "score": min(float(c.get("score", 0.0)) for c in group),
        "ocr_flags": flags,
        "source": "ocr",
    }


def recognize_endpoints(
    rgb: np.ndarray,
    columns: Sequence[dict[str, Any]],
    row_y0: int,
    row_y1: int,
    backend: OcrBackend,
    settings: PipelineSettings,
    stats: OcrStats | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """OCR the outermost stop groups, preserving attached note columns.

    A printed execution date at the edge is discarded and the adjacent group
    is tried. An unreadable terminal stays empty for review; it must never be
    silently replaced by an intermediate stop.
    """
    groups = group_columns(columns)
    if len(groups) < 2:
        raise RuntimeError("cannot locate two distinct route endpoint groups")
    left, right = 0, len(groups) - 1
    recognized: dict[int, dict[str, Any]] = {}
    while left < right:
        indices = sorted(set(groups[left] + groups[right]))
        pending = [i for i in indices if i not in recognized]
        if pending:
            # Nearby short vertical names can be detected as one horizontal
            # line. A wider white gap keeps the two terminals independent.
            values = recognize_columns(
                rgb, [columns[i] for i in pending], row_y0, row_y1,
                backend, settings.ocr, replace(settings.canvas, gap=max(100, settings.canvas.gap)), stats,
            )
            by_x = {col["x0"]: col for col in values}
            for index in pending:
                recognized[index] = by_x.get(columns[index]["x0"], dict(columns[index]))
        if _metadata(str(recognized[groups[left][0]].get("text", ""))):
            left += 1
            continue
        if _metadata(str(recognized[groups[right][0]].get("text", ""))):
            right -= 1
            continue
        endpoints = [
            assemble_endpoint([recognized[i] for i in groups[left]], "first"),
            assemble_endpoint([recognized[i] for i in groups[right]], "last"),
        ]
        return endpoints, [recognized[i] for i in sorted(recognized)]
    raise RuntimeError("cannot locate two route endpoints after excluding metadata")
