"""Workbench persistence, provenance and local API boundaries."""
import json
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from PIL import Image

from buspipe.store import ManifestStore, scan_directory
from buspipe.web import Workbench, make_server


@pytest.fixture
def saved_output(tmp_path):
    source = tmp_path / "photos"
    source.mkdir()
    for name, color in [("测试站-东-01", "red"), ("测试站-西-02", "blue")]:
        Image.new("RGB", (20, 30), color).save(source / f"{name}.jpg")
    output = tmp_path / "export"
    store = ManifestStore(output / ".batch" / "manifest.sqlite")
    scan_directory(store, source)
    rows = []
    for poster in store.posters():
        store.set_status(poster["image_hash"], "done")
        east = poster["direction"] == "东"
        rows.append(dict(station="测试站", direction=poster["direction"], station_key=f"测试站-{poster['direction']}",
                         route="K1", first_stop="甲站" if east else "乙站", last_stop="乙站" if east else "甲站",
                         route_direction="do not trust this computed field", needs_review=not east,
                         poster=f"测试站-{'东-01' if east else '西-02'}", block_index=0))
    (output / "station_routes.json").write_text(json.dumps(rows), encoding="utf-8")
    (output / "batch_report.json").write_text(json.dumps(dict(extract_mode="endpoints", source_dir=str(source),
        counts=dict(total=2, done=2, failed=0, skipped=0, incomplete=0), ocr=dict(remote_calls=0), vlm=dict(calls=0))), encoding="utf-8")
    return output


def test_import_keeps_order_direction_provenance_and_persists(tmp_path, saved_output):
    app = Workbench(tmp_path / "db")
    job_id = app.import_output(saved_output)["id"]
    rows = app.records({})["rows"]
    assert len(rows) == 2
    assert {r["route_direction"] for r in rows} == {"甲站→乙站", "乙站→甲站"}
    assert {r["station_key"] for r in rows} == {"测试站-东", "测试站-西"}
    assert all(r["image_hash"] and r["image_path"] for r in rows)
    assert app.records({"review": "1"})["total"] == 1
    assert app.records({"q": "%"})["total"] == 0
    assert app.records({"q": "K1", "direction": "东"})["total"] == 1
    assert app.import_output(saved_output)["id"] == job_id
    reopened = Workbench(tmp_path / "db")
    assert reopened.records({})["total"] == 2
    with pytest.raises(ValueError, match="只读"):
        reopened.start(job_id)


def test_import_rollback_and_mode_validation(tmp_path, saved_output):
    app = Workbench(tmp_path / "db")
    report = saved_output / "batch_report.json"
    report.write_text('{"extract_mode":"full"}', encoding="utf-8")
    with pytest.raises(ValueError, match="endpoints"):
        app.import_output(saved_output)
    assert app.overview()["jobs"] == []


def test_missing_values_stay_empty_and_are_reviewed(tmp_path, saved_output):
    rows_file = saved_output / "station_routes.json"
    row = json.loads(rows_file.read_text())[0]
    row.update(route="", last_stop="", direction="上客点", needs_review=False)
    rows_file.write_text(json.dumps([row]), encoding="utf-8")
    app = Workbench(tmp_path / "db")
    app.import_output(saved_output)
    result = app.records({})["rows"][0]
    assert result["direction"] == "上客点"
    assert result["route"] == result["last_stop"] == result["route_direction"] == ""
    assert result["needs_review"] == 1


def test_same_named_sources_do_not_guess_image(tmp_path, saved_output):
    store = ManifestStore(saved_output / ".batch" / "manifest.sqlite")
    extra = tmp_path / "extra"
    extra.mkdir()
    Image.new("RGB", (40, 40), "green").save(extra / "测试站-东-01.jpg")
    scan_directory(store, extra)
    app = Workbench(tmp_path / "db")
    app.import_output(saved_output)
    assert app.records({"direction": "东"})["rows"][0]["image_path"] == ""


def test_real_cli_scan_without_ocr(tmp_path):
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    Image.new("RGB", (20, 30), "red").save(source / "nested" / "测试站-东-01.jpg")
    (source / "notes.txt").write_text("ignored")
    app = Workbench(tmp_path / "db")
    job_id = app.create(source)["id"]
    deadline = time.monotonic() + 20
    while app.job(job_id)["status"] == "scanning" and time.monotonic() < deadline:
        time.sleep(.05)
    result = app.detail(job_id)
    assert result["status"] == "ready", result["error"]
    assert result["counts"]["total"] == 1
    assert result["posters"][0]["station"] == "测试站"
    assert result["posters"][0]["direction"] == "东"
    assert result["report"]["ocr"]["local_calls"] == 0
    assert app.records({})["total"] == 0
    app.close()


def test_cli_exit_zero_with_failures_is_partial(tmp_path, saved_output):
    report_file = saved_output / "batch_report.json"
    report = json.loads(report_file.read_text())
    report["counts"].update(total=4, failed=1, skipped=1)
    report_file.write_text(json.dumps(report))
    app = Workbench(tmp_path / "db")
    job_id = app.import_output(saved_output)["id"]
    assert app.job(job_id)["status"] == "partial"
    assert app.records({})["total"] == 2


def test_server_api_token_origin_host_and_export(tmp_path, saved_output):
    app = Workbench(tmp_path / "db")
    job_id = app.import_output(saved_output)["id"]
    server = make_server(app, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(base + "/api/session") as response:
            token = json.load(response)["token"]
        with urlopen(base + "/api/records?q=K1") as response:
            assert json.load(response)["total"] == 2
        with urlopen(base + f"/api/export/{job_id}") as response:
            assert response.read().decode("utf-8-sig").count("K1") == 2
        for headers in [{}, {"X-Workbench-Token": token, "Origin": "https://untrusted.invalid"}, {"X-Workbench-Token": token, "Host": "untrusted.invalid"}]:
            with pytest.raises(HTTPError) as caught:
                urlopen(Request(base + "/api/jobs", data=b'{"path":"bad"}', headers=headers))
            assert caught.value.code == 403
        with pytest.raises(HTTPError) as caught:
            urlopen(Request(base + "/api/jobs", data=b'{"path":""}', headers={"X-Workbench-Token": token}))
        assert caught.value.code == 400
        with pytest.raises(HTTPError) as caught:
            urlopen(base + "/../AGENTS.md")
        assert caught.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        app.close()


def test_restart_marks_active_job_interrupted(tmp_path, saved_output):
    app = Workbench(tmp_path / "db")
    job_id = app.import_output(saved_output)["id"]
    app.update(job_id, status="running")
    reopened = Workbench(tmp_path / "db")
    assert reopened.job(job_id)["status"] == "interrupted"


def test_malformed_record_does_not_create_batch(tmp_path, saved_output):
    (saved_output / "station_routes.json").write_text('[{"route": 141}]')
    app = Workbench(tmp_path / "db")
    with pytest.raises(ValueError, match="字符串"):
        app.import_output(saved_output)
    assert not app.overview()["jobs"]


def test_browser_filters_details_and_mobile(tmp_path, saved_output):
    playwright = pytest.importorskip("playwright.sync_api")
    app = Workbench(tmp_path / "db")
    app.import_output(saved_output)
    server = make_server(app, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with playwright.sync_playwright() as runtime:
            try:
                browser = runtime.chromium.launch()
            except playwright.Error:
                pytest.skip("Playwright Chromium is not installed")
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}")
            playwright.expect(page.locator("#rows tr")).to_have_count(2)
            page.locator("#review-view").click()
            playwright.expect(page.locator("#review-filter")).to_have_value("1")
            playwright.expect(page.locator("#rows tr")).to_have_count(1)
            page.locator("#all-view").click()
            page.locator("#direction").select_option("东")
            playwright.expect(page.locator("#rows tr")).to_have_count(1)
            page.locator("[data-record]").first.click()
            playwright.expect(page.locator("#source-image")).to_be_visible()
            playwright.expect(page.locator("#detail-title")).to_contain_text("测试站")
            page.locator("#detail-dialog .close").click()
            for width in (320, 375, 414, 768):
                page.set_viewport_size({"width": width, "height": 900})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.locator("#menu-toggle").click()
                playwright.expect(page.locator("#new-job")).to_be_visible()
                page.locator("#menu-toggle").click()
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        app.close()
