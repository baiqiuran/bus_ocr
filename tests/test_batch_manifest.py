"""Ticket 02: manifest, duplicate handling, failure isolation and dry-run."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from buspipe.batch import BatchConfig, run_batch
from buspipe.store import STATUS_DONE, STATUS_FAILED, STATUS_PENDING, ManifestStore
from tests.synth import SyntheticOcrBackend, make_poster


def _source(tmp_path: Path, count: int = 3) -> Path:
    source = tmp_path / "src"
    source.mkdir()
    for index in range(count):
        make_poster(
            source / f"测试小区-北-{index + 1:03d}.jpg",
            routes=(index + 1,),
            stops=8,
            current_index=1,
            variant=index,
        )
    return source


def _config(tmp_path: Path, **kwargs) -> BatchConfig:
    return BatchConfig(out_dir=tmp_path / "out", current_stop="测试小区", workers=1, **kwargs)


def test_three_posters_complete_and_write_json(tmp_path):
    source = _source(tmp_path)
    backend = SyntheticOcrBackend()
    result = run_batch(source, _config(tmp_path), ocr_backend=backend)

    assert result.scan["new"] == 3
    assert result.counts[STATUS_DONE] == 3
    assert len(result.poster_files) == 3
    assert not result.errors
    for file_name in result.poster_files.values():
        payload = json.loads(Path(file_name).read_text(encoding="utf-8"))
        assert len(payload["entries"]) == 1
        entry = payload["entries"][0]
        assert entry["stop_count"] == 8
        assert entry["next_stop_matches"] is True
        assert entry["current_index"] == 1

    store = ManifestStore(result.manifest_path)
    assert {row["status"] for row in store.posters()} == {STATUS_DONE}
    assert all(row["attempts"] >= 1 for row in store.posters())


def test_duplicate_content_is_processed_once(tmp_path):
    source = _source(tmp_path, count=1)
    shutil.copyfile(source / "测试小区-北-001.jpg", source / "副本-北-999.jpg")
    make_poster(source / "另一张-北-002.jpg", routes=(2,), stops=6, current_index=0, variant=50)
    backend = SyntheticOcrBackend()
    result = run_batch(source, _config(tmp_path), ocr_backend=backend)

    assert result.scan["duplicates"] == 1
    assert result.counts["total"] == 2
    assert result.counts["duplicate"] == 1
    assert result.counts[STATUS_DONE] == 2
    # Two unique posters, three backend images each (header, number, canvas).
    assert backend.images_seen == 6
    store = ManifestStore(result.manifest_path)
    with store.connection() as conn:
        duplicates = conn.execute("SELECT COUNT(*) AS n FROM paths WHERE is_primary = 0").fetchone()["n"]
    assert duplicates == 1


def test_second_run_skips_completed_posters(tmp_path):
    source = _source(tmp_path)
    backend = SyntheticOcrBackend()
    config = _config(tmp_path)
    run_batch(source, config, ocr_backend=backend)
    before = (backend.recognize_calls, backend.images_seen)

    result = run_batch(source, config, ocr_backend=backend)
    assert result.counts[STATUS_DONE] == 3
    assert (backend.recognize_calls, backend.images_seen) == before
    assert result.poster_files == {}


def test_corrupt_image_is_isolated(tmp_path):
    source = _source(tmp_path, count=2)
    bad = source / "坏图-北-003.jpg"
    bad.write_text("this is not a jpeg", encoding="utf-8")
    backend = SyntheticOcrBackend()
    result = run_batch(source, _config(tmp_path), ocr_backend=backend)

    assert result.counts[STATUS_DONE] == 2
    assert result.counts[STATUS_FAILED] == 1
    assert result.errors
    store = ManifestStore(result.manifest_path)
    failed = [row for row in store.posters() if row["status"] == STATUS_FAILED]
    assert len(failed) == 1
    assert failed[0]["error"]


def test_renamed_file_keeps_content_identity_and_path(tmp_path):
    source = _source(tmp_path, count=1)
    config = _config(tmp_path)
    backend = SyntheticOcrBackend()
    first = run_batch(source, config, ocr_backend=backend)
    store = ManifestStore(first.manifest_path)
    image_hash = store.posters()[0]["image_hash"]
    old_path = Path(store.posters()[0]["path"])
    new_path = old_path.with_name("测试小区-北-009.jpg")
    old_path.rename(new_path)

    second = run_batch(source, config, ocr_backend=backend)
    assert second.counts["total"] == 1
    assert second.counts[STATUS_DONE] == 1
    assert second.counts["duplicate"] == 0
    row = store.posters()[0]
    assert row["image_hash"] == image_hash
    assert Path(row["path"]) == new_path
    with store.connection() as conn:
        primary = conn.execute(
            "SELECT path FROM paths WHERE image_hash = ? AND is_primary = 1", (image_hash,)
        ).fetchone()
    assert primary["path"] == str(new_path)


def test_dry_run_only_builds_manifest(tmp_path):
    source = _source(tmp_path)
    backend = SyntheticOcrBackend()
    result = run_batch(source, _config(tmp_path, dry_run=True), ocr_backend=backend)

    assert result.dry_run is True
    assert result.counts[STATUS_PENDING] == 3
    assert backend.recognize_calls == 0
    assert not (tmp_path / "out" / "posters").exists()
    store = ManifestStore(result.manifest_path)
    assert {row["status"] for row in store.posters()} == {STATUS_PENDING}
