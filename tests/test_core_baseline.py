"""Ticket 01 regression: layout and parsing match the recorded 安平 baseline."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from buspipe.config import DEFAULT_ROUTE_HINTS, PipelineSettings
from buspipe.layout import detect_route_blocks, locate_stop_band
from buspipe.parsing import assemble_stops, parse_header

ROOT = Path(__file__).resolve().parents[1]


def _settings() -> PipelineSettings:
    return PipelineSettings(current_stop="安平小区", route_hints=DEFAULT_ROUTE_HINTS)


def test_route_blocks_and_columns_match_golden(anping_east_path, golden_east):
    rgb = np.asarray(Image.open(anping_east_path).convert("RGB"))
    settings = _settings()
    blocks = detect_route_blocks(rgb, settings.layout)
    assert [[int(a), int(b)] for a, b in blocks] == golden_east["route_blocks"]

    for index, (route_y0, route_y1) in enumerate(blocks):
        entry = golden_east["entries"][index]
        next_start = blocks[index + 1][0] if index + 1 < len(blocks) else None
        row_y0, row_y1, columns, _ = locate_stop_band(rgb, route_y1, next_start, settings.layout)
        assert [row_y0, row_y1] == entry["row"]
        assert len(columns) == len(entry["columns"])
        for column, expected in zip(columns, entry["columns"]):
            assert [column["x0"], column["x1"], column["y0"], column["y1"]] == [
                expected["x0"],
                expected["x1"],
                expected["y0"],
                expected["y1"],
            ]


def test_header_fields_match_golden(golden_east):
    settings = _settings()
    for index, entry in enumerate(golden_east["entries"]):
        header = parse_header(
            entry["header_items"],
            entry["number_items"],
            entry["time_items"],
            entry["date_items"],
            settings.route_hints,
            golden_east["poster"],
            index,
            entry["row"][0],
        )
        for key in (
            "route",
            "route_ocr",
            "next_stop",
            "destination",
            "fare_raw",
            "fare_yuan",
            "operator",
            "first_bus",
            "last_bus",
            "times_all",
            "effective_date",
            "header_text",
        ):
            assert header[key] == entry[key], (index, key)


def test_stop_assembly_matches_golden(golden_east):
    for entry in golden_east["entries"]:
        stops, current_index = assemble_stops(entry["columns"], "安平小区", entry["next_stop"])
        assert current_index == entry["current_index"]
        assert len(stops) == len(entry["stops"])
        for stop, expected in zip(stops, entry["stops"]):
            for key, value in expected.items():
                assert stop[key] == value, (expected["seq"], key)
        expected_next = ""
        if current_index is not None and current_index + 1 < len(stops):
            expected_next = stops[current_index + 1]["display_name"]
        assert expected_next == entry["expected_next"]
        assert (
            bool(entry["next_stop"])
            and expected_next.replace("（", "(").replace("）", ")")
            == entry["next_stop"].replace("（", "(").replace("）", ")")
        ) == entry["next_stop_matches"]


def test_direction_with_artwork_note():
    from buspipe.config import poster_direction, poster_station

    name = "安铺村-北，小灯箱80x130CM-01"
    assert poster_direction(name) == "北"
    assert poster_station(name) == "安铺村"


def test_cli_help_and_missing_poster():
    completed = subprocess.run(
        [sys.executable, "extract.py", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0
    assert "--poster" in completed.stdout

    completed = subprocess.run(
        [sys.executable, "extract.py", "--poster", "不存在的海报"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "BUS_SRC": str(ROOT / "tests" / "fixtures")},
    )
    assert completed.returncode != 0
    assert "no matching posters" in (completed.stdout + completed.stderr)


@pytest.mark.slow
def test_real_rapidocr_core_structure(anping_east_path, golden_east):
    """Optional slow check: real OCR wiring matches the baseline structure."""
    if os.environ.get("RUN_REAL_OCR") != "1":
        pytest.skip("set RUN_REAL_OCR=1 to run the real RapidOCR regression")

    from buspipe.core import PosterExtractor
    from buspipe.ocr import LocalRapidOcrBackend

    backend = LocalRapidOcrBackend(threads=4)
    result = PosterExtractor(backend, _settings()).extract(anping_east_path, station="安平小区")
    assert result["route_blocks"] == golden_east["route_blocks"]
    assert len(result["entries"]) == len(golden_east["entries"])
    for entry, expected in zip(result["entries"], golden_east["entries"]):
        assert entry["route"] == expected["route"]
        assert entry["next_stop"] == expected["next_stop"]
        assert entry["destination"] == expected["destination"]
        assert entry["fare_yuan"] == expected["fare_yuan"]
        assert entry["first_bus"] == expected["first_bus"]
        assert entry["last_bus"] == expected["last_bus"]
        assert entry["stop_count"] == expected["stop_count"]
        assert entry["current_index"] == expected["current_index"]


def test_core_has_no_environment_dependency(tmp_path, monkeypatch):
    """Settings are immutable data; constructing a core must not read BUS_STOP."""
    from buspipe.core import PosterExtractor

    class NeverCalled:
        model_version = "fake"

        def recognize(self, images, params):  # pragma: no cover - must not run
            raise AssertionError("no OCR call expected")

    monkeypatch.setenv("BUS_STOP", "不该被读取")
    extractor = PosterExtractor(NeverCalled(), _settings())
    assert extractor.settings.current_stop == "安平小区"
