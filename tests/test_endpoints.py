"""Minimal extraction keeps station identity and ordered terminals, not stop sequences."""

from __future__ import annotations

import csv
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from buspipe.batch import BatchConfig, BatchError, run_batch
from buspipe.cache import BlockCache, FileCache
from buspipe.config import PipelineSettings
from buspipe.core import PosterExtractor
from buspipe.endpoints import recognize_endpoints
from buspipe.parsing import refresh_entry_validation
from buspipe.report import export_manifest
from buspipe.review import ReviewEngine, apply_overrides, summaries_from_result
from buspipe.store import ManifestStore
from buspipe.vlm import HttpVlmReviewer, VlmConfig
from tests.conftest import FIXTURES
from tests.fake_services import FakeVlmService, serve
from tests.synth import (
    GREEN, MAGENTA, YELLOW, SyntheticOcrBackend, _bars, _has_color,
    _marker_present, make_poster,
)


SETTINGS = PipelineSettings(extract_mode="endpoints", route_hints={})


class NamedBackend(SyntheticOcrBackend):
    def __init__(self):
        super().__init__()
        self.stop_columns_seen = 0

    def _one(self, image):
        if _has_color(image, GREEN) or self.detect_route_number(image) is not None:
            return super()._one(image)
        bars = _bars(image)
        self.stop_columns_seen += len(bars)
        return [
            {"text": "起点站" if _marker_present(image, bar, YELLOW) else
                     "终点站" if _marker_present(image, bar, MAGENTA) else "中间站",
             "score": 0.99, "x0": bar[0] - 1, "x1": bar[1] + 1,
             "y0": bar[2], "y1": bar[3]}
            for bar in bars
        ]


def _named_poster(path):
    return make_poster(path, stops=8, low_score_index=0, cross_index=7)


def test_only_number_and_endpoint_pixels_reach_ocr(tmp_path):
    path = _named_poster(tmp_path / "安铺村-北，小灯箱80x130CM-01.jpg")
    backend = NamedBackend()
    result = PosterExtractor(backend, SETTINGS).extract(path, station="不能覆盖文件名")
    entry = result["entries"][0]
    assert result["station_key"] == entry["station_key"] == "安铺村-北"
    assert entry["route"] == entry["route_ocr"] == "1"
    assert entry["first_stop"] == "起点站"
    assert entry["last_stop"] == "终点站"
    assert entry["route_direction"] == "起点站→终点站"
    assert [e["role"] for e in entry["endpoints"]] == ["first", "last"]
    assert entry["stops"] == []
    assert "stop_count" not in entry and "next_stop" not in entry
    assert "fare_yuan" not in entry and "first_bus" not in entry
    assert backend.images_seen == 4
    assert backend.stop_columns_seen == 2


@pytest.mark.parametrize("fixture", ["anping_east01_raw.json", "anping_west01_raw.json", "anping_west02_raw.json"])
def test_endpoint_selection_matches_all_golden_routes(monkeypatch, fixture):
    golden = json.loads((FIXTURES / fixture).read_text(encoding="utf-8"))
    for entry in golden["entries"]:
        selected = []

        def replay(rgb, columns, *args):
            selected.extend(columns)
            return list(columns)

        monkeypatch.setattr("buspipe.endpoints.recognize_columns", replay)
        endpoints, _ = recognize_endpoints(None, entry["columns"], *entry["row"], None, SETTINGS)
        assert [e["display_name"] for e in endpoints] == [
            entry["stops"][0]["display_name"], entry["stops"][-1]["display_name"],
        ], entry["route"]
        assert len(selected) <= 4


def _column(x, text, width=30, score=0.99):
    return {"x0": x, "x1": x + width - 1, "y0": 100, "y1": 300, "text": text, "score": score}


@pytest.mark.parametrize("last_text", ["", "末站"])
def test_endpoint_notes_and_empty_terminal_are_not_promoted(monkeypatch, last_text):
    columns = [_column(10, "首站"), _column(70, "中间一"), _column(130, "中间二"),
               _column(190, last_text), _column(223, "近路口", width=15, score=0.5)]
    monkeypatch.setattr("buspipe.endpoints.recognize_columns", lambda rgb, cols, *args: list(cols))
    endpoints, selected = recognize_endpoints(None, columns, 100, 300, None, SETTINGS)
    assert endpoints[-1]["name"] == last_text
    assert endpoints[-1]["note"] == "近路口"
    assert endpoints[-1]["score"] == 0.5
    assert len(selected) == 3
    if not last_text:
        assert endpoints[-1]["display_name"] == ""
        assert "empty" in endpoints[-1]["ocr_flags"]


def test_dates_at_edge_are_excluded_without_recognizing_middle(monkeypatch):
    columns = [_column(10, "首站"), _column(70, "中间站"), _column(130, "末站"),
               _column(190, "2026.10.01执行")]
    seen = []

    def replay(rgb, cols, *args):
        seen.extend(c["text"] for c in cols)
        return list(cols)

    monkeypatch.setattr("buspipe.endpoints.recognize_columns", replay)
    endpoints, _ = recognize_endpoints(None, columns, 100, 300, None, SETTINGS)
    assert [e["name"] for e in endpoints] == ["首站", "末站"]
    assert seen == ["首站", "2026.10.01执行", "末站"]


def test_single_group_is_not_invented_as_two_terminals():
    with pytest.raises(RuntimeError, match="two distinct"):
        recognize_endpoints(None, [_column(10, "单站名")], 100, 300, None, SETTINGS)


def test_endpoint_cache_and_full_mode_are_separate(tmp_path):
    assert SETTINGS.fingerprint() != replace(SETTINGS, extract_mode="full").fingerprint()
    path = _named_poster(tmp_path / "站点-西-01.jpg")
    backend = NamedBackend()
    checkpoint = BlockCache(FileCache(tmp_path / "cache", "blocks"), "image", SETTINGS.fingerprint())
    first = PosterExtractor(backend, SETTINGS, checkpoint=checkpoint).extract(path)
    before = backend.images_seen
    second = PosterExtractor(backend, SETTINGS, checkpoint=checkpoint).extract(path)
    assert second["entries"][0]["route_direction"] == first["entries"][0]["route_direction"]
    assert second["entries"][0]["cache_hit"] is True
    assert backend.images_seen == before


def test_endpoint_batch_export_resume_override_and_mode_guard(tmp_path):
    source, out = tmp_path / "src", tmp_path / "out"
    _named_poster(source / "站点-西-01.jpg")
    backend = NamedBackend()
    config = BatchConfig(out_dir=out, pipeline=SETTINGS)
    result = run_batch(source, config, ocr_backend=backend)
    assert result.counts["done"] == 1 and result.review_counts == {}
    rows = json.loads(Path(result.report_files["station_routes_json"]).read_text(encoding="utf-8"))
    assert rows[0]["station_key"] == "站点-西"
    assert rows[0]["route_direction"] == "起点站→终点站"
    assert rows[0]["needs_review"] is False
    with Path(result.report_files["station_routes_csv"]).open(encoding="utf-8-sig", newline="") as f:
        assert list(csv.DictReader(f))[0]["first_stop"] == "起点站"
    before = backend.images_seen
    run_batch(source, config, ocr_backend=backend)
    assert backend.images_seen == before
    with pytest.raises(BatchError, match="separate"):
        run_batch(source, replace(config, pipeline=PipelineSettings()), ocr_backend=backend)
    store = ManifestStore(result.manifest_path)
    image_hash = store.posters()[0]["image_hash"]
    store.set_override(image_hash, "1", "西", 2, "人工末站", "备注")
    store.set_status(image_hash, "validated")
    rerun = run_batch(source, config, ocr_backend=backend)
    assert backend.images_seen == before
    entry = json.loads(Path(rerun.poster_files[image_hash]).read_text(encoding="utf-8"))["entries"][0]
    assert entry["last_stop"] == "人工末站（备注）"
    assert "stop_count" not in entry
    assert entry["route_direction"] == "起点站→人工末站（备注）"
    exported = export_manifest(result.manifest_path, tmp_path / "export")
    assert json.loads(Path(exported["station_routes_json"]).read_text(encoding="utf-8"))[0]["last_stop"] == "人工末站（备注）"


def test_multiple_routes_keep_each_endpoint_review_item(tmp_path):
    path = make_poster(tmp_path / "站点-东-01.jpg", blocks=2, routes=(1, 2), low_score_index=0)
    result = PosterExtractor(SyntheticOcrBackend(), SETTINGS).extract(path)
    items = ReviewEngine(SETTINGS).collect(result, "hash")
    assert {i["route"] for i in items if i["reason"] == "low_confidence"} == {"1", "2"}
    assert not any(i["reason"] in {"current_stop_unknown", "next_stop_mismatch"} for i in items)
    assert summaries_from_result(result, "hash", "站点") == []


def test_ordered_endpoint_direction_survives_manual_updates():
    entry = {"extract_mode": "endpoints", "route": "K1", "direction": "西", "stops": [],
             "endpoints": [{"role": "first", "seq": 1, "display_name": "甲"},
                           {"role": "last", "seq": 2, "display_name": "乙"}]}
    refresh_entry_validation(entry)
    assert entry["route_direction"] == "甲→乙"
    entry["endpoints"].reverse()
    refresh_entry_validation(entry)
    assert entry["route_direction"] == "甲→乙"
    entry["endpoints"].reverse()
    apply_overrides({"entries": [entry]}, [{"route": "K1", "direction": "西", "seq": 1, "name": "丙"}])
    assert entry["route_direction"] == "丙→乙"


def test_endpoint_vlm_checks_only_uncertain_terminal(tmp_path):
    source = tmp_path / "src"
    make_poster(source / "站点-北-01.jpg", stops=8, low_score_index=0)
    service = FakeVlmService()
    with serve(service) as url:
        result = run_batch(
            source, BatchConfig(out_dir=tmp_path / "out", pipeline=SETTINGS, enable_vlm=True),
            ocr_backend=SyntheticOcrBackend(),
            vlm_reviewer=HttpVlmReviewer(VlmConfig(url=url, model="fake-vlm", disable_proxy=True)),
        )
    assert result.vlm["calls"] == result.vlm["confirmed"] == 1
    content = service.requests[0]["messages"][1]["content"]
    assert len([part for part in content if part["type"] == "image_url"]) == 1
    entry = json.loads(Path(next(iter(result.poster_files.values()))).read_text(encoding="utf-8"))["entries"][0]
    assert entry["endpoints"][0]["source"] == "ocr+vlm"
    assert entry["route_direction"] == "测试站→测试站"


def test_route_number_missing_is_reviewed(tmp_path):
    path = _named_poster(tmp_path / "站点-东-01.jpg")

    class NoRoute(NamedBackend):
        def _one(self, image):
            if self.detect_route_number(image) is not None:
                return []
            return super()._one(image)

    result = PosterExtractor(NoRoute(), SETTINGS).extract(path)
    assert result["entries"][0]["route"] == ""
    assert any(i["reason"] == "route_number_unknown" for i in ReviewEngine(SETTINGS).collect(result, "hash"))


def test_manual_terminal_fix_resolves_empty_column_review(tmp_path):
    path = make_poster(tmp_path / "站点-东-01.jpg", stops=8, skip_index=7)
    result = PosterExtractor(SyntheticOcrBackend(), SETTINGS).extract(path)
    assert result["entries"][0]["last_stop"] == ""
    assert ReviewEngine(SETTINGS).collect(result, "hash")
    apply_overrides(result, [{"route": "1", "direction": "东", "seq": 2, "name": "人工末站"}])
    assert ReviewEngine(SETTINGS).collect(result, "hash") == []


@pytest.mark.slow
def test_real_endpoint_ocr(anping_sample_dir):
    if os.environ.get("RUN_REAL_ENDPOINT_OCR") != "1":
        pytest.skip("set RUN_REAL_ENDPOINT_OCR=1 to recognize real endpoint crops")
    from buspipe.ocr import LocalRapidOcrBackend

    backend = LocalRapidOcrBackend(threads=4)
    golden = json.loads((FIXTURES / "anping_east01_raw.json").read_text(encoding="utf-8"))
    result = PosterExtractor(backend, SETTINGS).extract(anping_sample_dir / "安平小区-东-01.jpg")
    assert len(result["entries"]) == len(golden["entries"])
    for actual, expected in zip(result["entries"], golden["entries"]):
        assert actual["route"] == expected["route"]
        assert [actual["first_stop"], actual["last_stop"]] == [
            expected["stops"][0]["display_name"], expected["stops"][-1]["display_name"],
        ]
    school = anping_sample_dir.parent / "安铺村" / "安铺村-北，小灯箱80x130CM-01.jpg"
    if school.exists():
        result = PosterExtractor(backend, SETTINGS).extract(school, only_blocks={8})
        entry = result["entries"][0]
        assert entry["route"] == "通学专线2"
        assert entry["first_stop"] == "东山村"
        assert entry["last_stop"] == "鼎屿"
