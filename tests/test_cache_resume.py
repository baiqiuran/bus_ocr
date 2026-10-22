"""Ticket 03: resumability, versioned cache and corrupt-cache recovery."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from buspipe.batch import BatchConfig, run_batch
from buspipe.cache import FileCache, key_for
from buspipe.store import STATUS_DONE, STATUS_FAILED, STATUS_PENDING, ManifestStore
from tests.synth import FailOnceBackend, SyntheticOcrBackend, make_poster


def _config(tmp_path: Path, **kwargs) -> BatchConfig:
    return BatchConfig(out_dir=tmp_path / "out", current_stop="测试小区", workers=1, **kwargs)


def _poster(tmp_path: Path, blocks: int = 1) -> Path:
    source = tmp_path / "src"
    source.mkdir()
    make_poster(source / "测试小区-北-001.jpg", blocks=blocks, routes=(1, 2, 3), stops=6, current_index=1)
    return source


def _single_hash(result) -> str:
    return next(iter(result.poster_files))


def test_resume_after_block_failure_reuses_completed_blocks(tmp_path):
    source = _poster(tmp_path, blocks=3)
    backend = SyntheticOcrBackend()
    flaky = FailOnceBackend(backend, route=2)
    config = _config(tmp_path)

    first = run_batch(source, config, ocr_backend=flaky)
    assert first.counts[STATUS_FAILED] == 1
    assert backend.images_seen == 10  # blocks 1 and 3 only, five images each

    second = run_batch(source, config, ocr_backend=flaky)
    assert second.counts[STATUS_DONE] == 1
    # Only block 2 is OCR'd again: five images, not fifteen.
    assert backend.images_seen == 15
    assert second.cache["block_hits"] == 2
    payload = json.loads(Path(next(iter(second.poster_files.values()))).read_text(encoding="utf-8"))
    assert [entry["route"] for entry in payload["entries"]] == ["1", "2", "3"]
    assert [bool(entry.get("cache_hit")) for entry in payload["entries"]] == [True, False, True]


def test_poster_cache_avoids_all_ocr_after_status_reset(tmp_path):
    source = _poster(tmp_path)
    backend = SyntheticOcrBackend()
    config = _config(tmp_path)
    first = run_batch(source, config, ocr_backend=backend)
    before = (backend.recognize_calls, backend.images_seen)
    store = ManifestStore(first.manifest_path)
    image_hash = store.posters()[0]["image_hash"]
    first_payload = json.loads(Path(next(iter(first.poster_files.values()))).read_text(encoding="utf-8"))

    store.set_status(image_hash, STATUS_PENDING)
    second = run_batch(source, config, ocr_backend=backend)
    assert second.counts[STATUS_DONE] == 1
    assert second.cache["poster_hits"] == 1
    assert (backend.recognize_calls, backend.images_seen) == before
    second_payload = json.loads(Path(next(iter(second.poster_files.values()))).read_text(encoding="utf-8"))
    assert second_payload["entries"] == first_payload["entries"]


def test_canvas_setting_change_invalidates_caches(tmp_path):
    source = _poster(tmp_path)
    backend = SyntheticOcrBackend()
    config = _config(tmp_path)
    first = run_batch(source, config, ocr_backend=backend)
    before = backend.images_seen
    store = ManifestStore(first.manifest_path)
    image_hash = store.posters()[0]["image_hash"]
    store.set_status(image_hash, STATUS_PENDING)

    changed = _config(tmp_path, canvas_max_columns=3)
    second = run_batch(source, changed, ocr_backend=backend)
    assert second.counts[STATUS_DONE] == 1
    assert second.cache["poster_misses"] >= 1
    assert second.cache["block_misses"] >= 1
    assert backend.images_seen > before
    payload = json.loads(Path(next(iter(second.poster_files.values()))).read_text(encoding="utf-8"))
    assert payload["entries"][0]["stop_count"] == 6


def test_corrupt_block_cache_is_ignored_and_rewritten(tmp_path):
    source = _poster(tmp_path)
    backend = SyntheticOcrBackend()
    config = _config(tmp_path)
    first = run_batch(source, config, ocr_backend=backend)
    before = backend.images_seen
    store = ManifestStore(first.manifest_path)
    image_hash = store.posters()[0]["image_hash"]
    store.set_status(image_hash, STATUS_PENDING)

    work = tmp_path / "out" / ".batch" / "cache"
    for poster_cache in (work / "posters").rglob("*.json"):
        poster_cache.unlink()
    block_files = sorted((work / "blocks").rglob("*.json"))
    assert block_files
    block_files[0].write_text("{not valid json", encoding="utf-8")

    second = run_batch(source, config, ocr_backend=backend)
    assert second.counts[STATUS_DONE] == 1
    assert second.cache["invalid"] >= 1
    assert backend.images_seen > before
    assert json.loads(block_files[0].read_text(encoding="utf-8"))["schema"] == 1
    payload = json.loads(Path(next(iter(second.poster_files.values()))).read_text(encoding="utf-8"))
    assert payload["entries"][0]["stop_count"] == 6


def test_concurrent_cache_writes_stay_valid(tmp_path):
    cache = FileCache(tmp_path / "cache", "concurrent")
    key = key_for({"kind": "test", "id": 1})

    def write(index: int) -> None:
        cache.put(key, {"index": index, "items": [1, 2, 3]})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(24)))

    data = cache.get(key, lambda value: isinstance(value, dict) and len(value["items"]) == 3)
    assert data is not None
    assert data["index"] in range(24)
    temp_files = list(cache.root.rglob("*.tmp"))
    assert temp_files == []
