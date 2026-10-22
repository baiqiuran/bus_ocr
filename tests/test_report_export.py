"""Ticket 08: batch reports, merged raw output and analyzer compatibility."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

from buspipe.batch import BatchConfig, run_batch
from tests.conftest import FIXTURES
from tests.synth import FailOnceBackend, SyntheticOcrBackend, make_poster

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = {
    "安平小区-东-01": "anping_east01_raw.json",
    "安平小区-西-01": "anping_west01_raw.json",
    "安平小区-西-02": "anping_west02_raw.json",
}


def _dummy_image(path: Path, variant: int) -> None:
    image = Image.new("RGB", (80, 80), "white")
    ImageDraw.Draw(image).point((variant, variant), fill=(0, 0, 0))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, quality=90)


def test_report_contains_counts_costs_and_merged_raw(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    for index in range(3):
        make_poster(source / f"测试小区-北-{index + 1:03d}.jpg", routes=(index + 1,), variant=index)

    result = run_batch(
        source,
        BatchConfig(out_dir=tmp_path / "out", current_stop="测试小区"),
        ocr_backend=SyntheticOcrBackend(),
    )
    assert result.report_files
    report = json.loads(Path(result.report_files["report_json"]).read_text(encoding="utf-8"))
    assert report["counts"]["total"] == 3
    assert report["counts"]["done"] == 3
    assert report["counts"]["failed"] == 0
    assert report["counts"]["duplicates"] == 0
    assert "hit_rate" in report["cache"]
    for key in ("images", "remote_calls", "remote_retries", "fallbacks"):
        assert key in report["ocr"]
    assert "calls" in report["vlm"]
    assert set(report["stage_seconds"]) >= {"scan", "extract", "review"}

    markdown = Path(result.report_files["report_md"]).read_text(encoding="utf-8")
    assert "站牌批量处理报告" in markdown
    assert "缓存命中率" in markdown

    merged = json.loads(Path(result.report_files["all_raw"]).read_text(encoding="utf-8"))
    assert merged["source_dir"] == str(source.resolve())
    assert len(merged["posters"]) == 3
    assert all(poster["entries"] for poster in merged["posters"])
    assert Path(result.review_csv).exists()


def test_resume_report_reflects_final_state_without_double_counting(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    for index in range(3):
        make_poster(source / f"测试小区-北-{index + 1:03d}.jpg", routes=(index + 1,), variant=index)
    backend = SyntheticOcrBackend()
    flaky = FailOnceBackend(backend, route=2)
    config = BatchConfig(out_dir=tmp_path / "out", current_stop="测试小区", workers=1)

    first = run_batch(source, config, ocr_backend=flaky)
    first_report = json.loads(Path(first.report_files["report_json"]).read_text(encoding="utf-8"))
    assert first_report["counts"]["done"] == 2
    assert first_report["counts"]["failed"] == 1
    assert first_report["failures"][0]["error"]
    assert first_report["failures"][0]["attempts"] >= 1

    second = run_batch(source, config, ocr_backend=flaky)
    report = json.loads(Path(second.report_files["report_json"]).read_text(encoding="utf-8"))
    assert report["counts"]["total"] == 3
    assert report["counts"]["done"] == 3
    assert report["counts"]["failed"] == 0
    assert report["failures"] == []
    merged = json.loads(Path(second.report_files["all_raw"]).read_text(encoding="utf-8"))
    assert len(merged["posters"]) == 3


def test_batch_output_can_feed_existing_analyzer(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    for index, stem in enumerate(GOLDEN):
        _dummy_image(source / f"{stem}.jpg", index + 1)

    def golden_extractor(path, settings, event_sink):
        return json.loads((FIXTURES / GOLDEN[path.stem]).read_text(encoding="utf-8"))

    result = run_batch(
        source,
        BatchConfig(
            out_dir=tmp_path / "out",
            current_stop="安平小区",
            enable_review=False,
        ),
        extractor=golden_extractor,
    )
    assert result.counts["done"] == 3
    merged = Path(result.report_files["all_raw"])
    assert merged.exists()

    analysis_dir = tmp_path / "analysis"
    completed = subprocess.run(
        [
            sys.executable,
            "analyze_anping.py",
            "--raw",
            str(merged),
            "--out-dir",
            str(analysis_dir),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    stops_csv = analysis_dir / "站点明细.csv"
    routes_csv = analysis_dir / "线路汇总.csv"
    assert stops_csv.exists() and stops_csv.stat().st_size > 100
    assert routes_csv.exists() and routes_csv.stat().st_size > 50
    assert (analysis_dir / "安平小区公交数据.xlsx").exists()
