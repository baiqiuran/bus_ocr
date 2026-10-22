"""Ticket 07: VLM only sees anomalies, advises, never silently overwrites."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from buspipe.batch import BatchConfig, run_batch
from buspipe.store import ManifestStore
from buspipe.vlm import HttpVlmReviewer, VlmConfig
from tests.fake_services import FakeVlmService, serve
from tests.synth import SyntheticOcrBackend, make_poster


def _reviewer(url: str) -> HttpVlmReviewer:
    return HttpVlmReviewer(
        VlmConfig(
            url=url,
            model="fake-vlm",
            timeout=2.0,
            max_retries=0,
            backoff=0.01,
            disable_proxy=True,
        )
    )


def _poster(tmp_path: Path, **kwargs) -> Path:
    source = tmp_path / "src"
    source.mkdir()
    make_poster(source / "测试小区-北-001.jpg", stops=8, current_index=1, **kwargs)
    return source


def _run(tmp_path: Path, source: Path, reviewer, **config_kwargs):
    config = BatchConfig(
        out_dir=tmp_path / "out",
        current_stop="测试小区",
        workers=1,
        enable_vlm=True,
        **config_kwargs,
    )
    return run_batch(source, config, ocr_backend=SyntheticOcrBackend(), vlm_reviewer=reviewer)


def test_only_anomaly_is_sent_and_agreement_confirms(tmp_path):
    source = _poster(tmp_path, low_score_index=2)
    service = FakeVlmService()
    with serve(service) as url:
        result = _run(tmp_path, source, _reviewer(url))

    assert result.counts["done"] == 1
    assert len(service.requests) == 1  # one low-confidence stop, not all eight
    assert result.vlm["calls"] == 1
    assert result.vlm["confirmed"] == 1

    payload = service.requests[0]
    assert payload["temperature"] == 0
    assert payload["prompt_version"] == "vlm-1"
    content = payload["messages"][1]["content"]
    text = content[0]["text"]
    assert "线路 1" in text and "方向 北" in text
    assert "OCR 候选 测试站" in text
    image_parts = [part for part in content if part["type"] == "image_url"]
    assert len(image_parts) == 3  # target + previous + next

    store = ManifestStore(result.manifest_path)
    item = store.review_items()[0]
    assert item["status"] == "vlm_confirmed"
    assert item["vlm_text"] == "测试站"
    assert item["source"] == "ocr+vlm"
    payload = json.loads(Path(next(iter(result.poster_files.values()))).read_text(encoding="utf-8"))
    assert payload["entries"][0]["stops"][2]["source"] == "ocr+vlm"


@pytest.mark.parametrize("agree", [True, False])
def test_disagreement_or_different_text_is_never_applied(tmp_path, agree):
    source = _poster(tmp_path, low_score_index=2)
    service = FakeVlmService(
        {
            "text": "别的站",
            "note": "",
            "agree": agree,
            "confidence": 0.95,
            "reason": "different glyph",
        }
    )
    with serve(service) as url:
        result = _run(tmp_path, source, _reviewer(url))

    store = ManifestStore(result.manifest_path)
    item = store.review_items()[0]
    assert item["status"] == "needs_human"
    assert item["vlm_text"] == "别的站"
    assert result.vlm["conflicts"] == 1
    payload = json.loads(Path(next(iter(result.poster_files.values()))).read_text(encoding="utf-8"))
    entry = payload["entries"][0]
    assert entry["stops"][2]["display_name"] == "测试站"
    assert entry["stops"][2]["source"] == "ocr"
    assert payload["vlm_opinions"][0]["display_name"] == "别的站"
    assert payload["vlm_opinions"][0]["ocr_text"] == "测试站"


def test_invalid_json_is_ignored(tmp_path):
    source = _poster(tmp_path, low_score_index=2)
    service = FakeVlmService(invalid_json=True)
    with serve(service) as url:
        result = _run(tmp_path, source, _reviewer(url))

    assert result.counts["vlm_review"] == 1
    assert result.vlm["errors"] == 1
    store = ManifestStore(result.manifest_path)
    assert store.review_items()[0]["status"] == "open"


def test_low_confidence_opinion_is_ignored(tmp_path):
    source = _poster(tmp_path, low_score_index=2)
    service = FakeVlmService(
        {
            "text": "测试站",
            "note": "",
            "agree": True,
            "confidence": 0.3,
            "reason": "unsure",
        }
    )
    with serve(service) as url:
        result = _run(tmp_path, source, _reviewer(url))

    assert result.vlm["ignored"] == 1
    store = ManifestStore(result.manifest_path)
    assert store.review_items()[0]["status"] == "open"


def test_empty_ocr_becomes_vlm_only_candidate_not_a_stop(tmp_path):
    source = _poster(tmp_path, skip_index=4)
    service = FakeVlmService(
        {
            "text": "补全站",
            "note": "备注",
            "agree": True,
            "confidence": 0.9,
            "reason": "recovered from crop",
        }
    )
    with serve(service) as url:
        result = _run(tmp_path, source, _reviewer(url))

    assert result.vlm["vlm_only"] == 1
    store = ManifestStore(result.manifest_path)
    item = next(item for item in store.review_items() if item["reason"] == "empty_ocr")
    assert item["status"] == "vlm_only"
    assert item["source"] == "vlm_only"
    assert item["vlm_text"] == "补全站（备注）"

    payload = json.loads(Path(next(iter(result.poster_files.values()))).read_text(encoding="utf-8"))
    entry = payload["entries"][0]
    assert entry["stop_count"] == 7
    assert all(stop["name"] != "补全站" for stop in entry["stops"])
    assert payload["vlm_candidates"][0]["display_name"] == "补全站（备注）"
    assert payload["vlm_candidates"][0]["source"] == "vlm_only"


def test_per_poster_call_cap(tmp_path):
    source = _poster(
        tmp_path,
        header_anomaly=True,
        low_score_index=2,
        cross_index=4,
        skip_index=6,
    )
    service = FakeVlmService()
    with serve(service) as url:
        result = _run(tmp_path, source, _reviewer(url), vlm_max_calls_per_poster=2)

    assert len(service.requests) == 2
    assert result.vlm["calls"] == 2


def test_vlm_cache_prevents_repeat_calls(tmp_path):
    source = _poster(tmp_path, low_score_index=2)
    service = FakeVlmService()
    with serve(service) as url:
        first = _run(tmp_path, source, _reviewer(url))
        store = ManifestStore(first.manifest_path)
        with store.connection() as conn, conn:
            conn.execute("UPDATE review_items SET status = 'open'")
        second = run_batch(
            source,
            BatchConfig(
                out_dir=tmp_path / "out",
                current_stop="测试小区",
                workers=1,
                enable_vlm=True,
            ),
            ocr_backend=SyntheticOcrBackend(),
            vlm_reviewer=_reviewer(url),
        )

    assert len(service.requests) == 1
    assert second.vlm["cache_hits"] == 1
    assert second.vlm["calls"] == 0
    item = ManifestStore(second.manifest_path).review_items()[0]
    assert item["status"] == "vlm_confirmed"
