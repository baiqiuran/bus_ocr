"""Ticket 09: 100-poster soak, bounded memory, resume and VLM limits."""

from __future__ import annotations

import json
import tracemalloc
from pathlib import Path

import pytest

from buspipe.batch import BatchConfig, run_batch
from buspipe.ocr import FallbackOcrBackend, OcrStats
from buspipe.remote import RemoteOcrBackend, RemoteOcrConfig
from buspipe.vlm import HttpVlmReviewer, VlmConfig
from tests.fake_services import FakeOcrService, FakeVlmService, serve
from tests.synth import SyntheticOcrBackend, make_poster


def _make_source(tmp_path: Path, count: int, *, anomalies: bool = False) -> Path:
    source = tmp_path / "src"
    source.mkdir()
    for index in range(count):
        make_poster(
            source / f"测试小区-北-{index + 1:04d}.jpg",
            routes=((index % 9) + 1,),
            stops=6,
            current_index=1,
            header_anomaly=anomalies and index % 20 == 0,
            low_score_index=3 if anomalies and index % 20 == 0 else None,
            variant=index,
        )
    return source


@pytest.mark.soak
def test_100_posters_with_remote_fallback_and_vlm_limits(tmp_path):
    source = _make_source(tmp_path, 100, anomalies=True)
    ocr_service = FakeOcrService(status_plan=[500, 500, 500])
    vlm_service = FakeVlmService()
    ocr_stats = OcrStats()
    with serve(ocr_service) as ocr_url, serve(vlm_service) as vlm_url:
        remote = RemoteOcrBackend(
            RemoteOcrConfig(
                url=ocr_url,
                model="remote-soak",
                timeout=5.0,
                max_retries=1,
                backoff=0.001,
                batch_size=8,
                disable_proxy=True,
            ),
            stats=ocr_stats,
        )
        backend = FallbackOcrBackend(remote, SyntheticOcrBackend(), stats=ocr_stats)
        reviewer = HttpVlmReviewer(
            VlmConfig(
                url=vlm_url,
                model="vlm-soak",
                timeout=5.0,
                max_retries=0,
                backoff=0.001,
                disable_proxy=True,
            )
        )
        tracemalloc.start()
        result = run_batch(
            source,
            BatchConfig(
                out_dir=tmp_path / "out",
                current_stop="测试小区",
                workers=2,
                enable_vlm=True,
                vlm_max_calls_per_poster=3,
            ),
            ocr_backend=backend,
            vlm_reviewer=reviewer,
        )
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()

    assert result.counts["done"] == 100
    assert result.counts["failed"] == 0
    assert result.ocr["fallbacks"] >= 1
    assert result.ocr["remote_retries"] >= 1
    assert result.vlm["calls"] == 10  # five anomaly posters, two items each
    assert len(vlm_service.requests) == 10
    assert peak < 512 * 1024 * 1024, f"peak traced memory {peak / 1024 / 1024:.1f} MiB"
    report = json.loads(Path(result.report_files["report_json"]).read_text(encoding="utf-8"))
    assert report["counts"]["total"] == 100
    assert report["counts"]["done"] == 100
    assert report["review"]["pending"] == 0  # every flagged item was VLM-confirmed
    merged = json.loads(Path(result.report_files["all_raw"]).read_text(encoding="utf-8"))
    assert len(merged["posters"]) == 100


@pytest.mark.soak
def test_limit_then_resume_matches_one_shot(tmp_path):
    source = _make_source(tmp_path, 12)
    config = BatchConfig(out_dir=tmp_path / "out", current_stop="测试小区", workers=1)
    backend = SyntheticOcrBackend()

    first = run_batch(source, config, ocr_backend=backend)
    assert first.counts["done"] == 12

    limited = run_batch(
        source,
        BatchConfig(
            out_dir=tmp_path / "out_resume",
            current_stop="测试小区",
            workers=1,
            limit=5,
        ),
        ocr_backend=backend,
    )
    assert limited.counts["done"] == 5
    report = json.loads(Path(limited.report_files["report_json"]).read_text(encoding="utf-8"))
    assert report["counts"]["incomplete"] == 7

    resumed = run_batch(
        source,
        BatchConfig(out_dir=tmp_path / "out_resume", current_stop="测试小区", workers=1),
        ocr_backend=backend,
    )
    assert resumed.counts["done"] == 12

    one_shot = json.loads(Path(first.report_files["all_raw"]).read_text(encoding="utf-8"))
    resumed_raw = json.loads(Path(resumed.report_files["all_raw"]).read_text(encoding="utf-8"))
    one_by_hash = {poster["poster"]: poster["entries"] for poster in one_shot["posters"]}
    resumed_by_hash = {poster["poster"]: poster["entries"] for poster in resumed_raw["posters"]}
    assert one_by_hash.keys() == resumed_by_hash.keys()
    for name in one_by_hash:
        assert _entry_signature(one_by_hash[name]) == _entry_signature(resumed_by_hash[name])


def _entry_signature(entries):
    return [
        {
            "route": entry["route"],
            "next_stop": entry["next_stop"],
            "current_index": entry["current_index"],
            "stops": [stop["display_name"] for stop in entry["stops"]],
        }
        for entry in entries
    ]
