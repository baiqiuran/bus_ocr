"""CLI smoke tests: dry-run, export, list and override."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from buspipe.batch import BatchConfig, run_batch
from buspipe.store import ManifestStore
from tests.synth import SyntheticOcrBackend, make_poster

ROOT = Path(__file__).resolve().parents[1]


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "buspipe", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_cli_dry_run_does_not_call_ocr(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    make_poster(source / "测试小区-北-001.jpg", stops=6)
    out = tmp_path / "out"
    completed = _run_cli("run", str(source), "--out", str(out), "--dry-run", "--station", "测试小区", "--extract-mode", "endpoints")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    summary = json.loads(completed.stdout)
    assert summary["dry_run"] is True
    assert summary["extract_mode"] == "endpoints"
    assert summary["counts"]["pending"] == 1
    assert not (out / "posters").exists()
    assert (out / ".batch" / "manifest.sqlite").exists()


def test_cli_export_and_override(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    make_poster(source / "测试小区-北-001.jpg", stops=6, current_index=1)
    out = tmp_path / "out"
    result = run_batch(
        source,
        BatchConfig(out_dir=out, current_stop="测试小区"),
        ocr_backend=SyntheticOcrBackend(),
    )
    store = ManifestStore(result.manifest_path)
    image_hash = store.posters()[0]["image_hash"]

    exported = tmp_path / "exported"
    completed = _run_cli("export", str(result.manifest_path), "--out", str(exported))
    assert completed.returncode == 0, completed.stdout + completed.stderr
    files = json.loads(completed.stdout)
    assert Path(files["all_raw"]).exists()
    assert Path(files["report_json"]).exists()

    completed = _run_cli("list", str(result.manifest_path))
    assert completed.returncode == 0
    assert "done" in completed.stdout

    completed = _run_cli(
        "override",
        str(result.manifest_path),
        image_hash,
        "1",
        "北",
        "2",
        "人工站",
        "--note",
        "人工备注",
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    store = ManifestStore(result.manifest_path)
    assert store.overrides(image_hash)[0]["name"] == "人工站"
    assert store.poster(image_hash)["status"] == "validated"

    rerun = run_batch(
        source,
        BatchConfig(out_dir=out, current_stop="测试小区"),
        ocr_backend=SyntheticOcrBackend(),
    )
    payload = json.loads(Path(rerun.poster_files[image_hash]).read_text(encoding="utf-8"))
    assert payload["entries"][0]["stops"][1]["display_name"] == "人工站（人工备注）"
