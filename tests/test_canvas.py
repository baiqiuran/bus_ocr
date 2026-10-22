"""Ticket 04: dynamic canvas packing, note grouping and fallback flags."""

from __future__ import annotations

from dataclasses import replace

from buspipe.config import CanvasSettings, PipelineSettings
from buspipe.core import PosterExtractor
from buspipe.layout import group_columns, pack_columns
from buspipe.ocr import OcrStats, OcrUnavailable
from tests.synth import SyntheticOcrBackend, make_poster


def _columns(widths, gaps=None, start=300):
    gaps = gaps or [30] * (len(widths) - 1)
    columns = []
    x = start
    for index, width in enumerate(widths):
        if index:
            x += gaps[index - 1]
        columns.append({"x0": x, "x1": x + width - 1, "y0": 0, "y1": 200})
        x += width
    return columns


def test_pack_respects_max_columns_and_width():
    settings = CanvasSettings(max_columns=4, max_width=1000, gap=30)
    columns = _columns([60] * 10)
    chunks = pack_columns(columns, settings)
    assert sum(len(chunk) for chunk in chunks) == len(columns)
    assert all(len(chunk) <= 4 for chunk in chunks)
    for chunk in chunks:
        width = settings.margin * 2 + sum(
            columns[i]["x1"] - columns[i]["x0"] + 5 for i in chunk
        ) + settings.gap * (len(chunk) - 1)
        assert width <= settings.max_width


def test_note_column_stays_with_its_stop():
    settings = CanvasSettings(max_columns=2, max_width=1000, gap=30)
    columns = _columns([60, 60, 20, 60, 60], gaps=[30, 5, 30, 30])
    groups = group_columns(columns)
    assert [1, 2] in groups
    chunks = pack_columns(columns, settings)
    for chunk in chunks:
        if 1 in chunk:
            assert 2 in chunk
        if 2 in chunk:
            assert 1 in chunk
        assert len(chunk) <= 2


def test_single_overwide_column_gets_its_own_canvas():
    settings = CanvasSettings(max_columns=4, max_width=200, gap=30)
    columns = _columns([300, 60])
    chunks = pack_columns(columns, settings)
    assert chunks[0] == [0]
    assert 1 in chunks[-1]


def test_canvas_settings_change_count_but_not_stops(tmp_path):
    path = make_poster(tmp_path / "测试小区-北-001.jpg", stops=10, current_index=1)
    settings = PipelineSettings(current_stop="测试小区")
    wide_stats = OcrStats()
    wide = PosterExtractor(
        SyntheticOcrBackend(), settings, stats=wide_stats
    ).extract(path, station="测试小区")
    narrow_stats = OcrStats()
    narrow = PosterExtractor(
        SyntheticOcrBackend(),
        replace(settings, canvas=replace(settings.canvas, max_columns=3)),
        stats=narrow_stats,
    ).extract(path, station="测试小区")

    assert narrow_stats.canvas_batches > wide_stats.canvas_batches
    wide_stops = [(s["name"], s["note"], s["seq"]) for s in wide["entries"][0]["stops"]]
    narrow_stops = [(s["name"], s["note"], s["seq"]) for s in narrow["entries"][0]["stops"]]
    assert wide_stops == narrow_stops


def test_cross_column_box_triggers_single_column_fallback(tmp_path):
    path = make_poster(tmp_path / "测试小区-北-001.jpg", stops=8, current_index=1, cross_index=2)
    result = PosterExtractor(SyntheticOcrBackend(), PipelineSettings(current_stop="测试小区")).extract(
        path, station="测试小区"
    )
    entry = result["entries"][0]
    assert entry["stop_count"] == 8
    flags = {index + 1: stop["ocr_flags"] for index, stop in enumerate(entry["stops"])}
    assert "cross_column" in flags[3]
    assert "cross_column" in flags[4]


class CanvasShyBackend:
    model_version = "canvas-shy"

    def __init__(self) -> None:
        self.inner = SyntheticOcrBackend()
        self.failed_canvas = False

    def recognize(self, images, params):
        if any(
            image.shape[1] > 250 and image.shape[0] > 220 and image.shape[1] < 850
            for image in images
        ):
            self.failed_canvas = True
            raise OcrUnavailable("canvas too large for this backend")
        return self.inner.recognize(images, params)


def test_whole_canvas_failure_falls_back_to_columns(tmp_path):
    path = make_poster(tmp_path / "测试小区-北-001.jpg", stops=8, current_index=1)
    backend = CanvasShyBackend()
    stats = OcrStats()
    result = PosterExtractor(
        backend, PipelineSettings(current_stop="测试小区"), stats=stats
    ).extract(path, station="测试小区")
    entry = result["entries"][0]
    assert backend.failed_canvas is True
    assert entry["stop_count"] == 8
    assert stats.snapshot()["canvas_fallbacks"] == 1
    assert any("canvas_fallback" in stop["ocr_flags"] for stop in entry["stops"])


def test_empty_ocr_column_is_retained_for_review(tmp_path):
    path = make_poster(tmp_path / "测试小区-北-001.jpg", stops=6, current_index=1, skip_index=3)
    result = PosterExtractor(SyntheticOcrBackend(), PipelineSettings(current_stop="测试小区")).extract(
        path, station="测试小区"
    )
    entry = result["entries"][0]
    assert entry["stop_count"] == 5
    empty = [column for column in entry["columns"] if not column["text"]]
    assert len(empty) == 1
    assert "empty" in empty[0]["fallback_flags"]
