"""Ticket 06: review queue, provenance, manual overrides and CSV export."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from buspipe.batch import BatchConfig, run_batch
from buspipe.config import PipelineSettings
from buspipe.review import PosterSummary, ReviewEngine
from buspipe.store import REASON_PRIORITY, ManifestStore
from tests.synth import SyntheticOcrBackend, make_poster


def test_anomalies_become_sorted_review_items_with_crops(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    make_poster(
        source / "测试小区-北-001.jpg",
        stops=8,
        current_index=1,
        header_anomaly=True,
        low_score_index=2,
        cross_index=5,
        skip_index=4,
    )
    result = run_batch(
        source,
        BatchConfig(out_dir=tmp_path / "out", current_stop="测试小区"),
        ocr_backend=SyntheticOcrBackend(),
    )
    store = ManifestStore(result.manifest_path)
    items = store.review_items()
    reasons = {item["reason"] for item in items}
    assert {"cross_column", "empty_ocr", "next_stop_mismatch", "low_confidence"} <= reasons

    priorities = [REASON_PRIORITY[item["reason"]] for item in items]
    assert priorities == sorted(priorities)

    for item in items:
        if item["reason"] in {"cross_column", "low_confidence", "empty_ocr"}:
            assert item["crop_path"]
            assert Path(item["crop_path"]).exists()
        assert item["source"] in {"ocr", "manual"}
    assert result.review_counts["open"] == len(items)


def test_manual_override_persists_and_beats_cache(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    make_poster(source / "测试小区-北-001.jpg", stops=8, current_index=1)
    backend = SyntheticOcrBackend()
    config = BatchConfig(out_dir=tmp_path / "out", current_stop="测试小区")
    first = run_batch(source, config, ocr_backend=backend)
    store = ManifestStore(first.manifest_path)
    image_hash = store.posters()[0]["image_hash"]
    calls_before = backend.images_seen

    store.set_override(image_hash, "1", "北", 5, "人工站", "人工备注")
    store.set_status(image_hash, "pending")
    second = run_batch(source, config, ocr_backend=backend)

    assert backend.images_seen == calls_before  # override applied on a cache hit
    payload = json.loads(Path(second.poster_files[image_hash]).read_text(encoding="utf-8"))
    stop = payload["entries"][0]["stops"][4]
    assert stop["name"] == "人工站"
    assert stop["display_name"] == "人工站（人工备注）"
    assert stop["source"] == "manual"
    assert stop["score"] == 1.0

    store.set_status(image_hash, "pending")
    third = run_batch(source, config, ocr_backend=backend)
    payload = json.loads(Path(third.poster_files[image_hash]).read_text(encoding="utf-8"))
    assert payload["entries"][0]["stops"][4]["display_name"] == "人工站（人工备注）"


def test_bidirectional_mismatch_fixture_creates_two_items(tmp_path):
    engine = ReviewEngine(PipelineSettings(current_stop="测试小区"))
    summaries = [
        PosterSummary("hash-east", "测试小区", "1", "东", ["甲", "乙", "丙", "丁"], 4, True),
        PosterSummary("hash-west", "测试小区", "1", "西", ["戊", "己", "庚", "辛"], 4, True),
    ]
    items = engine.bidirectional_items(summaries, threshold=0.9)
    assert len(items) == 2
    assert {item["image_hash"] for item in items} == {"hash-east", "hash-west"}
    assert all(item["reason"] == "bidirectional_mismatch" for item in items)
    assert all(item["seq"] == 0 for item in items)


def test_bidirectional_match_fixture_creates_no_items():
    engine = ReviewEngine(PipelineSettings(current_stop="测试小区"))
    names = ["甲", "乙", "丙", "丁"]
    summaries = [
        PosterSummary("hash-east", "测试小区", "1", "东", names, 4, True),
        PosterSummary("hash-west", "测试小区", "1", "西", list(reversed(names)), 4, True),
    ]
    assert engine.bidirectional_items(summaries, threshold=0.9) == []


def test_csv_includes_manual_corrected_value(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    make_poster(source / "测试小区-北-001.jpg", stops=8, current_index=1, low_score_index=2)
    backend = SyntheticOcrBackend()
    config = BatchConfig(out_dir=tmp_path / "out", current_stop="测试小区")
    first = run_batch(source, config, ocr_backend=backend)
    store = ManifestStore(first.manifest_path)
    image_hash = store.posters()[0]["image_hash"]
    store.set_override(image_hash, "1", "北", 3, "人工站")
    store.set_status(image_hash, "validated")
    second = run_batch(source, config, ocr_backend=backend)

    with Path(second.review_csv).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    mismatch = [row for row in rows if row["原因"] == "next_stop_mismatch"]
    assert mismatch
    assert mismatch[0]["修正值"] == "人工站"


def test_review_csv_contains_provenance_and_vlm_columns(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    make_poster(source / "测试小区-北-001.jpg", stops=6, current_index=1, low_score_index=2)
    result = run_batch(
        source,
        BatchConfig(out_dir=tmp_path / "out", current_stop="测试小区"),
        ocr_backend=SyntheticOcrBackend(),
    )
    assert result.review_csv
    with Path(result.review_csv).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows
    assert set(rows[0]) >= {
        "OCR结果",
        "修正值",
        "VLM结果",
        "来源",
        "原因",
        "状态",
        "裁片",
    }
    assert any(row["原因"] == "low_confidence" for row in rows)
    assert all(row["VLM结果"] == "" for row in rows)
