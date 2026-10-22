"""Ticket 05: remote OCR contract, retries, breaker and local fallback."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from buspipe.batch import BatchConfig, run_batch
from buspipe.ocr import FallbackOcrBackend, OcrError, OcrParams, OcrStats
from buspipe.remote import RemoteOcrBackend, RemoteOcrConfig
from buspipe.store import ManifestStore
from tests.fake_services import FakeOcrService, serve
from tests.synth import SyntheticOcrBackend, make_poster


def _bar_image(bars: int = 3) -> np.ndarray:
    image = Image.new("RGB", (500, 260), "white")
    draw = ImageDraw.Draw(image)
    for index in range(bars):
        x = 20 + index * 60
        draw.rectangle([x, 30, x + 29, 229], fill=(0, 0, 0))
    return np.asarray(image)


def _config(url: str, **kwargs) -> RemoteOcrConfig:
    defaults = dict(
        url=url,
        model="remote-test-v1",
        timeout=5.0,
        max_retries=2,
        backoff=0.01,
        batch_size=6,
        disable_proxy=True,
    )
    defaults.update(kwargs)
    return RemoteOcrConfig(**defaults)


def test_multiple_canvases_are_sent_in_one_request():
    service = FakeOcrService()
    stats = OcrStats()
    with serve(service) as url:
        backend = RemoteOcrBackend(_config(url, api_key="secret-key"), stats=stats)
        images = [_bar_image(3), _bar_image(2), _bar_image(1)]
        results = backend.recognize(images, OcrParams())

    assert len(results) == 3
    assert [len(items) for items in results] == [3, 2, 1]
    assert len(service.requests) == 1
    assert len(service.requests[0]["ids"]) == 3
    assert service.requests[0]["authorization"] == "Bearer secret-key"
    assert service.requests[0]["idempotency"]
    snapshot = stats.snapshot()
    assert snapshot["remote_calls"] == 1


def test_retry_after_5xx_then_success():
    service = FakeOcrService(status_plan=[500, 200])
    stats = OcrStats()
    with serve(service) as url:
        backend = RemoteOcrBackend(_config(url), stats=stats)
        result = backend.recognize([_bar_image()], OcrParams())

    assert result[0][0]["text"] == "测试站"
    assert len(service.requests) == 2
    assert service.requests[0]["idempotency"] == service.requests[1]["idempotency"]
    assert stats.snapshot()["remote_retries"] == 1
    assert stats.snapshot()["remote_calls"] == 2


def test_429_uses_retry_after_header():
    service = FakeOcrService(status_plan=[429, 200])
    stats = OcrStats()
    with serve(service) as url:
        backend = RemoteOcrBackend(_config(url, backoff=10.0), stats=stats)
        started = time.perf_counter()
        result = backend.recognize([_bar_image()], OcrParams())
        elapsed = time.perf_counter() - started

    assert result[0]
    assert len(service.requests) == 2
    assert elapsed < 2.0
    assert stats.snapshot()["remote_retries"] == 1


def test_breaker_opens_and_falls_back_to_local():
    service = FakeOcrService(always_status=500)
    stats = OcrStats()
    local = SyntheticOcrBackend()
    with serve(service) as url:
        remote = RemoteOcrBackend(
            _config(url, max_retries=0, breaker_threshold=2, breaker_cooldown=5.0),
            stats=stats,
        )
        backend = FallbackOcrBackend(remote, local, stats=stats)
        for _ in range(3):
            result = backend.recognize([_bar_image()], OcrParams())
            assert result[0][0]["text"] == "测试站"

    assert len(service.requests) == 2  # third call short-circuits on the open breaker
    snapshot = stats.snapshot()
    assert snapshot["remote_failures"] == 2
    assert snapshot["fallbacks"] == 3


def test_timeout_falls_back_to_local():
    service = FakeOcrService(delay=0.3)
    stats = OcrStats()
    with serve(service) as url:
        remote = RemoteOcrBackend(_config(url, timeout=0.05, max_retries=0), stats=stats)
        backend = FallbackOcrBackend(remote, SyntheticOcrBackend(), stats=stats)
        started = time.perf_counter()
        result = backend.recognize([_bar_image()], OcrParams())
        elapsed = time.perf_counter() - started

    assert result[0][0]["text"] == "测试站"
    assert elapsed < 2.0
    assert stats.snapshot()["fallbacks"] == 1


def test_model_version_mismatch_is_rejected():
    service = FakeOcrService(reported_model="unexpected-model")
    with serve(service) as url:
        backend = RemoteOcrBackend(_config(url))
        with pytest.raises(OcrError):
            backend.recognize([_bar_image()], OcrParams())


@pytest.mark.parametrize("drop", ["drop_box", "drop_score"])
def test_contract_rejects_missing_coordinates_or_confidence(drop):
    service = FakeOcrService(**{drop: True})
    with serve(service) as url:
        backend = RemoteOcrBackend(_config(url))
        with pytest.raises(OcrError):
            backend.recognize([_bar_image()], OcrParams())


def test_api_key_from_environment_and_not_logged(monkeypatch, caplog):
    monkeypatch.setenv("BUS_OCR_API_KEY", "env-secret-value")
    service = FakeOcrService()
    caplog.set_level(logging.DEBUG, logger="buspipe.remote")
    with serve(service) as url:
        backend = RemoteOcrBackend(_config(url, api_key=None))
        backend.recognize([_bar_image()], OcrParams())

    assert service.requests[0]["authorization"] == "Bearer env-secret-value"
    assert "env-secret-value" not in caplog.text
    assert "env-secret-value" not in repr(_config(url))


def test_batch_end_to_end_matches_local_backend(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    for index in range(2):
        make_poster(
            source / f"测试小区-北-{index + 1:03d}.jpg",
            routes=(index + 1,),
            stops=12,
            current_index=1,
            variant=index,
        )

    local_result = run_batch(
        source,
        BatchConfig(out_dir=tmp_path / "local", current_stop="测试小区", workers=1),
        ocr_backend=SyntheticOcrBackend(),
    )
    assert local_result.counts["done"] == 2

    service = FakeOcrService()
    stats = OcrStats()
    with serve(service) as url:
        remote_result = run_batch(
            source,
            BatchConfig(out_dir=tmp_path / "remote", current_stop="测试小区", workers=1),
            ocr_backend=RemoteOcrBackend(_config(url), stats=stats),
        )
    assert remote_result.counts["done"] == 2
    # Two requests per poster (header crops + all stop canvases), not one per image.
    assert len(service.requests) == 4
    assert stats.snapshot()["remote_calls"] == 4

    for image_hash, local_file in local_result.poster_files.items():
        local_payload = json.loads(Path(local_file).read_text(encoding="utf-8"))
        remote_payload = json.loads(
            Path(remote_result.poster_files[image_hash]).read_text(encoding="utf-8")
        )
        assert path_stops(local_payload) == path_stops(remote_payload)


def test_remote_model_change_invalidates_cache(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    make_poster(source / "测试小区-北-001.jpg", stops=8, current_index=1)
    service = FakeOcrService()
    config = BatchConfig(out_dir=tmp_path / "out", current_stop="测试小区", workers=1)
    with serve(service) as url:
        first = run_batch(
            source,
            config,
            ocr_backend=RemoteOcrBackend(_config(url, model="model-v1")),
        )
        assert first.counts["done"] == 1
        requests_after_first = len(service.requests)
        store = ManifestStore(first.manifest_path)
        image_hash = store.posters()[0]["image_hash"]
        store.set_status(image_hash, "pending")

        second = run_batch(
            source,
            config,
            ocr_backend=RemoteOcrBackend(_config(url, model="model-v2")),
        )

    assert second.counts["done"] == 1
    assert second.cache["poster_misses"] >= 1
    assert len(service.requests) > requests_after_first


def path_stops(payload: dict):
    return [
        {
            "route": entry["route"],
            "stops": [(stop["display_name"], stop["seq"]) for stop in entry["stops"]],
            "next_stop": entry["next_stop"],
            "current_index": entry["current_index"],
        }
        for entry in payload["entries"]
    ]
